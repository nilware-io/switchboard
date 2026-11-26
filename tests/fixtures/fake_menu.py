#!/usr/bin/env python3
"""A synthetic interactive menu for the optional live tmux tests, shaped like Claude Code's: an
assistant line, a boxed question with four numbered options (the selected one marked `❯`) and the
key hints "↑/↓ choose · ←/→ change size · enter confirm · esc not now".

Usage: fake_menu.py normal|alt <record>. `alt` draws on the alternate screen (no scrollback, like a
full-screen program); `normal` prints 40 earlier output lines first and draws each update below the
last one. The terminal is in raw mode: Up and Down move the selection, Enter or a digit 1..4 chooses,
and the chosen number is written to <record>. `<record>.ready` appears once the menu is drawn; a
resize redraws it. Standard library only; it never runs anything it receives. Stops after 60 s.
"""

from __future__ import annotations

import os
import select
import signal
import sys
import termios
import time
import tty

QUESTION = "How big should the high score table be?"
OPTIONS = ("Small (10 entries)", "Medium (50 entries)", "Large (100 entries)", "Type something else")
WIDTH = 62


def menu(selected: int) -> list[str]:
    def boxed(text: str) -> str:
        return "│ " + text.ljust(WIDTH - 4) + " │"

    lines = ["⏺ I can set the high score table up in a few ways.", "", "╭" + "─" * (WIDTH - 2) + "╮"]
    lines += [boxed(QUESTION), boxed("")]
    lines += [boxed(f"{'❯' if n == selected else ' '} {n}. {text}") for n, text in enumerate(OPTIONS, start=1)]
    lines += ["╰" + "─" * (WIDTH - 2) + "╯", "  ↑/↓ choose · ←/→ change size · enter confirm · esc not now"]
    return lines


def main() -> int:
    alternate = sys.argv[1] == "alt"
    record = sys.argv[2]
    fd = sys.stdin.fileno()
    out = sys.stdout.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    redraw = [False]
    signal.signal(signal.SIGWINCH, lambda *_: redraw.__setitem__(0, True))

    def draw(selected: int) -> None:
        text = "\r\n".join(menu(selected))
        if alternate:
            os.write(out, ("\x1b[H\x1b[2J" + text).encode("utf-8"))
        else:
            os.write(out, ("\r\n" + text).encode("utf-8"))

    try:
        if alternate:
            os.write(out, b"\x1b[?1049h")
        else:
            os.write(out, "\r\n".join(f"earlier output line {i}" for i in range(40)).encode("utf-8"))
        selected = 1
        draw(selected)
        with open(record + ".ready", "w", encoding="utf-8"):
            pass
        pending = b""
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                ready, _, _ = select.select([fd], [], [], 0.1)
            except InterruptedError:
                ready = []
            if redraw[0]:
                redraw[0] = False
                draw(selected)
            if not ready:
                continue
            pending += os.read(fd, 64)
            chosen = None
            while pending:
                if pending.startswith((b"\x1b[A", b"\x1bOA")):
                    selected, pending = max(1, selected - 1), pending[3:]
                elif pending.startswith((b"\x1b[B", b"\x1bOB")):
                    selected, pending = min(len(OPTIONS), selected + 1), pending[3:]
                elif pending[:1] == b"\r":
                    chosen, pending = selected, pending[1:]
                elif pending[:1] in (b"1", b"2", b"3", b"4"):
                    selected = chosen = int(pending[:1])
                    pending = pending[1:]
                elif pending[:1] == b"\x1b" and len(pending) < 3:
                    break
                else:
                    pending = pending[1:]
            draw(selected)
            if chosen is not None:
                with open(record, "w", encoding="utf-8") as handle:
                    handle.write(str(chosen))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return 0


if __name__ == "__main__":
    sys.exit(main())
