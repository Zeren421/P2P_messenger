import sys
import os
import json
import random
import threading
import time
import urllib.parse
from datetime import datetime
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *
from PIL import Image, ImageFilter
from io import BytesIO
import shutil
import platform
import subprocess

from connection_manager import ConnectionManager

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# ===================================================
#  НАСТРОЙКИ
# ===================================================
DEBUG = True

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SOUND_PATH = os.path.join(APP_DIR, "din-don.wav")
HISTORY_FILE = os.path.join(APP_DIR, "history.jsonl")
NAME_FILE = os.path.join(APP_DIR, "username.txt")
PEER_FILE = os.path.join(APP_DIR, "peer.txt")
HISTORY_LIMIT = 1000

SIDE_PORTS = {"a": 9090, "b": 9091}
TOPIC_PREFIX = "p2pchat"

USER_COLORS = [
    "#FF4757", "#2ED573", "#1E90FF", "#FFA502", "#A29BFE",
    "#FF6B81", "#7BED9F", "#70A1FF", "#FFDA79", "#F368E0",
    "#2BCBBA", "#54A0FF", "#FF9FF3", "#00D2D3", "#FECA57",
]


def darken_color(hex_color, factor=0.6):
    hex_color = hex_color.lstrip('#')
    if len(hex_color) == 8:
        hex_color = hex_color[:6]
    r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
    r = int(r * factor)
    g = int(g * factor)
    b = int(b * factor)
    return f"#{r:02x}{g:02x}{b:02x}"


def encode_username(username: str) -> str:
    try:
        return username.encode('idna').decode('ascii')
    except:
        import re
        safe = re.sub(r'[^a-zA-Z0-9_]', '_', username)
        return safe


def save_peer(peer_name):
    try:
        with open(PEER_FILE, "w", encoding="utf-8") as f:
            f.write(peer_name.strip())
        if DEBUG:
            print(f"[DEBUG] Собеседник сохранён: {peer_name}")
    except Exception as e:
        if DEBUG:
            print(f"Ошибка сохранения собеседника: {e}")

