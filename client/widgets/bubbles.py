"""
widgets/bubbles.py — MessageBubble and FileBubble widgets.
Full versions with gradients, shadows, thumbnails, and progress indicators.
"""

from __future__ import annotations

import os
import platform
import subprocess
import shutil
import time
from datetime import datetime
from io import BytesIO

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QPainter, QColor, QFont, QBrush, QPen, QPixmap, QImage
from PyQt5.QtWidgets import (
    QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QProgressBar, QFileDialog, QMessageBox, QGraphicsDropShadowEffect,
    QWidget
)
from PIL import Image, ImageFilter

from helpers.ui_utils import darken_color
from .indicators import CircularProgressIndicator

# ============================================================
#  MESSAGE BUBBLE
# ============================================================

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


# ============================================================
#  FILE BUBBLE
# ============================================================

class FileBubble(QFrame):
    def __init__(self, sender_name, file_id, filename, file_size, thumbnail=None,
                 caption="", is_own=False, time_str=None, parent=None,
                 original_bytes=None):
        super().__init__(parent)
        self.setObjectName("file_bubble")
        self.file_id = file_id
        self.filename = filename
        self.file_size = file_size
        self.is_own = is_own
        self.sender_name = sender_name
        self.time_str = time_str or datetime.now().strftime("%H:%M")
        self.caption = caption
        self._completed = False
        self._image_loaded = False
        self._progress_complete = False
        self.file_path = ""
        self._interactable = False
        self.image_bytes = b''
        
        # Для ETA и скорости
        self._start_time = None
        self._on_cancel_callback = None

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

        header = QLabel(f"{self.sender_name} · {self.time_str}")
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

        # --- ETA и скорость ---
        self._eta_label = QLabel()
        self._eta_label.setStyleSheet("color: #888888; font-size: 11px;")
        self._eta_label.hide()
        layout.addWidget(self._eta_label)

        # --- Кнопка отмены ---
        self._cancel_btn = QPushButton("✕ Отменить")
        self._cancel_btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #ff6b6b;
                border: 1px solid #ff6b6b;
                border-radius: 6px;
                font-size: 11px;
                padding: 2px 8px;
            }
            QPushButton:hover { background-color: #ff6b6b; color: #000000; }
        """)
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self._cancel_btn)

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

    # --- Thumbnail loading ---

    def _load_thumbnail(self, thumbnail_bytes: bytes):
        self.thumbnail_blurred = QPixmap()
        pix = QPixmap()
        if pix.loadFromData(thumbnail_bytes):
            self.thumbnail_blurred = pix
            try:
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
            return
        self._image_loaded = True
        self.thumbnail_clear = self.original_pixmap.scaled(
            400, 400, Qt.KeepAspectRatio, Qt.SmoothTransformation
        )
        if self._progress_complete:
            self._reveal_timer.start(400)

    # --- Progress ---

    def update_progress(self, ratio):
        if self._completed:
            return
        
        percent = int(ratio * 100)
        
        if self.is_image:
            if hasattr(self, 'progress_indicator'):
                self.progress_indicator.set_progress(ratio)
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
                self._cancel_btn.hide()
                self._eta_label.hide()
                return

        # Показываем кнопку отмены только пока передача идет
        if not self._completed:
            self._cancel_btn.show()
            
            # --- Расчет ETA и скорости ---
            if self._start_time is None:
                self._start_time = time.time()
            else:
                elapsed = time.time() - self._start_time
                if elapsed > 0.5 and ratio > 0:
                    bytes_done = int(ratio * self.file_size)
                    speed_bps = bytes_done / elapsed
                    remaining_bytes = self.file_size - bytes_done
                    eta_seconds = remaining_bytes / speed_bps if speed_bps > 0 else 0

                    kb_s = speed_bps / 1024
                    if eta_seconds < 60:
                        eta_str = f"{int(eta_seconds)} сек"
                    else:
                        eta_str = f"{int(eta_seconds / 60)} мин"

                    self._eta_label.setText(f"{kb_s:.0f} KB/s · осталось {eta_str}")
                    self._eta_label.show()

    def set_progress_done(self):
        self._cancel_btn.hide()
        self._eta_label.hide()
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

    def _on_cancel_clicked(self):
        if self._on_cancel_callback:
            self._on_cancel_callback(self.file_id)
        self._cancel_btn.hide()
        self._eta_label.setText("Отменено")
        self._eta_label.show()
        if not self.is_image:
            self.progress_bar.setValue(0)
            self.progress_bar.hide()

    def set_cancel_callback(self, callback):
        self._on_cancel_callback = callback

    # --- Interaction ---

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
            except Exception:
                return
        if image_bytes:
            from .image_viewer import ImageViewer
            viewer = ImageViewer(image_bytes, self.filename, self)
            viewer.show()

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