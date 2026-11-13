"""
widgets/indicators.py — TypingIndicator and CircularProgressIndicator.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt, QTimer, QObject, pyqtSignal
from PyQt5.QtGui import QPainter, QColor, QPen, QBrush
from PyQt5.QtWidgets import QWidget

# ============================================================
#  TYPING INDICATOR
# ============================================================

class TypingIndicator(QObject):
    status_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.timer = QTimer()
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide_status)
        self.dots_timer = QTimer()
        self.dots_timer.timeout.connect(self.update_dots)
        self.dots_count = 0
        self.sender_name = ""
        self.active = False

    def show_status(self, sender_name):
        if self.active and self.sender_name == sender_name:
            self.refresh()
            return
        self.sender_name = sender_name
        self.active = True
        self.dots_count = 0
        self.update_status_text()
        self.timer.start(3000)
        self.dots_timer.start(500)

    def hide_status(self):
        self.active = False
        self.dots_timer.stop()
        self.status_changed.emit("")

    def update_dots(self):
        if not self.active:
            return
        self.dots_count = (self.dots_count + 1) % 4
        dots = "." * self.dots_count
        if self.dots_count == 0:
            dots = ""
        self.update_status_text(dots)

    def update_status_text(self, dots=None):
        if dots is None:
            dots = "..." if self.dots_count > 0 else ""
        text = f"{self.sender_name} печатает{dots}"
        self.status_changed.emit(text)

    def refresh(self):
        if self.active:
            self.timer.start(3000)

# ============================================================
#  CIRCULAR PROGRESS INDICATOR
# ============================================================

class CircularProgressIndicator(QWidget):
    def __init__(self, parent=None, size=50, line_width=4):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._line_width = line_width
        self._timer = QTimer()
        self._timer.timeout.connect(self._rotate)
        self._timer.start(30)
        self._running = True

    def _rotate(self):
        if not self._running:
            return
        self._angle = (self._angle + 5) % 360
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 100))
        painter.drawEllipse(self.rect())

        pen = QPen()
        pen.setColor(QColor("#6c5ce7"))
        pen.setWidth(self._line_width)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)

        span_angle = 270 * 16
        start_angle = -self._angle * 16
        rect = self.rect().adjusted(4, 4, -4, -4)
        painter.drawArc(rect, start_angle, span_angle)

    def stop(self):
        self._running = False
        self._timer.stop()
        self.hide()

    def start(self):
        self._running = True
        self._timer.start(30)
        self.show()
