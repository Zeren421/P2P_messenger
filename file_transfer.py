"""
Передача файлов чанками с поддержкой миниатюры (thumbnail) в FILE_INFO.

Формат FILE_INFO (бинарный, зашифрованный):
  [4 байта]  file_id
  [2 байта]  длина имени файла (UTF-8)
  [N байт]   имя файла (UTF-8)
  [4 байта]  total_size (uint32)
  [2 байта]  длина подписи (caption, UTF-8)
  [M байт]   подпись (UTF-8, может быть пустой)
  [2 байта]  длина миниатюры (thumbnail)
  [K байт]   миниатюра (JPEG/PNG, сжатая)

Миниатюра генерируется на стороне отправителя и отправляется один раз
вместе с метаданными. Получатель сразу может показать её (с blur-эффектом)
и круговой прогресс, а полное изображение собирается из чанков.
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional
from io import BytesIO

from protocol import pack_file_info, pack_file_chunk, pack_file_chunk_ack, pack_file_done

# ===== НАСТРОЙКИ =====
CHUNK_SIZE = 1024
MAX_FILE_SIZE = 10 * 1024 * 1024   # 10 МБ
WINDOW_SIZE = 24
CHUNK_RESEND_INTERVAL = 0.5
MAX_CHUNK_ATTEMPTS = 8
MAX_CONCURRENT_TRANSFERS = 1
MAX_THUMBNAIL_SIZE = 8 * 1024      # 8 КБ максимум на миниатюру


class FileTransferError(Exception):
    """Ошибка на любом этапе передачи файла."""
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
    """
    Собирает бинарный payload для FILE_INFO.
    Все строки — UTF-8.
    """
    filename_bytes = filename.encode("utf-8")
    caption_bytes = caption.encode("utf-8")

    if len(filename_bytes) > 65535:
        filename_bytes = filename_bytes[:65535]
    if len(caption_bytes) > 65535:
        caption_bytes = caption_bytes[:65535]
    if len(thumbnail) > MAX_THUMBNAIL_SIZE:
        thumbnail = thumbnail[:MAX_THUMBNAIL_SIZE]

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
    """
    Распарсивает бинарный payload FILE_INFO.
    Возвращает: (file_id, filename, total_size, caption, thumbnail)
    """
    if len(data) < 6:
        raise ValueError("FILE_INFO слишком короткий")

    offset = 0
    file_id = int.from_bytes(data[offset:offset+4], "big")
    offset += 4

    if len(data) < offset + 2:
        raise ValueError("Нет данных о длине имени файла")
    name_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    if len(data) < offset + name_len:
        raise ValueError("Имя файла обрезано")
    filename = data[offset:offset+name_len].decode("utf-8", errors="replace")
    offset += name_len

    if len(data) < offset + 4:
        raise ValueError("Нет данных о размере файла")
    total_size = int.from_bytes(data[offset:offset+4], "big")
    offset += 4

    if len(data) < offset + 2:
        raise ValueError("Нет данных о длине подписи")
    caption_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    caption = data[offset:offset+caption_len].decode("utf-8", errors="replace")
    offset += caption_len

    if len(data) < offset + 2:
        raise ValueError("Нет данных о длине миниатюры")
    thumb_len = int.from_bytes(data[offset:offset+2], "big")
    offset += 2
    thumbnail = data[offset:offset+thumb_len]
    offset += thumb_len

    return file_id, filename, total_size, caption, thumbnail


# ============================================================
#  ГЕНЕРАЦИЯ МИНИАТЮРЫ
# ============================================================

def generate_thumbnail_from_file(filepath: str, max_size: int = 200) -> bytes:
    """
    Создаёт миниатюру для изображения (JPEG, качество 70).
    Если не удалось — возвращает пустые байты.
    """
    try:
        from PIL import Image
        img = Image.open(filepath)
        # Конвертируем в RGB, если нужно
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=70, optimize=True)
        data = buf.getvalue()
        if len(data) <= MAX_THUMBNAIL_SIZE:
            return data
        # Если всё равно слишком много — сжимаем сильнее
        img.thumbnail((max_size // 2, max_size // 2), Image.Resampling.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=50)
        return buf.getvalue()
    except Exception:
        return b""


# ============================================================
#  ВСПОМОГАТЕЛЬНЫЕ ДАТАКЛАССЫ
# ============================================================

@dataclass
class OutgoingChunk:
    chunk_seq: int
    data: bytes
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
    chunks: dict = field(default_factory=dict)  # chunk_seq -> OutgoingChunk
    cancelled: bool = False


@dataclass
class ReceivingFile:
    file_id: int
    filename: str
    caption: str
    thumbnail: bytes
    total_size: int
    total_chunks: int
    save_path: str
    received: dict = field(default_factory=dict)  # chunk_seq -> bytes
    started_at: float = field(default_factory=time.time)

    def is_complete(self) -> bool:
        return len(self.received) == self.total_chunks

    def assemble(self) -> bytes:
        return b"".join(self.received[i] for i in range(self.total_chunks))


# ============================================================
#  ОСНОВНОЙ КЛАСС FileTransferManager
# ============================================================

class FileTransferManager:
    """
    Управляет отправкой и приёмом файлов.
    Не знает о сети — только о данных и колбэках.
    """

    def __init__(
        self,
        send_raw: Callable[[bytes], bool],
        encrypt: Callable[[bytes, int], bytes],
        decrypt: Callable[[bytes, int], bytes],
        next_seq: Callable[[], int],
        on_progress: Optional[Callable[[int, float, bool], None]] = None,
        on_file_received: Optional[Callable[[int, str, str], None]] = None,
        on_file_error: Optional[Callable[[int, str], None]] = None,
        on_file_info: Optional[Callable[[int, str, int, bytes, str], None]] = None,  # (file_id, filename, total_size, thumbnail, caption)
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
        self._outgoing: dict[int, SendingFile] = {}
        self._incoming: dict[int, ReceivingFile] = {}
        self._next_file_id = 1

        self._running = False
        self._pump_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Публичный API
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
        with self._lock:
            self._outgoing.clear()
            self._incoming.clear()

    def send_file(
        self,
        filepath: str,
        caption: str = "",
        thumbnail: Optional[bytes] = None,
    ) -> int:
        """
        Отправляет файл с опциональной миниатюрой и подписью.
        Если thumbnail не передан, генерирует его автоматически (для изображений).
        Возвращает file_id.
        """
        if not os.path.isfile(filepath):
            raise FileTransferError(f"Файл не найден: {filepath}")

        total_size = os.path.getsize(filepath)
        if total_size > MAX_FILE_SIZE:
            raise FileTransferError(f"Файл слишком большой: {total_size} байт (лимит {MAX_FILE_SIZE})")
        if total_size == 0:
            raise FileTransferError("Файл пустой")

        # Генерируем миниатюру, если не передана
        if thumbnail is None:
            thumbnail = generate_thumbnail_from_file(filepath)

        with self._lock:
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            if active >= MAX_CONCURRENT_TRANSFERS:
                raise FileTransferError("Уже идёт другая передача файла — дождитесь завершения")
            file_id = self._next_file_id
            self._next_file_id += 1

        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        filename = os.path.basename(filepath)

        sending = SendingFile(
            file_id=file_id,
            filepath=filepath,
            filename=filename,
            caption=caption,
            thumbnail=thumbnail or b"",
            total_size=total_size,
            total_chunks=total_chunks,
        )

        # Читаем файл по чанкам
        with open(filepath, "rb") as f:
            for chunk_seq in range(total_chunks):
                data = f.read(CHUNK_SIZE)
                sending.chunks[chunk_seq] = OutgoingChunk(chunk_seq=chunk_seq, data=data)

        with self._lock:
            self._outgoing[file_id] = sending

        # Отправляем FILE_INFO с метаданными и миниатюрой
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

        # Запускаем окно
        self._kick_window(file_id)
        return file_id

    def cancel_send(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is not None:
                sending.cancelled = True

    # ------------------------------------------------------------------
    # Обработка входящих пакетов (вызывается из ConnectionManager)
    # ------------------------------------------------------------------

    def handle_file_info_packet(self, seq: int, payload: bytes) -> None:
        """Обрабатывает FILE_INFO с миниатюрой и метаданными."""
        try:
            decrypted = self._decrypt(payload, seq)
            file_id, filename, total_size, caption, thumbnail = parse_file_info_payload(decrypted)
        except Exception as e:
            if self._on_file_error is not None:
                self._on_file_error(0, f"Не удалось разобрать FILE_INFO: {e}")
            return

        if total_size > MAX_FILE_SIZE:
            if self._on_file_error is not None:
                self._on_file_error(file_id, f"Файл превышает лимит {MAX_FILE_SIZE} байт — отклонён")
            return

        safe_name = os.path.basename(filename) or f"file_{file_id}"
        save_path = self._unique_save_path(safe_name)

        with self._lock:
            self._incoming[file_id] = ReceivingFile(
                file_id=file_id,
                filename=safe_name,
                caption=caption,
                thumbnail=thumbnail,
                total_size=total_size,
                total_chunks=(total_size + CHUNK_SIZE - 1) // CHUNK_SIZE,
                save_path=save_path,
            )

        # ===== УВЕДОМЛЯЕМ GUI О МИНИАТЮРЕ СРАЗУ =====
        if self._on_file_info is not None:
            try:
                self._on_file_info(file_id, safe_name, total_size, thumbnail, caption)
            except Exception as e:
                print(f"[FT] on_file_info callback error: {e}")

    def handle_file_chunk_packet(self, seq: int, file_id: int, chunk_seq: int, encrypted_data: bytes) -> None:
        with self._lock:
            incoming = self._incoming.get(file_id)

        if incoming is None:
            # FILE_INFO ещё не пришёл — ACK всё равно шлём, чтобы отправитель не повторял
            self._send_chunk_ack(file_id, chunk_seq)
            return

        if chunk_seq in incoming.received:
            self._send_chunk_ack(file_id, chunk_seq)
            return

        try:
            data = self._decrypt(encrypted_data, seq)
        except Exception:
            # Повреждённый чанк — не подтверждаем, отправитель повторит
            return

        incoming.received[chunk_seq] = data
        self._send_chunk_ack(file_id, chunk_seq)

        if self._on_progress is not None:
            ratio = len(incoming.received) / incoming.total_chunks
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
            acked = sum(1 for c in sending.chunks.values() if c.acked)
            total = sending.total_chunks

        if self._on_progress is not None:
            self._on_progress(file_id, acked / total, True)

        self._kick_window(file_id)

    def handle_file_done_packet(self, file_id: int) -> None:
        with self._lock:
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
        data = incoming.assemble()
        try:
            with open(incoming.save_path, "wb") as f:
                f.write(data)
        except OSError as e:
            if self._on_file_error is not None:
                self._on_file_error(incoming.file_id, f"Не удалось сохранить файл: {e}")
            return

        with self._lock:
            self._incoming.pop(incoming.file_id, None)

        seq = self._next_seq()
        self._send_raw(pack_file_done(seq, incoming.file_id))

        if self._on_file_received is not None:
            self._on_file_received(incoming.file_id, incoming.filename, incoming.save_path)

    def _send_chunk_ack(self, file_id: int, chunk_seq: int) -> None:
        seq = self._next_seq()
        self._send_raw(pack_file_chunk_ack(seq, file_id, chunk_seq))

    def _kick_window(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None or sending.cancelled:
                return

            in_flight = sum(1 for c in sending.chunks.values() if not c.acked and c.attempts > 0)
            free_slots = max(0, WINDOW_SIZE - in_flight)
            if free_slots == 0:
                return

            to_send = [
                c for c in sorted(sending.chunks.values(), key=lambda c: c.chunk_seq)
                if not c.acked and c.attempts == 0
            ][:free_slots]

        for chunk in to_send:
            self._send_chunk(sending, chunk)

    def _send_chunk(self, sending: SendingFile, chunk: OutgoingChunk) -> None:
        seq = self._next_seq()
        encrypted = self._encrypt(chunk.data, seq)
        header = sending.file_id.to_bytes(4, "big") + chunk.chunk_seq.to_bytes(2, "big")
        payload = header + encrypted
        self._send_raw(pack_file_chunk(seq, payload))

        chunk.sent_at = time.time()
        chunk.attempts += 1

    def _pump_loop(self) -> None:
        """Периодическая отправка чанков и повторы."""
        while self._running:
            time.sleep(CHUNK_RESEND_INTERVAL)
            now = time.time()

            with self._lock:
                file_ids = list(self._outgoing.keys())

            for file_id in file_ids:
                with self._lock:
                    sending = self._outgoing.get(file_id)
                    if sending is None:
                        continue
                    if sending.cancelled:
                        self._outgoing.pop(file_id, None)
                        continue

                    to_retry = [
                        c for c in sending.chunks.values()
                        if not c.acked and c.attempts > 0
                        and (now - c.sent_at) >= CHUNK_RESEND_INTERVAL
                        and c.attempts < MAX_CHUNK_ATTEMPTS
                    ]
                    failed = [
                        c for c in sending.chunks.values()
                        if not c.acked and c.attempts >= MAX_CHUNK_ATTEMPTS
                    ]

                if failed:
                    if self._on_file_error is not None:
                        self._on_file_error(file_id, "Часть чанков не удалось доставить — передача прервана")
                    with self._lock:
                        self._outgoing.pop(file_id, None)
                    continue

                for chunk in to_retry:
                    self._send_chunk(sending, chunk)

                self._kick_window(file_id)
