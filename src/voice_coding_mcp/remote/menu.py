"""The interactive menu at the bottom of a pane, for speech (docs/SWITCHBOARD.md, "remote_status").

The detection is the dispatcher's own `find_menu`, so what the hub reads back is what the dispatcher
binds keys to (`menu_hash`). Menu text is screen text: untrusted, and quoted by the speech layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..text import clip
from .dispatcher import MENU_FOOTER_RE, find_menu, shows_picker

MENU_OPTION_MAX = 80
MENU_DESCRIPTION_MAX = 80


@dataclass(frozen=True)
class ScreenMenu:
    """An interactive menu at the bottom of a cleaned screen (`dispatcher.find_menu`)."""

    question: str | None
    """The line or lines directly above the options, clipped to QUESTION_MAX_CHARS."""
    options: tuple[tuple[int, str], ...]
    """(number, text), each text clipped to MENU_OPTION_MAX."""
    selected: int | None
    footer: str | None
    numbered: bool = True
    """False for a menu without numbers: its options are numbered by position, and a digit key does
    not choose them."""
    descriptions: tuple[str | None, ...] = ()
    """Per option (same order), the lines under it joined and clipped to MENU_DESCRIPTION_MAX, or None."""


QUESTION_MAX_CHARS = 200


def screen_menu(screen: str) -> ScreenMenu | None:
    """The menu at the bottom of a cleaned (terminal-stripped, redacted) screen, or None."""
    found = find_menu(screen)
    if found is None:
        return None
    question = found["question"]
    options = found["options"]
    selected = found["selected"]
    footer = found["footer"]
    descriptions = found.get("descriptions")
    assert isinstance(options, list)
    assert isinstance(descriptions, list)
    return ScreenMenu(
        question=clip(question, QUESTION_MAX_CHARS) if isinstance(question, str) else None,
        options=tuple((int(number), clip(str(text), MENU_OPTION_MAX)) for number, text in options),
        selected=selected if isinstance(selected, int) else None,
        footer=footer if isinstance(footer, str) else None,
        numbered=found["numbered"] is not False,
        descriptions=tuple(clip(str(text), MENU_DESCRIPTION_MAX) if text else None for text in descriptions),
    )


FOOTER_BLOCK_LINES = 8
FOOTER_BLOCK_MAX_CHARS = 400


def footer_block(screen: str) -> str | None:
    """For a screen whose bottom is a picker footer (`dispatcher.shows_picker`: up/down plus enter or
    esc hints) but no menu `screen_menu` can read: the last FOOTER_BLOCK_LINES non-empty, non-border
    lines above the hints (the question and options), joined with " / " and clipped. None without
    such a footer."""
    if not shows_picker(screen):
        return None
    lines = [line.strip().strip("│┃║|").strip() for line in screen.split("\n")]
    index = len(lines) - 1
    while index >= 0 and (not lines[index] or _border(lines[index]) or MENU_FOOTER_RE.search(lines[index])):
        index -= 1
    block: list[str] = []
    while index >= 0 and len(block) < FOOTER_BLOCK_LINES:
        if lines[index] and not _border(lines[index]):
            block.insert(0, " ".join(lines[index].split()))
        index -= 1
    return clip(" / ".join(block), FOOTER_BLOCK_MAX_CHARS) if block else None


def _border(line: str) -> bool:
    return all(ch in "─━│┃╭╮╰╯┌┐└┘├┤┬┴┼═║╔╗╚╝-_ " for ch in line)
