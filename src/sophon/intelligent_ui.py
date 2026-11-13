"""Responsive terminal layout and motion preferences for Sophon's UI."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class TerminalLayout:
    columns: int
    rows: int
    compact: bool
    color: bool
    motion: bool


def color_enabled(stream=None) -> bool:
    stream = stream or sys.stdout
    return bool(
        getattr(stream, "isatty", lambda: False)()
        and "NO_COLOR" not in os.environ
        and os.environ.get("TERM", "") != "dumb"
    )


def motion_enabled() -> bool:
    if os.environ.get("SOPHON_REDUCED_MOTION", "").casefold() in {"1", "true", "yes", "on"}:
        return False
    return os.environ.get("TERM", "") != "dumb"


def is_termux() -> bool:
    return bool(
        os.environ.get("TERMUX_VERSION")
        or "/data/data/com.termux/" in os.environ.get("PREFIX", "")
        or "/data/data/com.termux/" in sys.prefix
        or os.environ.get("ANDROID_ROOT")
    )


def terminal_layout(fallback: tuple[int, int] = (80, 24)) -> TerminalLayout:
    size = terminal_size(fallback)
    columns, rows = max(1, size.columns), max(1, size.lines)
    return TerminalLayout(
        columns=columns,
        rows=rows,
        compact=columns < 64 or rows < 18,
        color=color_enabled(),
        motion=motion_enabled(),
    )


def terminal_size(fallback: tuple[int, int] = (80, 24)) -> os.terminal_size:
    """Return a safe terminal size shared by every renderer.

    Termux can report the emulator's logical column count while its font and
    display scaling leave fewer cells visible. Capping portrait-style mobile
    layouts prevents hard terminal wrapping from tearing bordered cards.
    Narrower terminals and landscape layouts continue to use their real size.
    Set SOPHON_COLUMNS to a smaller value when a particular terminal needs it.
    """
    size = shutil.get_terminal_size(fallback)
    columns = max(1, size.columns)
    override = os.environ.get("SOPHON_COLUMNS", "").strip()
    if override.isdigit() and int(override) > 0:
        columns = min(columns, int(override))
    elif is_termux() and columns > 48:
        columns = 64
    return os.terminal_size((columns, max(1, size.lines)))


def card_width(columns: int, preferred: int = 68, margin: int = 8) -> int:
    return max(1, min(preferred, columns - margin))


def card_inner_width(columns: int, preferred: int = 68) -> int:
    """Return the exact content width after applying the shared card limits."""
    return max(1, min(preferred, card_width(columns, preferred), columns - 6))


def available_rows(rows: int, *reserved: int) -> int:
    return max(0, rows - sum(max(0, value) for value in reserved))


def animation_interval(default: float = 0.06) -> float:
    """Return the shared cadence used by terminal status animations."""
    return default if motion_enabled() else max(0.18, default)


def animation_frame(frame: int) -> int:
    return frame if motion_enabled() else 0
