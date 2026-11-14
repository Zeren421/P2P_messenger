"""
helpers/voice_recorder.py — запись с микрофона в WAV (через sounddevice).
С поддержкой авто-выбора Bluetooth HFP (Hands-Free) и усилением (Gain).
"""

from __future__ import annotations

import os
import tempfile
import threading
import wave
from datetime import datetime

import numpy as np
import sounddevice as sd


def find_voice_device():
    """
    Универсальный выбор микрофона:
    1. Если устройство по умолчанию — Bluetooth (A2DP), ищем его HFP-версию.
    2. Иначе — устройство по умолчанию Windows.
    3. Если у дефолтного нет микрофона — первое WASAPI input.
    Возвращает (device_index, samplerate).
    """
    hostapis = sd.query_hostapis()
    devices = sd.query_devices()
    
    # 1. Устройство по умолчанию для ввода
    default_in = sd.default.device[0]
    default_device = None
    if default_in is not None and default_in >= 0:
        default_device = devices[default_in]
    
    # 2. Если дефолт — Bluetooth A2DP (нет микрофона) → ищем HFP
    if default_device is not None and default_device['max_input_channels'] == 0:
        # Извлекаем имя модели (например, "soundcore Q20i")
        default_name = default_device['name'].lower()
        # Ищем устройство с "hands-free" или "headset" в имени
        for d in devices:
            if d['max_input_channels'] < 1:
                continue
            name = d['name'].lower()
            if 'hands-free' in name or 'headset' in name:
                # Проверяем, что это та же модель (по первому слову модели)
                # Например, "soundcore q20i" в обоих именах
                model = default_name.split('(')[-1].split(')')[0].strip() if '(' in default_name else default_name
                if model and model in name:
                    print(f"[VoiceRecorder] Bluetooth A2DP → HFP: {d['name']} (idx {d['index']})")
                    return d['index'], int(d['default_samplerate'])
    
    # 3. Если у дефолта есть микрофон — используем его
    if default_device is not None and default_device['max_input_channels'] >= 1:
        print(f"[VoiceRecorder] Устройство по умолчанию: {default_device['name']} (idx {default_in})")
        return default_in, int(default_device['default_samplerate'])
    
    # 4. Fallback: первое WASAPI input
    wasapi_idx = next((i for i, h in enumerate(hostapis) if 'WASAPI' in h['name']), None)
    if wasapi_idx is not None:
        for d in devices:
            if d['hostapi'] == wasapi_idx and d['max_input_channels'] >= 1:
                print(f"[VoiceRecorder] Fallback WASAPI: {d['name']} (idx {d['index']})")
                return d['index'], int(d['default_samplerate'])
    
    # 5. Совсем fallback — дефолт
    print("[VoiceRecorder] Используется дефолт (может быть A2DP)")
    return None, 44100

class VoiceRecorder:
    """Записывает с микрофона в WAV-файл. Кроссплатформенный (sounddevice)."""

    RATE = 16000          # HFP часто работает на 16 кГц (телефонное качество)
    CHANNELS = 1          # моно
    MAX_DURATION = 300    # максимум 5 минут
    GAIN = 3.0            # Усиление для тихих Bluetooth-микрофонов

    def __init__(self) -> None:
        self._frames: list[np.ndarray] = []
        self._recording = False
        self._thread: threading.Thread | None = None
        self._device = find_voice_device()

    def start(self) -> bool:
        if self._recording:
            return False
        try:
            self._frames.clear()
            self._recording = True
            self._thread = threading.Thread(target=self._record_loop, daemon=True)
            self._thread.start()
            return True
        except Exception as e:
            print(f"[VoiceRecorder] start error: {e}")
            self._recording = False
            return False

    def _record_loop(self) -> None:
        def callback(indata, frames, time_info, status):
            if status:
                print(f"[VoiceRecorder] status: {status}")
            if self._recording:
                self._frames.append(indata.copy())

        try:
            with sd.InputStream(
                samplerate=self.RATE,
                channels=self.CHANNELS,
                callback=callback,
                dtype="float32",        # float32 для правильного усиления
                device=self._device,
                blocksize=1024,
                latency="low",
            ):
                import time
                start = time.time()
                while self._recording:
                    time.sleep(0.05)
                    if time.time() - start > self.MAX_DURATION:
                        self._recording = False
                        break
        except Exception as e:
            print(f"[VoiceRecorder] stream error: {e}")
            self._recording = False

    def stop(self) -> str:
        self._recording = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if not self._frames:
            return ""
            
        # Склеиваем и усиливаем
        audio = np.concatenate(self._frames, axis=0)
        audio = np.clip(audio * self.GAIN, -1.0, 1.0)  # Усиление + защита от клиппинга
        audio_int16 = (audio * 32767).astype(np.int16)
        
        path = os.path.join(
            tempfile.gettempdir(),
            f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}.wav",
        )
        with wave.open(path, "wb") as wf:
            wf.setnchannels(self.CHANNELS)
            wf.setsampwidth(2)       # int16 = 2 байта
            wf.setframerate(self.RATE)
            wf.writeframes(audio_int16.tobytes())
            
        self._last_path = path
        self._frames.clear()
        return path

    def cancel(self) -> None:
        self._recording = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._frames.clear()