"""
helpers/file_sender.py — file sending logic.

Изменения:
  * Убран дублирующий active_bubbles — берём bubble из parent_widget.active_file_bubbles.
  * original_bytes читается в память ТОЛЬКО для маленьких картинок (<= MAX_ORIGINAL_BYTES),
    иначе — пустой, показывается размытая миниатюра, чёткая появится после приёма.
  * _set_bubble_path вызывается сразу, без QTimer (bubble уже в чате после callback).
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

from PyQt5.QtWidgets import QFileDialog, QWidget

from file_transfer import generate_thumbnail_from_file

# Если картинка больше этого — не читаем её в память ради «чёткой» миниатюры
MAX_ORIGINAL_BYTES = 5 * 1024 * 1024   # 5 МБ


class FileSenderHelper:
    def __init__(
        self,
        parent_widget: QWidget,
        send_file_callback: Callable[[str, str, Optional[bytes]], int],
        add_bubble_callback: Callable[[object, bool], None],
        file_filter: str = (
            "All files (*.*);;"
            "Images (*.png *.jpg *.jpeg *.gif *.bmp *.webp);;"
            "Documents (*.pdf *.doc *.txt)"
        ),
    ):
        self.parent_widget = parent_widget
        self.send_file_callback = send_file_callback
        self.add_bubble_callback = add_bubble_callback
        self.file_filter = file_filter

    def send_file_from_path(self, filepath: str, caption: str = "") -> bool:
        if not os.path.isfile(filepath):
            return False

        try:
            size = os.path.getsize(filepath)
            if size > 50 * 1024 * 1024 * 1024:  # <--- 50 ГБ вместо 2 ГБ
                return False
        except OSError:
            return False
        filename = os.path.basename(filepath)
        is_image = filename.lower().endswith(
            ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp')
        )

        thumbnail = b""
        original_bytes = b""
        if is_image:
            try:
                thumbnail = generate_thumbnail_from_file(filepath, max_size=200)
                # Ради «чёткой» миниатюры тащим оригинал в RAM только если он маленький
                if size <= MAX_ORIGINAL_BYTES:
                    with open(filepath, 'rb') as f:
                        original_bytes = f.read()
            except Exception as e:
                print(f"[FileSender] Thumbnail error: {e}")

        try:
            file_id = self.send_file_callback(filepath, caption, thumbnail)
        except Exception as e:
            print(f"[FileSender] Send error: {e}")
            # Показать пользователю, что не ушло
            try:
                from PyQt5.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self.parent_widget, "Отправка файла",
                    f"Не удалось отправить файл:\n{e}",
                )
            except Exception:
                pass
            return False

        from widgets.bubbles import FileBubble

        bubble = FileBubble(
            sender_name="Я",
            file_id=file_id,
            filename=filename,
            file_size=size,
            thumbnail=thumbnail,
            caption=caption,
            is_own=True,
            time_str=datetime.now().strftime("%H:%M"),
            parent=self.parent_widget,
            original_bytes=original_bytes,
        )

        # Колбэк отмены — ДО добавления в чат, чтобы кнопка сразу работала
        if hasattr(self.parent_widget, "_on_file_cancel"):
            bubble.set_cancel_callback(self.parent_widget._on_file_cancel)

        # Добавляем в чат (main_window сам положит в active_file_bubbles)
        self.add_bubble_callback(bubble, is_own=True)

        # Включаем кнопку «Открыть» для исходящего bubble — файл уже на диске
        bubble.set_file_path(filepath)
        return True

    def attach_file(self) -> None:
        filepath, _ = QFileDialog.getOpenFileName(
            self.parent_widget,
            "Выберите файл для отправки",
            "",
            self.file_filter,
        )
        if filepath:
            self.send_file_from_path(filepath)

    def handle_drop(self, urls: list) -> None:
        for url in urls:
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                self.send_file_from_path(path)