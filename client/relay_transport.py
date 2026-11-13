"""
RelayTransport — исправленная версия
"""

from __future__ import annotations

import base64
import json
import os
import ssl
import threading
import time
import urllib.request
from typing import Optional

from transport import ITransport

def _ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()

def _get_ntfy_server() -> str:
    return os.getenv("NTFY_SERVER", "https://ntfy.sh")

class RelayTransport(ITransport):
    def __init__(self, my_topic: str, peer_topic: str) -> None:
        super().__init__()
        self._my_topic = my_topic
        self._peer_topic = peer_topic
        self._running = False
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None
        self._server = _get_ntfy_server()

    def start(self) -> bool:
        self._running = True
        self._stop_event.clear()
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()
        print(f"[RELAY] [OK] Started, publishing to '{self._my_topic}', reading from '{self._peer_topic}'")
        return True

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)

    def send(self, data: bytes) -> bool:
        if not self._running:
            return False
        # Наш протокол — произвольные байты (шифротекст AES-GCM почти
        # всегда содержит невалидные UTF-8 последовательности), а поле
        # "message" в JSON-ответе ntfy обязано быть валидной Unicode-
        # строкой. Без base64 такие байты либо ломаются при кодировании
        # в JSON на сервере, либо запрос вовсе не проходит — раньше это
        # уже было учтено, а при переписывании транспорта потерялось.
        encoded = base64.b64encode(data)
        url = f"{self._server}/{self._my_topic}"
        req = urllib.request.Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "text/plain")
        try:
            with urllib.request.urlopen(req, timeout=5, context=_ssl_context()) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            print(f"[RELAY] [ERROR] Send failed: {e}")
            return False

    def is_alive(self) -> bool:
        return self._running

    def _recv_loop(self) -> None:
        while self._running and not self._stop_event.is_set():
            url = f"{self._server}/{self._peer_topic}/json?poll=1"
            got_message = False
            try:
                with urllib.request.urlopen(url, timeout=10, context=_ssl_context()) as resp:
                    if resp.status != 200:
                        continue
                    raw = resp.read().decode()
                    for line in raw.splitlines():
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                            message_data = msg.get("message", "")
                            if message_data:
                                got_message = True
                                # Симметрично send(): декодируем base64
                                # обратно в сырые байты пакета.
                                try:
                                    padding = len(message_data) % 4
                                    if padding:
                                        message_data += "=" * (4 - padding)
                                    data = base64.b64decode(message_data)
                                except Exception as e:
                                    print(f"[RELAY] [WARN] Base64 decode failed: {e}")
                                    continue
                                self._deliver(data)
                        except json.JSONDecodeError:
                            continue
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    pass
                elif e.code == 429:
                    print("[RELAY] [WARN] Rate limited, waiting 5s...")
                    time.sleep(5)
                else:
                    print(f"[RELAY] [WARN] HTTP error: {e}")
            except Exception as e:
                print(f"[RELAY] [WARN] Recv loop error: {e}")
                time.sleep(1)

            # КРИТИЧНО: без паузы на "пустом" успешном ответе цикл долбит
            # ntfy.sh настолько часто, насколько позволяет сеть — это
            # практически гарантированно словит 429 (см. историю в этом
            # чате с одноразовым обменом адресами). Раньше был persistent
            # subscribe без busy-loop; при переходе на поллинг это
            # ограничение нужно было сохранить явно.
            if not got_message and self._running:
                self._stop_event.wait(1.5)

    def _deliver(self, data: bytes) -> None:
        if self._on_receive:
            try:
                self._on_receive(data)
            except Exception as e:
                print(f"[RELAY] [ERROR] Deliver callback failed: {e}")
