"""Menu detection (`dispatcher.find_menu`, `menu_hash`, `menu.screen_menu`), the menu-footer status
patterns, key names (`send.parse_keys`, `key_words`, `chosen_option`) and how a menu is spoken.

All screens are synthetic (tests/remote/menu_fixtures.py), shaped like the report from the first
voice test: a question box with numbered options and the footer
"↑/↓ choose · ←/→ change size · enter confirm · esc not now"."""

from __future__ import annotations

import pytest

from tests.remote.helpers import SESSION_A
from tests.remote.menu_fixtures import ASSISTANT, FOOTER, OPTIONS, QUESTION, capture, menu_lines
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.menu import ScreenMenu, screen_menu
from voice_coding_mcp.remote.pending import screen_ask
from voice_coding_mcp.remote.protocol import PaneInfo
from voice_coding_mcp.remote.redact import clean_screen
from voice_coding_mcp.remote.send import chosen_option, key_words, parse_keys
from voice_coding_mcp.remote.speech import footer_words, format_status, menu_words
from voice_coding_mcp.remote.summarize import is_status_line, last_meaningful_line

EXPECTED_OPTIONS = tuple(enumerate(OPTIONS, start=1))


@pytest.mark.parametrize(
    ("rows", "alternate"),
    [(40, False), (40, True), (8, False), (24, True), (11, True)],
    ids=["normal-40", "alternate-40", "normal-short-8", "alternate-24", "alternate-just-fits"],
)
def test_the_whole_menu_is_found_on_every_screen_it_fits(rows: int, alternate: bool) -> None:
    screen = clean_screen(capture(menu_lines(), rows=rows, alternate=alternate))
    menu = screen_menu(screen)
    assert menu == ScreenMenu(
        question=QUESTION,
        options=EXPECTED_OPTIONS,
        selected=1,
        footer=FOOTER.strip(),
    )


def test_a_short_alternate_screen_keeps_the_question_and_options_but_not_the_lines_above() -> None:
    """The one case the capture cannot fix: the alternate screen has no scrollback, so an 8-row pane
    shows only the last 8 lines of the menu. The question line is still there; the assistant line
    above the box is cut (remote_status then suggests a resize)."""
    raw = capture(menu_lines(), rows=8, alternate=True)
    assert ASSISTANT not in raw
    menu = screen_menu(clean_screen(raw))
    assert menu is not None
    assert menu.options == EXPECTED_OPTIONS
    assert menu.question == QUESTION
    # Six rows: the box top and the question are gone; the options remain, with no question.
    cut = screen_menu(clean_screen(capture(menu_lines(), rows=6, alternate=True)))
    assert cut is not None
    assert cut.question is None
    assert cut.options == EXPECTED_OPTIONS


def test_long_scrollback_above_does_not_matter() -> None:
    raw = capture(menu_lines(selected=3), rows=30, alternate=False, history=500, requested=500)
    menu = screen_menu(clean_screen(raw))
    assert menu is not None
    assert menu.selected == 3
    assert menu.question == QUESTION


def test_the_footer_is_a_status_line_so_the_heuristic_no_longer_quotes_it() -> None:
    """The reported bug: the one-line heuristic said only the footer."""
    assert is_status_line(FOOTER.strip())
    for hint in (
        "↑↓ to navigate",
        "Enter to select",
        "enter to confirm",
        "Esc to cancel",
        "esc not now",
        "Tab to switch",
    ):
        assert is_status_line(hint), hint
    screen = clean_screen(capture(menu_lines(), rows=40, alternate=False))
    assert last_meaningful_line(screen) != FOOTER.strip()


def test_options_with_descriptions_a_separator_and_other_footers() -> None:
    screen = "\n".join(
        [
            "⏺ Before I start, one question.",
            "",
            " ☐ Size ",
            "",
            "Which layout do you want for the table?",
            "",
            "❯ 1. Compact",
            "     One line per entry, no avatars",
            "  2. Detailed",
            "     Two lines per entry with the date",
            "  3. Type something.",
            "────────────────────────────",
            "  4. Chat about this",
            "",
            "Enter to select · ↑/↓ to navigate · Esc to cancel",
            "",
            "",
        ]
    )
    menu = screen_menu(screen)
    assert menu is not None
    assert menu.question == "Which layout do you want for the table?"
    assert [number for number, _ in menu.options] == [1, 2, 3, 4]
    assert menu.options[1] == (2, "Detailed")
    assert menu.selected == 1


