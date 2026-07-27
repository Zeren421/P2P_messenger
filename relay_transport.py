"""
RelayTransport — реализация ITransport через ntfy.sh как постоянный
push-канал, без всякого NAT traversal. Используется как fallback, когда
UDP hole punching не удался.
"""

from __future__ import annotations

import base64
import json
import ssl
import threading
import time
import urllib.request
from typing import Optional

from transport import ITransport

_KEEPALIVE_TIMEOUT = 60.0
_RECONNECT_DELAY = 3.0


def _ssl_context() -> ssl.SSLContext:
    # Используем стандартную проверку сертификатов (без отключения проверки)
    return ssl.create_default_context()


class RelayTransport(ITransport):
    def __init__(self, my_topic: str, peer_topic: str) -> None:
        super().__init__()
        self._my_topic = my_topic
        self._peer_topic = peer_topic

        self._running = False
        self._connected = False
        self._stop_event = threading.Event()
        self._recv_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # ITransport
    # ------------------------------------------------------------------

    def start(self) -> bool:
        self._stop_event.clear()
        self._running = True

        self._recv_thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        self._recv_thread.start()

        for _ in range(20):
            if self._connected:
                return True
            if self._stop_event.wait(0.25):
                break
        return self._connected

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        if self._recv_thread is not None:
            self._recv_thread.join(timeout=2.0)
        self._connected = False

    def send(self, data: bytes) -> bool:
        # Убираем проверку на _connected — отправляем всегда
        # (это не зависит от подписки)
        encoded = base64.b64encode(data).decode('ascii')
        req = urllib.request.Request(
            f"https://ntfy.sh/{self._my_topic}",
            data=encoded.encode('utf-8'),
            method="POST",
            headers={"Content-Type": "text/plain"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10, context=_ssl_context()) as resp:
                return 200 <= resp.status < 300
        except Exception as e:
            print(f"[RELAY] [ERROR] Send failed: {e}")
            return False

    def is_alive(self) -> bool:
        return self._running and self._connected

    # ------------------------------------------------------------------
    # Внутреннее
    # ------------------------------------------------------------------

    def _subscribe_loop(self) -> None:
        url = f"https://ntfy.sh/{self._peer_topic}/json"

        while self._running:
            try:
                print(f"[RELAY] [INFO] Subscribing to '{self._peer_topic}'...")
                req = urllib.request.Request(url, method="GET")

                with urllib.request.urlopen(
                    req, timeout=_KEEPALIVE_TIMEOUT, context=_ssl_context()
                ) as response:
                    self._connected = True
                    print(f"[RELAY] [OK] Subscribed to '{self._peer_topic}'")

                    for raw_line in response:
                        if not self._running:
                            break
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if line:
                            self._handle_line(line)

            except Exception as e:
                if self._running:
                    print(f"[RELAY] [WARN] Subscription dropped ({e}) — reconnecting...")

            self._connected = False
            if self._running:
                self._stop_event.wait(_RECONNECT_DELAY)

    def _handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return

        if event.get("event") != "message":
            return

        message = event.get("message", "")
        if not message:
            return

        try:
            # Добавляем паддинг base64, если его нет
            # ntfy иногда обрезает паддинг
            padding = len(message) % 4
            if padding:
                message += "=" * (4 - padding)
            data = base64.b64decode(message)
        except Exception as e:
            print(f"[RELAY] [WARN] Failed to decode message payload: {e}")
            return

        try:
            self._deliver(data)
        except Exception as e:
            print(f"[RELAY] [ERROR] Deliver callback failed: {e}")
