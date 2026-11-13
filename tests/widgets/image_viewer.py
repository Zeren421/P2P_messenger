"""
widgets/image_viewer.py — Full-screen image viewer with zoom.
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap, QPainter, QColor
from PyQt5.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem, QFrame, QApplication

class ImageViewer(QDialog):
    def __init__(self, image_bytes, filename, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Title bar
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

        # Image view
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

        # Bottom bar
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
