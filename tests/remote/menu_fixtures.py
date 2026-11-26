"""Synthetic interactive menus shaped like Claude Code's (no real screen text): a question box with
numbered options, a selection marker and the key hints below it. `capture` lays a menu out the way
`tmux capture-pane -p -J -S -<n>` returns it for a pane of a given height, on the normal screen
(scrollback above the visible rows) or on the alternate screen (no scrollback: only the visible rows,
the menu drawn from the top and blank rows under it)."""

from __future__ import annotations

ASSISTANT = "⏺ I can set the high score table up in a few ways."
QUESTION = "How big should the high score table be?"
OPTIONS = ("Small (10 entries)", "Medium (50 entries)", "Large (100 entries)", "Type something else")
FOOTER = "  ↑/↓ choose · ←/→ change size · enter confirm · esc not now"
WIDTH = 62


def _boxed(text: str) -> str:
    return "│ " + text.ljust(WIDTH - 4) + " │"


def menu_lines(selected: int = 1, *, question: str = QUESTION, options: tuple[str, ...] = OPTIONS) -> list[str]:
    """The menu as drawn: the assistant line, an empty line, the boxed question and options, the
    footer (11 lines for the default menu)."""
    lines = [ASSISTANT, "", "╭" + "─" * (WIDTH - 2) + "╮", _boxed(question), _boxed("")]
    for number, text in enumerate(options, start=1):
        mark = "❯" if number == selected else " "
        lines.append(_boxed(f"{mark} {number}. {text}"))
    lines.append("╰" + "─" * (WIDTH - 2) + "╯")
    lines.append(FOOTER)
    return lines


def capture(lines: list[str], *, rows: int, alternate: bool, history: int = 40, requested: int = 120) -> str:
    """What `capture-pane -p -J -S -<requested>` gives for `lines` printed at the bottom of a pane of
    `rows` rows: on the normal screen `history` earlier output lines above them (at most `requested`
    lines of scrollback kept), on the alternate screen only the visible rows."""
    if alternate:
        visible = lines[-rows:] if len(lines) > rows else lines + [""] * (rows - len(lines))
        return "\n".join(visible) + "\n"
    everything = [f"earlier output line {i}" for i in range(history)] + lines
    above, visible = everything[:-rows], everything[-rows:]
    return "\n".join(above[-requested:] + visible) + "\n"
