"""The interactive menu at the bottom of a pane, for speech (docs/SWITCHBOARD.md, "remote_status").

The detection is the dispatcher's own `find_menu`, so what the hub reads back is what the dispatcher
binds keys to (`menu_hash`). Menu text is screen text: untrusted, and quoted by the speech layer.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..text import clip
from .dispatcher import find_menu

MENU_OPTION_MAX = 80


@dataclass(frozen=True)
class ScreenMenu:
    """An interactive menu at the bottom of a cleaned screen (`dispatcher.find_menu`)."""

    question: str | None
    """The line or lines directly above the options, clipped to QUESTION_MAX_CHARS."""
    options: tuple[tuple[int, str], ...]
    """(number, text), each text clipped to MENU_OPTION_MAX."""
    selected: int | None
    footer: str | None


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
    assert isinstance(options, list)
    return ScreenMenu(
        question=clip(question, QUESTION_MAX_CHARS) if isinstance(question, str) else None,
        options=tuple((int(number), clip(str(text), MENU_OPTION_MAX)) for number, text in options),
        selected=selected if isinstance(selected, int) else None,
        footer=footer if isinstance(footer, str) else None,
    )
