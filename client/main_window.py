"""
main_window.py — Main ChatClient window class for the modular P2P chat client.
"""

import sys
import os
import json
import random
import threading
import time
import logging
from datetime import datetime

from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QEvent
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTextEdit,
    QPushButton, QLabel, QFrame, QScrollArea, QFileDialog, QMessageBox,
    QInputDialog, QSystemTrayIcon, QStyle, QGraphicsDropShadowEffect,
    QLineEdit, QApplication
)
from PyQt5.QtGui import QColor, QPalette

from connection_manager import ConnectionManager
from widgets import MessageBubble, FileBubble, TypingIndicator, ImageViewer
from helpers import (
    ClipboardHelper, FileSenderHelper, MessageRenderer, ScrollManager
)
from helpers.ui_utils import USER_COLORS, resource_path
from helpers.history import (
    save_history, load_history, save_username, load_username,
    save_peer, load_peer, clear_history
)

# ============================================================
#  ЛОГИРОВАНИЕ В ФАЙЛ
# ============================================================

logging.basicConfig(
    filename=f"p2p_log_{os.getpid()}.txt",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    encoding="utf-8",
)
log = logging.getLogger(__name__)

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

DEBUG = True
SOUND_PATH = resource_path("din-don.wav")
SIDE_PORTS = {"a": 9090, "b": 9091}
TOPIC_PREFIX = "p2pchat"