def load_peer():
    if not os.path.isfile(PEER_FILE):
        return ""
    try:
        with open(PEER_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception as e:
        if DEBUG:
            print(f"Ошибка загрузки собеседника: {e}")
        return ""


# ===================================================
#  ИНДИКАТОР «ПЕЧАТАЕТ...»
# ===================================================
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


# ===================================================
#  КРУГОВОЙ ПРОГРЕСС
# ===================================================
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


# ===================================================
#  ВИДЖЕТ ПУЗЫРЯ ДЛЯ СООБЩЕНИЙ
# ===================================================
class MessageBubble(QFrame):
    def __init__(self, sender, text, time_str, is_own=False, is_system=False, color=None, parent=None):
        super().__init__(parent)
        self.setObjectName("bubble")

        if is_system:
            bg = "#111111"
            border = "#333333"
            text_color = "#999999"
        elif is_own:
            bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7a68ea, stop:1 #6c5ce7)"
            border = "rgba(255,255,255,0.08)"
            text_color = "#ffffff"
        else:
            if color:
                base_color = color
                faded_color = darken_color(base_color, 0.55)
                bg = f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {faded_color}, stop:1 {base_color})"
                border = "rgba(255,255,255,0.06)"
                text_color = "#e8e8e8"
            else:
                bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #333333, stop:1 #262626)"
                border = "rgba(255,255,255,0.06)"
                text_color = "#e0e0e0"

        if is_system:
            radius_css = "border-radius: 16px;"
        elif is_own:
            radius_css = "border-top-left-radius: 18px; border-top-right-radius: 18px; border-bottom-left-radius: 18px; border-bottom-right-radius: 4px;"
        else:
            radius_css = "border-top-left-radius: 18px; border-top-right-radius: 18px; border-bottom-left-radius: 4px; border-bottom-right-radius: 18px;"

        self.setStyleSheet(f"""
            QFrame#bubble {{
                background: {bg};
                border: 1px solid {border};
                {radius_css}
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 10, 16, 10)
        layout.setSpacing(4)

        if not is_system:
            header = QLabel(f"{sender} · {time_str}")
            header.setStyleSheet(f"""
                color: {'rgba(255,255,255,0.6)' if is_own else '#ff6b6b'};
                font-size: 11px;
                font-weight: 700;
            """)
            layout.addWidget(header)
            body_text = text
        else:
            body_text = f"✦ {sender} {text}" if sender else f"✦ {text}"

        body = QLabel(body_text)
        body.setWordWrap(True)
        body.setMinimumWidth(420)
        body.setMaximumWidth(650)
        body.setStyleSheet(f"""
            color: {text_color};
            font-size: {'13px' if is_system else '16px'};
        """)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(body)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22 if not is_system else 10)
        shadow.setOffset(0, 4)
        if is_own:
            shadow.setColor(QColor(108, 92, 231, 140))
        else:
            shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(shadow)
        self.setMaximumWidth(720)


# ===================================================
#  ВИДЖЕТ ПУЗЫРЯ ДЛЯ ФАЙЛОВ
# ===================================================
class FileBubble(QFrame):
    def __init__(self, file_id, filename, file_size, thumbnail=None,
                 caption="", is_own=False, time_str=None, parent=None,
                 original_bytes=None):
        super().__init__(parent)
        self.setObjectName("file_bubble")
        self.file_id = file_id
        self.filename = filename
        self.file_size = file_size
        self.is_own = is_own
        self.time_str = time_str or datetime.now().strftime("%H:%M")
        self.caption = caption
        self._completed = False
        self._image_loaded = False
        self._progress_complete = False
        self.file_path = ""
        self._interactable = False
        self.image_bytes = b''

        self.thumbnail_bytes = thumbnail if thumbnail is not None else b''
        self.original_bytes = original_bytes if original_bytes is not None else b''
        self.original_pixmap = QPixmap()
        self.thumbnail_blurred = QPixmap()
        self.thumbnail_clear = QPixmap()

        ext = os.path.splitext(filename)[1].lower()
        self.is_image = ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')

        if is_own:
            bg = "qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #7a68ea, stop:1 #6c5ce7)"
            border = "rgba(255,255,255,0.08)"
            radius = "border-top-left-radius: 18px; border-top-right-radius: 18px; border-bottom-left-radius: 18px; border-bottom-right-radius: 4px;"
            text_color = "#ffffff"
            header_color = "rgba(255,255,255,0.6)"
        else:
            bg = "#2d2d44" if self.is_image else "#1a1a2e"
            border = "rgba(255,255,255,0.06)"
            radius = "border-top-left-radius: 18px; border-top-right-radius: 18px; border-bottom-left-radius: 4px; border-bottom-right-radius: 18px;"
            text_color = "#e8e8e8"
            header_color = "#ff6b6b"

        self.setStyleSheet(f"""
            QFrame#file_bubble {{
                background: {bg};
                border: 1px solid {border};
                {radius}
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        header = QLabel(f"{filename} · {self.time_str}")
        header.setStyleSheet(f"color: {header_color}; font-size: 11px; font-weight: 700;")
        layout.addWidget(header)

        if caption:
            caption_label = QLabel(f"✏️ {caption}")
            caption_label.setStyleSheet(f"color: {text_color}; font-size: 12px; font-style: italic;")
            caption_label.setWordWrap(True)
            layout.addWidget(caption_label)

        self.preview_container = QWidget()
        self.preview_layout = QVBoxLayout(self.preview_container)
        self.preview_layout.setContentsMargins(0, 0, 0, 0)

        self.progress_indicator = CircularProgressIndicator(self, size=50, line_width=4)
        self.progress_indicator.hide()

        if self.is_image:
            self.image_label = QLabel()
            self.image_label.setAlignment(Qt.AlignCenter)
            self.image_label.setStyleSheet("border: none; background: transparent;")
            self.image_label.setMinimumHeight(100)

            if self.thumbnail_bytes:
                self._load_thumbnail(self.thumbnail_bytes)
                self._show_blurred_thumbnail()
            else:
                self.image_label.setText("🖼️")

            if self.original_bytes:
                self._load_original_for_thumbnail(self.original_bytes)

            container = QWidget()
            container_layout = QVBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.addWidget(self.image_label, alignment=Qt.AlignCenter)
            container_layout.addWidget(self.progress_indicator, alignment=Qt.AlignCenter)
            self.preview_layout.addWidget(container)

            self.info_label = QLabel(f"🖼️ {file_size//1024} KB")
            self.info_label.setStyleSheet(f"color: {text_color}; font-size: 11px;")
            self.info_label.hide()
            layout.addWidget(self.preview_container)
            layout.addWidget(self.info_label)

            self.click_hint = QLabel("Нажмите для просмотра")
            self.click_hint.setStyleSheet("color: #888888; font-size: 11px;")
            self.click_hint.setAlignment(Qt.AlignCenter)
            self.click_hint.hide()
            layout.addWidget(self.click_hint)

        else:
            icon_label = QLabel("📄")
            icon_label.setStyleSheet("font-size: 32px;")
            icon_label.setAlignment(Qt.AlignCenter)
            self.preview_layout.addWidget(icon_label)

            info = QLabel(f"{filename} · {file_size//1024} KB")
            info.setStyleSheet(f"color: {text_color}; font-size: 13px;")
            self.preview_layout.addWidget(info)

            self.progress_bar = QProgressBar()
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)
            self.progress_bar.setStyleSheet("""
                QProgressBar {
                    background-color: #1a1a2e;
                    border: 1px solid #2a2a4a;
                    border-radius: 6px;
                    height: 8px;
                    text-align: center;
                    color: transparent;
                }
                QProgressBar::chunk {
                    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                        stop:0 #6c5ce7,
                        stop:0.5 #a29bfe,
                        stop:1 #6c5ce7);
                    border-radius: 6px;
                }
            """)
            self.preview_layout.addWidget(self.progress_bar)
            layout.addWidget(self.preview_container)

            actions = QHBoxLayout()
            actions.setSpacing(8)
            open_btn = QPushButton("📂 Открыть")
            open_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2d2d44; color: #ffffff;
                    border: 1px solid #4a4a6a; border-radius: 8px;
                    font-size: 12px; padding: 4px 12px;
                }
                QPushButton:hover { background-color: #3d3d5c; }
            """)
            open_btn.clicked.connect(self._open_file)
            actions.addWidget(open_btn)

            save_btn = QPushButton("💾 Сохранить как...")
            save_btn.setStyleSheet("""
                QPushButton {
                    background-color: #2d2d44; color: #ffffff;
                    border: 1px solid #4a4a6a; border-radius: 8px;
                    font-size: 12px; padding: 4px 12px;
                }
                QPushButton:hover { background-color: #3d3d5c; }
            """)
            save_btn.clicked.connect(self._save_as)
            actions.addWidget(save_btn)
            layout.addLayout(actions)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(22)
        shadow.setOffset(0, 4)
        if is_own:
            shadow.setColor(QColor(108, 92, 231, 140))
        else:
            shadow.setColor(QColor(0, 0, 0, 180))
        self.setGraphicsEffect(shadow)
        self.setMaximumWidth(450)

        self._reveal_timer = QTimer(self)
        self._reveal_timer.setSingleShot(True)
        self._reveal_timer.timeout.connect(self._show_clear_thumbnail)

    # --------------------------------------------------------------
    # Методы для работы с миниатюрой
    # --------------------------------------------------------------
    def _load_thumbnail(self, thumbnail_bytes: bytes):
        self.thumbnail_blurred = QPixmap()
        pix = QPixmap()
        if pix.loadFromData(thumbnail_bytes):
            self.thumbnail_blurred = pix
            try:
                from PIL import Image, ImageFilter
                from io import BytesIO
                pil_img = Image.open(BytesIO(thumbnail_bytes))
                pil_img.thumbnail((400, 400), Image.Resampling.LANCZOS)
                blurred = pil_img.filter(ImageFilter.GaussianBlur(radius=8))
                buf = BytesIO()
                blurred.save(buf, format="PNG")
                self.thumbnail_blurred.loadFromData(buf.getvalue())
            except Exception:
                pass

    def _load_original_for_thumbnail(self, image_bytes: bytes):
        pix = QPixmap()
        if pix.loadFromData(image_bytes):
            self.thumbnail_clear = pix.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self._image_loaded = True

    def _show_blurred_thumbnail(self):
        if not self.thumbnail_blurred.isNull():
            scaled = self.thumbnail_blurred.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.image_label.setPixmap(scaled)
        else:
            self.image_label.setText("🖼️")
        if hasattr(self, 'progress_indicator'):
            self.progress_indicator.show()
        self._interactable = False

    def _show_clear_thumbnail(self):
        if self.thumbnail_clear.isNull():
            return
        scaled = self.thumbnail_clear.scaled(400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.image_label.setPixmap(scaled)
        if hasattr(self, 'progress_indicator'):
            self.progress_indicator.stop()
        self.info_label.show()
        self.click_hint.show()
        self._completed = True
        self._interactable = True
        self.setCursor(Qt.PointingHandCursor)

    def load_image_bytes(self, image_bytes: bytes):
        if not self.is_image or self._image_loaded:
            return

        self.image_bytes = image_bytes
        self.original_pixmap = QPixmap()
        if not self.original_pixmap.loadFromData(image_bytes):
            print(f"[FileBubble] Failed to load original image for file_id={self.file_id}")
            return

        self._image_loaded = True
        self.thumbnail_clear = self.original_pixmap.scaled(
            400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )

        if self._progress_complete:
            self._reveal_timer.start(400)

    def update_progress(self, ratio):
        if self._completed:
            return
        percent = int(ratio * 100)

        if self.is_image:
            if percent >= 100:
                self._progress_complete = True
                if self.is_own and not self.thumbnail_clear.isNull():
                    self._show_clear_thumbnail()
                elif self._image_loaded and not self._completed:
                    self._reveal_timer.start(400)
        else:
            self.progress_bar.setValue(percent)
            if percent >= 100:
                self.progress_bar.hide()
                self._completed = True
                self._interactable = True
                self.setCursor(Qt.PointingHandCursor)

    def set_progress_done(self):
        if self.is_image:
            if hasattr(self, 'progress_indicator'):
                self.progress_indicator.stop()
            self.image_label.setText("❌")
        else:
            self.progress_bar.setValue(100)
            self.progress_bar.hide()
            self._completed = True
            self._interactable = True
            self.setCursor(Qt.PointingHandCursor)

    def set_file_path(self, path: str):
        self.file_path = path
        self._interactable = True
        self.setCursor(Qt.PointingHandCursor)
        if self.is_image:
            self.click_hint.show()

    def mousePressEvent(self, event):
        if not self._interactable or not self.is_image:
            return super().mousePressEvent(event)
        self._open_image_viewer()

    def _open_image_viewer(self):
        image_bytes = self.image_bytes if hasattr(self, 'image_bytes') else b''
        if not image_bytes and self.file_path and os.path.exists(self.file_path):
            try:
                with open(self.file_path, 'rb') as f:
                    image_bytes = f.read()
            except Exception as e:
                print(f"[FileBubble] Error reading image: {e}")
                return
        if image_bytes:
            viewer = ImageViewer(image_bytes, self.filename, self)
            viewer.show()
        else:
            print("[FileBubble] No image data to display")

    def _open_file(self):
        if not self.file_path or not os.path.exists(self.file_path):
            return
        try:
            if platform.system() == "Windows":
                os.startfile(self.file_path)
            elif platform.system() == "Darwin":
                subprocess.call(["open", self.file_path])
            else:
                subprocess.call(["xdg-open", self.file_path])
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось открыть файл: {e}")

    def _save_as(self):
        if not self.file_path or not os.path.exists(self.file_path):
            return
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить файл", self.filename)
        if path:
            try:
                shutil.copy(self.file_path, path)
            except Exception as e:
                QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить: {e}")


# ===================================================
#  ПРОСМОТРЩИК ИЗОБРАЖЕНИЙ
# ===================================================
class ImageViewer(QDialog):
    def __init__(self, image_bytes, filename, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        title_bar = QFrame()
        title_bar.setFixedHeight(50)
        title_bar.setStyleSheet("background: rgba(0,0,0,180); border: none;")
        title_layout = QHBoxLayout(title_bar)
        title_layout.setContentsMargins(16, 0, 16, 0)

        title_label = QLabel(f"🖼️ {filename}")
        title_label.setStyleSheet("color: white; font-size: 15px; font-weight: bold;")
        title_layout.addWidget(title_label)
        title_layout.addStretch()

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(32, 32)
        close_btn.setStyleSheet("""
            QPushButton {
                background: transparent;
                color: white;
                border: none;
                font-size: 18px;
                border-radius: 16px;
            }
            QPushButton:hover {
                background: rgba(255, 70, 70, 0.8);
            }
        """)
        close_btn.clicked.connect(self.close)
        title_layout.addWidget(close_btn)

        layout.addWidget(title_bar)

        self.view = QGraphicsView()
        self.view.setDragMode(QGraphicsView.ScrollHandDrag)
        self.view.setRenderHint(QPainter.Antialiasing)
        self.view.setStyleSheet("background: rgba(0,0,0,200); border: none;")
        self.view.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

        self.scene = QGraphicsScene()
        self.pixmap = QPixmap()
        if not self.pixmap.loadFromData(image_bytes):
            self.pixmap = QPixmap(400, 400)
            self.pixmap.fill(Qt.black)
            QPainter(self.pixmap).drawText(self.pixmap.rect(), Qt.AlignCenter, "❌\nНе удалось загрузить")
        self.item = QGraphicsPixmapItem(self.pixmap)
        self.scene.addItem(self.item)
        self.view.setScene(self.scene)

        layout.addWidget(self.view, 1)

        bottom_bar = QFrame()
        bottom_bar.setFixedHeight(60)
        bottom_bar.setStyleSheet("background: rgba(0,0,0,180); border: none;")
        bottom_layout = QHBoxLayout(bottom_bar)
        bottom_layout.setContentsMargins(20, 0, 20, 0)

        btn_style = """
            QPushButton {
                background: rgba(255,255,255,0.1);
                color: white;
                border: 1px solid rgba(255,255,255,0.2);
                border-radius: 8px;
                padding: 6px 14px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: rgba(255,255,255,0.2);
            }
        """
        zoom_out_btn = QPushButton("−")
        zoom_out_btn.setFixedSize(36, 36)
        zoom_out_btn.setStyleSheet(btn_style)
        zoom_out_btn.clicked.connect(self.zoom_out)

        zoom_in_btn = QPushButton("+")
        zoom_in_btn.setFixedSize(36, 36)
        zoom_in_btn.setStyleSheet(btn_style)
        zoom_in_btn.clicked.connect(self.zoom_in)

        reset_btn = QPushButton("1:1")
        reset_btn.setFixedSize(46, 36)
        reset_btn.setStyleSheet(btn_style)
        reset_btn.clicked.connect(self.reset_zoom)

        bottom_layout.addWidget(zoom_out_btn)
        bottom_layout.addWidget(zoom_in_btn)
        bottom_layout.addWidget(reset_btn)

        bottom_layout.addStretch()

        info_label = QLabel(f"{self.pixmap.width()} × {self.pixmap.height()}")
        info_label.setStyleSheet("color: rgba(255,255,255,0.6); font-size: 13px;")
        bottom_layout.addWidget(info_label)

        bottom_layout.addStretch()

        save_btn = QPushButton("💾 Сохранить")
        save_btn.setStyleSheet("""
            QPushButton {
                background: #6c5ce7;
                color: white;
                border: none;
                border-radius: 12px;
                padding: 8px 20px;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #7c6cf7;
            }
        """)
        save_btn.clicked.connect(self.save_image)
        bottom_layout.addWidget(save_btn)

        layout.addWidget(bottom_bar)

        self.adjust_size()
        self.center_window()
        self.setFocusPolicy(Qt.StrongFocus)

    def adjust_size(self):
        screen = QApplication.primaryScreen().geometry()
        max_w = int(screen.width() * 0.9)
        max_h = int(screen.height() * 0.9)
        img_w = self.pixmap.width()
        img_h = self.pixmap.height()
        if img_w > max_w or img_h + 110 > max_h:
            self.view.fitInView(self.item, Qt.KeepAspectRatio)
            self.view.setSceneRect(self.item.boundingRect())
        new_w = int(min(img_w, max_w))
        new_h = int(min(img_h + 110, max_h))
        self.resize(new_w, new_h)

    def center_window(self):
        screen = QApplication.primaryScreen().geometry()
        self.move((screen.width() - self.width()) // 2,
                  (screen.height() - self.height()) // 2)

    def zoom_in(self):
        self.view.scale(1.2, 1.2)

    def zoom_out(self):
        self.view.scale(1/1.2, 1/1.2)

    def reset_zoom(self):
        self.view.resetTransform()
        self.view.fitInView(self.item, Qt.KeepAspectRatio)

    def save_image(self):
        filepath, _ = QFileDialog.getSaveFileName(
            self,
            "Сохранить картинку",
            "",
            "Изображения (*.png *.jpg *.jpeg *.gif *.bmp)"
        )
        if filepath:
            if self.pixmap.save(filepath):
                QMessageBox.information(self, "Успех", f"✅ Картинка сохранена:\n{filepath}", QMessageBox.Ok)
            else:
                QMessageBox.warning(self, "Ошибка", "❌ Не удалось сохранить картинку", QMessageBox.Ok)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
        elif event.key() == Qt.Key_Plus or event.key() == Qt.Key_Equal:
            self.zoom_in()
        elif event.key() == Qt.Key_Minus:
            self.zoom_out()
        elif event.key() == Qt.Key_R:
            self.reset_zoom()
        elif event.key() == Qt.Key_Home:
            self.view.fitInView(self.item, Qt.KeepAspectRatio)
        else:
            super().keyPressEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.ControlModifier:
            delta = event.angleDelta().y()
            if delta > 0:
                self.zoom_in()
            else:
                self.zoom_out()
        else:
            event.ignore()


# ===================================================
#  ОСНОВНОЙ КЛИЕНТ
# ===================================================
class ChatClient(QMainWindow):
    message_signal = pyqtSignal(str, str, bool, bool, str)
    status_signal = pyqtSignal(str, str)
    typing_signal = pyqtSignal(bool, str)
    ui_state_signal = pyqtSignal(bool, bool)
    peer_bye_signal = pyqtSignal()
    password_retry_signal = pyqtSignal(int)
    file_bubble_signal = pyqtSignal(int, str, int, bytes, str, bool, str, bytes)
    file_progress_signal = pyqtSignal(int, float)
    file_error_ui_signal = pyqtSignal(int)
    incoming_file_info_signal = pyqtSignal(int, str, int, bytes, str, bool, str)
    file_received_image_signal = pyqtSignal(int, bytes)

    def __init__(self):
        super().__init__()
        self.auto_scroll = True
        self.unread_count = 0
        self.loading_history = False
        self.user_colors = {}

        self.my_username = ""
        self.peer_username = ""
        self.password = ""
        self.my_topic = ""
        self.peer_topic = ""
        self.side = "a"
        self.local_port = 9090

        self.connected = False
        self.conn_manager = None
        self.peer_name = "Собеседник"
        self.peer_color = USER_COLORS[0]
        self._last_manual_reconnect = 0.0
        self._connection_type = ""

        self.active_file_bubbles = {}
        self.last_file_progress = {}

        self.init_ui()
        self.init_signals()
        self.load_history()

        self.typing_indicator = TypingIndicator(self)
        self.typing_indicator.status_changed.connect(self.show_typing_status)
        self.last_typing_sent = 0

        self.ask_username_and_peer()
        self.start_connection()

    def init_ui(self):
        self.setWindowTitle("Мессенджер v5.0 (P2P)")
        self.setGeometry(100, 100, 940, 720)
        self.setMinimumSize(700, 500)
        self.running = True
        self.last_date = ""

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.tray_icon.setVisible(True)

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("background-color: #000000;")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        header = QFrame()
        header.setObjectName("header")
        header.setFixedHeight(60)
        header.setStyleSheet("QFrame#header { background-color: #0a0a0a; border: 1px solid #222222; border-radius: 18px; }")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(18, 0, 14, 0)

        title_label = QLabel("💬 Мессенджер")
        title_label.setStyleSheet("color: #ffffff; font-size: 17px; font-weight: 700;")

        self.status_dot = QLabel("●")
        self.status_text = QLabel("подключение...")
        self.status_text.setStyleSheet("color: #888888; font-size: 13px;")

        self.reconnect_btn = QPushButton("🔄 Переподключиться")
        self.reconnect_btn.setFixedHeight(38)
        self.reconnect_btn.setStyleSheet("""
            QPushButton {
                background-color: #2d2d44;
                color: #ffffff;
                border: 1px solid #4a4a6a;
                border-radius: 12px;
                font-size: 14px;
                font-weight: bold;
                padding: 0 14px;
            }
            QPushButton:hover {
                background-color: #3d3d5c;
            }
            QPushButton:disabled {
                color: #666666;
                border-color: #333333;
            }
        """)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.reconnect_btn.setEnabled(False)

        self.clear_button = QPushButton("🗑 Очистить")
        self.clear_button.setFixedHeight(38)
        self.clear_button.setStyleSheet("""
            QPushButton { background-color: transparent; color: #ff6b6b; border: 2px solid #ff6b6b;
                          border-radius: 12px; font-size: 14px; font-weight: bold; padding: 0 14px; }
            QPushButton:hover { background-color: #ff6b6b; color: #000000; }
        """)
        self.clear_button.clicked.connect(self.clear_chat)

        header_layout.addWidget(title_label)
        header_layout.addWidget(self.status_dot)
        header_layout.addWidget(self.status_text)
        header_layout.addStretch(1)
        header_layout.addWidget(self.reconnect_btn)
        header_layout.addWidget(self.clear_button)

        self.typing_status_label = QLabel("")
        self.typing_status_label.setStyleSheet("color: #888888; font-size: 13px; background-color: #0a0a0a; padding: 4px 10px; border-radius: 8px;")
        self.typing_status_label.setAlignment(Qt.AlignLeft)

        self.chat_card = QFrame()
        self.chat_card.setObjectName("chatCard")
        self.chat_card.setStyleSheet("QFrame#chatCard { background-color: #060606; border: 1px solid #1c1c1c; border-radius: 20px; }")
        chat_card_layout = QVBoxLayout(self.chat_card)
        chat_card_layout.setContentsMargins(0, 0, 0, 0)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet("QScrollArea { background: transparent; border: none; }")

        self.chat_container = QWidget()
        self.chat_container.setStyleSheet("background: transparent;")
        self.chat_layout = QVBoxLayout(self.chat_container)
        self.chat_layout.setContentsMargins(16, 16, 16, 16)
        self.chat_layout.setSpacing(8)
        self.chat_layout.addStretch(1)

        self.scroll_area.setWidget(self.chat_container)
        self.scroll_area.verticalScrollBar().rangeChanged.connect(self.ensure_scrolled_to_bottom)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self.on_scroll_changed)

        chat_card_layout.addWidget(self.scroll_area)

        self.scroll_down_btn = QPushButton("⬇", self.chat_card)
        self.scroll_down_btn.setFixedSize(52, 52)
        self.scroll_down_btn.setCursor(Qt.PointingHandCursor)
        self.scroll_down_btn.setStyleSheet("""
            QPushButton {
                background-color: #6c5ce7;
                color: white;
                border: none;
                border-radius: 26px;
                font-size: 20px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #7c6cf7; }
            QPushButton:pressed { background-color: #5a4bd1; }
        """)
        self.scroll_btn_shadow = QGraphicsDropShadowEffect()
        self.scroll_btn_shadow.setBlurRadius(20)
        self.scroll_btn_shadow.setOffset(0, 4)
        self.scroll_btn_shadow.setColor(QColor(0, 0, 0, 180))
        self.scroll_down_btn.setGraphicsEffect(self.scroll_btn_shadow)
        self.scroll_down_btn.clicked.connect(self.on_scroll_down_clicked)
        self.scroll_down_btn.hide()

        self.input_field = QTextEdit()
        self.input_field.setPlaceholderText("Введите сообщение... (Shift+Enter - перенос, Enter - отправка)")
        self.input_field.setFixedHeight(90)
        self.input_field.setStyleSheet("""
            QTextEdit {
                background-color: #0a0a0a;
                color: #ffffff;
                border: 2px solid #333333;
                border-radius: 16px;
                padding: 12px 18px;
                font-size: 17px;
            }
            QTextEdit:focus {
                border: 2px solid #6c5ce7;
                background-color: #0d0d0d;
            }
        """)
        input_palette = self.input_field.palette()
        input_palette.setColor(QPalette.PlaceholderText, QColor("#777777"))
        self.input_field.setPalette(input_palette)
        self.input_field.installEventFilter(self)
        self.input_field.textChanged.connect(self.on_text_changed)

        self.send_button = QPushButton("📤 Отправить")
        self.send_button.setFixedHeight(55)
        self.send_button.setStyleSheet("""
            QPushButton { background-color: #6c5ce7; color: white; border: none; border-radius: 16px;
                          font-size: 18px; font-weight: bold; padding: 0 20px; }
            QPushButton:hover { background-color: #7c6cf7; }
        """)
        self.send_button.clicked.connect(self.send_message)

        self.attach_button = QPushButton("📎")
        self.attach_button.setFixedSize(55, 55)
        self.attach_button.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #ffffff;
                border: 2px solid #444444;
                border-radius: 16px;
                font-size: 20px;
            }
            QPushButton:hover {
                background-color: #2d2d44;
            }
        """)
        self.attach_button.clicked.connect(self.attach_file)

        input_card = QFrame()
        input_card.setObjectName("inputCard")
        input_card.setStyleSheet("QFrame#inputCard { background-color: #050505; border: 1px solid #1c1c1c; border-radius: 20px; }")
        input_layout = QHBoxLayout(input_card)
        input_layout.setContentsMargins(10, 10, 10, 10)
        input_layout.addWidget(self.input_field, 4)
        input_layout.addWidget(self.attach_button, 0)
        input_layout.addWidget(self.send_button, 1)

        main_layout.addWidget(header)
        main_layout.addWidget(self.chat_card, 1)
        main_layout.addWidget(self.typing_status_label)
        main_layout.addWidget(input_card)

        self.scroll_down_btn.show()
        QTimer.singleShot(50, self.reposition_scroll_button)
        self.scroll_down_btn.hide()

    def reconnect(self):
        self._last_manual_reconnect = time.time()
        self.connected = False
        self._set_ui_state(False, False)
        self.status_signal.emit("⏳ Переподключение...", "#ffb142")
        self.add_message("", "Переподключение...", is_system=True)
        threading.Thread(target=self._do_reconnect, daemon=True).start()

    def _do_reconnect(self):
        if self.conn_manager is not None:
            try:
                self.conn_manager.send_bye()
                time.sleep(0.2)
            except Exception:
                pass
            try:
                self.conn_manager.disconnect()
            except Exception:
                pass
            self.conn_manager = None
        self.start_connection()

    @pyqtSlot()
    def _on_peer_bye(self):
        if time.time() - self._last_manual_reconnect < 5.0:
            return
        self.reconnect()

    def init_signals(self):
        self.message_signal.connect(self.add_message_safe)
        self.status_signal.connect(self.set_status)
        self.typing_signal.connect(self.handle_typing)
        self.ui_state_signal.connect(self._set_ui_state)
        self.peer_bye_signal.connect(self._on_peer_bye)
        self.password_retry_signal.connect(self._on_password_retry)
        self.file_bubble_signal.connect(self._add_file_bubble_safe)
        self.file_progress_signal.connect(self._apply_file_progress)
        self.file_error_ui_signal.connect(self._apply_file_error_ui)
        self.incoming_file_info_signal.connect(self._on_incoming_file_info)
        self.file_received_image_signal.connect(self._load_image_to_bubble)

    @pyqtSlot(int, str, int, bytes, str, bool, str, bytes)
    def _add_file_bubble_safe(self, file_id, filename, file_size, thumbnail, caption, is_own, time_str, original_bytes):
        bubble = FileBubble(
            file_id, filename, file_size,
            thumbnail=thumbnail,
            caption=caption,
            is_own=is_own,
            time_str=time_str,
            parent=self,
            original_bytes=original_bytes
        )
        if is_own:
            self._insert_row(bubble, "right")
        else:
            self._insert_row(bubble, "left")
        self.active_file_bubbles[file_id] = bubble
        if file_id in self.last_file_progress:
            progress = self.last_file_progress[file_id]
            bubble.update_progress(progress)
            if progress >= 1.0:
                del self.last_file_progress[file_id]
        self.ensure_scrolled_to_bottom()

    @pyqtSlot(bool, str)
    def handle_typing(self, is_typing, sender):
        if is_typing:
            self.typing_indicator.show_status(sender)
        else:
            self.typing_indicator.hide_status()

    @pyqtSlot(bool, bool)
    def _set_ui_state(self, input_enabled, reconnect_enabled):
        self.send_button.setEnabled(input_enabled)
        self.input_field.setEnabled(input_enabled)
        self.reconnect_btn.setEnabled(reconnect_enabled)
        self.attach_button.setEnabled(input_enabled)

    @pyqtSlot(int)
    def _on_password_retry(self, attempts_left):
        msg = f"Неверный пароль. Осталось попыток: {attempts_left}"
        new_pass, ok = QInputDialog.getText(
            self,
            "🔐 Неверный пароль",
            msg + "\nВведите правильный пароль:",
            QLineEdit.Password
        )
        if ok and new_pass is not None:
            self.password = new_pass.strip()
            self.conn_manager.set_password(self.password)
            self.conn_manager.send_password()
        else:
            self._on_status("Пароль не введён, соединение не установлено")

    def show_typing_status(self, text):
        self.typing_status_label.setText(text)
        self.typing_status_label.setVisible(bool(text))

    def on_text_changed(self):
        if not self.connected:
            return
        now = time.time()
        if now - self.last_typing_sent >= 2:
            self.last_typing_sent = now
            self.conn_manager.send_typing(True, self.my_username)

    def ask_username_and_peer(self):
        saved = self.load_username()
        name, ok = QInputDialog.getText(
            self,
            "👤 Ваш ник",
            "Введите ваш ник (русские и английские буквы, цифры, _):",
            text=saved or ""
        )
        if ok and name.strip():
            self.my_username = name.strip()
        else:
            self.my_username = f"user_{random.randint(1000, 9999)}"
        self.save_username(self.my_username)

        saved_peer = load_peer()
        peer, ok = QInputDialog.getText(
            self,
            "🔍 Найти собеседника",
            "Введите ник собеседника:",
            text=saved_peer or ""
        )
        if ok and peer.strip():
            self.peer_username = peer.strip()
            save_peer(self.peer_username)
        else:
            self.peer_username = "friend"

        passwd, ok = QInputDialog.getText(
            self,
            "🔐 Пароль",
            "Введите общий пароль (оставьте пустым, если не требуется):",
            QLineEdit.Password
        )
        if ok and passwd is not None:
            self.password = passwd.strip()
        else:
            self.password = ""

        my_encoded = encode_username(self.my_username)
        peer_encoded = encode_username(self.peer_username)

        usernames = sorted([my_encoded, peer_encoded])
        base_topic = f"{TOPIC_PREFIX}_{usernames[0]}_{usernames[1]}"

        self.side = "a" if my_encoded == usernames[0] else "b"
        self.my_topic = f"{base_topic}_{self.side}"
        peer_side = "b" if self.side == "a" else "a"
        self.peer_topic = f"{base_topic}_{peer_side}"
        self.local_port = SIDE_PORTS[self.side]

        self.add_message(
            "",
            f"Ваш ник: {self.my_username} (код: {my_encoded})",
            is_system=True,
        )
        self.add_message(
            "",
            f"Собеседник: {self.peer_username} (код: {peer_encoded})",
            is_system=True,
        )
        self.add_message(
            "",
            f"Канал: {base_topic}, сторона {self.side.upper()} (порт {self.local_port})",
            is_system=True,
        )

    def save_username(self, username):
        try:
            with open(NAME_FILE, "w", encoding="utf-8") as f:
                f.write(username)
        except Exception as e:
            if DEBUG:
                print(f"Ошибка сохранения имени: {e}")

    def load_username(self):
        if not os.path.isfile(NAME_FILE):
            return None
        try:
            with open(NAME_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            if DEBUG:
                print(f"Ошибка загрузки имени: {e}")
            return None

    def attach_file(self):
        if not self.connected:
            self.add_message("", "❌ Нет соединения для отправки файлов", is_system=True)
            return

        filepath, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите файл для отправки",
            "",
            "Все файлы (*.*);;Изображения (*.png *.jpg *.jpeg *.gif *.bmp);;Документы (*.pdf *.doc *.txt)"
        )
        if not filepath:
            return

        from udp_transport import UdpTransport
        if not isinstance(self.conn_manager._transport, UdpTransport):
            self.add_message("", "❌ Передача файлов недоступна через Relay", is_system=True)
            return

        try:
            caption, ok = QInputDialog.getText(
                self,
                "✏️ Подпись к файлу",
                "Введите подпись (необязательно):",
                text=""
            )
            if not ok:
                return
            caption = caption.strip() if caption else ""

            file_id = self.conn_manager.send_file(filepath, caption)
            filename = os.path.basename(filepath)
            size = os.path.getsize(filepath)
            self.add_message("", f"📤 Отправка файла: {filename} ({size//1024} KB)", is_system=True)

            thumbnail = b''
            original_bytes = b''
            is_image = filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))
            if is_image:
                try:
                    from PIL import Image
                    img = Image.open(filepath)
                    img_thumb = img.copy()
                    img_thumb.thumbnail((200, 200), Image.Resampling.LANCZOS)
                    buf = BytesIO()
                    img_thumb.save(buf, format="JPEG", quality=70)
                    thumbnail = buf.getvalue()
                    with open(filepath, 'rb') as f:
                        original_bytes = f.read()
                except Exception as e:
                    print(f"Ошибка создания миниатюры: {e}")

            time_str = datetime.now().strftime("%H:%M")
            self.file_bubble_signal.emit(file_id, filename, size, thumbnail, caption, True, time_str, original_bytes)
            QTimer.singleShot(50, lambda fid=file_id, fp=filepath: self._set_bubble_path(fid, fp))

        except Exception as e:
            self.add_message("", f"❌ Ошибка отправки файла: {e}", is_system=True)

    def _set_bubble_path(self, file_id: int, filepath: str):
        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            bubble.set_file_path(filepath)

    def start_connection(self):
        self.conn_manager = ConnectionManager(
            my_topic=self.my_topic,
            peer_topic=self.peer_topic,
            local_port=self.local_port,
            on_message=self._on_peer_message,
            on_typing=self._on_peer_typing,
            on_message_failed=self._on_message_failed,
            on_status=self._on_status,
            on_peer_name=self._on_peer_name,
            on_password_retry=self._on_password_retry_callback,
            on_file_received=self._on_file_received,
            on_file_progress=self._on_file_progress,
            on_file_error=self._on_file_error,
            on_file_info=self._on_file_info_callback,
        )
        self.conn_manager.set_password(self.password)
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        ok = self.conn_manager.connect()
        if ok:
            self.connected = True
            self.conn_manager.send_name(self.my_username)

    def _on_password_retry_callback(self, attempts_left: int):
        self.password_retry_signal.emit(attempts_left)

    def _on_peer_message(self, text):
        self.add_message(self.peer_name, text, is_own=False, is_system=False, color=self.peer_color)

    def _on_peer_typing(self, is_typing, sender_name):
        self.typing_signal.emit(is_typing, sender_name or self.peer_name)

    def _on_message_failed(self, text):
        self.add_message("", f"Не удалось доставить: «{text}»", is_system=True)

    def _on_peer_name(self, name):
        if name and name != self.peer_name:
            self.peer_name = name
            self.peer_color = self.get_user_color(name)
            self.add_message("", f"Собеседник: {name}", is_system=True)

    def _on_file_info_callback(self, file_id: int, filename: str, total_size: int, thumbnail: bytes, caption: str):
        ext = os.path.splitext(filename)[1].lower()
        is_image = ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
        time_str = datetime.now().strftime("%H:%M")
        self.incoming_file_info_signal.emit(file_id, filename, total_size, thumbnail, caption, is_image, time_str)

    # ======================================================================
    #  ВАЖНО: ЭТОТ МЕТОД ОТВЕЧАЕТ ЗА УВЕДОМЛЕНИЯ ПРИ ПОЛУЧЕНИИ ФАЙЛА
    # ======================================================================
    @pyqtSlot(int, str, int, bytes, str, bool, str)
    def _on_incoming_file_info(self, file_id, filename, size, thumbnail, caption, is_image, time_str):
        """Создаёт бабл с миниатюрой сразу при получении FILE_INFO."""
        bubble = FileBubble(
            file_id, filename, size,
            thumbnail=thumbnail,
            caption=caption,
            is_own=False,
            time_str=time_str,
            parent=self,
            original_bytes=None
        )
        self._insert_row(bubble, "left")
        self.active_file_bubbles[file_id] = bubble

        if file_id in self.last_file_progress:
            bubble.update_progress(self.last_file_progress[file_id])

        # ===== УВЕДОМЛЕНИЕ О НОВОМ ФАЙЛЕ =====
        self.play_notification_sound()
        if not self.isActiveWindow():
            self.show_tray_notification("📎 Получен файл", filename)
    # ======================================================================

    def _on_file_received(self, file_id, filename, save_path):
        self.add_message("", f"📥 Получен файл: {filename} (сохранён в {save_path})", is_system=True)

        ext = os.path.splitext(filename)[1].lower()
        is_image = ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')

        bubble = self.active_file_bubbles.get(file_id)
        if bubble is not None:
            bubble.set_file_path(save_path)
            if is_image:
                try:
                    with open(save_path, 'rb') as f:
                        image_bytes = f.read()
                    self.file_received_image_signal.emit(file_id, image_bytes)
                except Exception as e:
                    print(f"Ошибка чтения картинки: {e}")
            return

        file_size = os.path.getsize(save_path)
        thumbnail = b''
        original_bytes = b''
        if is_image:
            try:
                with open(save_path, 'rb') as f:
                    original_bytes = f.read()
                from PIL import Image
                img = Image.open(save_path)
                img.thumbnail((200, 200), Image.Resampling.LANCZOS)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=70)
                thumbnail = buf.getvalue()
            except Exception:
                pass
        time_str = datetime.now().strftime("%H:%M")
        self.file_bubble_signal.emit(file_id, filename, file_size, thumbnail, "", False, time_str, original_bytes)

    @pyqtSlot(int, bytes)
    def _load_image_to_bubble(self, file_id, image_bytes):
        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            bubble.image_bytes = image_bytes
            bubble.load_image_bytes(image_bytes)

    def _on_file_progress(self, file_id, ratio, is_outgoing):
        self.last_file_progress[file_id] = ratio
        self.file_progress_signal.emit(file_id, ratio)

    @pyqtSlot(int, float)
    def _apply_file_progress(self, file_id, ratio):
        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            bubble.update_progress(ratio)
            if ratio >= 1.0:
                QTimer.singleShot(2000, lambda: self.active_file_bubbles.pop(file_id, None))
                if file_id in self.last_file_progress:
                    del self.last_file_progress[file_id]

    def _on_file_error(self, file_id, reason):
        self.add_message("", f"❌ Ошибка файла: {reason}", is_system=True)
        self.file_error_ui_signal.emit(file_id)

    @pyqtSlot(int)
    def _apply_file_error_ui(self, file_id):
        bubble = self.active_file_bubbles.pop(file_id, None)
        if bubble:
            bubble.set_progress_done()
        if file_id in self.last_file_progress:
            del self.last_file_progress[file_id]

    def get_user_color(self, sender):
        if sender not in self.user_colors:
            available = [c for c in USER_COLORS if c not in self.user_colors.values()]
            if not available:
                available = USER_COLORS
            self.user_colors[sender] = random.choice(available)
        return self.user_colors[sender]

    def _on_status(self, message):
        low = message.lower()

        if "connected via udp" in low or "direct p2p" in low:
            self._connection_type = "UDP"
            color = "#4caf50"
            status_text = "✅ Прямое (UDP)"
            self.connected = True
            input_enabled, reconnect_enabled = True, True
        elif "connected via relay" in low:
            self._connection_type = "Relay"
            color = "#ffb142"
            status_text = "🔄 Через Relay"
            self.connected = True
            input_enabled, reconnect_enabled = True, True
        elif "connecting" in low or "punch" in low:
            color = "#ffb142"
            status_text = "⏳ Подключение..."
            self.connected = False
            input_enabled, reconnect_enabled = False, False
        elif "fail" in low or "error" in low:
            color = "#ff6b6b"
            status_text = "❌ Ошибка"
            self.connected = False
            input_enabled, reconnect_enabled = False, True
        elif "disconnected" in low:
            color = "#ff6b6b"
            status_text = "❌ Не в сети"
            self.connected = False
            input_enabled, reconnect_enabled = False, True
            if message == "Peer disconnected":
                self.peer_bye_signal.emit()
        elif "connection lost" in low:
            color = "#ff6b6b"
            status_text = "❌ Соединение потеряно"
            self.connected = False
            input_enabled, reconnect_enabled = False, True
            QTimer.singleShot(0, self.reconnect)
            self.add_message("", "Соединение потеряно, переподключение...", is_system=True)
        elif "password verified" in low:
            if self._connection_type == "UDP":
                status_text = "✅ Прямое (UDP) 🔒 Пароль принят"
            elif self._connection_type == "Relay":
                status_text = "🔄 Через Relay 🔒 Пароль принят"
            else:
                status_text = "🔒 Пароль принят"
            color = "#4caf50"
            self.connected = True
            input_enabled, reconnect_enabled = True, True
        elif "password" in low and "reject" in low:
            color = "#ff6b6b"
            status_text = "❌ Неверный пароль"
            self.connected = False
            input_enabled, reconnect_enabled = False, True
        else:
            if "no password required" in low:
                self.connected = True
                input_enabled, reconnect_enabled = True, True
                return
            else:
                color = "#888888"
                status_text = "💬 " + message[:30]
                input_enabled, reconnect_enabled = self.connected, True

        self.status_signal.emit(status_text, color)
        self.ui_state_signal.emit(input_enabled, reconnect_enabled)

        if any(w in low for w in ["connected", "fail", "error", "disconnected", "password", "verified", "connection lost"]):
            self.add_message("", message, is_system=True)

    # --------------------------------------------------------------
    #  ИСТОРИЯ, ВЫВОД, ПРОКРУТКА
    # --------------------------------------------------------------
    def save_to_history(self, sender, text, is_system=False):
        now = datetime.now()
        entry = {
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "sender": sender,
            "text": text,
            "is_own": (sender == self.my_username),
            "is_system": is_system,
        }
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._trim_history()
        except Exception as e:
            if DEBUG:
                print(f"Ошибка сохранения истории: {e}")

    def _trim_history(self):
        if not os.path.isfile(HISTORY_FILE):
            return
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > HISTORY_LIMIT:
                with open(HISTORY_FILE, "w", encoding="utf-8") as f:
                    f.writelines(lines[-HISTORY_LIMIT:])
        except Exception as e:
            if DEBUG:
                print(f"Ошибка обрезки истории: {e}")

    def load_history(self):
        if not os.path.isfile(HISTORY_FILE):
            return

        self.loading_history = True

        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                lines = f.readlines()

            entries = []
            for line in lines[-HISTORY_LIMIT:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    if DEBUG:
                        print(f"Ошибка парсинга JSON: {line}")

            current_date = None
            for entry in entries:
                entry_date = entry.get("date", "")
                date_part = entry_date[:10] if len(entry_date) >= 10 else ""

                if date_part and date_part != current_date:
                    current_date = date_part
                    separator = QLabel(f"📅 {date_part}")
                    separator.setStyleSheet("""
                        color: #666666; font-size: 14px; background-color: #111111;
                        padding: 6px 20px; border-radius: 20px; border: 1px solid #333333;
                    """)
                    separator.setAlignment(Qt.AlignCenter)
                    self._insert_row(separator, "center")

                sender = entry.get("sender", "")
                text = entry.get("text", "")
                is_own = entry.get("is_own", False)
                is_system = entry.get("is_system", False)

                self.add_message(sender, text, is_own=is_own, is_system=is_system)

        except Exception as e:
            if DEBUG:
                print(f"Ошибка загрузки истории: {e}")

        self.loading_history = False

    def add_message_safe(self, sender, text, is_own, is_system, color=None):
        time_str = datetime.now().strftime("%H:%M")

        if not self.loading_history and not is_system:
            self.save_to_history(sender, text, is_system)

        self.add_date_separator()
        bubble = MessageBubble(sender, text, time_str, is_own=is_own, is_system=is_system, color=color)

        if is_system:
            self._insert_row(bubble, "center")
        elif is_own:
            self._insert_row(bubble, "right")
        else:
            self._insert_row(bubble, "left")

        if is_own:
            self.auto_scroll = True
        if not is_own and not is_system and not self.auto_scroll:
            self.unread_count += 1

        self.ensure_scrolled_to_bottom()

        if not self.loading_history and not is_own and not is_system:
            self.play_notification_sound()
            if not self.isActiveWindow():
                self.show_tray_notification(sender, text)

    def add_date_separator(self):
        current_date = datetime.now().strftime("%d.%m.%Y")
        if current_date != self.last_date:
            self.last_date = current_date
            label = QLabel(f"📅 {current_date}")
            label.setStyleSheet("""
                color: #666666; font-size: 14px; background-color: #111111;
                padding: 6px 20px; border-radius: 20px; border: 1px solid #333333;
            """)
            label.setAlignment(Qt.AlignCenter)
            self._insert_row(label, "center")

    def _insert_row(self, widget, align):
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
        insert_index = self.chat_layout.count() - 1
        self.chat_layout.insertLayout(insert_index, row)

    def add_message(self, sender, text, is_own=False, is_system=False, color=None):
        self.message_signal.emit(sender, text, is_own, is_system, color)

    def reposition_scroll_button(self):
        margin = 20
        card_width = self.chat_card.width()
        card_height = self.chat_card.height()
        btn_width = self.scroll_down_btn.width()
        btn_height = self.scroll_down_btn.height()
        x = max(0, card_width - btn_width - margin)
        y = max(0, card_height - btn_height - margin)
        self.scroll_down_btn.move(x, y)
        self.scroll_down_btn.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.scroll_down_btn.isVisible():
            self.reposition_scroll_button()

    def on_scroll_changed(self, value):
        scrollbar = self.scroll_area.verticalScrollBar()
        near_bottom = value >= scrollbar.maximum() - 100
        self.auto_scroll = near_bottom
        if near_bottom:
            self.unread_count = 0
            self.scroll_down_btn.hide()
        else:
            self._update_scroll_button_text()
            self.scroll_down_btn.show()
            self.reposition_scroll_button()

    def ensure_scrolled_to_bottom(self, min_val=None, max_val=None):
        QTimer.singleShot(10, self._check_scroll_position)

    def _check_scroll_position(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        near_bottom = scrollbar.value() >= scrollbar.maximum() - 100
        if self.auto_scroll or near_bottom:
            self.animate_scroll_to_bottom()
            self.unread_count = 0
            self.scroll_down_btn.hide()
        else:
            self._update_scroll_button_text()
            self.scroll_down_btn.show()
            self.reposition_scroll_button()

    def animate_scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        target = scrollbar.maximum()
        if scrollbar.value() == target:
            return
        anim = QPropertyAnimation(scrollbar, b"value", self)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setDuration(220)
        anim.setStartValue(scrollbar.value())
        anim.setEndValue(target)
        anim.start()

    def on_scroll_down_clicked(self):
        self.auto_scroll = True
        self.unread_count = 0
        self.animate_scroll_to_bottom()
        self.scroll_down_btn.hide()

    def _update_scroll_button_text(self):
        if self.unread_count > 0:
            text = f"⬇ {self.unread_count}" if self.unread_count <= 99 else "⬇ 99+"
            self.scroll_down_btn.setText(text)
        else:
            self.scroll_down_btn.setText("⬇")

    def set_status(self, text, color):
        self.status_text.setText(text)
        self.status_dot.setStyleSheet(f"color: {color}; font-size: 12px;")

    def eventFilter(self, obj, event):
        if obj == self.input_field and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.send_message()
                    return True
        return super().eventFilter(obj, event)

    def send_message(self):
        msg = self.input_field.toPlainText().strip()
        if not msg or not self.connected:
            return
        self.add_message(self.my_username, msg, is_own=True, is_system=False, color="#6c5ce7")
        self.conn_manager.send_message(msg)
        self.conn_manager.send_typing(False, self.my_username)
        self.input_field.clear()

    def play_notification_sound(self):
        if not HAS_WINSOUND:
            return
        if not os.path.isfile(SOUND_PATH):
            return
        try:
            winsound.PlaySound(SOUND_PATH, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except:
            pass

    def show_tray_notification(self, sender, text):
        preview = text[:60] + ("..." if len(text) > 60 else "")
        try:
            self.tray_icon.showMessage(f"✉️ {sender}", preview, QSystemTrayIcon.Information, 3000)
        except:
            pass

    def clear_chat(self):
        reply = QMessageBox.question(
            self, "Очистка чата",
            "Очистить историю сообщений?\n(только на вашем устройстве)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            while self.chat_layout.count() > 1:
                item = self.chat_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
                elif item.layout():
                    while item.layout().count():
                        sub = item.layout().takeAt(0)
                        if sub.widget():
                            sub.widget().deleteLater()
            self.last_date = ""
            if os.path.isfile(HISTORY_FILE):
                try:
                    open(HISTORY_FILE, "w").close()
                except:
                    pass
            if os.path.isfile(NAME_FILE):
                try:
                    os.remove(NAME_FILE)
                except:
                    pass
            self.user_colors = {}
            self.add_message("", "Чат очищен", is_system=True)
            QTimer.singleShot(10, self.scroll_to_bottom)

    def scroll_to_bottom(self):
        scrollbar = self.scroll_area.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        self.auto_scroll = True
        self.unread_count = 0
        self.scroll_down_btn.hide()

    def closeEvent(self, event):
        self.running = False
        if self.conn_manager is not None:
            try:
                self.conn_manager.send_bye()
            except Exception:
                pass
            self.conn_manager.disconnect()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet("""
        QMainWindow { background-color: #000000; }
        QScrollBar:vertical {
            background: transparent; width: 6px; margin: 0px;
        }
        QScrollBar::handle:vertical {
            background: rgba(108, 92, 231, 0.4); border-radius: 3px; min-height: 30px;
        }
        QScrollBar::handle:vertical:hover { background: rgba(108, 92, 231, 0.6); }
    """)
    window = ChatClient()
    window.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
