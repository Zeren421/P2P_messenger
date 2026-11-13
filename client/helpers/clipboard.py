"""
helpers/clipboard.py — clipboard paste/image handling.
Uses QBuffer to send images directly from memory.
"""

from __future__ import annotations

import os
from typing import Callable, Optional

from PyQt5.QtCore import QBuffer, QByteArray, QIODevice
from PyQt5.QtGui import QImage, QPixmap
from PyQt5.QtWidgets import QApplication


class ClipboardHelper:
    """
    Handles pasting from clipboard: images, files, and text paths.
    Requires callbacks for sending files and image bytes.
    """

    def __init__(
        self,
        send_file_callback: Callable[[str], None],
        send_image_bytes_callback: Callable[[bytes, str], None],
    ):
        """
        Args:
            send_file_callback: Called with file path when a file is pasted.
            send_image_bytes_callback: Called with (image_bytes, filename) when an image is pasted.
        """
        self.send_file_callback = send_file_callback
        self.send_image_bytes_callback = send_image_bytes_callback

    def try_handle_paste(self) -> bool:
        """
        Try to extract image or file from clipboard and send it.
        Returns True if something was sent, False if clipboard contains only text.
        """
        clipboard = QApplication.clipboard()
        mime = clipboard.mimeData()

        # 1. Try to get image (most reliable: clipboard.pixmap())
        pixmap = None
        if mime.hasImage():
            image = mime.imageData()
            if image and not image.isNull():
                pixmap = QPixmap.fromImage(image)

        if pixmap is None or pixmap.isNull():
            try:
                pixmap = clipboard.pixmap()
            except Exception:
                pass

        if pixmap and not pixmap.isNull():
            self._send_pixmap(pixmap)
            return True

        # 2. Try to get file from URLs
        if mime.hasUrls():
            for url in mime.urls():
                path = url.toLocalFile()
                if path and os.path.isfile(path):
                    self.send_file_callback(path)
                    return True

        # 3. Try to get file path from text (e.g., Windows Screen Clip)
        if mime.hasText():
            text = mime.text().strip()
            if os.path.isfile(text):
                # If it's an image file, try to load it as QImage first
                ext = os.path.splitext(text)[1].lower()
                if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
                    image = QImage(text)
                    if not image.isNull():
                        self._send_qimage(image)
                        return True
                self.send_file_callback(text)
                return True

        # Nothing matched
        return False

    def _send_pixmap(self, pixmap: QPixmap) -> None:
        """Convert QPixmap to bytes and send via callback."""
        from datetime import datetime

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        filename = f"Screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        self.send_image_bytes_callback(bytes(ba), filename)

    def _send_qimage(self, image: QImage) -> None:
        """Convert QImage to bytes and send via callback."""
        pixmap = QPixmap.fromImage(image)
        self._send_pixmap(pixmap)