def test_a_multi_line_question_is_joined() -> None:
    screen = "\n".join(
        [
            "⏺ Done.",
            "",
            "Should I keep the old endpoint",
            "for one more release?",
            "> 1. Yes",
            "  2. No",
            "esc to cancel",
        ]
    )
    menu = screen_menu(screen)
    assert menu is not None
    assert menu.question == "Should I keep the old endpoint for one more release?"
    assert menu.selected == 1


def test_an_assistant_line_directly_above_is_the_question() -> None:
    screen = "\n".join(["⏺ Which one should I keep?", "❯ 1. The old one", "  2. The new one", "↑/↓ choose"])
    menu = screen_menu(screen)
    assert menu is not None
    assert menu.question == "Which one should I keep?"


@pytest.mark.parametrize(
    "screen",
    [
        # Prose with a numbered list above Claude Code's input box.
        "⏺ Pick one:\n  1. Rewrite it\n  2. Keep it\n╭────╮\n│ >  │\n╰────╯\n  ? for shortcuts\n",
        # A numbered list at the end of an answer, no marker and no key hints.
        "⏺ Steps:\n1. build\n2. test\n3. ship\n",
        # Numbers out of order.
        "❯ 1. a\n  3. b\n↑/↓ choose\n",
        # A lone option.
        "❯ 1. only\n↑/↓ choose\n",
        # The draft in the input box looks like an option.
        "╭────╮\n│ ❯ 1. do this │\n╰────╯\n",
        # An assistant line between the options and the bottom.
        "❯ 1. a\n  2. b\n⏺ Thinking about it\nesc to interrupt\n",
        "",
    ],
)
def test_prose_and_other_screens_are_no_menu(screen: str) -> None:
    assert d.find_menu(screen) is None
    assert d.menu_hash(screen) is None


def test_a_permission_dialog_is_a_menu_too() -> None:
    dialog = (
        "╭──────────────────────────────╮\n"
        "│ Bash command                 │\n"
        "│   git push origin main       │\n"
        "│ Do you want to proceed?      │\n"
        "│ ❯ 1. Yes                     │\n"
        "│   2. No, and tell Claude     │\n"
        "╰──────────────────────────────╯\n"
        "  Esc to cancel\n"
    )
    menu = screen_menu(dialog)
    assert menu is not None
    assert menu.question == "Bash command git push origin main Do you want to proceed?"
    assert menu.options == ((1, "Yes"), (2, "No, and tell Claude"))


def test_menu_hash_binds_question_options_and_selection_not_layout() -> None:
    base = capture(menu_lines(), rows=40, alternate=False)
    same_menu_other_pane = capture(menu_lines(), rows=20, alternate=True)
    assert d.menu_hash(base) is not None
    assert d.menu_hash(base) == d.menu_hash(same_menu_other_pane)
    assert d.menu_hash(base) == d.menu_hash(base.replace("│", "┃"))
    assert d.menu_hash(base) != d.menu_hash(capture(menu_lines(selected=2), rows=40, alternate=False))
    other_question = capture(menu_lines(question="How many players?"), rows=40, alternate=False)
    assert d.menu_hash(base) != d.menu_hash(other_question)
    other_options = capture(menu_lines(options=("Tiny", "Huge")), rows=40, alternate=False)
    assert d.menu_hash(base) != d.menu_hash(other_options)
    assert len(d.menu_hash(base) or "") == 24


def test_screen_ask_still_sees_a_menu_question_only_through_the_menu() -> None:
    """`pending_input`'s 15-line tail is not enough for a tall menu; the menu helper reads it."""
    screen = clean_screen(capture(menu_lines(), rows=40, alternate=False))
    ask = screen_ask(screen)
    assert ask is None or ask.kind == "question"
    assert screen_menu(screen) is not None


# --- speech ------------------------------------------------------------------------------------


def _pane(status: str = "waiting") -> PaneInfo:
    return PaneInfo.model_validate(
        {
            "session": "fpga",
            "paneId": "%1",
            "panePid": 1,
            "claude": {"sessionId": SESSION_A, "name": "fpga", "status": status, "waitingFor": "user answer"},
        }
    )


def test_the_heuristic_names_the_question_and_every_option() -> None:
    screen = clean_screen(capture(menu_lines(), rows=40, alternate=False))
    menu = screen_menu(screen)
    text = format_status("dev, fpga", _pane(), summary=None, line=last_meaningful_line(screen), menu=menu)
    assert text == (
        "dev, fpga: Claude Code is waiting for your answer. A menu on the screen asks: "
        '"How big should the high score table be?". Options: 1 "Small (10 entries)", selected; '
        '2 "Medium (50 entries)"; 3 "Large (100 entries)"; 4 "Type something else". '
        'Its key hints also say: "left/right change size".'
    )
    assert "choose" not in text


