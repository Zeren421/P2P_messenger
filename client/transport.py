"""
Абстрактный интерфейс транспорта.

ITransport ничего не знает о протоколе сообщений (PacketType, seq, ACK) —
это забота ConnectionManager. Транспорт занимается только доставкой
сырых байтов: как именно они дойдут (UDP hole punching, ntfy-relay,
что-то ещё в будущем) — деталь конкретной реализации.

Это специально сделано так, чтобы ConnectionManager мог прозрачно
переключаться между реализациями (например, с UdpTransport на
RelayTransport при неудачном hole punching), не меняя ни строчки
логики протокола.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional

# Колбэк вызывается транспортом при получении новых сырых байтов
ReceiveCallback = Callable[[bytes], None]


class TransportError(Exception):
    """Ошибка транспортного уровня (не удалось отправить, не удалось запустить и т.п.)."""


class ITransport(ABC):
    def __init__(self) -> None:
        self._on_receive: Optional[ReceiveCallback] = None

    def set_on_receive(self, callback: ReceiveCallback) -> None:
        """Регистрирует функцию, которая будет вызвана при получении новых байт."""
        self._on_receive = callback

    def _deliver(self, data: bytes) -> None:
        """Вызывается реализацией транспорта, когда пришли новые байты."""
        if self._on_receive is not None:
            self._on_receive(data)

    @abstractmethod
    def start(self) -> bool:
        """
        Запускает транспорт (открывает сокет, начинает punch/поллинг и т.п.).
        Возвращает True при успешном старте, False при неудаче — например,
        если hole punching не удался и ConnectionManager должен переключиться
        на fallback-транспорт.
        """
        raise NotImplementedError

    @abstractmethod
    def stop(self) -> None:
        """Останавливает транспорт и освобождает ресурсы (сокет, потоки и т.п.)."""
        raise NotImplementedError

    @abstractmethod
    def send(self, data: bytes) -> bool:
        """
        Отправляет сырые байты. Возвращает True, если отправка технически
        удалась (это НЕ подтверждение доставки — за это отвечает ACK на
        уровне протокола, а не транспорт).
        """
        raise NotImplementedError

    @abstractmethod
    def is_alive(self) -> bool:
        """
        Жив ли транспорт прямо сейчас (сокет открыт, соединение с relay не
        оборвано и т.п.). ConnectionManager использует это, чтобы решить,
        не пора ли переключаться на fallback.
        """
        raise NotImplementedError
