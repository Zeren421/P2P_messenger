#!/usr/bin/env python3
"""
p2p_chat_client.py — Entry point for the modular P2P chat client.
"""

import sys
from PyQt5.QtWidgets import QApplication
from main_window import ChatClient

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

if __name__ == '__main__':
    main()
