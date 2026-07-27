from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.request
from typing import Optional, Tuple

import stun_client
from protocol import PacketType, pack
from transport import ITransport


def _ssl_context() -> ssl.SSLContext:
    # Используем стандартную проверку сертификатов (без отключения проверки)
    return ssl.create_default_context()


def _get_local_ip() -> Optional[str]:
    tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        tmp.connect(("8.8.8.8", 80))
        return tmp.getsockname()[0]
    except OSError:
        return None
    finally:
        tmp.close()


def _publish_address(topic: str, ext_ip: str, ext_port: int,
                      local_ip: Optional[str], local_port: int) -> bool:
    lines = ["SERVER IS RUNNING", f"Address: {ext_ip}:{ext_port}"]
    if local_ip:
        lines.append(f"LocalAddress: {local_ip}:{local_port}")
    message = "\n".join(lines).encode("utf-8")

    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as resp:
            return 200 <= resp.status < 300
    except Exception as e:
        print(f"[UDP] [ERROR] Failed to publish address: {e}")
        return False


def _fetch_peer_address(topic: str, stop_event: threading.Event,
                        max_attempts: int = 5, attempt_interval: float = 2.0,
                        request_timeout: float = 10.0) -> Tuple[Optional[str], Optional[str]]:
    """
    Циклически пытается получить адрес пира.
    Каждая попытка — запрос с poll=1 и таймаутом request_timeout.
    """
    ctx = _ssl_context()
    for attempt in range(max_attempts):
        if stop_event.is_set():
            return None, None

        try:
            print(f"[UDP] [INFO] Fetch attempt {attempt + 1}/{max_attempts} from '{topic}'...")
            url = f"https://ntfy.sh/{topic}/json?poll=1&since=all"
            with urllib.request.urlopen(url, timeout=request_timeout, context=ctx) as response:
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
                    for line in last_message.split("\n"):
                        stripped = line.strip()
                        if stripped.startswith("LocalAddress:"):
                            local_address = stripped.replace("LocalAddress:", "").strip()
                        elif stripped.startswith("Address:"):
                            address = stripped.replace("Address:", "").strip()
                    if address:
                        print(f"[UDP] [OK] Peer address found on attempt {attempt + 1}")
                        return address, local_address

        except Exception as e:
            print(f"[UDP] [WARN] Attempt {attempt + 1} failed: {e}")

        if attempt < max_attempts - 1:
            print(f"[UDP] [INFO] Waiting {attempt_interval}s before next attempt...")
            if stop_event.wait(attempt_interval):
                return None, None

    return None, None


