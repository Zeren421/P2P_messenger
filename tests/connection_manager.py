"""
ConnectionManager — улучшенная версия с поддержкой отправки байтов (для скриншотов)
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional, Dict, Deque, Set

RESEND_INTERVAL = 0.4
MAX_RESEND_ATTEMPTS = 5
DEDUP_WINDOW = 500

from protocol import (
    Packet, PacketType, ProtocolError, SessionCrypto,
    pack, pack_ack, pack_msg, pack_name, pack_pubkey,
    pack_password, pack_password_accept, pack_password_reject,
    pack_typing,
    unpack, unpack_ack_target, unpack_name, unpack_pubkey,
    unpack_password, unpack_password_accept, unpack_password_reject,
    unpack_text, unpack_typing, unpack_file_chunk_ack_bitmap,
)
from transport import ITransport
from udp_transport import UdpTransport
from relay_transport import RelayTransport
from file_transfer import FileTransferManager

@dataclass(slots=True)
class PendingEntry:
    kind: str
    data: bytes
    text: str
    sent_at: float
    attempts: int

class ConnectionManager:
    def __init__(
        self,
        my_topic: str,
        peer_topic: str,
        local_port: int = 9090,
        on_message: Optional[Callable[[str], None]] = None,
        on_typing: Optional[Callable[[bool, str], None]] = None,
        on_message_failed: Optional[Callable[[str], None]] = None,
        on_status: Optional[Callable[[str], None]] = None,
        on_peer_name: Optional[Callable[[str], None]] = None,
        on_password_retry: Optional[Callable[[int], None]] = None,
        on_file_received: Optional[Callable[[int, str, str], None]] = None,
        on_file_progress: Optional[Callable[[int, float, bool], None]] = None,
        on_file_error: Optional[Callable[[int, str], None]] = None,
        on_file_info: Optional[Callable[[int, str, int, bytes, str], None]] = None,
    ) -> None:
        self._my_topic = my_topic
        self._peer_topic = peer_topic
        self._local_port = local_port

        self._on_message = on_message
        self._on_typing = on_typing
        self._on_message_failed = on_message_failed
        self._on_status = on_status
        self._on_peer_name = on_peer_name
        self._on_password_retry = on_password_retry
        self._on_file_received = on_file_received
        self._on_file_progress = on_file_progress
        self._on_file_error = on_file_error
        self._on_file_info = on_file_info

        self._transport: Optional[ITransport] = None
        self._seq_lock = threading.Lock()
        self._next_seq_value = 1
        self._pending_lock = threading.Lock()
        self._pending: Dict[int, PendingEntry] = {}
        self._dedup_lock = threading.Lock()
        self._seen_seqs: Deque[int] = deque(maxlen=DEDUP_WINDOW)
        self._seen_seqs_set: Set[int] = set()

        self._running = False
        self._retry_thread: Optional[threading.Thread] = None

        self._crypto: Optional[SessionCrypto] = None
        self._key_exchanged = False
        self._pending_messages: list[str] = []
        self._key_exchange_start_time: Optional[float] = None
        self._key_exchange_attempts = 0
        self._max_key_exchange_attempts = 3

        self._password: Optional[str] = None
        self._password_verified = False
        self._password_attempts = 0
        self._max_password_attempts = 3

        self._ping_interval = 15.0
        self._ping_retries = 5
        self._last_pong_time = time.time()
        self._ping_missed = 0

        self._file_manager: Optional[FileTransferManager] = None
        self._reconnecting = False

    def set_password(self, password: str) -> None:
        self._password = password.strip() if password else None
        self._password_attempts = 0
        self._password_verified = False

    def connect(self) -> bool:
        self._status("Connecting via UDP (hole punching)...")
        udp = UdpTransport(
            self._my_topic,
            self._peer_topic,
            local_port=self._local_port,
            max_discovery_time=180.0,
            discovery_interval=3.0,
        )
        if udp.start():
            self._transport = udp
            self._status("Connected via UDP (direct P2P)")
        else:
            udp.stop()
            self._status("UDP failed, falling back to relay...")
            relay = RelayTransport(self._my_topic, self._peer_topic)
            if not relay.start():
                self._status("Relay also failed to connect")
                return False
            self._transport = relay
            self._status("Connected via relay (ntfy fallback)")

        self._transport.set_on_receive(self._on_raw_received)
        self._running = True
        self._retry_thread = threading.Thread(target=self._retry_loop, daemon=True)
        self._retry_thread.start()

        self._crypto = SessionCrypto()
        self._key_exchanged = False
        self._pending_messages.clear()
        self._key_exchange_start_time = time.time()
        self._key_exchange_attempts = 0
        self._password_verified = False
        self._password_attempts = 0
        self._last_pong_time = time.time()
        self._ping_missed = 0
        self.send_pubkey()

        self._file_manager = FileTransferManager(
            send_raw=self._send_raw,
            encrypt=lambda data, seq: self._crypto.encrypt(data, seq) if self._crypto else None,
            decrypt=lambda data, seq: self._crypto.decrypt(data, seq) if self._crypto else None,
            next_seq=self._next_seq,
            on_progress=self._on_file_progress,
            on_file_received=self._on_file_received,
            on_file_error=self._on_file_error,
            on_file_info=self._on_file_info,
            downloads_dir=".",
        )
        self._file_manager.start()
        return True

    def disconnect(self) -> None:
        self._running = False
        self._reconnecting = False
        if self._retry_thread is not None:
            self._retry_thread.join(timeout=2.0)
        if self._transport is not None:
            self._transport.stop()
            self._transport = None
        with self._pending_lock:
            self._pending.clear()
        self._key_exchanged = False
        self._pending_messages.clear()
        if self._file_manager is not None:
            self._file_manager.stop()
            self._file_manager = None
        self._crypto = None
        self._key_exchange_start_time = None
        self._key_exchange_attempts = 0
        self._password_verified = False
        self._last_pong_time = time.time()
        self._ping_missed = 0

    # ==========================================================
    # НОВЫЙ МЕТОД ДЛЯ ОТПРАВКИ ИЗОБРАЖЕНИЙ ПРЯМО ИЗ БАЙТОВ
    # ==========================================================
    def send_image_bytes(self, image_bytes: bytes, caption: str = "") -> int:
        """
        Отправляет изображение напрямую из оперативной памяти без создания временного файла.
        Возвращает file_id для отслеживания прогресса.
        """
        if self._file_manager is None:
            raise Exception("Connection not established")
        
        # Разрешаем отправку даже через Relay (ранее было только UDP)
        # Это нужно, если hole-punching не сработал, но хочется отправить хотя бы превью
        # if not isinstance(self._transport, UdpTransport):
        #     raise Exception("File transfer only available over UDP")
            
        if self._crypto is None or not self._key_exchanged or not self._password_verified:
            raise Exception("Encryption not ready")

        # Используем встроенную возможность менеджера файлов работать с памятью
        return self._file_manager.send_memory_file(
            data=image_bytes,
            filename="Скриншот.png",
            caption=caption,
            thumbnail=None
        )

    def send_message(self, text: str) -> None:
        if self._crypto is None or not self._key_exchanged:
            self._pending_messages.append(text)
            return
        if not self._password_verified and self._password is not None:
            self._pending_messages.append(text)
            return

        seq = self._next_seq()
        encrypted = self._crypto.encrypt(text.encode(), seq)
        data = pack(PacketType.MSG, seq, encrypted)

        with self._pending_lock:
            self._pending[seq] = PendingEntry("msg", data, text, time.time(), 1)
        self._send_raw(data)

    def send_typing(self, is_typing: bool, sender_name: str = "") -> None:
        seq = self._next_seq()
        self._send_raw(pack_typing(seq, is_typing, sender_name))

    def send_name(self, name: str) -> None:
        seq = self._next_seq()
        data = pack_name(seq, name)
        with self._pending_lock:
            self._pending[seq] = PendingEntry("name", data, name, time.time(), 1)
        self._send_raw(data)

    def send_pubkey(self) -> None:
        if self._crypto is None:
            return
        self._key_exchange_start_time = time.time()
        self._key_exchange_attempts += 1
        seq = self._next_seq()
        data = pack_pubkey(seq, self._crypto.public_key_bytes)
        with self._pending_lock:
            self._pending[seq] = PendingEntry("pubkey", data, "", time.time(), 1)
        self._send_raw(data)

    def send_password(self) -> None:
        if self._crypto is None or not self._key_exchanged:
            return
        if self._password is None or self._password == "":
            self._password_verified = True
            return
        seq = self._next_seq()
        encrypted = self._crypto.encrypt(self._password.encode(), seq)
        data = pack_password(seq, encrypted)
        with self._pending_lock:
            self._pending[seq] = PendingEntry("password", data, "", time.time(), 1)
        self._send_raw(data)

    def send_bye(self) -> None:
        seq = self._next_seq()
        self._send_raw(pack(PacketType.BYE, seq))

    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.is_alive()

    # Измененный старый метод: убрано жесткое ограничение "только UDP"
    def send_file(self, filepath: str, caption: str = "", thumbnail: Optional[bytes] = None) -> int:
        if self._file_manager is None:
            raise Exception("Connection not established")
        
        # Теперь можно отправлять файлы и через Relay, если прямой канал недоступен
        # if not isinstance(self._transport, UdpTransport):
        #     raise Exception("File transfer only available over UDP")
            
        if self._crypto is None or not self._key_exchanged or not self._password_verified:
            raise Exception("Encryption not ready")
        return self._file_manager.send_file(filepath, caption, thumbnail)

    def _next_seq(self) -> int:
        with self._seq_lock:
            seq = self._next_seq_value
            self._next_seq_value = (self._next_seq_value + 1) & 0xFFFFFFFF
            return seq

    def _send_raw(self, data: bytes) -> bool:
        return self._transport is not None and self._transport.send(data)

    def _status(self, msg: str) -> None:
        print(f"[CM] [INFO] {msg}")
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _on_raw_received(self, data: bytes) -> None:
        try:
            packet = unpack(data)
        except ProtocolError as e:
            print(f"[CM] [WARN] Malformed packet: {e}")
            return

        handler = {
            PacketType.MSG: self._handle_msg,
            PacketType.ACK: self._handle_ack,
            PacketType.TYPING: self._handle_typing,
            PacketType.NAME: self._handle_name,
            PacketType.PUBKEY: self._handle_pubkey,
            PacketType.PASSWORD: self._handle_password,
            PacketType.PASSWORD_ACCEPT: self._handle_password_accept,
            PacketType.PASSWORD_REJECT: self._handle_password_reject,
            PacketType.PING: self._handle_ping,
            PacketType.PONG: self._handle_pong,
            PacketType.BYE: self._handle_bye,
            PacketType.FILE_INFO: self._handle_file_info,
            PacketType.FILE_CHUNK: self._handle_file_chunk,
            PacketType.FILE_CHUNK_ACK: self._handle_file_chunk_ack,
            PacketType.FILE_CHUNK_ACK_BITMAP: self._handle_file_chunk_ack_bitmap,
            PacketType.FILE_DONE: self._handle_file_done,
        }.get(packet.type)
        if handler:
            handler(packet)
        elif packet.type != PacketType.HELLO:
            print(f"[CM] [WARN] Unknown packet type: {packet.type}")

    def _handle_msg(self, packet: Packet) -> None:
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        if self._is_duplicate(packet.seq):
            return
        if self._crypto is None:
            return
        try:
            text = self._crypto.decrypt(packet.payload, packet.seq).decode()
        except Exception as e:
            print(f"[CM] [WARN] Decrypt failed: {e}")
            return
        if self._on_message is not None:
            try:
                self._on_message(text)
            except Exception:
                pass

    def _handle_ack(self, packet: Packet) -> None:
        try:
            acked_seq = unpack_ack_target(packet)
        except ProtocolError:
            return
        with self._pending_lock:
            self._pending.pop(acked_seq, None)

    def _handle_typing(self, packet: Packet) -> None:
        try:
            is_typing, sender = unpack_typing(packet)
        except ProtocolError:
            return
        if self._on_typing is not None:
            try:
                self._on_typing(is_typing, sender)
            except Exception:
                pass

    def _handle_name(self, packet: Packet) -> None:
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        try:
            name = unpack_name(packet)
        except ProtocolError:
            return
        if self._on_peer_name is not None:
            try:
                self._on_peer_name(name)
            except Exception:
                pass

    def _handle_pubkey(self, packet: Packet) -> None:
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        if self._key_exchanged or self._crypto is None:
            return
        try:
            self._crypto.set_peer_public_key(unpack_pubkey(packet))
            self._key_exchanged = True
            self._key_exchange_start_time = None
            self._key_exchange_attempts = 0
        except Exception as e:
            print(f"[CM] [WARN] Pubkey error: {e}")
            return

        if self._password and self._password != "":
            self.send_password()
        else:
            self._password_verified = True
            self._status("No password required")

        if self._password_verified and self._pending_messages:
            msgs = self._pending_messages[:]
            self._pending_messages.clear()
            for text in msgs:
                self.send_message(text)

    def _handle_password(self, packet: Packet) -> None:
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        if self._crypto is None or not self._key_exchanged:
            return
        try:
            received = self._crypto.decrypt(unpack_password(packet), packet.seq).decode()
        except Exception:
            return

        if self._password is None or self._password == "":
            self._send_raw(pack_password_reject(self._next_seq(), "No password expected"))
            return

        if received == self._password:
            self._send_raw(pack_password_accept(self._next_seq()))
            self._password_verified = True
            if self._pending_messages:
                msgs = self._pending_messages[:]
                self._pending_messages.clear()
                for text in msgs:
                    self.send_message(text)
        else:
            self._send_raw(pack_password_reject(self._next_seq(), "Invalid password"))

    def _handle_password_accept(self, packet: Packet) -> None:
        self._password_verified = True
        self._status("Password verified")
        if self._pending_messages:
            msgs = self._pending_messages[:]
            self._pending_messages.clear()
            for text in msgs:
                self.send_message(text)

    def _handle_password_reject(self, packet: Packet) -> None:
        self._password_attempts += 1
        if self._password_attempts >= self._max_password_attempts:
            self._status("Too many password attempts, disconnecting")
            self.disconnect()
            return
        if self._on_password_retry is not None:
            try:
                reason = unpack_password_reject(packet)
            except ProtocolError:
                reason = "Invalid password"
            self._on_password_retry(self._max_password_attempts - self._password_attempts)

    def _handle_ping(self, packet: Packet) -> None:
        self._send_raw(pack(PacketType.PONG, self._next_seq()))

    def _handle_pong(self, packet: Packet) -> None:
        self._last_pong_time = time.time()
        self._ping_missed = 0

    def _handle_bye(self, packet: Packet) -> None:
        self._status("Peer disconnected (graceful)")
        self._key_exchanged = False
        self._pending_messages.clear()
        with self._pending_lock:
            self._pending.clear()
        self._crypto = None
        self._key_exchange_start_time = None
        self._key_exchange_attempts = 0
        self._password_verified = False
        self._last_pong_time = time.time()
        self._ping_missed = 0

    def _handle_file_info(self, packet: Packet) -> None:
        if self._file_manager is None or not self._key_exchanged or not self._password_verified:
            return
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        if not self._is_duplicate(packet.seq):
            try:
                self._file_manager.handle_file_info_packet(packet.seq, packet.payload)
            except Exception as e:
                print(f"[CM] File info error: {e}")

    def _handle_file_chunk(self, packet: Packet) -> None:
        if self._file_manager is None or not self._key_exchanged or not self._password_verified:
            return
        try:
            if len(packet.payload) < 6:
                return
            file_id = int.from_bytes(packet.payload[:4], "big")
            chunk_seq = int.from_bytes(packet.payload[4:6], "big")
            encrypted = packet.payload[6:]
            self._file_manager.handle_file_chunk_packet(packet.seq, file_id, chunk_seq, encrypted)
        except Exception as e:
            print(f"[CM] File chunk error: {e}")

    def _handle_file_chunk_ack(self, packet: Packet) -> None:
        if self._file_manager is None or not self._key_exchanged or not self._password_verified:
            return
        try:
            if len(packet.payload) < 6:
                return
            file_id = int.from_bytes(packet.payload[:4], "big")
            chunk_seq = int.from_bytes(packet.payload[4:6], "big")
            self._file_manager.handle_file_chunk_ack_packet(file_id, chunk_seq)
        except Exception as e:
            print(f"[CM] File ack error: {e}")

    def _handle_file_chunk_ack_bitmap(self, packet: Packet) -> None:
        if self._file_manager is None or not self._key_exchanged or not self._password_verified:
            return
        try:
            file_id, base_seq, bitmap = unpack_file_chunk_ack_bitmap(packet.payload)
            self._file_manager.handle_file_chunk_ack_bitmap_packet(file_id, base_seq, bitmap)
        except Exception as e:
            print(f"[CM] File bitmap ack error: {e}")

    def _handle_file_done(self, packet: Packet) -> None:
        if self._file_manager is None or not self._key_exchanged or not self._password_verified:
            return
        self._send_raw(pack_ack(self._next_seq(), packet.seq))
        try:
            if len(packet.payload) < 4:
                return
            file_id = int.from_bytes(packet.payload[:4], "big")
            self._file_manager.handle_file_done_packet(file_id)
            if self._on_file_progress is not None:
                self._on_file_progress(file_id, 1.0, True)
        except Exception as e:
            print(f"[CM] FILE_DONE error: {e}")

    def _is_duplicate(self, seq: int) -> bool:
        with self._dedup_lock:
            if seq in self._seen_seqs_set:
                return True
            if len(self._seen_seqs) >= self._seen_seqs.maxlen:
                oldest = self._seen_seqs.popleft()
                self._seen_seqs_set.discard(oldest)
            self._seen_seqs.append(seq)
            self._seen_seqs_set.add(seq)
            return False

    def _retry_loop(self) -> None:
        last_ping_sent = time.time()
        while self._running:
            time.sleep(RESEND_INTERVAL)
            now = time.time()

            if self._key_exchanged and self._password_verified:
                if now - last_ping_sent >= self._ping_interval:
                    self._send_raw(pack(PacketType.PING, self._next_seq()))
                    last_ping_sent = now
                    self._ping_missed += 1
                if self._ping_missed >= self._ping_retries:
                    self._status("Connection lost (keep-alive)")
                    self._ping_missed = 0
                    if not self._reconnecting:
                        self._reconnecting = True
                        threading.Thread(target=self._reconnect_in_background, daemon=True).start()
                    continue

            if not self._key_exchanged and self._key_exchange_start_time is not None:
                if now - self._key_exchange_start_time > 30.0:
                    if self._key_exchange_attempts < self._max_key_exchange_attempts:
                        self._status(f"Key exchange timeout, retry {self._key_exchange_attempts+1}/{self._max_key_exchange_attempts}")
                        self.send_pubkey()
                        self._key_exchange_start_time = now
                    else:
                        self._status("Key exchange failed after retries")
                        self._key_exchange_start_time = None
                        if not self._reconnecting:
                            self._reconnecting = True
                            threading.Thread(target=self._reconnect_in_background, daemon=True).start()

            with self._pending_lock:
                items = list(self._pending.items())

            for seq, entry in items:
                if now - entry.sent_at < RESEND_INTERVAL:
                    continue
                if entry.attempts >= MAX_RESEND_ATTEMPTS:
                    with self._pending_lock:
                        self._pending.pop(seq, None)
                    if entry.kind == "msg" and self._on_message_failed is not None:
                        try:
                            self._on_message_failed(entry.text)
                        except Exception:
                            pass
                    continue
                self._send_raw(entry.data)
                with self._pending_lock:
                    if seq in self._pending:
                        self._pending[seq].sent_at = now
                        self._pending[seq].attempts += 1

    def _reconnect_in_background(self) -> None:
        try:
            self._reconnecting = True
            max_attempts = 3
            attempt = 0
            while attempt < max_attempts and self._running:
                attempt += 1
                self._status(f"Reconnect attempt {attempt}/{max_attempts}...")
                time.sleep(2.0)
                if not self._running:
                    break
                self.disconnect()
                time.sleep(1.0)
                if not self._running:
                    break
                if self.connect():
                    self._status("Reconnected successfully")
                    return
                else:
                    self._status(f"Reconnect attempt {attempt} failed")
            self._status("Reconnect failed after all attempts")
        finally:
            self._reconnecting = False
