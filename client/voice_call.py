"""
voice_call.py — минимальный голосовой канал на сыром PCM (без Opus).
Push-to-talk: нажал — говоришь, отпустил — слушаешь.
"""
import threading
import time
import queue
from typing import Callable, Optional

import sounddevice as sd
import numpy as np

RATE = 16000          # 16 кГц — стандарт для голоса
CHANNELS = 1
FRAME_MS = 20         # 20 мс кадры
FRAME_SIZE = RATE // 1000 * FRAME_MS  # 320 сэмплов


class VoiceCallSession:
    def __init__(
        self,
        send_raw: Callable[[bytes], bool],
        next_seq: Callable[[], int],
        on_call_ended: Optional[Callable[[], None]] = None,
    ):
        self._send_raw = send_raw
        self._next_seq = next_seq
        self._on_call_ended = on_call_ended

        self.call_id = int(time.time()) & 0xFFFFFFFF
        self._running = False
        self._recording = False
        self._input_stream = None
        self._output_stream = None
        self._audio_seq = 0
        self._play_queue = queue.Queue(maxsize=50)

    def start(self) -> bool:
        """Открывает динамики для воспроизведения. Микрофон — по требованию."""
        self._running = True
        try:
            self._output_stream = sd.OutputStream(
                samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=FRAME_SIZE, callback=self._play_callback
            )
            self._output_stream.start()
            return True
        except Exception as e:
            print(f"[Voice] Output stream failed: {e}")
            self._running = False
            return False

    def start_talking(self):
        """Начать передачу голоса (нажали кнопку)."""
        if self._recording or not self._running:
            return
        self._recording = True
        self._audio_seq = 0
        try:
            self._input_stream = sd.InputStream(
                samplerate=RATE, channels=CHANNELS, dtype="int16",
                blocksize=FRAME_SIZE, callback=self._record_callback
            )
            self._input_stream.start()
        except Exception as e:
            print(f"[Voice] Input stream failed: {e}")
            self._recording = False

    def stop_talking(self):
        """Остановить передачу голоса (отпустили кнопку)."""
        self._recording = False
        if self._input_stream is not None:
            try:
                self._input_stream.stop()
                self._input_stream.close()
            except Exception:
                pass
            self._input_stream = None

    def stop(self):
        """Завершить звонок."""
        self._running = False
        self.stop_talking()
        if self._output_stream is not None:
            try:
                self._output_stream.stop()
                self._output_stream.close()
            except Exception:
                pass
            self._output_stream = None
        if self._on_call_ended:
            self._on_call_ended()

    def _record_callback(self, indata, frames, time_info, status):
        if not self._recording:
            return
        try:
            pcm = indata.tobytes()   # int16 → bytes
            from protocol import pack_voice_data
            seq = self._next_seq()
            packet = pack_voice_data(seq, self.call_id, self._audio_seq, pcm)
            self._send_raw(packet)
            self._audio_seq += 1
        except Exception as e:
            print(f"[Voice] Record error: {e}")

    def _play_callback(self, outdata, frames, time_info, status):
        try:
            pcm = self._play_queue.get_nowait()
            arr = np.frombuffer(pcm, dtype=np.int16)
            if len(arr) < FRAME_SIZE:
                arr = np.pad(arr, (0, FRAME_SIZE - len(arr)))
            elif len(arr) > FRAME_SIZE:
                arr = arr[:FRAME_SIZE]
            outdata[:] = arr.reshape(-1, 1)
        except queue.Empty:
            outdata.fill(0)

    def on_voice_data(self, pcm_data: bytes):
        """Вызывается при получении VOICE_DATA."""
        try:
            self._play_queue.put_nowait(pcm_data)
        except queue.Full:
            # Дропаем старый кадр, чтобы не копился лаг
            try:
                self._play_queue.get_nowait()
                self._play_queue.put_nowait(pcm_data)
            except Exception:
                pass