"""Claude Code's prompt suggestion (dim text in the empty input box) on the dispatcher: the styled
capture is split into the plain screen and the suggestion (`split_suggestion`, real captures from
tests/remote/suggestion_fixtures.py), `capture` reports it apart, and `send`, `keys` and `clear` do
not take it for a draft. Real drafts keep refusing."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest

from tests.remote import suggestion_fixtures as fx
from tests.remote.helpers import SESSION_A, RemoteWorld, pane
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.redact import strip_terminal

FORCED = ["--sessions", "fpga,notes,work-*", "--allow-send"]
CLAUDE_PID = 9_100_301


def plain(styled: str) -> str:
    """The same screen as a capture without -e gives it."""
    return re.sub(r"\x1b\[[0-9;:]*m", "", styled)


# --- split_suggestion ----------------------------------------------------------------------------


def test_a_suggestion_is_cut_from_the_input_line_and_returned_apart() -> None:
    screen, suggestion = d.split_suggestion(fx.SUGGESTION)
    assert suggestion == "move the import to the top of the file"
    assert "move the import" not in screen
    assert "❯" in screen
    assert not d.shows_draft(screen)
    assert d.draft_hash(screen) is None
    assert d.shows_empty_input(screen)
    # Without -e the same screen reads as a draft: the reported bug.
    assert d.shows_draft(plain(fx.SUGGESTION))


def test_an_open_pr_suggestion_is_no_draft() -> None:
    screen, suggestion = d.split_suggestion(fx.OPEN_PR)
    assert suggestion == "open a PR for this"
    assert not d.shows_draft(screen)
    assert d.input_draft(screen) is None


@pytest.mark.parametrize("styled", [fx.TYPED, fx.PASTE, fx.SLASH, fx.HINT])
def test_typed_text_paste_placeholders_slash_commands_and_the_hint_stay(styled: str) -> None:
    screen, suggestion = d.split_suggestion(styled)
    assert suggestion is None
    assert screen == plain(styled), "the plain screen equals a capture without -e"
    assert screen == strip_terminal(styled)


def test_real_drafts_still_refuse() -> None:
    for styled in (fx.TYPED, fx.PASTE, fx.SLASH):
        screen, _ = d.split_suggestion(styled)
        assert d.shows_draft(screen)
        assert d.draft_hash(screen) is not None
    hint, _ = d.split_suggestion(fx.HINT)
    assert not d.shows_draft(hint)


def test_the_rest_of_the_screen_is_unchanged() -> None:
    screen, _ = d.split_suggestion(fx.SUGGESTION)
    before = plain(fx.SUGGESTION).split("\n")
    after = screen.split("\n")
    assert len(before) == len(after)
    changed = [index for index, (a, b) in enumerate(zip(before, after, strict=True)) if a != b]
    assert len(changed) == 1
    assert after[changed[0]].strip() == "❯"


@pytest.mark.parametrize(
    "line",
    [
        "❯ \x1b[2mfix\x1b[0m the tests",  # partly dim: typed text
        "❯ fix \x1b[2mthe tests\x1b[0m",
        "❯ \x1b[38;5;2mgreen is not dim\x1b[39m",  # 256-colour 2 is a colour, not SGR 2
        "❯ \x1b[38;2;2;2;2mtrue colour twos\x1b[39m",
        "❯ \x1b[2;22mdim switched off\x1b[0m",
        "❯ \x1b[2m\x1b[mreset by an empty SGR\x1b[0m",
    ],
)
def test_only_a_wholly_dim_input_is_a_suggestion(line: str) -> None:
    screen, suggestion = d.split_suggestion(fx.screen(line))
    assert suggestion is None
    assert d.shows_draft(screen)


@pytest.mark.parametrize(
    "line",
    [
        "❯ \x1b[1;2mbold and dim\x1b[0m",
        "❯ \x1b[2;38;5;246mdim and grey\x1b[0m",
        "│ > \x1b[2mold box style\x1b[0m           │",
        "\x1b[2m❯ dim from the prompt on\x1b[0m",
    ],
)
def test_dim_with_other_attributes_and_box_edges_is_a_suggestion(line: str) -> None:
    screen, suggestion = d.split_suggestion(fx.screen(line))
    assert suggestion is not None
    assert not d.shows_draft(screen)


def test_dim_carried_from_an_earlier_line_counts() -> None:
    styled = fx.screen("❯ carried").replace(fx.RULE + "\n❯", fx.RULE + "\x1b[2m\n❯")
    _, suggestion = d.split_suggestion(styled)
    assert suggestion == "carried"


def test_only_the_input_line_is_touched() -> None:
    # A dim line of output above the box (a submitted prompt, a hint) stays in the plain screen.
    styled = "\x1b[2m❯ an earlier prompt\x1b[0m\n" + fx.TYPED
    screen, suggestion = d.split_suggestion(styled)
    assert suggestion is None
    assert "an earlier prompt" in screen


def test_an_old_input_box_in_the_scrollback_loses_its_suggestion_too() -> None:
    styled = fx.SUGGESTION_AFTER_CLEAR + fx.OPEN_PR
    screen, suggestion = d.split_suggestion(styled)
    assert suggestion == "open a PR for this", "only the live input line is reported"
    assert "move the import" not in screen
    assert "open a PR" not in screen


def test_escape_sequences_never_reach_the_plain_screen_or_the_suggestion() -> None:
    styled = fx.screen(
        "❯ \x1b[2m\x1b]8;;https://example.com\x07link\x1b]8;;\x07 text \x1b(B\x1b[0m",
        status="\x1b[39m  \x1b[?25lstatus\x1b",
    )
    screen, suggestion = d.split_suggestion(styled)
    assert "\x1b" not in screen
    assert suggestion == "link text"
    assert "\x07" not in screen


def test_a_long_suggestion_is_clipped_and_cleaned() -> None:
    styled = fx.screen("❯ \x1b[2m" + "word\u200b " * 200 + "\x1b[0m")
    _, suggestion = d.split_suggestion(styled)
    assert suggestion is not None
    assert len(suggestion) <= d.SUGGESTION_MAX
    assert "\u200b" not in suggestion


# --- the verbs -----------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    show(world, fx.OPEN_PR)
    return world


def show(world: RemoteWorld, text: str, **extra: Any) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": text}, **extra)
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="idle", tmux="0:@0.%1")


def call(world: RemoteWorld, request: str, stdin: bytes | None = None) -> dict[str, Any]:
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22", d.TEST_CLEAR_NO_WAIT: "1"}
    env["SSH_ORIGINAL_COMMAND"] = f"switchboard-remote {request}"
    return d.handle(FORCED, env, stdin)


def pressed(world: RemoteWorld) -> list[list[str]]:
    return [c["argv"] for c in world.tmux_calls() if c["argv"][:1] == ["send-keys"]]


def test_capture_reports_the_suggestion_apart(world: RemoteWorld) -> None:
    out = call(world, "capture %1 40")
    assert out["ok"] is True
    assert out["suggestion"] == "open a PR for this"
    assert "open a PR" not in out["text"]
    assert "\x1b" not in out["text"]
    show(world, fx.TYPED)
    out = call(world, "capture %1 40")
    assert "suggestion" not in out
    assert "hello there" in out["text"]


def test_send_into_a_suggestion_is_not_draft_present(world: RemoteWorld) -> None:
    out = call(world, f"send %1 {CLAUDE_PID} {SESSION_A} message", b"run the tests")
    assert out == {"protocol": 1, "ok": True, "sent": True, "bytes": 13, "enter": True}


def test_send_into_a_real_draft_still_refuses(world: RemoteWorld) -> None:
    show(world, fx.TYPED)
    assert call(world, f"send %1 {CLAUDE_PID} {SESSION_A} message", b"run the tests")["error"] == "draft_present"


def test_keys_at_a_suggestion(world: RemoteWorld) -> None:
    # Enter alone does not submit a suggestion (2.1.332), Escape and Up do not accept it.
    out = call(world, f"keys %1 {CLAUDE_PID} {SESSION_A} menu none Escape")
    assert out["ok"] is True
    # Right accepts it into the input: refused, so no later Enter submits a prompt nobody typed.
    out = call(world, f"keys %1 {CLAUDE_PID} {SESSION_A} menu none Right")
    assert out == {"protocol": 1, "ok": False, "error": "suggestion_present"}
    assert pressed(world) == [["send-keys", "-t", "%1", "Escape"]]
    show(world, fx.TYPED)
    assert call(world, f"keys %1 {CLAUDE_PID} {SESSION_A} menu none Escape")["error"] == "draft_present"


def test_clear_at_a_suggestion_is_nothing_to_clear(world: RemoteWorld) -> None:
    out = call(world, f"clear %1 {CLAUDE_PID} {SESSION_A} {'0' * 24}")
    assert out == {"protocol": 1, "ok": False, "error": "nothing_to_clear"}
    assert pressed(world) == [], "never Tab, Right or Ctrl+C for a suggestion"


def test_clear_counts_the_suggestion_that_comes_back_as_an_empty_box(world: RemoteWorld) -> None:
    show(world, fx.TYPED, afterKeys={"text": {"%1": fx.screen(fx.SUGGESTION_LINE)}})
    typed, _ = d.split_suggestion(fx.TYPED)
    out = call(world, f"clear %1 {CLAUDE_PID} {SESSION_A} {d.draft_hash(typed)}")
    assert (out["ok"], out["cleared"]) == (True, True)
    assert pressed(world) == [["send-keys", "-t", "%1", "C-c"]]


def test_accepts_suggestion() -> None:
    assert d.accepts_suggestion(["Right"])
    assert d.accepts_suggestion(["Down", "Tab"])
    assert not d.accepts_suggestion(["Escape", "Up", "Enter"])


# --- wrapped suggestions and escape strings ------------------------------------------------------

WRAPPED = fx.screen(
    "\x1b[39m❯\xa0\x1b[2mmove the import to the top of the file and then\x1b[0m\n  \x1b[2mrun the tests again\x1b[0m"
)


def test_a_suggestion_that_wraps_is_cut_on_every_row_and_joined() -> None:
    screen, suggestion = d.split_suggestion(WRAPPED)
    assert suggestion == "move the import to the top of the file and then run the tests again"
    assert "run the tests again" not in screen
    assert not d.shows_draft(screen)
    assert d.input_draft(screen) is None
    assert d.draft_hash(screen) is None
    assert d.clear_target(screen) == "nothing_to_clear"
    assert d.shows_empty_input(screen)


def test_a_typed_row_under_a_dim_line_keeps_the_whole_input() -> None:
    styled = fx.screen("\x1b[39m❯\xa0\x1b[2mmove the import\x1b[0m\n  run the tests again")
    screen, suggestion = d.split_suggestion(styled)
    assert suggestion is None
    assert d.input_draft(screen) == (["move the import", "run the tests again"], False)


def test_without_a_bottom_border_only_the_input_line_is_cut() -> None:
    styled = "⏺ Done.\n" + fx.RULE + "\n\x1b[39m❯\xa0\x1b[2mrun the tests\x1b[0m\n\x1b[2m  dim text\x1b[0m\n"
    screen, suggestion = d.split_suggestion(styled)
    assert suggestion == "run the tests"
    assert "dim text" in screen


@pytest.mark.parametrize(
    "sequence",
    ["\x1bP1;2q\x1b\\", "\x1b^private\x1b\\", "\x1b_app\x1b\\", "\x1bXstring\x1b\\", "\x1bPunterminated"],
)
def test_dcs_pm_apc_and_sos_strings_are_removed_with_their_payload(sequence: str) -> None:
    styled = f"\x1b[2m❯ x\x1b]0;title\x07y{sequence}z\n"
    screen, _ = d.split_suggestion(styled)
    assert "\x1b" not in screen
    for word in ("1;2q", "private", "app", "string", "unterminated", "title"):
        assert word not in screen
    _, suggestion = d.split_suggestion(f"\x1b[39m❯ \x1b[2mx{sequence}y\x1b[0m\n")
    # An unterminated string runs to the next escape: its payload is never screen text.
    assert suggestion == ("x" if sequence == "\x1bPunterminated" else "xy")
