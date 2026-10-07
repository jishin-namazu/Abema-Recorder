"""Console output.

Colour is off unless the stream is a terminal and NO_COLOR is unset.
"""

from __future__ import annotations

import os
import sys

_ENABLED = sys.stdout.isatty() and not os.environ.get("NO_COLOR")

_DIM = "\033[2m" if _ENABLED else ""
_BOLD = "\033[1m" if _ENABLED else ""
_RED = "\033[31m" if _ENABLED else ""
_YELLOW = "\033[33m" if _ENABLED else ""
_GREEN = "\033[32m" if _ENABLED else ""
_OFF = "\033[0m" if _ENABLED else ""


def _emit(text: str, stream=sys.stdout) -> None:
    stream.write(text + "\n")
    stream.flush()


def stage(text: str) -> None:
    _emit(f"{_BOLD}==>{_OFF} {text}")


def say(text: str) -> None:
    _emit(text)


def detail(text: str) -> None:
    _emit(f"{_DIM}    {text}{_OFF}")


def good(text: str) -> None:
    _emit(f"{_GREEN}ok{_OFF}  {text}")


def warn(text: str) -> None:
    _emit(f"{_YELLOW}!{_OFF}   {text}", sys.stderr)


def fail(text: str, remedy: str | None = None) -> None:
    _emit(f"{_RED}x{_OFF}   {text}", sys.stderr)
    if remedy:
        for line in remedy.splitlines():
            _emit(f"    {_DIM}{line}{_OFF}", sys.stderr)


def table(rows: list[tuple[str, str]]) -> None:
    if not rows:
        return
    width = max(len(left) for left, _ in rows)
    for left, right in rows:
        _emit(f"    {left:<{width}}  {right}")


_BAR_WIDTH = 28
_last_progress_len = 0


def progress(label: str, done: int, total: int) -> None:
    """Redraw a one-line progress bar. Close the line with progress_done()."""
    global _last_progress_len
    fraction = min(1.0, done / total) if total > 0 else 1.0
    filled = int(_BAR_WIDTH * fraction)
    bar = "█" * filled + "░" * (_BAR_WIDTH - filled)
    text = (
        f"{bar} {fraction * 100:5.1f}%  {label}  "
        f"{done / 1_048_576:,.0f}/{total / 1_048_576:,.0f} MB"
    )
    padded = text.ljust(_last_progress_len)
    _last_progress_len = len(text)
    clear = "\033[K" if _ENABLED else ""
    sys.stdout.write("\r" + padded + clear)
    sys.stdout.flush()


def progress_done() -> None:
    global _last_progress_len
    _last_progress_len = 0
    sys.stdout.write("\n")
    sys.stdout.flush()
