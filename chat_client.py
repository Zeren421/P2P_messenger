import sys
import os
import json
import random
import socket
import threading
import time
from datetime import datetime
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtGui import *

try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# ===================================================
#  НАСТРОЙКИ
# ===================================================
SERVER_IP = "26.90.78.166"
SERVER_PORT = 8080
DEBUG = False

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))

SOUND_PATH = os.path.join(APP_DIR, "din-don.wav")
HISTORY_FILE = os.path.join(APP_DIR, "history.jsonl")
NAME_FILE = os.path.join(APP_DIR, "name.txt")
HISTORY_LIMIT = 1000

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


# ===================================================
#  ИНДИКАТОР «ПЕЧАТАЕТ...» (БЕЗОПАСНЫЙ ДЛЯ ГЛАВНОГО ПОТОКА)
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
        """Показывает статус печатания от указанного пользователя"""
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
        """Скрывает статус"""
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
#  ВИДЖЕТ ПУЗЫРЯ
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
#  ОСНОВНОЙ КЛИЕНТ
# ===================================================
class ChatClient(QMainWindow):
    message_signal = pyqtSignal(str, str, bool, bool, str)
    status_signal = pyqtSignal(str, str)
    typing_signal = pyqtSignal(str)  # <-- сигнал для безопасной обработки typing

    def __init__(self):
        super().__init__()
        self.auto_scroll = True
        self.unread_count = 0
        self.loading_history = False
        self.user_colors = {}

        saved_name = self.load_name()
        self.name = saved_name if saved_name else "Аноним"

        self.init_ui()
        self.init_signals()
        self.load_history()
        self.init_socket()

        self.typing_indicator = TypingIndicator(self)
        self.typing_indicator.status_changed.connect(self.show_typing_status)
        self.last_typing_sent = 0

    # ===================================================
    #  ИНТЕРФЕЙС
    # ===================================================
    def init_ui(self):
        self.setWindowTitle("Мессенджер v4.2")
        self.setGeometry(100, 100, 940, 720)
        self.setMinimumSize(700, 500)
        self.running = True
        self.connected = False
        self.last_date = ""
        self.buffer = ""

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(self.style().standardIcon(QStyle.SP_MessageBoxInformation))
        self.tray_icon.setVisible(True)

        central = QWidget()
        self.setCentralWidget(central)
        central.setStyleSheet("background-color: #000000;")
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # Шапка
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
        header_layout.addWidget(self.clear_button)

        # Статус печатает
        self.typing_status_label = QLabel("")
        self.typing_status_label.setStyleSheet("color: #888888; font-size: 13px; background-color: #0a0a0a; padding: 4px 10px; border-radius: 8px;")
        self.typing_status_label.setAlignment(Qt.AlignLeft)

        # Чат
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

        # Кнопка "Вниз"
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

        # Поле ввода
        self.input_field = QTextEdit()
        self.input_field.setPlaceholderText("Введите сообщение... (Shift+Enter - перенос, Enter - отправка)")
        self.input_field.setFixedHeight(90)
        self.input_field.setStyleSheet("""
            QTextEdit { background-color: #0a0a0a; color: #ffffff; border: 2px solid #333333;
                        border-radius: 16px; padding: 12px 18px; font-size: 17px; }
            QTextEdit:focus { border: 2px solid #6c5ce7; background-color: #0d0d0d; }
        """)
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

        input_card = QFrame()
        input_card.setObjectName("inputCard")
        input_card.setStyleSheet("QFrame#inputCard { background-color: #050505; border: 1px solid #1c1c1c; border-radius: 20px; }")
        input_layout = QHBoxLayout(input_card)
        input_layout.setContentsMargins(10, 10, 10, 10)
        input_layout.addWidget(self.input_field, 4)
        input_layout.addWidget(self.send_button, 1)

        main_layout.addWidget(header)
        main_layout.addWidget(self.chat_card, 1)
        main_layout.addWidget(self.typing_status_label)
        main_layout.addWidget(input_card)

        self.scroll_down_btn.show()
        QTimer.singleShot(50, self.reposition_scroll_button)
        self.scroll_down_btn.hide()

    # ===================================================
    #  СИГНАЛЫ
    # ===================================================
    def init_signals(self):
        self.message_signal.connect(self.add_message_safe)
        self.status_signal.connect(self.set_status)
        self.typing_signal.connect(self.handle_typing)

    # ===================================================
    #  СЛОТЫ ДЛЯ СИГНАЛОВ
    # ===================================================
    @pyqtSlot(str)
    def handle_typing(self, sender):
        self.typing_indicator.show_status(sender)

    def show_typing_status(self, text):
        self.typing_status_label.setText(text)
        self.typing_status_label.setVisible(bool(text))

    # ===================================================
    #  СТАТУС «ПЕЧАТАЕТ...» (ОТПРАВКА)
    # ===================================================
    def on_text_changed(self):
        if not self.connected:
            return
        now = time.time()
        if now - self.last_typing_sent >= 2:
            self.last_typing_sent = now
            try:
                self.sock.send(f"/typing {self.name}\n".encode('cp1251'))
            except:
                pass

    # ===================================================
    #  ОБРАБОТКА СООБЩЕНИЙ (С ПОДДЕРЖКОЙ /typing)
    # ===================================================
    def process_message(self, msg):
        if msg.startswith("/typing "):
            sender = msg[8:].strip()
            if sender and sender != self.name:
                self.typing_signal.emit(sender)
            return

        if ": " in msg:
            parts = msg.split(": ", 1)
            if len(parts) == 2:
                sender, text = parts
                if sender == self.name:
                    return
                color = self.get_user_color(sender)
                self.add_message(sender, text, is_own=False, is_system=False, color=color)
                return
        self.add_message("Сервер", msg, is_system=True)

    # ===================================================
    #  ИМЯ / ИСТОРИЯ / ЦВЕТА
    # ===================================================
    def save_name(self, name):
        try:
            with open(NAME_FILE, "w", encoding="utf-8") as f:
                f.write(name)
        except Exception as e:
            if DEBUG:
                print(f"Ошибка сохранения имени: {e}")

    def load_name(self):
        if not os.path.isfile(NAME_FILE):
            return None
        try:
            with open(NAME_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            if DEBUG:
                print(f"Ошибка загрузки имени: {e}")
            return None

    def get_user_color(self, sender):
        if sender not in self.user_colors:
            available = [c for c in USER_COLORS if c not in self.user_colors.values()]
            if not available:
                available = USER_COLORS
            self.user_colors[sender] = random.choice(available)
        return self.user_colors[sender]

    def save_to_history(self, sender, text, is_system=False):
        now = datetime.now()
        entry = {
            "date": now.strftime("%Y-%m-%d %H:%M"),
            "sender": sender,
            "text": text,
            "is_own": (sender == self.name),
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

    # ===================================================
    #  ВЫВОД В ЧАТ
    # ===================================================
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

    # ===================================================
    #  ПРОКРУТКА
    # ===================================================
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

    def init_socket(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(1.0)
        threading.Thread(target=self.connect_to_server, daemon=True).start()

    def eventFilter(self, obj, event):
        if obj == self.input_field and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & Qt.ShiftModifier):
                    self.send_message()
                    return True
        return super().eventFilter(obj, event)

    def connect_to_server(self):
        try:
            self.sock.connect((SERVER_IP, SERVER_PORT))
            self.connected = True
            self.status_signal.emit("в сети", "#4caf50")
            self.add_message("", "Подключено к серверу!", is_system=True)
            QMetaObject.invokeMethod(self, "ask_name", Qt.QueuedConnection)
            threading.Thread(target=self.receive_messages, daemon=True).start()
        except Exception as e:
            self.status_signal.emit("не в сети", "#ff6b6b")
            self.add_message("", f"Ошибка подключения: {e}", is_system=True)

    @pyqtSlot()
    def ask_name(self):
        dialog = QInputDialog(self)
        dialog.setWindowTitle("👤 Вход в чат")
        dialog.setLabelText("Введите ваше имя:")
        dialog.setTextValue(self.name if self.name != "Аноним" else "")
        dialog.resize(450, 180)
        ok = dialog.exec_()
        name = dialog.textValue()
        if ok and name.strip():
            self.name = name.strip()
        else:
            self.name = "Аноним"
        self.save_name(self.name)
        try:
            self.sock.send(self.name.encode('cp1251'))
            self.add_message("", f"Вы вошли как {self.name}", is_system=True)
        except Exception as e:
            self.add_message("", f"Ошибка отправки имени: {e}", is_system=True)

    def receive_messages(self):
        while self.running and self.connected:
            try:
                data = self.sock.recv(4096)
                if not data:
                    break
                try:
                    decoded = data.decode('cp1251')
                except UnicodeDecodeError:
                    try:
                        decoded = data.decode('utf-8')
                    except:
                        decoded = data.decode('cp1251', errors='replace')
                self.buffer += decoded
                has_separator = False
                while '\n' in self.buffer or '\r\n' in self.buffer:
                    has_separator = True
                    if '\r\n' in self.buffer:
                        msg, self.buffer = self.buffer.split('\r\n', 1)
                    else:
                        msg, self.buffer = self.buffer.split('\n', 1)
                    msg = msg.strip()
                    if msg:
                        self.process_message(msg)
                if not has_separator and self.buffer and ": " in self.buffer:
                    msg = self.buffer.strip()
                    self.buffer = ""
                    if msg:
                        self.process_message(msg)
            except socket.timeout:
                if self.buffer and ": " in self.buffer:
                    msg = self.buffer.strip()
                    self.buffer = ""
                    if msg:
                        self.process_message(msg)
                continue
            except ConnectionResetError:
                self.add_message("", "Соединение разорвано сервером!", is_system=True)
                break
            except Exception as e:
                if self.running:
                    self.add_message("", f"Ошибка приёма: {e}", is_system=True)
                break

        if self.running:
            self.connected = False
            self.status_signal.emit("не в сети", "#ff6b6b")
            self.add_message("", "Соединение потеряно!", is_system=True)

    def send_message(self):
        msg = self.input_field.toPlainText().strip()
        if not msg or not self.connected:
            return
        try:
            self.add_message(self.name, msg, is_own=True, is_system=False, color="#6c5ce7")
            full_msg = f"{self.name}: {msg}\n"
            self.sock.send(full_msg.encode('cp1251'))
            self.input_field.clear()
        except Exception as e:
            self.add_message("", f"Ошибка отправки: {e}", is_system=True)

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
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.sock.close()
        except:
            pass
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
