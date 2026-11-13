"""
UdpTransport — улучшенная версия с подавлением 10054 при локальных тестах
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import threading
import time
import urllib.request
from typing import Optional, Tuple

import stun_client
from protocol import PacketType, pack
from transport import ITransport

# ============================================================
# Вспомогательные функции
# ============================================================

def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()

def _get_ntfy_server() -> str:
    return os.getenv("NTFY_SERVER", "https://ntfy.sh")

def _get_local_ip() -> Optional[str]:
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tmp.connect(("8.8.8.8", 80))
        return tmp.getsockname()[0]
    except OSError:
        return None
    finally:
        tmp.close()

# ============================================================
# NAT-детектор
# ============================================================

def _detect_nat_type(sock: socket.socket) -> str:
    servers = [
        ("stun.cloudflare.com", 3478),
        ("stun.l.google.com", 19302),
        ("stun.voiparound.com", 3478),
        ("stun.voipbuster.com", 3478),
    ]
    ports = []
    for host, port in servers:
        try:
            old_timeout = sock.gettimeout()
            sock.settimeout(2.0)
            ext = stun_client.get_external_address(sock, stun_host=host, stun_port=port)
            sock.settimeout(old_timeout)
            if ext:
                ports.append(ext[1])
            else:
                return "unknown"
        except Exception:
            return "unknown"
        if len(ports) >= 2:
            break
    if len(ports) < 2:
        return "unknown"
    return "full-cone" if ports[0] == ports[1] else "symmetric"

# ============================================================
# Публикация и получение адреса
# ============================================================

def _publish_address_with_retry(topic: str, ext_ip: str, ext_port: int,
                                local_ip: Optional[str], local_port: int,
                                nat_type: str = "unknown", retries: int = 3) -> bool:
    server = _get_ntfy_server()
    lines = [
        "SERVER IS RUNNING",
        f"Address: {ext_ip}:{ext_port}",
        f"NatType: {nat_type}"
    ]
    if local_ip:
        lines.append(f"LocalAddress: {local_ip}:{local_port}")
    message = "\n".join(lines).encode("utf-8")
    url = f"{server}/{topic}"
    
    for attempt in range(retries):
        req = urllib.request.Request(url, data=message, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception as e:
            print(f"[UDP] [WARN] Publish attempt {attempt+1} failed: {e}")
            time.sleep(1)
    return False

def _fetch_peer_address_once(topic: str, timeout: float = 5.0) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    server = _get_ntfy_server()
    url = f"{server}/{topic}/json?poll=1&since=all"
    try:
        with urllib.request.urlopen(url, timeout=timeout, context=_ssl_context()) as response:
            raw = response.read().decode()
            messages = []
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            if messages:
                last_message = messages[-1].get("message", "")
                address = None
                local_address = None
                nat_type = None
                for line in last_message.split("\n"):
                    stripped = line.strip()
                    if stripped.startswith("LocalAddress:"):
                        local_address = stripped.replace("LocalAddress:", "").strip()
                    elif stripped.startswith("Address:"):
                        address = stripped.replace("Address:", "").strip()
                    elif stripped.startswith("NatType:"):
                        nat_type = stripped.replace("NatType:", "").strip()
                if address:
                    return address, local_address, nat_type
    except Exception:
        pass
    return None, None, None

# ============================================================
# Основной класс UdpTransport
# ============================================================

class UdpTransport(ITransport):
    def __init__(
        self,
        my_topic: str,
        peer_topic: str,
        local_port: int = 9090,
        punch_interval: float = 0.3,
        max_discovery_time: float = 180.0,
        discovery_interval: float = 3.0,
    ) -> None:
        super().__init__()
        self._my_topic = my_topic
        self._peer_topic = peer_topic
        self._local_port = local_port
        self._punch_interval = punch_interval
        self._max_discovery_time = max_discovery_time
        self._discovery_interval = discovery_interval

        self._sock: Optional[socket.socket] = None
        self._peer_addr: Optional[Tuple[str, int]] = None
        self._running = False
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._my_nat_type: str = "unknown"
        self._error_10054_count = 0  # счётчик для подавления

    def start(self) -> bool:
        self._stop_event.clear()
        self._error_10054_count = 0

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self._local_port))

        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4_194_304)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4_194_304)
        except OSError as e:
            print(f"[UDP] [WARN] Buffer increase failed: {e}")

        # STUN
        print("[UDP] [INFO] Determining own external address via STUN...")
        ext = stun_client.get_external_address_with_fallback(self._sock)
        if not ext:
            print("[UDP] [ERROR] STUN request failed")
            self._close_socket()
            return False

        my_ext_ip, my_ext_port = ext
        print(f"[UDP] [OK] My external address: {my_ext_ip}:{my_ext_port}")

        # NAT-детект
        print("[UDP] [INFO] Detecting NAT type...")
        self._my_nat_type = _detect_nat_type(self._sock)
        print(f"[UDP] [OK] My NAT type: {self._my_nat_type}")

        my_local_ip = _get_local_ip()
        if my_local_ip:
            print(f"[UDP] [OK] My local address: {my_local_ip}:{self._local_port}")

        # Публикация адреса
        print(f"[UDP] [INFO] Publishing my address to '{self._my_topic}'...")
        if not _publish_address_with_retry(self._my_topic, my_ext_ip, my_ext_port,
                                           my_local_ip, self._local_port,
                                           nat_type=self._my_nat_type):
            print("[UDP] [ERROR] Failed to publish own address after retries")
            self._close_socket()
            return False
        print("[UDP] [OK] Address published")

        print(f"[UDP] [INFO] Waiting for peer (up to {self._max_discovery_time:.0f}s)...")
        self._sock.settimeout(0.3)
        hello_packet = pack(PacketType.HELLO, seq=0)

        deadline = time.time() + self._max_discovery_time
        peer_addr: Optional[Tuple[str, int]] = None
        peer_ext_ip: Optional[str] = None
        peer_nat_type: Optional[str] = None
        last_fetch = 0.0
        last_punch = 0.0
        last_progress_log = time.time()
        success = False
        local_test = False

        while time.time() < deadline:
            if self._stop_event.is_set():
                break

            now = time.time()

            if peer_addr is None and now - last_fetch >= self._discovery_interval:
                last_fetch = now
                addr, local_addr, nat_type = _fetch_peer_address_once(self._peer_topic, timeout=5.0)
                if addr:
                    ip, port_str = addr.split(":")
                    peer_ext_ip = ip
                    peer_nat_type = nat_type
                    if peer_nat_type:
                        print(f"[UDP] [INFO] Peer NAT type: {peer_nat_type}")

                    if self._my_nat_type == "symmetric" and peer_nat_type == "symmetric":
                        print("[UDP] [WARN] Both symmetric NAT — direct punch impossible, falling back to relay")
                        self._close_socket()
                        return False

                    if ip == my_ext_ip and local_addr:
                        lip, lport = local_addr.split(":")
                        peer_addr = (lip, int(lport))
                        print(f"[UDP] [WARN] Same external IP — using LOCAL address {peer_addr}")
                        local_test = True
                    else:
                        peer_addr = (ip, int(port_str))
                    print(f"[UDP] [OK] Peer address discovered: {peer_addr}")

                    if local_test:
                        print("[UDP] [INFO] Local test detected, waiting 1s to avoid port conflicts...")
                        time.sleep(1.0)

            if peer_addr is not None and now - last_punch >= self._punch_interval:
                last_punch = now
                base_ip, base_port = peer_addr
                if local_test:
                    deltas = (0, 1, -1)
                else:
                    deltas = (0, 1, -1, 2, -2, 3, -3)
                for delta in deltas:
                    alt_port = base_port + delta
                    if 1 <= alt_port <= 65535:
                        alt_addr = (base_ip, alt_port)
                        try:
                            self._sock.sendto(hello_packet, alt_addr)
                        except OSError:
                            pass

            if now - last_progress_log >= 10.0:
                last_progress_log = now
                remaining = int(deadline - now)
                state = f"punching {peer_addr}" if peer_addr else "looking for peer address"
                print(f"[UDP] [INFO] {state}, {remaining}s left")

            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError as e:
                # Подавляем 10054 при локальных тестах
                if e.errno == 10054 and local_test:
                    self._error_10054_count += 1
                    if self._error_10054_count <= 5:
                        print(f"[UDP] [ERROR] Receive error: {e} (suppressed after 5)")
                    continue
                print(f"[UDP] [ERROR] Receive error: {e}")
                break

            if peer_addr is not None and (addr == peer_addr or addr[0] == peer_ext_ip):
                print(f"[UDP] [OK] Received response from {addr}")
                self._peer_addr = addr
                success = True
                break

        if not success:
            print(f"[UDP] [ERROR] No response within {self._max_discovery_time:.0f}s")
            self._close_socket()
            return False

        self._running = True
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        print(f"[UDP] [OK] Connected to {self._peer_addr}")
        return True

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)
        self._close_socket()

    def send(self, data: bytes) -> bool:
        if not self._running or self._sock is None or self._peer_addr is None:
            return False
        try:
            with self._send_lock:
                self._sock.sendto(data, self._peer_addr)
            return True
        except OSError as e:
            if e.errno in (10054, 10053):
                return True
            print(f"[UDP] [ERROR] Send failed: {e}")
            return False

    def is_alive(self) -> bool:
        return self._running and self._peer_addr is not None

    def _recv_loop(self) -> None:
        assert self._sock is not None
        self._sock.settimeout(1.0)
        while self._running:
            try:
                data, addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError as e:
                if not self._running:
                    break
                if e.errno in (10054, 10053):
                    continue
                print(f"[UDP] [WARN] Socket error: {e}")
                time.sleep(0.1)
                continue

            if self._peer_addr is not None and addr != self._peer_addr:
                continue

            try:
                self._deliver(data)
            except Exception as e:
                print(f"[UDP] [ERROR] Deliver callback failed: {e}")

    def _close_socket(self) -> None:
        self._peer_addr = None
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
