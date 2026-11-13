"""
helpers/history.py — chat history persistence + username/peer storage.
All data is stored in the `data/` folder at the project root.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

# ── Определяем корневую папку проекта ──────────────────────────────────────

if getattr(sys, "frozen", False):
    # Если запущено из .exe — берём папку, где лежит exe
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    # Если из .py — helpers/history.py лежит в client/helpers/
    # Поднимаемся на два уровня вверх, чтобы попасть в корень проекта
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ── Папка для пользовательских данных ──────────────────────────────────────

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
os.makedirs(DATA_DIR, exist_ok=True)  # создаём, если нет

HISTORY_FILE = os.path.join(DATA_DIR, "history.jsonl")
NAME_FILE    = os.path.join(DATA_DIR, "username.txt")
PEER_FILE    = os.path.join(DATA_DIR, "peer.txt")
HISTORY_LIMIT = 1000


# ── История ──────────────────────────────────────────────────────────────────

def save_history(entry: dict[str, Any]) -> None:
    """Append a single message entry to history.jsonl."""
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[history] save error: {exc}")


def load_history() -> list[dict[str, Any]]:
    """Load up to HISTORY_LIMIT most recent entries."""
    if not os.path.exists(HISTORY_FILE):
        return []
    entries: list[dict[str, Any]] = []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    except OSError as exc:
        print(f"[history] load error: {exc}")
    return entries[-HISTORY_LIMIT:]


def clear_history() -> None:
    """Delete the history file."""
    try:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
    except OSError as exc:
        print(f"[history] clear error: {exc}")


# ── Username ─────────────────────────────────────────────────────────────────

def save_username(name: str) -> None:
    try:
        with open(NAME_FILE, "w", encoding="utf-8") as f:
            f.write(name.strip())
    except OSError as exc:
        print(f"[history] save_username error: {exc}")


def load_username() -> str:
    if not os.path.exists(NAME_FILE):
        return ""
    try:
        with open(NAME_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ── Peer name ────────────────────────────────────────────────────────────────

def save_peer(peer: str) -> None:
    try:
        with open(PEER_FILE, "w", encoding="utf-8") as f:
            f.write(peer.strip())
    except OSError as exc:
        print(f"[history] save_peer error: {exc}")


def load_peer() -> str:
    if not os.path.exists(PEER_FILE):
        return ""
    try:
        with open(PEER_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""
