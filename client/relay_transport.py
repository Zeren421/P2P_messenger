"""
RelayTransport — с корректной обработкой 429 Too Many Requests.

Изменения относительно предыдущей версии:
  * Парсинг заголовка Retry-After (ntfy его отдаёт) — ждём ровно столько.
  * Экспоненциальный backoff с джиттером (1s -> 2s -> 4s -> 8s, ±20%),
    если заголовка нет.
  * Глобальный cooldown: после 429 притормаживаем ВСЕ отправки на N секунд,
    а не только текущую — иначе следующий пакет тут же словит новый 429.
  * Мягкий отказ: после MAX_429_STREAK подряд возвращаем False,
    чтобы ConnectionManager понял, что транспорт "не в настроении".
"""

from __future__ import annotations

import base64
import json
import os
import random
import ssl
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from transport import ITransport

# ============================================================
# Настройки backoff
# ============================================================
MAX_429_STREAK = 8          # после стольких подряд 429 — сдаться
BASE_BACKOFF = 1.0          # секунд, первый backoff
MAX_BACKOFF = 30.0          # секунд, потолок
JITTER = 0.2                # ±20% к backoff


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

        # Состояние backoff
        self._backoff_lock = threading.Lock()
        self._cooldown_until = 0.0          # timestamp, до которого нельзя отправлять
        self._consecutive_429 = 0           # сколько 429 подряд
        self._current_backoff = BASE_BACKOFF

    # ============================================================
    #  Жизненный цикл
    # ============================================================

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

    def is_alive(self) -> bool:
        return self._running

    # ============================================================
    #  Отправка с обработкой 429
    # ============================================================

    def send(self, data: bytes) -> bool:
        if not self._running:
            return False

        # Если мы в глобальном cooldown — не пытаемся даже стучаться.
        # Возвращаем False: ConnectionManager увидит, что пакет не ушёл,
        # и сделает свою попытку ретрая через RESEND_INTERVAL.
        with self._backoff_lock:
            cooldown_left = self._cooldown_until - time.time()
        if cooldown_left > 0:
            return False

        encoded = base64.b64encode(data)
        url = f"{self._server}/{self._my_topic}"
        req = urllib.request.Request(url, data=encoded, method="POST")
        req.add_header("Content-Type", "text/plain")

        try:
            with urllib.request.urlopen(req, timeout=5, context=_ssl_context()) as resp:
                ok = 200 <= resp.status < 300
                if ok:
                    self._on_send_success()
                return ok
        except urllib.error.HTTPError as e:
            if e.code == 429:
                self._handle_429(e)
                return False
            print(f"[RELAY] [ERROR] Send HTTP {e.code}: {e}")
            return False
        except Exception as e:
            print(f"[RELAY] [ERROR] Send failed: {e}")
            return False

    def _on_send_success(self) -> None:
        """Сбрасываем backoff при успешной отправке."""
        with self._backoff_lock:
            self._consecutive_429 = 0
            self._current_backoff = BASE_BACKOFF
            self._cooldown_until = 0.0

    def _handle_429(self, err: urllib.error.HTTPError) -> None:
        """Обрабатывает 429: читает Retry-After или считает backoff."""
        # 1. Пытаемся взять Retry-After (могут быть секунды или HTTP-date,
        #    ntfy обычно отдаёт секунды).
        retry_after: Optional[float] = None
        header_val = err.headers.get("Retry-After") if err.headers else None
        if header_val:
            try:
                retry_after = float(header_val.strip())
            except ValueError:
                # HTTP-date — не парсим, fallback на backoff.
                pass

        with self._backoff_lock:
            self._consecutive_429 += 1
            streak = self._consecutive_429

            if retry_after is not None:
                wait = max(0.5, retry_after)
            else:
                wait = min(MAX_BACKOFF, self._current_backoff)
                # Джиттер ±JITTER
                wait *= 1.0 + random.uniform(-JITTER, JITTER)
                wait = max(0.5, wait)
                self._current_backoff = min(MAX_BACKOFF, self._current_backoff * 2)

            self._cooldown_until = max(self._cooldown_until, time.time() + wait)

        print(f"[RELAY] [WARN] 429 #{streak}, cooldown {wait:.1f}s")

        if streak >= MAX_429_STREAK:
            # Слишком долго бьёмся — притворяемся, что транспорт "устал".
            # ConnectionManager увидит False и сделает свою логику.
            print(f"[RELAY] [ERROR] {MAX_429_STREAK} consecutive 429 — giving up temporarily")

    # ============================================================
    #  Приём
    # ============================================================

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
                    # Тот же backoff, что и на отправке, но с локальным таймером.
                    self._handle_429(e)
                else:
                    print(f"[RELAY] [WARN] HTTP error: {e}")
            except Exception as e:
                print(f"[RELAY] [WARN] Recv loop error: {e}")
                time.sleep(1)

            # Без паузы на "пустом" успешном ответе цикл долбит ntfy.sh
            # настолько часто, насколько позволяет сеть — почти гарантированный 429.
            if not got_message and self._running:
                # Учитываем и глобальный cooldown: если мы в нём — ждём дольше.
                with self._backoff_lock:
                    cooldown_left = self._cooldown_until - time.time()
                self._stop_event.wait(max(1.5, cooldown_left))

    def _deliver(self, data: bytes) -> None:
        if self._on_receive:
            try:
                self._on_receive(data)
            except Exception as e:
                print(f"[RELAY] [ERROR] Deliver callback failed: {e}")