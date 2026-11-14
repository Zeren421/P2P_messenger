"""
Бинарный протокол сообщений мессенджера.

Формат пакета:
    version      : 1 байт  (версия протокола)
    type         : 1 байт  (см. PacketType)
    seq          : 4 байта, uint32 BE (порядковый номер от отправителя)
    payload_len  : 2 байта, uint16 BE
    payload      : payload_len байт
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

# ===================================================
#  КРИПТОГРАФИЯ
# ===================================================
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

PROTOCOL_VERSION = 1

_HEADER_FORMAT = "!BBIH"  # version, type, seq, payload_len
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)  # 8 байт

MAX_PAYLOAD_SIZE = 60000 - 16  # с запасом под MTU/фрагментацию UDP

class PacketType(IntEnum):
    HELLO = 0x01
    MSG = 0x02
    ACK = 0x03
    TYPING = 0x04
    PING = 0x05
    PONG = 0x06
    BYE = 0x07
    NAME = 0x08
    PUBKEY = 0x09
    PASSWORD = 0x0A
    PASSWORD_ACCEPT = 0x0B
    PASSWORD_REJECT = 0x0C
    # === ФАЙЛОВЫЕ ПАКЕТЫ ===
    FILE_INFO = 0x10
    FILE_CHUNK = 0x11
    FILE_CHUNK_ACK = 0x12
    FILE_DONE = 0x13
    FILE_CHUNK_ACK_BITMAP = 0x14   # НОВЫЙ ТИП ДЛЯ КУМУЛЯТИВНЫХ ACK
    FILE_CANCEL = 0x15

class ProtocolError(Exception):
    """Некорректный или повреждённый пакет."""

@dataclass
class Packet:
    type: PacketType
    seq: int
    payload: bytes
    version: int = PROTOCOL_VERSION

    def is_reliable(self) -> bool:
        return self.type in (PacketType.MSG, PacketType.NAME, PacketType.PASSWORD)

# ===================================================
#  КЛАСС ДЛЯ КРИПТОГРАФИИ СЕССИИ
# ===================================================
class SessionCrypto:
    def __init__(self):
        self.private_key = X25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.public_key_bytes = self.public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.send_key = None
        self.recv_key = None
        self.peer_public_key = None
        self.ready = False

    def set_peer_public_key(self, peer_bytes: bytes) -> None:
        if len(peer_bytes) != 32:
            raise ValueError("Публичный ключ должен быть ровно 32 байта")
        self.peer_public_key = X25519PublicKey.from_public_bytes(peer_bytes)
        shared_secret = self.private_key.exchange(self.peer_public_key)

        if self.public_key_bytes < peer_bytes:
            send_info, recv_info = b"chat_key_a_to_b", b"chat_key_b_to_a"
        else:
            send_info, recv_info = b"chat_key_b_to_a", b"chat_key_a_to_b"

        self.send_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"p2pchat_salt", info=send_info,
        ).derive(shared_secret)
        self.recv_key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=b"p2pchat_salt", info=recv_info,
        ).derive(shared_secret)
        self.ready = True

    def encrypt(self, plaintext: bytes, seq: int) -> bytes:
        if not self.ready:
            raise ValueError("Ключ ещё не установлен")
        nonce = seq.to_bytes(12, "big")
        aesgcm = AESGCM(self.send_key)
        return aesgcm.encrypt(nonce, plaintext, None)

    def decrypt(self, ciphertext: bytes, seq: int) -> bytes:
        if not self.ready:
            raise ValueError("Ключ ещё не установлен")
        nonce = seq.to_bytes(12, "big")
        aesgcm = AESGCM(self.recv_key)
        return aesgcm.decrypt(nonce, ciphertext, None)

# ===================================================
#  ФУНКЦИИ УПАКОВКИ/РАСПАКОВКИ ПАКЕТОВ
# ===================================================
def pack(packet_type: PacketType, seq: int, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD_SIZE:
        raise ProtocolError(f"Payload too large: {len(payload)} bytes (max {MAX_PAYLOAD_SIZE})")
    if not (0 <= seq <= 0xFFFFFFFF):
        raise ProtocolError(f"seq out of range: {seq}")

    header = struct.pack(_HEADER_FORMAT, PROTOCOL_VERSION, int(packet_type), seq, len(payload))
    return header + payload

def pack_msg(seq: int, text: str) -> bytes:
    return pack(PacketType.MSG, seq, text.encode("utf-8"))

def pack_ack(seq: int, acked_seq: int) -> bytes:
    return pack(PacketType.ACK, seq, struct.pack("!I", acked_seq))

def pack_typing(seq: int, is_typing: bool, sender_name: str = "") -> bytes:
    flag = bytes([1 if is_typing else 0])
    return pack(PacketType.TYPING, seq, flag + sender_name.encode("utf-8"))

def pack_name(seq: int, name: str) -> bytes:
    return pack(PacketType.NAME, seq, name.encode("utf-8"))

def pack_pubkey(seq: int, pubkey_bytes: bytes) -> bytes:
    if len(pubkey_bytes) != 32:
        raise ProtocolError(f"Публичный ключ должен быть 32 байта, получено {len(pubkey_bytes)}")
    return pack(PacketType.PUBKEY, seq, pubkey_bytes)

def pack_password(seq: int, encrypted_password: bytes) -> bytes:
    return pack(PacketType.PASSWORD, seq, encrypted_password)

def pack_password_accept(seq: int) -> bytes:
    return pack(PacketType.PASSWORD_ACCEPT, seq, b"")

def pack_password_reject(seq: int, reason: str = "") -> bytes:
    return pack(PacketType.PASSWORD_REJECT, seq, reason.encode("utf-8"))

def unpack(data: bytes) -> Packet:
    if len(data) < _HEADER_SIZE:
        raise ProtocolError(f"Packet too short: {len(data)} bytes (need at least {_HEADER_SIZE})")

    version, type_byte, seq, payload_len = struct.unpack(_HEADER_FORMAT, data[:_HEADER_SIZE])

    try:
        packet_type = PacketType(type_byte)
    except ValueError:
        raise ProtocolError(f"Unknown packet type: {type_byte}")

    payload = data[_HEADER_SIZE:]
    if len(payload) != payload_len:
        raise ProtocolError(f"Payload length mismatch: header says {payload_len}, got {len(payload)}")

    return Packet(type=packet_type, seq=seq, payload=payload, version=version)

def unpack_ack_target(packet: Packet) -> int:
    if packet.type != PacketType.ACK:
        raise ProtocolError("unpack_ack_target called on a non-ACK packet")
    if len(packet.payload) != 4:
        raise ProtocolError(f"Malformed ACK payload: {len(packet.payload)} bytes (expected 4)")
    return struct.unpack("!I", packet.payload)[0]

def unpack_text(packet: Packet) -> str:
    if packet.type != PacketType.MSG:
        raise ProtocolError("unpack_text called on a non-MSG packet")
    return packet.payload.decode("utf-8")

def unpack_typing(packet: Packet) -> tuple[bool, str]:
    if packet.type != PacketType.TYPING:
        raise ProtocolError("unpack_typing called on a non-TYPING packet")
    if len(packet.payload) < 1:
        raise ProtocolError("Malformed TYPING payload: empty")
    is_typing = packet.payload[0] == 1
    sender_name = packet.payload[1:].decode("utf-8", errors="replace")
    return is_typing, sender_name

def unpack_name(packet: Packet) -> str:
    if packet.type != PacketType.NAME:
        raise ProtocolError("unpack_name called on a non-NAME packet")
    return packet.payload.decode("utf-8", errors="replace")

def unpack_pubkey(packet: Packet) -> bytes:
    if packet.type != PacketType.PUBKEY:
        raise ProtocolError("unpack_pubkey called on a non-PUBKEY packet")
    if len(packet.payload) != 32:
        raise ProtocolError(f"Malformed PUBKEY payload: {len(packet.payload)} bytes (expected 32)")
    return packet.payload

def unpack_password(packet: Packet) -> bytes:
    if packet.type != PacketType.PASSWORD:
        raise ProtocolError("unpack_password called on a non-PASSWORD packet")
    return packet.payload

def unpack_password_accept(packet: Packet) -> None:
    if packet.type != PacketType.PASSWORD_ACCEPT:
        raise ProtocolError("unpack_password_accept called on a non-PASSWORD_ACCEPT packet")
    if packet.payload:
        raise ProtocolError("PASSWORD_ACCEPT payload should be empty")

def unpack_password_reject(packet: Packet) -> str:
    if packet.type != PacketType.PASSWORD_REJECT:
        raise ProtocolError("unpack_password_reject called on a non-PASSWORD_REJECT packet")
    return packet.payload.decode("utf-8", errors="replace")

# ===================================================
#  ФАЙЛОВЫЕ ПАКЕТЫ (с поддержкой битмапа)
# ===================================================

def pack_file_info(seq: int, payload: bytes) -> bytes:
    return pack(PacketType.FILE_INFO, seq, payload)

def pack_file_chunk(seq: int, payload: bytes) -> bytes:
    return pack(PacketType.FILE_CHUNK, seq, payload)

def pack_file_chunk_ack(seq: int, file_id: int, chunk_seq: int) -> bytes:
    # chunk_seq теперь CUMULATIVE ACK (4 байта вместо 2)
    payload = file_id.to_bytes(4, 'big') + chunk_seq.to_bytes(4, 'big')
    return pack(PacketType.FILE_CHUNK_ACK, seq, payload)

def pack_file_chunk_ack_bitmap(seq: int, file_id: int, base_seq: int, bitmap: int) -> bytes:
    """
    Упаковывает ACK с битмапом.
    bitmap: 64-битное целое, где бит n соответствует чанку base_seq + n.
    """
    payload = file_id.to_bytes(4, 'big') + base_seq.to_bytes(4, 'big') + bitmap.to_bytes(8, 'big')
    return pack(PacketType.FILE_CHUNK_ACK_BITMAP, seq, payload)

def unpack_file_chunk_ack_bitmap(payload: bytes) -> tuple[int, int, int]:
    if len(payload) != 16:
        raise ProtocolError("Invalid FILE_CHUNK_ACK_BITMAP payload size")
    file_id = int.from_bytes(payload[0:4], 'big')
    base_seq = int.from_bytes(payload[4:8], 'big')
    bitmap = int.from_bytes(payload[8:16], 'big')
    return file_id, base_seq, bitmap

def pack_file_done(seq: int, file_id: int) -> bytes:
    payload = file_id.to_bytes(4, 'big')
    return pack(PacketType.FILE_DONE, seq, payload)
def pack_file_cancel(seq: int, file_id: int, by_sender: bool) -> bytes:
    payload = file_id.to_bytes(4, "big") + bytes([1 if by_sender else 0])
    return pack(PacketType.FILE_CANCEL, seq, payload)

def unpack_file_cancel(packet: Packet) -> tuple[int, bool]:
    if packet.type != PacketType.FILE_CANCEL:
        raise ProtocolError("Not a FILE_CANCEL packet")
    if len(packet.payload) != 5:
        raise ProtocolError("Malformed FILE_CANCEL payload")
    return int.from_bytes(packet.payload[:4], "big"), packet.payload[4] == 1
