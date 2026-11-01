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


def terminal_layout(fallback: tuple[int, int] = (80, 24)) -> TerminalLayout:
    size = shutil.get_terminal_size(fallback)
    columns, rows = max(1, size.columns), max(1, size.lines)
    return TerminalLayout(
        columns=columns,
        rows=rows,
        compact=columns < 64 or rows < 18,
        color=color_enabled(),
        motion=motion_enabled(),
    )


def card_width(columns: int, preferred: int = 68, margin: int = 8) -> int:
    return max(10, min(preferred, columns - margin))


def available_rows(rows: int, *reserved: int) -> int:
    return max(0, rows - sum(max(0, value) for value in reserved))


def animation_interval(default: float = 0.075) -> float:
    return default if motion_enabled() else max(0.18, default)


def animation_frame(frame: int) -> int:
    return frame if motion_enabled() else 0
