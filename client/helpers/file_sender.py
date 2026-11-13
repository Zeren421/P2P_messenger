"""
helpers/file_sender.py — file sending logic.
No direct imports from widgets to avoid circular dependencies.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Callable, Optional

from PyQt5.QtWidgets import QFileDialog, QWidget
from PyQt5.QtCore import QTimer

# Импортируем generate_thumbnail_from_file из file_transfer.py
from file_transfer import generate_thumbnail_from_file


class FileSenderHelper:
    def __init__(
        self,
        parent_widget: QWidget,
        send_file_callback: Callable[[str, str, Optional[bytes]], int],
        add_bubble_callback: Callable[[object, bool], None],
        file_filter: str = "All files (*.*);;Images (*.png *.jpg *.jpeg *.gif *.bmp);;Documents (*.pdf *.doc *.txt)",
    ):
        self.parent_widget = parent_widget
        self.send_file_callback = send_file_callback
        self.add_bubble_callback = add_bubble_callback
        self.file_filter = file_filter
        self.active_bubbles = {}

    def send_file_from_path(self, filepath: str, caption: str = "") -> bool:
        if not os.path.isfile(filepath):
            return False

        try:
            size = os.path.getsize(filepath)
            if size > 2_147_483_647:
                return False
        except OSError:
            return False

        filename = os.path.basename(filepath)
        is_image = filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'))

        thumbnail = b''
        original_bytes = b''
        if is_image:
            try:
                from PIL import Image
                thumbnail = generate_thumbnail_from_file(filepath, max_size=200)
                with open(filepath, 'rb') as f:
                    original_bytes = f.read()
            except Exception as e:
                print(f"[FileSender] Thumbnail error: {e}")

        try:
            file_id = self.send_file_callback(filepath, caption, thumbnail)
        except Exception as e:
            print(f"[FileSender] Send error: {e}")
            return False

        from widgets.bubbles import FileBubble

        time_str = datetime.now().strftime("%H:%M")
        bubble = FileBubble(
            sender_name="Я",
            file_id=file_id,
            filename=filename,
            file_size=size,
            thumbnail=thumbnail,
            caption=caption,
            is_own=True,
            time_str=time_str,
            parent=self.parent_widget,
            original_bytes=original_bytes,
        )
        self.active_bubbles[file_id] = bubble
        self.add_bubble_callback(bubble, is_own=True)

        QTimer.singleShot(50, lambda fid=file_id, fp=filepath: self._set_bubble_path(fid, fp))
        return True

    def attach_file(self) -> None:
        filepath, _ = QFileDialog.getOpenFileName(
            self.parent_widget,
            "Выберите файл для отправки",
            "",
            self.file_filter
        )
        if filepath:
            self.send_file_from_path(filepath)

    def handle_drop(self, urls: list) -> None:
        for url in urls:
            path = url.toLocalFile()
            if path and os.path.isfile(path):
                self.send_file_from_path(path)

    def _set_bubble_path(self, file_id: int, filepath: str) -> None:
        bubble = self.active_bubbles.get(file_id)
        if bubble:
            bubble.set_file_path(filepath)
