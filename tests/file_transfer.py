"""
FileTransferManager — с поддержкой кумулятивных ACK (битмап)
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, List, Set
from io import BytesIO

from protocol import (
    pack_file_info, pack_file_chunk, pack_file_chunk_ack, pack_file_done,
    pack_file_chunk_ack_bitmap, unpack_file_chunk_ack_bitmap, PacketType
)

CHUNK_SIZE = 1200

INITIAL_WINDOW_SIZE = 32
MAX_WINDOW_SIZE = 512
MIN_WINDOW_SIZE = 8

MAX_CHUNK_ATTEMPTS = 10
MAX_CONCURRENT_TRANSFERS = 1

class FileTransferError(Exception):
    pass

# ============================================================
#  БИНАРНЫЙ ФОРМАТ FILE_INFO
# ============================================================

def build_file_info_payload(
    file_id: int,
    filename: str,
    total_size: int,
    caption: str,
    thumbnail: bytes,
) -> bytes:
    filename_bytes = filename.encode("utf-8")
    caption_bytes = caption.encode("utf-8")

    if len(filename_bytes) > 65535:
        filename_bytes = filename_bytes[:65535]
    if len(caption_bytes) > 65535:
        caption_bytes = caption_bytes[:65535]
    if len(thumbnail) > 8000:
        thumbnail = thumbnail[:8000]

    payload = (
        file_id.to_bytes(4, "big") +
        len(filename_bytes).to_bytes(2, "big") +
        filename_bytes +
        total_size.to_bytes(4, "big") +
        len(caption_bytes).to_bytes(2, "big") +
        caption_bytes +
        len(thumbnail).to_bytes(2, "big") +
        thumbnail
    )
    return payload

def parse_file_info_payload(data: bytes) -> tuple[int, str, int, str, bytes]:
    if len(data) < 6:
        raise ValueError("FILE_INFO too short")

    offset = 0
    file_id = int.from_bytes(data[offset:offset+4], "big")
    offset += 4

    if len(data) < offset + 2:
        raise ValueError("No filename length")
    name_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    if len(data) < offset + name_len:
        raise ValueError("Filename truncated")
    filename = data[offset:offset+name_len].decode("utf-8", errors="replace")
    offset += name_len

    if len(data) < offset + 4:
        raise ValueError("No file size")
    total_size = int.from_bytes(data[offset:offset+4], "big")
    offset += 4

    if len(data) < offset + 2:
        raise ValueError("No caption length")
    caption_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    caption = data[offset:offset+caption_len].decode("utf-8", errors="replace")
    offset += caption_len

    if len(data) < offset + 2:
        raise ValueError("No thumbnail length")
    thumb_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    thumbnail = data[offset:offset+thumb_len]
    offset += thumb_len

    return file_id, filename, total_size, caption, thumbnail

# ============================================================
#  ГЕНЕРАЦИЯ МИНИАТЮРЫ
# ============================================================

def _thumbnail_bytes_from_image(img, max_size: int = 200) -> bytes:
    from PIL import Image
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=70, optimize=True)
    data = buf.getvalue()
    if len(data) <= 8000:
        return data
    img.thumbnail((max_size // 2, max_size // 2), Image.Resampling.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue()

def generate_thumbnail_from_file(filepath: str, max_size: int = 200) -> bytes:
    try:
        from PIL import Image
        img = Image.open(filepath)
        return _thumbnail_bytes_from_image(img, max_size)
    except Exception:
        return b""

def generate_thumbnail_from_bytes(data: bytes, max_size: int = 200) -> bytes:
    try:
        from PIL import Image
        img = Image.open(BytesIO(data))
        return _thumbnail_bytes_from_image(img, max_size)
    except Exception:
        return b""

# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ДАТАКЛАССЫ
# ============================================================

@dataclass
class OutgoingChunkMeta:
    chunk_seq: int
    sent_at: float = 0.0
    attempts: int = 0
    acked: bool = False

@dataclass
class SendingFile:
    file_id: int
    filepath: str
    filename: str
    caption: str
    thumbnail: bytes
    total_size: int
    total_chunks: int
    window_size: int = INITIAL_WINDOW_SIZE
    rtt_samples: List[float] = field(default_factory=list)
    cancelled: bool = False
    file_handle: Optional[object] = None
    chunks: Dict[int, OutgoingChunkMeta] = field(default_factory=dict)
    _next_chunk_to_send: int = 0

    def resend_timeout(self) -> float:
        if not self.rtt_samples:
            return 0.3
        recent = self.rtt_samples[-5:]
        avg_rtt = sum(recent) / len(recent)
        return max(0.05, min(1.0, avg_rtt * 2.0))

    def close_file(self) -> None:
        if self.file_handle is not None:
            try:
                self.file_handle.close()
            except:
                pass
            self.file_handle = None

@dataclass
class ReceivingFile:
    file_id: int
    filename: str
    caption: str
    thumbnail: bytes
    total_size: int
    total_chunks: int
    save_path: str
    started_at: float = field(default_factory=time.time)
    file_handle: Optional[object] = None
    received_chunks: Set[int] = field(default_factory=set)
    done_sent: bool = False

    def is_complete(self) -> bool:
        return len(self.received_chunks) == self.total_chunks

    def write_chunk(self, chunk_seq: int, data: bytes) -> None:
        if self.file_handle is None:
            return
        offset = chunk_seq * CHUNK_SIZE
        self.file_handle.seek(offset)
        self.file_handle.write(data)
        self.received_chunks.add(chunk_seq)

    def close_file(self) -> None:
        if self.file_handle is not None:
            try:
                self.file_handle.close()
            except:
                pass
            self.file_handle = None

# ============================================================
#  ОСНОВНОЙ КЛАСС
# ============================================================

class FileTransferManager:
    def __init__(
        self,
        send_raw: Callable[[bytes], bool],
        encrypt: Callable[[bytes, int], bytes],
        decrypt: Callable[[bytes, int], bytes],
        next_seq: Callable[[], int],
        on_progress: Optional[Callable[[int, float, bool], None]] = None,
        on_file_received: Optional[Callable[[int, str, str], None]] = None,
        on_file_error: Optional[Callable[[int, str], None]] = None,
        on_file_info: Optional[Callable[[int, str, int, bytes, str], None]] = None,
        downloads_dir: str = ".",
    ) -> None:
        self._send_raw = send_raw
        self._encrypt = encrypt
        self._decrypt = decrypt
        self._next_seq = next_seq
        self._on_progress = on_progress
        self._on_file_received = on_file_received
        self._on_file_error = on_file_error
        self._on_file_info = on_file_info
        self._downloads_dir = downloads_dir

        self._lock = threading.Lock()
        self._outgoing: Dict[int, SendingFile] = {}
        self._incoming: Dict[int, ReceivingFile] = {}
        self._next_file_id = 1

        self._running = False
        self._pump_thread: Optional[threading.Thread] = None
        self._bitmap_window = 64  # размер битмапа

    def start(self) -> None:
        self._running = True
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
        with self._lock:
            for f in self._outgoing.values():
                f.close_file()
            for f in self._incoming.values():
                f.close_file()
            self._outgoing.clear()
            self._incoming.clear()

    def send_file(
        self,
        filepath: str,
        caption: str = "",
        thumbnail: Optional[bytes] = None,
    ) -> int:
        if not os.path.isfile(filepath):
            raise FileTransferError(f"File not found: {filepath}")

        total_size = os.path.getsize(filepath)
        if total_size == 0:
            raise FileTransferError("File is empty")
        if total_size > 2_147_483_647:
            raise FileTransferError("File too large (max 2 GB)")

        if thumbnail is None:
            thumbnail = generate_thumbnail_from_file(filepath)

        with self._lock:
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            if active >= MAX_CONCURRENT_TRANSFERS:
                raise FileTransferError("Another transfer in progress")
            file_id = self._next_file_id
            self._next_file_id += 1

        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        filename = os.path.basename(filepath)

        try:
            file_handle = open(filepath, "rb")
        except OSError as e:
            raise FileTransferError(f"Failed to open file: {e}")

        sending = SendingFile(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            caption=caption,
            thumbnail=thumbnail or b"",
            total_size=total_size,
            total_chunks=total_chunks,
            window_size=INITIAL_WINDOW_SIZE,
            file_handle=file_handle,
        )

        for chunk_seq in range(total_chunks):
            sending.chunks[chunk_seq] = OutgoingChunkMeta(chunk_seq=chunk_seq)

        with self._lock:
            self._outgoing[file_id] = sending

        seq = self._next_seq()
        info_plaintext = build_file_info_payload(
            file_id=file_id,
            filename=filename,
            total_size=total_size,
            caption=caption,
            thumbnail=thumbnail or b"",
        )
        encrypted_info = self._encrypt(info_plaintext, seq)
        self._send_raw(pack_file_info(seq, encrypted_info))

        self._kick_window(file_id)
        return file_id

    def send_memory_file(
        self,
        data: bytes,
        filename: str,
        caption: str = "",
        thumbnail: Optional[bytes] = None,
    ) -> int:
        """
        Аналог send_file(), но источник данных — байты в памяти, а не файл на
        диске. Используется, например, для отправки скриншота прямо из
        буфера обмена без создания временного файла.
        """
        total_size = len(data)
        if total_size == 0:
            raise FileTransferError("Data is empty")
        if total_size > 2_147_483_647:
            raise FileTransferError("File too large (max 2 GB)")

        if thumbnail is None:
            thumbnail = generate_thumbnail_from_bytes(data)

        with self._lock:
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            if active >= MAX_CONCURRENT_TRANSFERS:
                raise FileTransferError("Another transfer in progress")
            file_id = self._next_file_id
            self._next_file_id += 1

        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        # BytesIO поддерживает тот же интерфейс seek/read/close, что и обычный
        # файловый хендл, поэтому его можно использовать вместо file_handle
        # без изменений в _send_chunk/_kick_window.
        file_handle = BytesIO(data)

        sending = SendingFile(
            file_id=file_id,
            filepath="",
            filename=filename,
            caption=caption,
            thumbnail=thumbnail or b"",
            total_size=total_size,
            total_chunks=total_chunks,
            window_size=INITIAL_WINDOW_SIZE,
            file_handle=file_handle,
        )

        for chunk_seq in range(total_chunks):
            sending.chunks[chunk_seq] = OutgoingChunkMeta(chunk_seq=chunk_seq)

        with self._lock:
            self._outgoing[file_id] = sending

        seq = self._next_seq()
        info_plaintext = build_file_info_payload(
            file_id=file_id,
            filename=filename,
            total_size=total_size,
            caption=caption,
            thumbnail=thumbnail or b"",
        )
        encrypted_info = self._encrypt(info_plaintext, seq)
        self._send_raw(pack_file_info(seq, encrypted_info))

        self._kick_window(file_id)
        return file_id

    def cancel_send(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is not None:
                sending.cancelled = True
                sending.close_file()

    # ------------------------------------------------------------------
    # Обработка входящих пакетов
    # ------------------------------------------------------------------

    def handle_file_info_packet(self, seq: int, payload: bytes) -> None:
        try:
            decrypted = self._decrypt(payload, seq)
            file_id, filename, total_size, caption, thumbnail = parse_file_info_payload(decrypted)
        except Exception as e:
            if self._on_file_error is not None:
                self._on_file_error(0, f"Failed to parse FILE_INFO: {e}")
            return

        if total_size > 2_147_483_647:
            if self._on_file_error is not None:
                self._on_file_error(file_id, "File too large (max 2 GB)")
            return

        safe_name = os.path.basename(filename) or f"file_{file_id}"
        save_path = self._unique_save_path(safe_name)

        try:
            file_handle = open(save_path, "w+b")
            file_handle.truncate(total_size)
        except OSError as e:
            if self._on_file_error is not None:
                self._on_file_error(file_id, f"Failed to create file: {e}")
            return

        with self._lock:
            self._incoming[file_id] = ReceivingFile(
                file_id=file_id,
                filename=safe_name,
                caption=caption,
                thumbnail=thumbnail,
                total_size=total_size,
                total_chunks=(total_size + CHUNK_SIZE - 1) // CHUNK_SIZE,
                save_path=save_path,
                file_handle=file_handle,
            )

        if self._on_file_info is not None:
            try:
                self._on_file_info(file_id, safe_name, total_size, thumbnail, caption)
            except Exception as e:
                print(f"[FT] on_file_info error: {e}")

    def handle_file_chunk_packet(self, seq: int, file_id: int, chunk_seq: int, encrypted_data: bytes) -> None:
        with self._lock:
            incoming = self._incoming.get(file_id)

        if incoming is None:
            # Если файл не найден, всё равно отправляем ACK, чтобы отправитель не ждал
            self._send_chunk_ack(file_id, chunk_seq)
            return

        if chunk_seq in incoming.received_chunks:
            self._send_chunk_ack(file_id, chunk_seq)
            return

        try:
            data = self._decrypt(encrypted_data, seq)
        except Exception:
            return

        try:
            incoming.write_chunk(chunk_seq, data)
        except Exception as e:
            if self._on_file_error is not None:
                self._on_file_error(file_id, f"Write error: {e}")
            return

        # Отправляем кумулятивный ACK, если накопилось 64 чанка или это последний
        self._send_bitmap_ack_if_needed(file_id)

        if self._on_progress is not None:
            ratio = len(incoming.received_chunks) / incoming.total_chunks
            self._on_progress(file_id, ratio, False)

        if incoming.is_complete():
            self._finish_incoming(incoming)

    def handle_file_chunk_ack_packet(self, file_id: int, chunk_seq: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None:
                return
            chunk = sending.chunks.get(chunk_seq)
            if chunk is not None:
                chunk.acked = True
                if chunk.sent_at > 0:
                    rtt = time.time() - chunk.sent_at
                    sending.rtt_samples.append(rtt)
                    if len(sending.rtt_samples) > 20:
                        sending.rtt_samples.pop(0)
                    avg_rtt = sum(sending.rtt_samples) / len(sending.rtt_samples)
                    if avg_rtt > 0.5:
                        sending.window_size = max(MIN_WINDOW_SIZE, int(sending.window_size * 0.8))
                    elif avg_rtt < 0.1 and sending.window_size < MAX_WINDOW_SIZE:
                        sending.window_size = min(MAX_WINDOW_SIZE, sending.window_size + 2)

        self._kick_window(file_id)

    def handle_file_chunk_ack_bitmap_packet(self, file_id: int, base_seq: int, bitmap: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None:
                return
            for i in range(64):
                if bitmap & (1 << i):
                    chunk_seq = base_seq + i
                    chunk = sending.chunks.get(chunk_seq)
                    if chunk is not None:
                        chunk.acked = True
                        if chunk.sent_at > 0:
                            rtt = time.time() - chunk.sent_at
                            sending.rtt_samples.append(rtt)
                            if len(sending.rtt_samples) > 20:
                                sending.rtt_samples.pop(0)
            # Обновляем окно (упрощённо)
            if sending.rtt_samples:
                avg_rtt = sum(sending.rtt_samples) / len(sending.rtt_samples)
                if avg_rtt > 0.5:
                    sending.window_size = max(MIN_WINDOW_SIZE, int(sending.window_size * 0.8))
                elif avg_rtt < 0.1 and sending.window_size < MAX_WINDOW_SIZE:
                    sending.window_size = min(MAX_WINDOW_SIZE, sending.window_size + 2)

        self._kick_window(file_id)

    def handle_file_done_packet(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is not None:
                sending.close_file()
                self._outgoing.pop(file_id, None)

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _unique_save_path(self, filename: str) -> str:
        base, ext = os.path.splitext(filename)
        candidate = os.path.join(self._downloads_dir, filename)
        i = 1
        while os.path.exists(candidate):
            candidate = os.path.join(self._downloads_dir, f"{base} ({i}){ext}")
            i += 1
        return candidate

    def _finish_incoming(self, incoming: ReceivingFile) -> None:
        incoming.close_file()

        with self._lock:
            self._incoming.pop(incoming.file_id, None)

        seq = self._next_seq()
        self._send_raw(pack_file_done(seq, incoming.file_id))

        if self._on_file_received is not None:
            self._on_file_received(incoming.file_id, incoming.filename, incoming.save_path)

        if self._on_progress is not None:
            self._on_progress(incoming.file_id, 1.0, False)

    def _send_chunk_ack(self, file_id: int, chunk_seq: int) -> None:
        seq = self._next_seq()
        self._send_raw(pack_file_chunk_ack(seq, file_id, chunk_seq))

    def _send_bitmap_ack_if_needed(self, file_id: int) -> None:
        with self._lock:
            incoming = self._incoming.get(file_id)
            if incoming is None:
                return
            # Если получено 64 чанка или файл завершён
            if len(incoming.received_chunks) % 64 == 0 or incoming.is_complete():
                # Собираем битмап для последних 64 чанков
                base_seq = max(0, max(incoming.received_chunks) - 63)
                bitmap = 0
                for i in range(64):
                    if (base_seq + i) in incoming.received_chunks:
                        bitmap |= (1 << i)
                seq = self._next_seq()
                self._send_raw(pack_file_chunk_ack_bitmap(seq, file_id, base_seq, bitmap))

    def _kick_window(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None or sending.cancelled:
                return

            in_flight = sum(1 for c in sending.chunks.values() if not c.acked and c.attempts > 0)
            free_slots = max(0, sending.window_size - in_flight)
            if free_slots == 0:
                return

            start = sending._next_chunk_to_send
            sent_any = False
            for seq in range(start, sending.total_chunks):
                if free_slots == 0:
                    break
                meta = sending.chunks.get(seq)
                if meta is not None and not meta.acked and meta.attempts == 0:
                    self._send_chunk(sending, meta)
                    free_slots -= 1
                    sent_any = True
                    sending._next_chunk_to_send = seq + 1
                else:
                    continue

    def _send_chunk(self, sending: SendingFile, chunk_meta: OutgoingChunkMeta) -> None:
        offset = chunk_meta.chunk_seq * CHUNK_SIZE
        try:
            sending.file_handle.seek(offset)
            data = sending.file_handle.read(CHUNK_SIZE)
        except Exception as e:
            if self._on_file_error is not None:
                self._on_file_error(sending.file_id, f"Read error: {e}")
            return

        seq = self._next_seq()
        encrypted = self._encrypt(data, seq)
        header = sending.file_id.to_bytes(4, "big") + chunk_meta.chunk_seq.to_bytes(2, "big")
        payload = header + encrypted
        self._send_raw(pack_file_chunk(seq, payload))

        chunk_meta.sent_at = time.time()
        chunk_meta.attempts += 1

    def _pump_loop(self) -> None:
        while self._running:
            time.sleep(0.03)
            now = time.time()

            with self._lock:
                file_ids = list(self._outgoing.keys())

            for file_id in file_ids:
                with self._lock:
                    sending = self._outgoing.get(file_id)
                    if sending is None:
                        continue
                    if sending.cancelled:
                        sending.close_file()
                        self._outgoing.pop(file_id, None)
                        continue

                    timeout = sending.resend_timeout()

                    to_retry = [
                        c for c in sending.chunks.values()
                        if not c.acked and c.attempts > 0
                        and (now - c.sent_at) >= timeout
                        and c.attempts < MAX_CHUNK_ATTEMPTS
                    ]
                    failed = [
                        c for c in sending.chunks.values()
                        if not c.acked and c.attempts >= MAX_CHUNK_ATTEMPTS
                    ]

                if failed:
                    if self._on_file_error is not None:
                        self._on_file_error(file_id, "Some chunks lost, transfer aborted")
                    with self._lock:
                        sending.close_file()
                        self._outgoing.pop(file_id, None)
                    continue

                for chunk_meta in to_retry:
                    self._send_chunk(sending, chunk_meta)

                self._kick_window(file_id)