class ChatClient(QMainWindow):
    # Сигналы
    message_received = pyqtSignal(str, str, datetime)
    typing_signal = pyqtSignal(bool, str)
    status_signal = pyqtSignal(str, str)
    peer_name_signal = pyqtSignal(str)
    password_retry_signal = pyqtSignal(int)
    file_progress_signal = pyqtSignal(int, float, bool)
    file_error_signal = pyqtSignal(int, str)
    file_info_signal = pyqtSignal(int, str, int, bytes, str)
    message_failed_signal = pyqtSignal(str)
    file_received_signal = pyqtSignal(int, str, str)
    file_queued_signal = pyqtSignal(int, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        log.info("ChatClient __init__ started")

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
        self._connection_type = ""
        self._last_manual_reconnect = 0.0
        self.user_colors = {}
        self.active_file_bubbles = {}
        self.last_file_progress = {}
        self._file_is_own = {}
        self._last_progress_ui = {}
        self.queued_file_ids = set()  # <--- НОВОЕ: храним ID файлов в очереди
        self.last_typing_sent = 0
        self.running = True
        self._loading_history = False

        log.info("Initializing UI...")
        self.init_ui()
        log.info("Initializing helpers...")
        self.init_helpers()
        log.info("Initializing signals...")
        self.init_signals()

        self.typing_indicator = TypingIndicator(self)
        self.typing_indicator.status_changed.connect(self.show_typing_status)

        log.info("Loading history...")
        self.load_history()
        log.info("Asking username and peer...")
        self.ask_username_and_peer()
        log.info("Starting connection...")
        self.start_connection()

        self.setAcceptDrops(True)
        log.info("ChatClient __init__ finished")

    # ============================================================
    #  UI
    # ============================================================

    def init_ui(self):
        log.info("init_ui started")
        self.setWindowTitle("Мессенджер v6.7 (P2P)")
        self.setGeometry(100, 100, 940, 720)
        self.setMinimumSize(700, 500)

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.tray_icon.setVisible(True)

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("background-color: #000000;")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Header
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
            QPushButton:hover { background-color: #3d3d5c; }
            QPushButton:disabled { color: #666666; border-color: #333333; }
        """)
        self.reconnect_btn.clicked.connect(self.reconnect)
        self.reconnect_btn.setEnabled(False)

        self.clear_button = QPushButton("🗑 Очистить")
        self.clear_button.setFixedHeight(38)
        self.clear_button.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #ff6b6b;
                border: 2px solid #ff6b6b;
                border-radius: 12px;
                font-size: 14px;
                font-weight: bold;
                padding: 0 14px;
            }
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

        # Chat area
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
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(20)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 180))
        self.scroll_down_btn.setGraphicsEffect(shadow)
        self.scroll_down_btn.hide()

        # Input area
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
        palette = self.input_field.palette()
        palette.setColor(QPalette.PlaceholderText, QColor("#777777"))
        self.input_field.setPalette(palette)
        self.input_field.installEventFilter(self)
        self.input_field.setAcceptDrops(False)
        self.input_field.textChanged.connect(self.on_text_changed)

        self.send_button = QPushButton("📤 Отправить")
        self.send_button.setFixedHeight(55)
        self.send_button.setStyleSheet("""
            QPushButton {
                background-color: #6c5ce7;
                color: white;
                border: none;
                border-radius: 16px;
                font-size: 18px;
                font-weight: bold;
                padding: 0 20px;
            }
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
            QPushButton:hover { background-color: #2d2d44; }
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

        self.set_send_enabled(False)
        log.info("init_ui finished")

    def init_helpers(self):
        log.info("init_helpers started")
        self.message_renderer = MessageRenderer(self.chat_layout)
        self.scroll_manager = ScrollManager(self.scroll_area, self.scroll_down_btn)
        self.file_sender = FileSenderHelper(
            parent_widget=self,
            send_file_callback=self._send_file_impl,
            add_bubble_callback=self._add_file_bubble_to_chat,
        )
        self.clipboard_helper = ClipboardHelper(
            send_file_callback=self.file_sender.send_file_from_path,
            send_image_bytes_callback=self._send_image_bytes_impl
        )
        log.info("init_helpers finished")

    def init_signals(self):
        log.info("init_signals started")
        self.message_received.connect(self.add_message_safe)
        self.status_signal.connect(self.set_status)
        self.typing_signal.connect(self.handle_typing)
        self.peer_name_signal.connect(self._on_peer_name_received)
        self.password_retry_signal.connect(self._on_password_retry)
        self.file_progress_signal.connect(self._on_file_progress_gui)
        self.file_error_signal.connect(self._on_file_error_gui)
        self.file_info_signal.connect(self._on_file_info)
        self.message_failed_signal.connect(self._on_message_failed_gui)
        self.file_received_signal.connect(self._on_file_received_gui)
        self.file_queued_signal.connect(self._on_file_queued_gui)
        log.info("init_signals finished")

    # ============================================================
    #  Хелперы-прокси
    # ============================================================

    def attach_file(self):
        log.debug("attach_file called")
        self.file_sender.attach_file()

    def handle_paste(self):
        log.debug("handle_paste called")
        return self.clipboard_helper.try_handle_paste()

    def _send_file_impl(self, filepath, caption, thumbnail):
        log.debug(f"_send_file_impl: {filepath}")
        if not self.connected or self.conn_manager is None:
            raise Exception("Not connected")
        return self.conn_manager.send_file(filepath, caption, thumbnail)

    def _send_image_bytes_impl(self, image_bytes, filename):
        log.debug(f"_send_image_bytes_impl: {filename}")
        if not self.connected or self.conn_manager is None:
            raise Exception("Not connected")
        return self.conn_manager.send_image_bytes(image_bytes, filename)

    def _add_file_bubble_to_chat(self, bubble, is_own):
        file_id = getattr(bubble, 'file_id', None)
        if file_id is not None:
            self._file_is_own[file_id] = is_own
            self.active_file_bubbles[file_id] = bubble

            pending_ratio = self.last_file_progress.pop(file_id, None)
            if pending_ratio is not None:
                try:
                    bubble.update_progress(pending_ratio)
                except Exception as e:
                    log.warning(f"_add_file_bubble_to_chat: update_progress error: {e}")

            # Если файл в очереди, сразу показываем плашку
            if file_id in self.queued_file_ids:
                self.queued_file_ids.remove(file_id)
                try:
                    bubble.set_queued()
                except Exception as e:
                    log.warning(f"_add_file_bubble_to_chat: set_queued error: {e}")

        align = "right" if is_own else "left"
        self.message_renderer.add_message(bubble, datetime.now(), align)
        self.scroll_manager.on_new_message(is_own)

    # ============================================================
    #  Сеть
    # ============================================================

    def start_connection(self):
        log.info("start_connection started")
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
            on_file_progress=self._on_file_progress_signal_emitter,
            on_file_error=self._on_file_error_signal_emitter,
            on_file_info=self._on_file_info_callback,
            on_file_queued=self._on_file_queued_signal_emitter,
        )
        self.conn_manager.set_password(self.password)
        log.info("ConnectionManager created, starting connect thread")
        threading.Thread(target=self._do_connect, daemon=True).start()

    def _do_connect(self):
        log.info("_do_connect started")
        ok = self.conn_manager.connect()
        log.info(f"_do_connect result: {ok}")
        if ok:
            self.connected = True
            self.conn_manager.send_name(self.my_username)
            self.set_send_enabled(True)
            log.info("_do_connect: connected successfully, UI enabled")
        else:
            log.warning("_do_connect: connection failed")

    def reconnect(self):
        log.info("reconnect called")
        self._last_manual_reconnect = time.time()
        self.connected = False
        self.set_send_enabled(False)
        self.add_system_message("Переподключение...")
        threading.Thread(target=self._do_reconnect, daemon=True).start()

    def _do_reconnect(self):
        log.info("_do_reconnect started")
        if self.conn_manager is not None:
            try:
                self.conn_manager.send_bye()
                time.sleep(0.2)
            except Exception as e:
                log.warning(f"_do_reconnect: send_bye error: {e}")
            try:
                self.conn_manager.disconnect()
            except Exception as e:
                log.warning(f"_do_reconnect: disconnect error: {e}")
            self.conn_manager = None
        self.start_connection()

    # ============================================================
    #  Колбэки от ConnectionManager (фоновый поток!)
    # ============================================================

    def _on_peer_message(self, text):
        log.debug(f"_on_peer_message: {text[:50]}...")
        self.message_received.emit(self.peer_name, text, datetime.now())

    def _on_peer_typing(self, is_typing, sender_name):
        log.debug(f"_on_peer_typing: {is_typing} from {sender_name}")
        self.typing_signal.emit(is_typing, sender_name or self.peer_name)

    def _on_message_failed(self, text):
        log.warning(f"_on_message_failed: {text}")
        self.message_failed_signal.emit(text)

    def _on_status(self, message):
        log.info(f"_on_status: {message}")
        low = message.lower()

        if "connected via udp" in low or "direct p2p" in low:
            color = "#4caf50"
            status_text = "✅ Прямое (UDP)"
            self._connection_type = "udp"
        elif "connected via relay" in low:
            color = "#ffb142"
            status_text = "🔄 Через Relay"
            self._connection_type = "relay"
        elif "connecting" in low or "punch" in low:
            color = "#ffb142"
            status_text = "⏳ Подключение..."
            self._connection_type = ""
        elif "fail" in low or "error" in low:
            color = "#ff6b6b"
            status_text = "❌ Ошибка"
            self._connection_type = ""
        elif "disconnected" in low:
            color = "#ff6b6b"
            status_text = "❌ Не в сети"
            self._connection_type = ""
        elif "no password required" in low:
            color = "#4caf50"
            status_text = "🔓 Без пароля"
            self._connection_type = ""
        elif "password verified" in low:
            color = "#4caf50"
            status_text = "🔒 Пароль принят"
            self._connection_type = ""
        else:
            color = "#888888"
            status_text = "💬 " + message[:40]
            self._connection_type = ""

        self.status_signal.emit(status_text, color)

    def _on_peer_name(self, name):
        log.info(f"_on_peer_name: {name}")
        if name and name != self.peer_name:
            self.peer_name = name
            self.peer_name_signal.emit(name)

    def _on_password_retry_callback(self, attempts_left):
        log.warning(f"_on_password_retry_callback: {attempts_left} attempts left")
        self.password_retry_signal.emit(attempts_left)

    def _on_file_received(self, file_id, filename, save_path):
        log.info(f"_on_file_received: {filename} -> {save_path}")
        self.file_received_signal.emit(file_id, filename, save_path)

    def _on_file_progress_signal_emitter(self, file_id, ratio, is_outgoing):
        self.file_progress_signal.emit(file_id, ratio, is_outgoing)

    def _on_file_error_signal_emitter(self, file_id, reason):
        log.error(f"_on_file_error_signal_emitter: file_id={file_id}, reason={reason}")
        self.file_error_signal.emit(file_id, reason)

    def _on_file_info_callback(self, file_id, filename, total_size, thumbnail, caption):
        log.info(f"_on_file_info_callback: {filename} ({total_size} bytes)")
        self.file_info_signal.emit(file_id, filename, total_size, thumbnail, caption)

    def _on_file_queued_signal_emitter(self, file_id, reason):
        self.file_queued_signal.emit(file_id, reason)

    # ============================================================
    #  Обработчики сигналов (GUI-поток)
    # ============================================================

    def _on_peer_name_received(self, name):
        log.debug(f"_on_peer_name_received: {name}")
        self.peer_name = name

    def _on_password_retry(self, attempts_left):
        log.warning(f"_on_password_retry: {attempts_left} attempts left")
        QMessageBox.warning(self, "Пароль", f"Неверный пароль. Осталось попыток: {attempts_left}")

    def _on_message_failed_gui(self, text):
        self.add_system_message(f"Не удалось доставить: «{text}»")

    def _on_file_progress_gui(self, file_id, ratio, is_outgoing):
        now = time.time()
        last = self._last_progress_ui.get(file_id)
        if last is not None:
            last_time, last_ratio = last
            if ratio < 1.0 and now - last_time < 0.1:
                return
            if ratio < last_ratio and ratio < 1.0:
                return
        self._last_progress_ui[file_id] = (now, ratio)

        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            try:
                bubble.update_progress(ratio)
            except Exception as e:
                log.warning(f"_on_file_progress_gui: update error: {e}")
        else:
            prev = self.last_file_progress.get(file_id, 0.0)
            self.last_file_progress[file_id] = max(prev, ratio)

    def _on_file_error_gui(self, file_id, reason):
        log.error(f"_on_file_error_gui: file_id={file_id}, reason={reason}")
        self.add_system_message(f"❌ Ошибка файла: {reason}")
        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            try:
                bubble.set_progress_done()
            except Exception:
                pass
        self.active_file_bubbles.pop(file_id, None)
        self.last_file_progress.pop(file_id, None)
        self._last_progress_ui.pop(file_id, None)
        self.queued_file_ids.discard(file_id)  # <--- Чистим

    def _on_file_queued_gui(self, file_id, reason):
        bubble = self.active_file_bubbles.get(file_id)
        if bubble and hasattr(bubble, 'set_queued'):
            bubble.set_queued()
        else:
            # Если bubble ещё не создан, запоминаем ID
            self.queued_file_ids.add(file_id)  # <--- Запоминаем

    def _on_file_received_gui(self, file_id, filename, save_path):
        self.add_system_message(f"📥 Получен файл: {filename} (сохранён в {save_path})")
        bubble = self.active_file_bubbles.get(file_id)
        if bubble:
            bubble.set_file_path(save_path)
            try:
                bubble.set_progress_done()
            except Exception as e:
                log.warning(f"_on_file_received_gui: set_progress_done error: {e}")
        self.active_file_bubbles.pop(file_id, None)
        self.last_file_progress.pop(file_id, None)
        self._last_progress_ui.pop(file_id, None)
        self.queued_file_ids.discard(file_id)  # <--- Чистим

    def _on_file_info(self, file_id, filename, total_size, thumbnail, caption):
        log.info(f"_on_file_info: {filename}")
        bubble = FileBubble(
            sender_name=self.peer_name,
            file_id=file_id,
            filename=filename,
            file_size=total_size,
            thumbnail=thumbnail,
            caption=caption,
            is_own=False,
            time_str=datetime.now().strftime("%H:%M"),
            parent=self,
            original_bytes=None,
        )
        bubble.set_cancel_callback(self._on_file_cancel)
        self._add_file_bubble_to_chat(bubble, is_own=False)
        self.play_notification_sound()
        if not self.isActiveWindow():
            self.show_tray_notification("📎 Получен файл", filename)

    # ============================================================
    #  UI-методы
    # ============================================================

    def set_send_enabled(self, enabled):
        log.debug(f"set_send_enabled: {enabled}")
        self.input_field.setEnabled(enabled)
        self.send_button.setEnabled(enabled)
        self.attach_button.setEnabled(enabled)

    def set_status(self, text, color=None):
        log.debug(f"set_status: {text}")
        self.status_text.setText(text)
        if color:
            self.status_dot.setStyleSheet(f"color: {color}; font-size: 12px;")

    def show_typing_status(self, text):
        self.typing_status_label.setText(text)
        self.typing_status_label.setVisible(bool(text))

    def handle_typing(self, is_typing, sender):
        if is_typing:
            self.typing_indicator.show_status(sender)
        else:
            self.typing_indicator.hide_status()

    # ============================================================
    #  Сообщения
    # ============================================================

    def add_system_message(self, text):
        log.debug(f"add_system_message: {text}")
        bubble = MessageBubble("", text, datetime.now().strftime("%H:%M"), is_system=True)
        self.message_renderer.add_message(bubble, datetime.now(), "center")
        self.scroll_manager.on_new_message(False)

    def add_message_safe(self, sender, text, timestamp):
        log.debug(f"add_message_safe: {sender}: {text[:50]}...")
        is_own = (sender == self.my_username)
        align = "right" if is_own else "left"
        color = self.get_user_color(sender)
        bubble = MessageBubble(sender, text, timestamp.strftime("%H:%M"), is_own=is_own, color=color)
        self.message_renderer.add_message(bubble, timestamp, align)
        self.scroll_manager.on_new_message(is_own)

        if not self._loading_history:
            self.save_to_history(sender, text)
            if not is_own and not self.isActiveWindow():
                self.play_notification_sound()
                self.show_tray_notification(sender, text)

    def get_user_color(self, sender):
        if sender not in self.user_colors:
            available = [c for c in USER_COLORS if c not in self.user_colors.values()]
            if not available:
                available = USER_COLORS
            self.user_colors[sender] = random.choice(available)
        return self.user_colors[sender]

    # ============================================================
    #  История
    # ============================================================

    def load_history(self):
        log.info("load_history started")
        entries = load_history()
        log.info(f"load_history: {len(entries)} entries loaded")

        self._loading_history = True
        try:
            for entry in entries:
                sender = entry.get("sender", "")
                text = entry.get("text", "")
                is_system = entry.get("is_system", False)
                if is_system:
                    self.add_system_message(text)
                else:
                    try:
                        timestamp = datetime.strptime(entry.get("date", ""), "%Y-%m-%d %H:%M")
                    except Exception:
                        timestamp = datetime.now()
                    self.add_message_safe(sender, text, timestamp)
        finally:
            self._loading_history = False

        QTimer.singleShot(0, self.scroll_manager._scroll_to_bottom)

    def save_to_history(self, sender, text, is_system=False):
        now = datetime.now()
        entry = {
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "sender": sender,
            "text": text,
            "is_own": (sender == self.my_username),
            "is_system": is_system,
        }
        save_history(entry)

    # ============================================================
    #  Настройка
    # ============================================================

    def ask_username_and_peer(self):
        log.info("ask_username_and_peer started")
        saved = load_username()
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
        save_username(self.my_username)
        log.info(f"Username: {self.my_username}")

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
        log.info(f"Peer username: {self.peer_username}")

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
        log.info(f"Password set: {'yes' if self.password else 'no'}")

        my_encoded = self.encode_username(self.my_username)
        peer_encoded = self.encode_username(self.peer_username)

        usernames = sorted([my_encoded, peer_encoded])
        base_topic = f"{TOPIC_PREFIX}_{usernames[0]}_{usernames[1]}"

        self.side = "a" if my_encoded == usernames[0] else "b"
        self.my_topic = f"{base_topic}_{self.side}"
        peer_side = "b" if self.side == "a" else "a"
        self.peer_topic = f"{base_topic}_{peer_side}"
        self.local_port = SIDE_PORTS[self.side]
        log.info(f"my_topic: {self.my_topic}, peer_topic: {self.peer_topic}, port: {self.local_port}")

        self.add_system_message(f"Ваш ник: {self.my_username} (код: {my_encoded})")
        self.add_system_message(f"Собеседник: {self.peer_username} (код: {peer_encoded})")
        self.add_system_message(f"Канал: {base_topic}, сторона {self.side.upper()} (порт {self.local_port})")

    @staticmethod
    def encode_username(username: str) -> str:
        try:
            return username.encode('idna').decode('ascii')
        except Exception:
            import re
            return re.sub(r'[^a-zA-Z0-9_]', '_', username)

    # ============================================================
    #  События
    # ============================================================

    def keyPressEvent(self, event):
        if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V:
            if self.handle_paste():
                return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.file_sender.handle_drop(urls)
            event.acceptProposedAction()

    def eventFilter(self, obj, event):
        if obj == self.input_field and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.send_message()
                    return True
            if event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V:
                if self.handle_paste():
                    return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        log.info("closeEvent called")
        self.running = False
        if self.conn_manager is not None:
            try:
                self.conn_manager.send_bye()
            except Exception as e:
                log.warning(f"closeEvent: send_bye error: {e}")
            self.conn_manager.disconnect()
        event.accept()

    # ============================================================
    #  Вспомогательные
    # ============================================================

    def send_message(self):
        msg = self.input_field.toPlainText().strip()
        if not msg or not self.connected:
            return
        log.debug(f"send_message: {msg[:50]}...")
        self.add_message_safe(self.my_username, msg, datetime.now())
        self.conn_manager.send_message(msg)
        self.conn_manager.send_typing(False, self.my_username)
        self.input_field.clear()

    def on_text_changed(self):
        if not self.connected:
            return
        now = time.time()
        if now - self.last_typing_sent >= 2:
            self.last_typing_sent = now
            self.conn_manager.send_typing(True, self.my_username)

    def play_notification_sound(self):
        if not HAS_WINSOUND:
            return
        if not os.path.isfile(SOUND_PATH):
            return
        try:
            winsound.PlaySound(SOUND_PATH, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        except Exception:
            pass

    def show_tray_notification(self, sender, text):
        preview = text[:60] + ("..." if len(text) > 60 else "")
        try:
            self.tray_icon.showMessage(f"✉️ {sender}", preview, QSystemTrayIcon.Information, 3000)
        except Exception:
            pass

    def _on_file_cancel(self, file_id: int):
        log.info(f"_on_file_cancel: file_id={file_id}")
        is_own = self._file_is_own.get(file_id, True)
        if self.conn_manager:
            try:
                self.conn_manager.cancel_file(file_id, is_outgoing=is_own)
            except Exception as e:
                log.warning(f"_on_file_cancel: cancel_file error: {e}")
        self.active_file_bubbles.pop(file_id, None)
        self.last_file_progress.pop(file_id, None)
        self._last_progress_ui.pop(file_id, None)
        self.queued_file_ids.discard(file_id)  # <--- Чистим
        self.add_system_message("❌ Передача файла отменена")

    def clear_chat(self):
        log.info("clear_chat called")
        reply = QMessageBox.question(
            self, "Очистка чата",
            "Очистить историю сообщений?\n(только на вашем устройстве)",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply == QMessageBox.Yes:
            while self.chat_layout.count() > 1:
                item = self.chat_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            self.message_renderer.reset()
            self.user_colors = {}
            self.active_file_bubbles.clear()
            self.last_file_progress.clear()
            self._file_is_own.clear()
            self._last_progress_ui.clear()
            self.queued_file_ids.clear()  # <--- Чистим
            clear_history()
            self.add_system_message("Чат очищен")
            self.scroll_manager._scroll_to_bottom()