@pytest.mark.parametrize(
    ("footer", "spoken"),
    [
        # The owner's footer: left and right do something in that menu, so the owner hears it.
        ("↑/↓ choose · ←/→ change size · enter confirm · esc not now", "left/right change size"),
        ("Enter to select · Tab/Arrow keys to navigate · Esc to cancel", "Tab/Arrow keys to navigate"),
        ("↑/↓ choose · enter confirm · esc not now", None),
        ("Esc to cancel", None),
        ("↑/↓ to choose  Enter to confirm  Esc to cancel", None),
        (None, None),
    ],
)
def test_the_footer_is_spoken_when_it_names_more_than_the_usual_keys(footer: str | None, spoken: str | None) -> None:
    assert footer_words(footer) == spoken
    text = menu_words(ScreenMenu(question="Size?", options=((1, "a"), (2, "b")), selected=1, footer=footer))
    if spoken is None:
        assert text == 'A menu on the screen asks: "Size?". Options: 1 "a", selected; 2 "b".'
    else:
        assert text.endswith(f'2 "b". Its key hints also say: "{spoken}".')


def test_long_menus_are_cut_with_a_count() -> None:
    menu = ScreenMenu(question="Pick?", options=tuple((n, "x" * 70) for n in range(1, 10)), selected=None, footer=None)
    text = menu_words(menu)
    assert len(text) < 480
    assert text.endswith("more.")
    assert "and 5 more" in text or "and 4 more" in text or "and 6 more" in text


def test_menu_text_is_redacted_and_quoted() -> None:
    menu = ScreenMenu(
        question='Use "token" ghp_abcdefghijklmnopqrstuvwxyz0123456789?',
        options=((1, "yes"), (2, "no")),
        selected=None,
        footer=None,
    )
    text = menu_words(menu)
    assert "ghp_" not in text
    assert "\"Use 'token' [secret]?\"" in text


# --- keys ---------------------------------------------------------------------------------------


def test_parse_keys_maps_names_and_aliases() -> None:
    assert parse_keys(["Down", "down", "ENTER", "return", "esc", "Shift Tab", "shift+tab", "7", "y", "N"]) == [
        "Down",
        "Down",
        "Enter",
        "Enter",
        "Escape",
        "BTab",
        "BTab",
        "7",
        "y",
        "n",
    ]
    assert parse_keys([]) == "empty"
    assert parse_keys(["Down"] * 11) == "too_many"


@pytest.mark.parametrize(
    "value",
    [
        "C-c",
        "M-x",
        "c-c",
        "^C",
        ";",
        "send-keys",
        "-t",
        "Enter;",
        "Down Enter",
        "0",
        "10",
        "yes",
        "rm -rf /",
        "Space",
        "BSpace",
        "C-m",
        "\x1b",
        "Escape\n",
        "%1",
        "#{pane_id}",
        "kill-server",
        "",
    ],
)
def test_parse_keys_rejects_everything_else(value: str) -> None:
    assert parse_keys([value]) == "unknown"
    assert parse_keys(["Down", value]) == "unknown"


def test_key_words() -> None:
    assert key_words(["Enter"]) == "enter"
    assert key_words(["Down", "Down", "Enter"]) == "down, down and enter"
    assert key_words(["BTab", "Escape", "3"]) == "shift tab, escape and 3"


def test_chosen_option_follows_the_selection() -> None:
    menu = screen_menu(capture(menu_lines(), rows=40, alternate=False))
    assert chosen_option(menu, ["Down", "Enter"]) == (2, "Medium (50 entries)")
    assert chosen_option(menu, ["Down", "Down", "Up", "Enter"]) == (2, "Medium (50 entries)")
    assert chosen_option(menu, ["3"]) == (3, "Large (100 entries)")
    assert chosen_option(menu, ["Enter"]) == (1, "Small (10 entries)")
    # Unknown: a move past the first option may wrap; no Enter yet; another key first; no such option.
    assert chosen_option(menu, ["Up", "Enter"]) is None
    assert chosen_option(menu, ["Down"]) is None
    assert chosen_option(menu, ["Escape", "Enter"]) is None
    assert chosen_option(menu, ["9"]) is None
    assert chosen_option(None, ["Enter"]) is None
    unmarked = ScreenMenu(question=None, options=((1, "a"), (2, "b")), selected=None, footer="↑/↓")
    assert chosen_option(unmarked, ["Down", "Enter"]) is None
    assert chosen_option(unmarked, ["2"]) == (2, "b")
