"""
Минимальный STUN-клиент (RFC 5389, Binding Request/Response).
Определяет внешний ip:port для переданного UDP-сокета — тот же принцип,
что и в C++ версии: сокет должен быть уже создан и (опционально) забинден
на нужный локальный порт до вызова, чтобы NAT видел один и тот же порт
и для STUN-запроса, и для последующего обмена с пиром.
"""

import socket
import struct
import os

STUN_SERVER = "stun.cloudflare.com"
STUN_PORT = 3478

# Публичные STUN-серверы время от времени становятся недоступны без
# предупреждения — Google STUN в частности known-flaky. Перебираем
# несколько по очереди вместо жёсткой привязки к одному.
FALLBACK_SERVERS = [
    ("stun.cloudflare.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.miwifi.com", 3478),
]

MAGIC_COOKIE = 0x2112A442
XOR_MAPPED_ADDRESS = 0x0020


def _build_binding_request():
    msg_type = 0x0001      # Binding Request
    msg_length = 0
    transaction_id = os.urandom(12)
    return struct.pack("!HHI12s", msg_type, msg_length, MAGIC_COOKIE, transaction_id)


def _parse_binding_response(data: bytes):
    if len(data) < 20:
        return None

    msg_type, msg_length, cookie = struct.unpack("!HHI", data[:8])
    if cookie != MAGIC_COOKIE:
        return None

    offset = 20
    while offset + 4 <= len(data):
        attr_type, attr_len = struct.unpack("!HH", data[offset:offset + 4])
        attr_value = data[offset + 4: offset + 4 + attr_len]

        if attr_type == XOR_MAPPED_ADDRESS and len(attr_value) >= 8:
            family = attr_value[1]
            if family == 0x01:  # IPv4
                xport = struct.unpack("!H", attr_value[2:4])[0]
                port = xport ^ (MAGIC_COOKIE >> 16)

                xip = struct.unpack("!I", attr_value[4:8])[0]
                ip_int = xip ^ MAGIC_COOKIE
                ip = ".".join(str((ip_int >> (8 * i)) & 0xFF) for i in (3, 2, 1, 0))
                return ip, port

        # STUN-атрибуты выровнены по границе 4 байт
        padded_len = attr_len + (4 - attr_len % 4) % 4
        offset += 4 + padded_len

    return None


def get_external_address(sock: socket.socket, stun_host=STUN_SERVER, stun_port=STUN_PORT, timeout=3.0):
    """
    Отправляет STUN Binding Request через переданный сокет и возвращает
    (ip, port) — внешний адрес, как его видит STUN-сервер, либо None при ошибке.
    """
    request = _build_binding_request()

    old_timeout = sock.gettimeout()
    sock.settimeout(timeout)
    try:
        sock.sendto(request, (stun_host, stun_port))
        data, _ = sock.recvfrom(2048)
    except socket.timeout:
        return None
    except socket.gaierror as e:
        print(f"[ERROR] Failed to resolve STUN server: {e}")
        return None
    finally:
        sock.settimeout(old_timeout)

    return _parse_binding_response(data)


def get_external_address_with_fallback(sock: socket.socket, servers=None, timeout=3.0):
    """
    Пробует несколько STUN-серверов по очереди и возвращает (ip, port)
    от первого, который ответил, либо None, если ни один не откликнулся.
    """
    if servers is None:
        servers = FALLBACK_SERVERS

    for host, port in servers:
        print(f"[INFO] Trying STUN server {host}:{port}...")
        result = get_external_address(sock, stun_host=host, stun_port=port, timeout=timeout)
        if result:
            print(f"[OK] STUN server {host} responded")
            return result
        print(f"[WARN] STUN server {host} did not respond, trying next...")

    print("[ERROR] All STUN servers failed")
    return None
