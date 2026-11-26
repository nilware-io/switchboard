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


PLAIN_OPTIONS = ("Small", "Medium", "Large")


def plain_menu_lines(selected: int = 1, *, options: tuple[str, ...] = PLAIN_OPTIONS) -> list[str]:
    """A menu without numbers: the boxed question, options marked only by `❯` and the same footer."""
    lines = [ASSISTANT, "", "╭" + "─" * (WIDTH - 2) + "╮", _boxed(QUESTION), _boxed("")]
    for position, text in enumerate(options, start=1):
        lines.append(_boxed(f"{'❯' if position == selected else ' '} {text}"))
    lines.append("╰" + "─" * (WIDTH - 2) + "╯")
    lines.append(FOOTER)
    return lines


def described_menu_lines(description_lines: int = 6) -> list[str]:
    """A numbered menu whose option 1 has `description_lines` lines of description under it."""
    lines = [ASSISTANT, "", "╭" + "─" * (WIDTH - 2) + "╮", _boxed(QUESTION), _boxed("")]
    lines.append(_boxed("❯ 1. A"))
    lines += [_boxed(f"     detail {i} of option A") for i in range(description_lines)]
    lines.append(_boxed("  2. B"))
    lines.append("╰" + "─" * (WIDTH - 2) + "╯")
    lines.append(FOOTER)
    return lines


TALL_QUESTION = "Which theme should the editor use?"


def tall_menu_lines(count: int = 12, selected: int = 9) -> list[str]:
    """A menu taller than a 24-row pane: `count` options `Theme n`, each with a description line
    under it (31 lines for 12 options; on a 24-row alternate screen option 1 is off the top)."""
    lines = [ASSISTANT, "", "╭" + "─" * (WIDTH - 2) + "╮", _boxed(TALL_QUESTION), _boxed("")]
    for number in range(1, count + 1):
        lines.append(_boxed(f"{'❯' if number == selected else ' '} {number}. Theme {number}"))
        lines.append(_boxed(f"     Description of theme {number}"))
    lines.append("╰" + "─" * (WIDTH - 2) + "╯")
    lines.append(FOOTER)
    return lines


PROSE_DIALOG = (
    "⏺ I refactored the parser.\n"
    "Do you want to run the tests now?\n"
    "╭──────────────────╮\n"
    "│ >                │\n"
    "╰──────────────────╯\n"
    "  ⏵⏵ accept edits on (shift+tab to cycle)\n"
)
"""Assistant prose that starts a line with "Do you want to ...?" above an idle input box: `dialog_hash`
sees a dialog, but no answer options are at the bottom (`dispatcher.dialog_bound` False)."""
PROSE_YES_NO = (
    "⏺ Should I keep the old parser?\n"
    "1. Yes, keep it\n"
    "2. No, delete it\n"
    "╭──────────────────╮\n"
    "│ >                │\n"
    "╰──────────────────╯\n"
    "  ? for shortcuts\n"
)
"""A prose `1. Yes ... 2. No` list above an idle input box, the same."""


def numbered_draft(lines: int, *, boxed: bool) -> str:
    """Claude Code's input box holding a numbered draft of `lines` lines (`> 1. fix the parser` /
    `  2. update tests` / `  3. write docs`), drawn as a rounded box (`boxed`) or between two rules."""
    body = ["> 1. fix the parser", "  2. update tests", "  3. write docs"][:lines]
    if boxed:
        rows = ["╭" + "─" * 30 + "╮", *(f"│ {line.ljust(28)} │" for line in body), "╰" + "─" * 30 + "╯"]
        status = "  ? for shortcuts"
    else:
        rows = ["─" * 32, *body, "─" * 32]
        status = "  ⏵⏵ accept edits on (shift+tab to cycle)"
    return "\n".join(["⏺ Done. All 41 tests pass.", "", *rows, status]) + "\n"
