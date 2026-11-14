"""
FileTransferManager v2 — с очередью передач, очищенной логикой ACK
и разделением папок для файлов и голосовых.
"""

from __future__ import annotations

import os
import time
import threading
from dataclasses import dataclass, field
from typing import Callable, Optional, Dict, Set, List
from io import BytesIO

from protocol import (
    pack_file_chunk, pack_file_chunk_ack, pack_file_done,
    pack_file_cancel, PacketType,
)

# ============================================================
# Тюнинг
# ============================================================

CHUNK_SIZE = 1200
INITIAL_WINDOW_BYTES = 64 * 1024
MAX_WINDOW_BYTES     = 16 * 1024 * 1024
MIN_WINDOW_BYTES     = 64 * 1024

RTO_INITIAL = 0.5
RTO_MIN = 0.1
RTO_MAX = 5.0

FAST_RETRANSMIT_THRESHOLD = 3
MAX_CHUNK_ATTEMPTS = 1000
MAX_CONCURRENT_TRANSFERS = 3

PROGRESS_INTERVAL = 0.1

class FileTransferError(Exception):
    pass

# ============================================================
# Бинарный FILE_INFO
# ============================================================

def build_file_info_payload(file_id, filename, total_size, caption, thumbnail) -> bytes:
    filename_bytes = filename.encode("utf-8")[:65535]
    caption_bytes = caption.encode("utf-8")[:65535]
    if len(thumbnail) > 8000:
        thumbnail = thumbnail[:8000]
    return (
        file_id.to_bytes(4, "big")
        + len(filename_bytes).to_bytes(2, "big") + filename_bytes
        + total_size.to_bytes(8, "big")
        + len(caption_bytes).to_bytes(2, "big") + caption_bytes
        + len(thumbnail).to_bytes(2, "big") + thumbnail
    )

def parse_file_info_payload(data: bytes):
    o = 0
    file_id = int.from_bytes(data[o:o+4], "big"); o += 4
    nl = int.from_bytes(data[o:o+2], "big"); o += 2
    filename = data[o:o+nl].decode("utf-8", "replace"); o += nl
    total_size = int.from_bytes(data[o:o+8], "big"); o += 8
    cl = int.from_bytes(data[o:o+2], "big"); o += 2
    caption = data[o:o+cl].decode("utf-8", "replace"); o += cl
    tl = int.from_bytes(data[o:o+2], "big"); o += 2
    thumbnail = data[o:o+tl]
    return file_id, filename, total_size, caption, thumbnail

def generate_thumbnail_from_file(filepath: str, max_size: int = 200) -> bytes:
    try:
        from PIL import Image
        img = Image.open(filepath)
        return _thumb_from_image(img, max_size)
    except Exception:
        return b""

def generate_thumbnail_from_bytes(data: bytes, max_size: int = 200) -> bytes:
    try:
        from PIL import Image
        img = Image.open(BytesIO(data))
        return _thumb_from_image(img, max_size)
    except Exception:
        return b""

def _thumb_from_image(img, max_size):
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

# ============================================================
# Состояние
# ============================================================

@dataclass
class OutgoingChunk:
    chunk_seq: int
    first_sent_at: float
    last_sent_at: float
    attempts: int = 1
    retransmitted: bool = False

@dataclass
class SendingFile:
    file_id: int
    filename: str
    caption: str
    thumbnail: bytes
    total_size: int
    total_chunks: int
    file_handle: Optional[object] = None
    memory_data: Optional[bytes] = None

    window_bytes: int = INITIAL_WINDOW_BYTES
    ssthresh: int = MAX_WINDOW_BYTES
    in_slow_start: bool = True
    next_chunk: int = 0
    highest_acked: int = -1
    inflight: Dict[int, OutgoingChunk] = field(default_factory=dict)
    dup_ack_count: int = 0
    srtt: Optional[float] = None
    rttvar: float = 0.0
    cancelled: bool = False
    file_info_acked: bool = False
    done_sent: bool = False
    last_progress_emit: float = 0.0

    def total_acked_bytes(self) -> int:
        b = (self.highest_acked + 1) * CHUNK_SIZE
        return min(b, self.total_size)

    def close_file(self) -> None:
        if self.file_handle is not None:
            try: self.file_handle.close()
            except: pass
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
    file_handle: object

    next_write_chunk: int = 0
    pending_chunks: Dict[int, bytes] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    done_sent: bool = False
    last_progress_emit: float = 0.0

    def is_complete(self) -> bool:
        return self.next_write_chunk >= self.total_chunks

    def write_chunk(self, chunk_seq: int, data: bytes) -> None:
        if self.file_handle is None: return
        if chunk_seq < self.next_write_chunk: return
        self.pending_chunks[chunk_seq] = data
        while self.next_write_chunk in self.pending_chunks:
            next_data = self.pending_chunks.pop(self.next_write_chunk)
            self.file_handle.write(next_data)
            self.next_write_chunk += 1

    def close_file(self) -> None:
        self.pending_chunks.clear()
        if self.file_handle is not None:
            try: self.file_handle.close()
            except: pass
            self.file_handle = None

