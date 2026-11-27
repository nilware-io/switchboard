#!/usr/bin/env python3
"""A tiny terminal program for the optional live tmux test: turns on bracketed paste (like Claude
Code), puts its terminal in raw mode and records every byte it receives to the file named by its
first argument. It stops after an Enter (CR) that follows the end of a bracketed paste, or after 30 s.

With `input-box` as its second argument it draws an input box the way Claude Code 2.1.332 does: a
typed draft (`fix the parser`, plain text) between two rules; Ctrl+C (byte 3) empties it, and the
empty box then shows a dim prompt suggestion (`open a PR for this`, SGR 2) and "Press Ctrl-C again to
exit" for about 0.8 s. It stops after a second Ctrl+C or after 30 s.
Standard library only; it never runs anything it receives.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty

RULE = "─" * 60


def draw(fd: int, input_line: str, status: str) -> None:
    """Redraws in place from the top left, as Claude Code does (a clear screen would push the old
    frame into tmux's scrollback)."""
    rows = ["⏺ Done. All 41 tests pass.", "", RULE, input_line, RULE, f"  {status}"]
    text = "\x1b[H" + "".join(f"{row}\x1b[K\r\n" for row in rows) + "\x1b[J"
    os.write(fd, text.encode("utf-8"))


def input_box(record: str) -> int:
    fd = sys.stdin.fileno()
    out = sys.stdout.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        draw(out, "❯\u00a0fix the parser", "? for shortcuts")
        with open(record + ".ready", "w", encoding="utf-8"):
            pass
        data = b""
        armed_until = 0.0
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if armed_until and time.monotonic() > armed_until:
                armed_until = 0.0
                draw(out, "❯\u00a0\x1b[2mopen a PR for this\x1b[0m", "? for shortcuts")
            ready, _, _ = select.select([fd], [], [], 0.1)
            if not ready:
                continue
            chunk = os.read(fd, 4096)
            data += chunk
            with open(record, "wb") as handle:
                handle.write(data)
            if b"\x03" in chunk:
                if armed_until:
                    break
                armed_until = time.monotonic() + 0.8
                draw(out, "❯\u00a0\x1b[2mopen a PR for this\x1b[0m", "Press Ctrl-C again to exit")
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


def main() -> int:
    record = sys.argv[1]
    if sys.argv[2:] == ["input-box"]:
        return input_box(record)
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        os.write(sys.stdout.fileno(), b"\x1b[?2004h")
        with open(record + ".ready", "w", encoding="utf-8"):
            pass
        data = b""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            data += os.read(fd, 4096)
            with open(record, "wb") as handle:
                handle.write(data)
            end = data.rfind(b"\x1b[201~")
            if end >= 0 and data[end:].endswith(b"\r"):
                break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
