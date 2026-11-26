#!/usr/bin/env python3
"""A tiny terminal program for the optional live tmux test: turns on bracketed paste (like Claude
Code), puts its terminal in raw mode and records every byte it receives to the file named by its
first argument. It stops after an Enter (CR) that follows the end of a bracketed paste, or after 30 s.
Standard library only; it never runs anything it receives.
"""

from __future__ import annotations

import os
import select
import sys
import termios
import time
import tty


def main() -> int:
    record = sys.argv[1]
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
