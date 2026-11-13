"""
helpers/ui_utils.py — UI utility functions and constants.
No Qt dependencies (pure Python), safe to import anywhere.
"""

from __future__ import annotations

import sys
import os

# Palette for user name colours
USER_COLORS: list[str] = [
    "#FF4757", "#2ED573", "#1E90FF", "#FFA502", "#A29BFE",
    "#FF6B81", "#7BED9F", "#70A1FF", "#FFDA79", "#F368E0",
    "#2BCBBA", "#54A0FF", "#FF9FF3", "#00D2D3", "#FECA57",
]


def resource_path(relative_path: str) -> str:
    """Return absolute path to a resource (works for both dev and PyInstaller .exe)."""
    try:
        base_path = sys._MEIPASS          # type: ignore[attr-defined]
    except AttributeError:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


def darken_color(hex_color: str, factor: float = 0.75) -> str:
    """Return a darker version of *hex_color* by multiplying each channel by *factor*."""
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16)
    g = int(hex_color[2:4], 16)
    b = int(hex_color[4:6], 16)
    return "#{:02x}{:02x}{:02x}".format(int(r * factor), int(g * factor), int(b * factor))


def encode_username(name: str) -> int:
    """Deterministic hash of *name* → int (used to pick a colour index)."""
    val = 0
    for ch in name:
        val = val * 31 + ord(ch)
    return val


def color_for_username(name: str) -> str:
    """Return a stable colour string for the given username."""
    return USER_COLORS[encode_username(name) % len(USER_COLORS)]
