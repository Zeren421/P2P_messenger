"""
helpers/render.py — rendering messages + scroll management.
Split into two classes for single responsibility.
"""

from __future__ import annotations

from datetime import datetime
from PyQt5.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QLabel, QSpacerItem, QSizePolicy

class MessageRenderer:
    """
    Handles adding message bubbles, date separators, and alignment.
    """
    def reset(self) -> None:
        """Сбрасывает состояние рендерера (например, после очистки чата)."""
        self._last_date = None

    def __init__(self, chat_layout):
        self.chat_layout = chat_layout
        self._last_date = None

    def add_message(self, widget: QWidget, timestamp: datetime, align: str = "left") -> None:
        date_str = timestamp.strftime("%Y-%m-%d")
        if self._last_date != date_str:
            self._add_date_separator(date_str)
            self._last_date = date_str
        self._insert_row(widget, align)

    def _add_date_separator(self, date_str: str) -> None:
        label = QLabel(f"📅 {date_str}")
        label.setStyleSheet("""
            color: #666666;
            font-size: 14px;
            background-color: #111111;
            padding: 6px 20px;
            border-radius: 20px;
            border: 1px solid #333333;
        """)
        label.setAlignment(Qt.AlignCenter)
        self._insert_row(label, "center")

    def _insert_row(self, widget: QWidget, align: str) -> None:
        layout = self.chat_layout
        spacer = None
        if layout.count() > 0:
            last_item = layout.itemAt(layout.count() - 1)
            if last_item and isinstance(last_item, QSpacerItem):
                spacer = layout.takeAt(layout.count() - 1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        if align == "right":
            row.addStretch(1)
            row.addWidget(widget)
        elif align == "center":
            row.addStretch(1)
            row.addWidget(widget)
            row.addStretch(1)
        else:
            row.addWidget(widget)
            row.addStretch(1)

        insert_index = layout.count()
        layout.insertLayout(insert_index, row)
        if spacer:
            layout.addItem(spacer)

class ScrollManager:
    """
    Manages scrollbar behavior: auto-scroll, "scroll down" button, unread counter.
    """

    def __init__(self, scroll_area, scroll_down_btn):
        self.scroll_area = scroll_area
        self.scroll_down_btn = scroll_down_btn
        self._scroll_anim = None
        self._unread_count = 0
        self._auto_scroll = True
        self._hide_timer = QTimer()
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_scroll_down_btn)

        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll_changed)
        self.scroll_down_btn.clicked.connect(self._on_scroll_down_clicked)

    def on_new_message(self, is_own: bool) -> None:
        if is_own:
            self._auto_scroll = True
            self._scroll_to_bottom()
        else:
            scrollbar = self.scroll_area.verticalScrollBar()
            if scrollbar.value() >= scrollbar.maximum() - 100:
                self._auto_scroll = True
                self._scroll_to_bottom()
            else:
                self._auto_scroll = False
                self._unread_count += 1
                self._update_scroll_button_text()

    def _scroll_to_bottom(self) -> None:
        scrollbar = self.scroll_area.verticalScrollBar()
        target = scrollbar.maximum()
        if scrollbar.value() == target:
            return
        if self._scroll_anim:
            self._scroll_anim.stop()
        self._scroll_anim = QPropertyAnimation(scrollbar, b"value")
        self._scroll_anim.setDuration(220)
        self._scroll_anim.setStartValue(scrollbar.value())
        self._scroll_anim.setEndValue(target)
        self._scroll_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._scroll_anim.start()
        self._unread_count = 0
        self.scroll_down_btn.hide()

    def _scroll_to_bottom_instant(self) -> None:
        """Мгновенная прокрутка вниз без анимации."""
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self._unread_count = 0
        self.scroll_down_btn.hide()

    def _on_scroll_changed(self, value: int) -> None:
        scrollbar = self.scroll_area.verticalScrollBar()
        near_bottom = value >= scrollbar.maximum() - 100
        if near_bottom:
            self._auto_scroll = True
            self._unread_count = 0
            self.scroll_down_btn.hide()
        else:
            self._auto_scroll = False
            self._update_scroll_button_text()
            self.scroll_down_btn.show()
            self._hide_timer.start(3000)

    def _on_scroll_down_clicked(self) -> None:
        self._auto_scroll = True
        self._unread_count = 0
        self._scroll_to_bottom()
        self.scroll_down_btn.hide()

    def _update_scroll_button_text(self) -> None:
        if self._unread_count > 0:
            text = f"⬇ {self._unread_count}" if self._unread_count <= 99 else "⬇ 99+"
            self.scroll_down_btn.setText(text)
        else:
            self.scroll_down_btn.setText("⬇")

    def _hide_scroll_down_btn(self) -> None:
        if not self._auto_scroll:
            scrollbar = self.scroll_area.verticalScrollBar()
            if scrollbar.value() < scrollbar.maximum() - 100:
                return
        self.scroll_down_btn.hide()