class UdpTransport(ITransport):
    def __init__(
        self,
        my_topic: str,
        peer_topic: str,
        local_port: int = 9090,
        punch_timeout: float = 12.0,
        punch_interval: float = 0.5,
        max_punch_attempts: int = 15,
    ) -> None:
        super().__init__()
        self._my_topic = my_topic
        self._peer_topic = peer_topic
        self._local_port = local_port
        self._punch_timeout = punch_timeout
        self._punch_interval = punch_interval
        self._max_punch_attempts = max_punch_attempts

        self._sock: Optional[socket.socket] = None
        self._peer_addr: Optional[Tuple[str, int]] = None
        self._running = False
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()

    def start(self) -> bool:
        self._stop_event.clear()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("0.0.0.0", self._local_port))

        # 1. STUN
        print("[UDP] [INFO] Determining own external address via STUN...")
        ext = stun_client.get_external_address_with_fallback(self._sock)
        if not ext:
            print("[UDP] [ERROR] STUN request failed")
            self._close_socket()
            return False

        my_ext_ip, my_ext_port = ext
        print(f"[UDP] [OK] My external address: {my_ext_ip}:{my_ext_port}")

        my_local_ip = _get_local_ip()
        if my_local_ip:
            print(f"[UDP] [OK] My local address: {my_local_ip}:{self._local_port}")

        # 2. Публикуем адрес (1 раз)
        print(f"[UDP] [INFO] Publishing my address to '{self._my_topic}'...")
        if not _publish_address(self._my_topic, my_ext_ip, my_ext_port,
                                 my_local_ip, self._local_port):
            print("[UDP] [ERROR] Failed to publish own address")
            self._close_socket()
            return False
        print("[UDP] [OK] Address published")

        # Даём пиру время на публикацию (увеличено до 5 сек)
        print("[UDP] [INFO] Waiting 5 seconds for peer to publish its address...")
        time.sleep(5.0)

        # 3. Забираем адрес пира (цикл попыток)
        peer_address, peer_local_address = _fetch_peer_address(
            self._peer_topic, self._stop_event,
            max_attempts=5,
            attempt_interval=2.0,
            request_timeout=10.0
        )
        if not peer_address:
            print("[UDP] [ERROR] Could not get peer address after attempts")
            self._close_socket()
            return False

        print(f"[UDP] [OK] Peer external address: {peer_address}")
        if peer_local_address:
            print(f"[UDP] [OK] Peer local address: {peer_local_address}")

        # 4. Выбор адреса (hairpinning)
        address_to_use = peer_address
        peer_ext_ip = peer_address.split(":")[0]
        if peer_ext_ip == my_ext_ip:
            print("[UDP] [WARN] Same external IP — same local network")
            if peer_local_address:
                print(f"[UDP] [WARN] Switching to LOCAL address: {peer_local_address}")
                address_to_use = peer_local_address
            else:
                print("[UDP] [WARN] No local address — punch may fail")

        try:
            peer_ip, peer_port_str = address_to_use.split(":")
            peer_port = int(peer_port_str)
        except Exception as e:
            print(f"[UDP] [ERROR] Invalid address format: {address_to_use} ({e})")
            self._close_socket()
            return False

        # 5. Punch
        if not self._punch(peer_ip, peer_port):
            print("[UDP] [ERROR] Hole punching failed")
            self._close_socket()
            return False

        self._peer_addr = (peer_ip, peer_port)
        self._running = True

        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        print(f"[UDP] [OK] Connected to {peer_ip}:{peer_port}")
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
            print(f"[UDP] [ERROR] Send failed: {e}")
            return False

    def is_alive(self) -> bool:
        return self._running and self._peer_addr is not None

    def _punch(self, peer_ip: str, peer_port: int) -> bool:
        assert self._sock is not None
        self._sock.settimeout(1.0)

        hello_packet = pack(PacketType.HELLO, seq=0)
        punch_stop = threading.Event()

        def punch_loop():
            for i in range(self._max_punch_attempts):
                if punch_stop.is_set():
                    return
                try:
                    self._sock.sendto(hello_packet, (peer_ip, peer_port))
                    if (i + 1) % 5 == 0:
                        print(f"[UDP] [PUNCH] HELLO #{i + 1} to {peer_ip}:{peer_port}")
                except OSError as e:
                    print(f"[UDP] [ERROR] Punch send failed: {e}")
                time.sleep(self._punch_interval)

        puncher = threading.Thread(target=punch_loop, daemon=True)
        puncher.start()

        deadline = time.time() + self._punch_timeout
        success = False

        while time.time() < deadline:
            try:
                data, addr = self._sock.recvfrom(1024)
                print(f"[UDP] [OK] Received response from {addr}")
                success = True
                break
            except socket.timeout:
                continue
            except Exception as e:
                print(f"[UDP] [ERROR] Receive error: {e}")
                break

        punch_stop.set()
        return success

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
                # WSAENOTSOCK (10038) — сокет закрыт
                if e.errno == 10038:
                    print("[UDP] [WARN] Socket closed, stopping recv loop")
                    break
                # Connection reset / abort / другие критические ошибки
                if e.errno in (10054, 10053):
                    print(f"[UDP] [WARN] Connection error {e.errno}, stopping recv loop")
                    break
                # Для других ошибок — просто логируем и продолжаем
                print(f"[UDP] [WARN] Socket error in recv loop: {e}")
                # Небольшая пауза, чтобы не спамить ошибками
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