# ============================================================
# Менеджер
# ============================================================

class FileTransferManager:
    def __init__(
        self,
        send_raw: Callable[[bytes], bool],
        encrypt: Callable[[bytes, int], bytes],
        decrypt: Callable[[bytes, int], bytes],
        next_seq: Callable[[], int],
        send_reliable_file_info: Optional[Callable] = None,
        on_progress: Optional[Callable[[int, float, bool], None]] = None,
        on_file_received: Optional[Callable[[int, str, str], None]] = None,
        on_file_error: Optional[Callable[[int, str], None]] = None,
        on_file_info: Optional[Callable[[int, str, int, bytes, str], None]] = None,
        on_file_cancelled: Optional[Callable[[int, bool], None]] = None,
        on_file_queued: Optional[Callable[[int, str], None]] = None,
        downloads_dir: str = ".",
        voice_dir: Optional[str] = None,   # <--- НОВОЕ
    ) -> None:
        self._send_raw = send_raw
        self._encrypt = encrypt
        self._decrypt = decrypt
        self._next_seq = next_seq
        self._send_reliable_file_info = send_reliable_file_info
        self._on_progress = on_progress
        self._on_file_received = on_file_received
        self._on_file_error = on_file_error
        self._on_file_info = on_file_info
        self._on_file_cancelled = on_file_cancelled
        self._on_file_queued = on_file_queued
        self._downloads_dir = downloads_dir
        self._voice_dir = voice_dir or downloads_dir   # <--- НОВОЕ

        self._lock = threading.Lock()
        self._outgoing: Dict[int, SendingFile] = {}
        self._incoming: Dict[int, ReceivingFile] = {}
        self._queued_transfers: List[SendingFile] = []
        self._next_file_id = 1

        self._running = False
        self._pump_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._running = True
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True)
        self._pump_thread.start()

    def stop(self) -> None:
        self._running = False
        if self._pump_thread is not None:
            self._pump_thread.join(timeout=1.0)
        with self._lock:
            for f in self._outgoing.values(): f.close_file()
            for f in self._incoming.values(): f.close_file()
            for f in self._queued_transfers: f.close_file()
            self._outgoing.clear()
            self._incoming.clear()
            self._queued_transfers.clear()

    def send_file(self, filepath: str, caption: str = "", thumbnail: Optional[bytes] = None) -> int:
        if not os.path.isfile(filepath):
            raise FileTransferError(f"File not found: {filepath}")
        total_size = os.path.getsize(filepath)
        if total_size == 0:
            raise FileTransferError("File is empty")
        if total_size > 50 * 1024 * 1024 * 1024:
            raise FileTransferError("File too large (max 50 GB)")

        if thumbnail is None:
            thumbnail = generate_thumbnail_from_file(filepath)

        with self._lock:
            file_id = self._next_file_id
            self._next_file_id += 1
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            must_queue = active >= MAX_CONCURRENT_TRANSFERS

        try:
            fh = open(filepath, "rb")
        except OSError as e:
            raise FileTransferError(f"Failed to open file: {e}")

        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        sending = SendingFile(
            file_id=file_id, filename=os.path.basename(filepath),
            caption=caption, thumbnail=thumbnail or b"",
            total_size=total_size, total_chunks=total_chunks, file_handle=fh,
        )
        
        if must_queue:
            with self._lock:
                self._queued_transfers.append(sending)
            if self._on_file_queued:
                try: self._on_file_queued(sending.file_id, "В очереди (начнётся после завершения текущих)")
                except Exception: pass
        else:
            self._start_sending(sending)
        return file_id

    def send_memory_file(self, data: bytes, filename: str, caption: str = "", thumbnail: Optional[bytes] = None) -> int:
        total_size = len(data)
        if total_size == 0:
            raise FileTransferError("Data is empty")
        if total_size > 50 * 1024 * 1024 * 1024:
            raise FileTransferError("File too large (max 50 GB)")

        if thumbnail is None:
            thumbnail = generate_thumbnail_from_bytes(data)

        with self._lock:
            file_id = self._next_file_id
            self._next_file_id += 1
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            must_queue = active >= MAX_CONCURRENT_TRANSFERS

        total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
        sending = SendingFile(
            file_id=file_id, filename=filename,
            caption=caption, thumbnail=thumbnail or b"",
            total_size=total_size, total_chunks=total_chunks, 
            file_handle=BytesIO(data), memory_data=data,
        )

        if must_queue:
            with self._lock:
                self._queued_transfers.append(sending)
            if self._on_file_queued:
                try: self._on_file_queued(sending.file_id, "В очереди (начнётся после завершения текущих)")
                except Exception: pass
        else:
            self._start_sending(sending)
        return file_id

    def _start_sending(self, sending: SendingFile) -> None:
        with self._lock:
            self._outgoing[sending.file_id] = sending

        info = build_file_info_payload(
            sending.file_id, sending.filename, sending.total_size,
            sending.caption, sending.thumbnail,
        )

        if self._send_reliable_file_info is not None:
            self._send_reliable_file_info(
                sending.file_id, info,
                lambda: self._on_file_info_acked(sending.file_id),
                lambda: self._on_file_info_failed(sending.file_id),
            )
        else:
            from protocol import pack_file_info
            for _ in range(3):
                seq = self._next_seq()
                enc = self._encrypt(info, seq)
                self._send_raw(pack_file_info(seq, enc))
            self._on_file_info_acked(sending.file_id)

    def _try_start_next_queued(self) -> None:
        with self._lock:
            active = sum(1 for f in self._outgoing.values() if not f.cancelled)
            if active >= MAX_CONCURRENT_TRANSFERS or not self._queued_transfers:
                return
            sending = self._queued_transfers.pop(0)
        self._start_sending(sending)

    def _on_file_info_acked(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None: return
            sending.file_info_acked = True
        self._kick_window(file_id)

    def _on_file_info_failed(self, file_id: int) -> None:
        self._abort_send(file_id)
        if self._on_file_error:
            try: self._on_file_error(file_id, "FILE_INFO delivery failed")
            except Exception: pass

    def cancel_send(self, file_id: int) -> None:
        if self._abort_send(file_id):
            self._send_raw(pack_file_cancel(self._next_seq(), file_id, by_sender=True))

    def cancel_receive(self, file_id: int) -> None:
        if self._abort_receive(file_id):
            self._send_raw(pack_file_cancel(self._next_seq(), file_id, by_sender=False))

    def abort_send(self, file_id: int) -> None: self._abort_send(file_id)
    def abort_receive(self, file_id: int) -> None: self._abort_receive(file_id)

    def _abort_send(self, file_id: int) -> bool:
        with self._lock:
            s = self._outgoing.pop(file_id, None)
            if s is None:
                for i, qs in enumerate(self._queued_transfers):
                    if qs.file_id == file_id:
                        s = self._queued_transfers.pop(i)
                        break
        if s is None: return False
        s.cancelled = True
        s.close_file()
        if self._on_file_cancelled:
            try: self._on_file_cancelled(file_id, True)
            except Exception: pass
        self._try_start_next_queued()
        return True

    def _abort_receive(self, file_id: int) -> bool:
        with self._lock:
            r = self._incoming.pop(file_id, None)
        if r is None: return False
        r.close_file()
        try: os.remove(r.save_path)
        except OSError: pass
        if self._on_file_cancelled:
            try: self._on_file_cancelled(file_id, False)
            except Exception: pass
        return True

    def _maybe_emit_progress(self, file_id: int, ratio: float, is_outgoing: bool, state_obj) -> None:
        if self._on_progress is None: return
        now = time.time()
        if ratio < 1.0 and now - state_obj.last_progress_emit < PROGRESS_INTERVAL: return
        state_obj.last_progress_emit = now
        try: self._on_progress(file_id, ratio, is_outgoing)
        except Exception: pass

    def handle_file_info_packet(self, seq: int, payload: bytes) -> None:
        try:
            decrypted = self._decrypt(payload, seq)
            file_id, filename, total_size, caption, thumbnail = parse_file_info_payload(decrypted)
        except Exception as e:
            if self._on_file_error:
                try: self._on_file_error(0, f"Failed to parse FILE_INFO: {e}")
                except Exception: pass
            return

        if total_size > 50 * 1024 * 1024 * 1024:
            if self._on_file_error:
                try: self._on_file_error(file_id, "File too large (max 50 GB)")
                except Exception: pass
            return

        with self._lock:
            if file_id in self._incoming: return

        safe_name = os.path.basename(filename) or f"file_{file_id}"
        
        # Голосовые — в voice/, остальное — в downloads/
        is_voice = "_voice" in safe_name and safe_name.endswith(".wav")
        target_dir = self._voice_dir if is_voice else self._downloads_dir
        save_path = self._unique_save_path(safe_name, target_dir)
        
        try:
            fh = open(save_path, "w+b")
        except OSError as e:
            if self._on_file_error:
                try: self._on_file_error(file_id, f"Failed to create file: {e}")
                except Exception: pass
            return

        incoming = ReceivingFile(
            file_id=file_id, filename=safe_name, caption=caption,
            thumbnail=thumbnail, total_size=total_size,
            total_chunks=(total_size + CHUNK_SIZE - 1) // CHUNK_SIZE,
            save_path=save_path, file_handle=fh,
        )
        with self._lock:
            self._incoming[file_id] = incoming

        if self._on_file_info:
            try: self._on_file_info(file_id, safe_name, total_size, thumbnail, caption)
            except Exception as e: print(f"[FT] on_file_info error: {e}")

    def handle_file_chunk_packet(self, seq: int, file_id: int, chunk_seq: int, encrypted_data: bytes) -> None:
        with self._lock:
            incoming = self._incoming.get(file_id)

        if incoming is None: return
        if chunk_seq < incoming.next_write_chunk or chunk_seq in incoming.pending_chunks:
            self._send_ack(incoming)
            return

        try:
            data = self._decrypt(encrypted_data, seq)
        except Exception:
            return

        try:
            incoming.write_chunk(chunk_seq, data)
        except Exception as e:
            if self._on_file_error:
                try: self._on_file_error(file_id, f"Write error: {e}")
                except Exception: pass
            return

        self._send_ack(incoming)

        if not incoming.is_complete():
            got = min(incoming.next_write_chunk, incoming.total_chunks) * CHUNK_SIZE
            got = min(got, incoming.total_size)
            ratio = got / incoming.total_size
            self._maybe_emit_progress(file_id, ratio, False, incoming)

        if incoming.is_complete():
            self._finish_incoming(incoming)

    def handle_file_chunk_ack_packet(self, file_id: int, cumulative_ack: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None or sending.cancelled: return

            now = time.time()
            if cumulative_ack > sending.highest_acked:
                sending.highest_acked = cumulative_ack
                chunk = sending.inflight.get(cumulative_ack)
                if chunk and not chunk.retransmitted:
                    sample = now - chunk.first_sent_at
                    if sample > 0:
                        self._update_rtt(sending, sample)

                for s in [k for k in sending.inflight if k <= cumulative_ack]:
                    del sending.inflight[s]

                sending.dup_ack_count = 0
                if sending.in_slow_start:
                    sending.window_bytes += CHUNK_SIZE
                    if sending.window_bytes >= sending.ssthresh:
                        sending.in_slow_start = False
                else:
                    sending.window_bytes += max(1, (CHUNK_SIZE * CHUNK_SIZE) // sending.window_bytes)
                sending.window_bytes = min(sending.window_bytes, MAX_WINDOW_BYTES)
            else:
                sending.dup_ack_count += 1
                if sending.dup_ack_count == FAST_RETRANSMIT_THRESHOLD:
                    nxt = cumulative_ack + 1
                    chunk = sending.inflight.get(nxt)
                    if chunk and not chunk.retransmitted:
                        chunk.retransmitted = True
                        chunk.attempts += 1
                        chunk.last_sent_at = now
                        self._send_chunk_data(sending, nxt, chunk)
                        sending.window_bytes = max(MIN_WINDOW_BYTES, sending.window_bytes // 2)
                        sending.ssthresh = sending.window_bytes
                        sending.in_slow_start = False

        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending:
                if sending.highest_acked < sending.total_chunks - 1:
                    ratio = sending.total_acked_bytes() / sending.total_size
                    self._maybe_emit_progress(file_id, ratio, True, sending)

                if not sending.done_sent and sending.highest_acked >= sending.total_chunks - 1:
                    sending.done_sent = True
                    self._send_raw(pack_file_done(self._next_seq(), file_id))
                    if self._on_progress:
                        try: self._on_progress(file_id, 1.0, True)
                        except Exception: pass

        self._kick_window(file_id)

    def handle_file_done_packet(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is not None:
                sending.close_file()
                self._outgoing.pop(file_id, None)
        self._try_start_next_queued()

    @staticmethod
    def _update_rtt(sending: SendingFile, sample: float) -> None:
        if sending.srtt is None:
            sending.srtt = sample
            sending.rttvar = sample / 2
        else:
            sending.rttvar = 0.75 * sending.rttvar + 0.25 * abs(sending.srtt - sample)
            sending.srtt = 0.875 * sending.srtt + 0.125 * sample

    @staticmethod
    def _current_rto(sending: SendingFile) -> float:
        if sending.srtt is None: return RTO_INITIAL
        rto = sending.srtt + 4.0 * sending.rttvar
        return max(RTO_MIN, min(RTO_MAX, rto))

    def _unique_save_path(self, filename: str, target_dir: Optional[str] = None) -> str:
        base, ext = os.path.splitext(filename)
        folder = target_dir or self._downloads_dir
        cand = os.path.join(folder, filename)
        i = 1
        while os.path.exists(cand):
            cand = os.path.join(folder, f"{base} ({i}){ext}")
            i += 1
        return cand

    def _finish_incoming(self, incoming: ReceivingFile) -> None:
        if incoming.done_sent: return
        incoming.done_sent = True
        incoming.close_file()
        with self._lock:
            self._incoming.pop(incoming.file_id, None)

        self._send_raw(pack_file_done(self._next_seq(), incoming.file_id))
        if self._on_file_received:
            try: self._on_file_received(incoming.file_id, incoming.filename, incoming.save_path)
            except Exception: pass
        if self._on_progress:
            try: self._on_progress(incoming.file_id, 1.0, False)
            except Exception: pass

    def _send_ack(self, incoming: ReceivingFile) -> None:
        if incoming.next_write_chunk == 0:
            return
        seq = self._next_seq()
        val = incoming.next_write_chunk - 1
        self._send_raw(pack_file_chunk_ack(seq, incoming.file_id, val))

    def _send_chunk_data(self, sending: SendingFile, chunk_seq: int, meta: OutgoingChunk) -> bool:
        offset = chunk_seq * CHUNK_SIZE
        try:
            sending.file_handle.seek(offset)
            data = sending.file_handle.read(CHUNK_SIZE)
        except Exception as e:
            if self._on_file_error:
                try: self._on_file_error(sending.file_id, f"Read error: {e}")
                except Exception: pass
            return False

        seq = self._next_seq()
        enc = self._encrypt(data, seq)
        payload = sending.file_id.to_bytes(4, "big") + chunk_seq.to_bytes(4, "big") + enc
        ok = self._send_raw(pack_file_chunk(seq, payload))
        if ok:
            now = time.time()
            if meta.first_sent_at == 0: meta.first_sent_at = now
            meta.last_sent_at = now
        return ok

    def _kick_window(self, file_id: int) -> None:
        with self._lock:
            sending = self._outgoing.get(file_id)
            if sending is None or sending.cancelled or not sending.file_info_acked: return

            inflight_bytes = len(sending.inflight) * CHUNK_SIZE
            sent_now = 0

            while (inflight_bytes + CHUNK_SIZE <= sending.window_bytes
                   and sending.next_chunk < sending.total_chunks
                   and sent_now < 200):
                cs = sending.next_chunk
                meta = OutgoingChunk(chunk_seq=cs, first_sent_at=0, last_sent_at=0)
                if not self._send_chunk_data(sending, cs, meta): break
                sending.inflight[cs] = meta
                sending.next_chunk += 1
                inflight_bytes += CHUNK_SIZE
                sent_now += 1

    def _pump_loop(self) -> None:
        while self._running:
            time.sleep(0.01)
            now = time.time()
            with self._lock:
                file_ids = list(self._outgoing.keys())

            for file_id in file_ids:
                freed_slot = False
                with self._lock:
                    sending = self._outgoing.get(file_id)
                    if sending is None or sending.cancelled or not sending.file_info_acked: 
                        continue
                        
                    rto = self._current_rto(sending)

                    for meta in list(sending.inflight.values()):
                        if now - meta.last_sent_at < rto: 
                            continue
                            
                        if meta.attempts >= MAX_CHUNK_ATTEMPTS:
                            self._outgoing.pop(file_id, None)
                            sending.close_file()
                            if self._on_file_error:
                                try: self._on_file_error(file_id, "Chunk delivery failed (max attempts)")
                                except Exception: pass
                            freed_slot = True
                            break
                            
                        meta.attempts += 1
                        meta.retransmitted = True
                        self._send_chunk_data(sending, meta.chunk_seq, meta)
                        sending.window_bytes = max(MIN_WINDOW_BYTES, sending.window_bytes // 2)
                        sending.ssthresh = sending.window_bytes
                        sending.in_slow_start = False
                
                if freed_slot:
                    self._try_start_next_queued()
                    
                self._kick_window(file_id)