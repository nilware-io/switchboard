"""The dispatcher's `clear` verb against the fake tmux: request validation, the send opt-in, Claude Code
panes only, rechecks of the pane, its process and session, every refusal (permission dialog, menu,
picker footer, not idle, vim normal mode, armed exit, no draft, a different draft), the race guard
(the checks run twice, the second right before the key), exactly one `send-keys C-c`, the waits and
the verification afterwards. Screens are synthetic, in the shape Claude Code 2.1 draws."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import SESSION_A, SESSION_B, RemoteWorld, pane
from tests.remote.menu_fixtures import FOOTER, capture, menu_lines
from voice_coding_mcp.remote import dispatcher as d

SESSIONS = ["--sessions", "fpga,notes,work-*"]
FORCED = [*SESSIONS, "--allow-send"]
CLAUDE_PID = 9_100_201
SHELL_PID = 9_100_202
RULE = "─" * 60
STATUS = "  ⏵⏵ auto mode on (shift+tab to cycle)"


def box(*content: str, top: str = RULE, below: str = STATUS) -> str:
    return "⏺ Done. All 41 tests pass.\n\n" + "\n".join([top, *content, RULE, below]) + "\n"


DRAFT = box("❯ fix the parser and then run the tests")
DRAFT_HASH = d.draft_hash(DRAFT)
EMPTY = box("❯ ")
HINT = box('❯ Try "refactor <filepath>"')
TALL = box(*[f"  typed row {n}" for n in range(36, 44)], top="─── ↑ 35 more ───")
DIALOG = (
    "╭──────────────────────────────╮\n"
    "│ Bash command                 │\n"
    "│   git push origin main       │\n"
    "│ Do you want to proceed?      │\n"
    "│ ❯ 1. Yes                     │\n"
    "│   2. No, and tell Claude     │\n"
    "╰──────────────────────────────╯\n"
    "  Esc to cancel\n"
)


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    show(world, DRAFT)
    return world


def show(world: RemoteWorld, text: str, *, status: str = "idle", **extra: Any) -> None:
    world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", SHELL_PID, window_index=1, window_name="shell", command="bash"),
            pane("private", "%3", 9_100_203, command="claude"),
        ],
        {"%1": text, "%2": "$ make", "%3": text},
        **extra,
    )
    world.register("2001", os.getpid(), sessionId=SESSION_A, status=status, tmux="0:@0.%1")


@pytest.fixture(autouse=True)
def no_waits(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The waits are recorded, not slept."""
    slept: list[float] = []
    monkeypatch.setattr(d, "_pause", slept.append)
    return slept


def call(world: RemoteWorld, request: str, forced: list[str] | None = None) -> dict[str, Any]:
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    env["SSH_ORIGINAL_COMMAND"] = f"switchboard-remote {request}"
    return d.handle(forced if forced is not None else FORCED, env)


def clear(
    world: RemoteWorld, draft: str | None = DRAFT_HASH, *, session: str = SESSION_A, forced: list[str] | None = None
) -> dict[str, Any]:
    return call(world, f"clear %1 {CLAUDE_PID} {session} {draft}", forced)


def tmux_argvs(world: RemoteWorld) -> list[list[str]]:
    return [c["argv"] for c in world.tmux_calls()]


def pressed(world: RemoteWorld) -> list[list[str]]:
    return [argv for argv in tmux_argvs(world) if argv[:1] == ["send-keys"]]


def assert_fixed_shapes(world: RemoteWorld) -> None:
    for argv in tmux_argvs(world):
        if argv[:1] == ["list-panes"]:
            assert argv == ["list-panes", "-a", "-F", d.LIST_FORMAT]
        elif argv[:1] == ["capture-pane"]:
            assert argv == ["capture-pane", "-p", "-e", "-J", "-t", "%1", "-S", f"-{d.SEND_CHECK_LINES}"]
        elif argv[:1] == ["send-keys"]:
            assert argv == ["send-keys", "-t", "%1", "C-c"]
        else:
            raise AssertionError(f"unexpected tmux call {argv}")


def refused(code: str) -> dict[str, Any]:
    return {"protocol": 1, "ok": False, "error": code}


# --- the happy path ----------------------------------------------------------------------------


def test_clears_the_draft_with_one_ctrl_c_and_verifies_an_empty_box(world: RemoteWorld, no_waits: list[float]) -> None:
    show(world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    out = clear(world)
    assert out == {
        "protocol": 1,
        "ok": True,
        "pressed": True,
        "cleared": True,
        "remainingLines": 0,
        "remainingChars": 0,
    }
    assert pressed(world) == [["send-keys", "-t", "%1", "C-c"]]
    assert_fixed_shapes(world)
    # Two checks (race guard), the key, one capture to verify.
    captures = [argv for argv in tmux_argvs(world) if argv[:1] == ["capture-pane"]]
    assert len(captures) == 3
    assert no_waits == [d.CLEAR_STABLE_SECONDS, d.CLEAR_SETTLE_SECONDS]
    assert d.CLEAR_SETTLE_SECONDS >= 1.5, "past Claude Code's exit window of about 0.8 s"


def test_the_empty_box_hint_and_the_exit_hint_after_the_key_still_count_as_cleared(world: RemoteWorld) -> None:
    after = box('❯ Try "refactor <filepath>"', below="  Press Ctrl-C again to exit")
    show(world, DRAFT, afterKeys={"text": {"%1": after}})
    out = clear(world)
    assert (out["ok"], out["cleared"]) == (True, True)
    assert len(pressed(world)) == 1, "never a second Ctrl+C"


def test_a_tall_draft_whose_prompt_scrolled_away_is_cleared_too(world: RemoteWorld) -> None:
    assert d.shows_draft(TALL)
    show(world, TALL, afterKeys={"text": {"%1": EMPTY}})
    out = clear(world, d.draft_hash(TALL))
    assert (out["ok"], out["cleared"]) == (True, True)


def test_text_left_after_the_key_is_not_cleared_and_nothing_else_is_pressed(world: RemoteWorld) -> None:
    show(world, DRAFT, afterKeys={"text": {"%1": box("❯ t two")}})
    out = clear(world)
    assert out == {
        "protocol": 1,
        "ok": True,
        "pressed": True,
        "cleared": False,
        "remainingLines": 1,
        "remainingChars": 5,
    }
    assert pressed(world) == [["send-keys", "-t", "%1", "C-c"]]


def test_a_screen_without_an_input_box_after_the_key_is_not_cleared(world: RemoteWorld) -> None:
    show(world, DRAFT, afterKeys={"text": {"%1": "⏺ Done.\n"}})
    out = clear(world)
    assert (out["ok"], out["cleared"]) == (True, False)


def test_a_failed_capture_after_the_key_reports_pressed_without_a_result(world: RemoteWorld) -> None:
    # The pane disappears with the key: the verification capture fails.
    show(world, DRAFT, afterKeys={"panes": [pane("fpga", "%2", SHELL_PID, window_index=1, command="bash")]})
    out = clear(world)
    assert out == {
        "protocol": 1,
        "ok": True,
        "pressed": True,
        "cleared": False,
        "remainingLines": None,
        "remainingChars": None,
    }
    assert len(pressed(world)) == 1


# --- request validation ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "request_text",
    [
        "clear",
        "clear %1",
        f"clear %1 {CLAUDE_PID} {SESSION_A}",
        f"clear %1 {CLAUDE_PID} {SESSION_A} {DRAFT_HASH} extra",
        f"clear 1 {CLAUDE_PID} {SESSION_A} {DRAFT_HASH}",
        f"clear %1 pid {SESSION_A} {DRAFT_HASH}",
        f"clear %1 {CLAUDE_PID} not-a-session {DRAFT_HASH}",
        f"clear %1 {CLAUDE_PID} {SESSION_A} none",
        f"clear %1 {CLAUDE_PID} {SESSION_A} {str(DRAFT_HASH).upper()}",
        f"clear %1 {CLAUDE_PID} {SESSION_A} {str(DRAFT_HASH)[:-1]}",
        f"clear %1 {CLAUDE_PID} {SESSION_A} C-c",
    ],
)
def test_malformed_requests_are_bad_request(world: RemoteWorld, request_text: str) -> None:
    assert call(world, request_text) == refused("bad_request")
    assert pressed(world) == []


def test_needs_allow_send(world: RemoteWorld) -> None:
    assert clear(world, forced=SESSIONS) == refused("not_allowed")
    assert tmux_argvs(world) == []


def test_clear_is_a_listed_verb_and_c_c_stays_out_of_the_key_names(world: RemoteWorld) -> None:
    out = call(world, "version")
    assert "clear" in out["verbs"]
    assert d.CLEAR_KEY not in d.KEY_NAMES
    assert call(world, f"keys %1 {CLAUDE_PID} {SESSION_A} menu none C-c") == refused("bad_request")


# --- refusals ------------------------------------------------------------------------------------


def test_gone_changed_and_other_sessions(world: RemoteWorld) -> None:
    assert call(world, f"clear %9 {CLAUDE_PID} {SESSION_A} {DRAFT_HASH}") == refused("gone")
    assert call(world, f"clear %3 9100203 {SESSION_A} {DRAFT_HASH}") == refused("gone"), "outside --sessions"
    assert call(world, f"clear %1 1234 {SESSION_A} {DRAFT_HASH}") == refused("changed")
    assert clear(world, session=SESSION_B) == refused("changed")
    assert pressed(world) == []


@pytest.mark.parametrize("forced", [FORCED, [*FORCED, "--send-to-any-pane"]])
def test_a_pane_without_claude_code_is_never_cleared(world: RemoteWorld, forced: list[str]) -> None:
    out = call(world, f"clear %2 {SHELL_PID} none {DRAFT_HASH}", forced)
    assert out == refused("not_claude")
    assert pressed(world) == []


@pytest.mark.parametrize(
    ("screen", "status", "code"),
    [
        (DIALOG, "idle", "permission_prompt"),
        (DRAFT, "waiting", "not_idle"),
        (DRAFT, "busy", "not_idle"),
        (capture(menu_lines(), rows=30, alternate=False), "idle", "menu_present"),
        (box("❯ fix the parser", below="  " + FOOTER), "idle", "menu_present"),
        (box("❯ fix the parser", below="  -- NORMAL --"), "idle", "vim_normal"),
        (box("❯ fix the parser", below="  Press Ctrl-C again to exit"), "idle", "exit_armed"),
        (EMPTY, "idle", "nothing_to_clear"),
        (HINT, "idle", "nothing_to_clear"),
        ("⏺ Done.\n$ ", "idle", "nothing_to_clear"),
        (box("❯ fix the parser and then run the linter"), "idle", "draft_changed"),
    ],
)
def test_refusals_press_nothing(world: RemoteWorld, screen: str, status: str, code: str) -> None:
    show(world, screen, status=status)
    assert clear(world) == refused(code)
    assert pressed(world) == []


def test_a_registry_permission_wait_is_a_permission_prompt(world: RemoteWorld) -> None:
    show(world, DRAFT, status="waiting")
    world.register(
        "2001", os.getpid(), sessionId=SESSION_A, status="waiting", waitingFor="permission to run", tmux="0:@0.%1"
    )
    assert clear(world) == refused("permission_prompt")
    assert pressed(world) == []


# --- race guard ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("second", "code"),
    [
        (DIALOG, "permission_prompt"),
        (box("❯ fix the parser and then run the tests", below="  Press Ctrl-C again to exit"), "exit_armed"),
        (box("❯ fix the parser and then run the tests now"), "draft_changed"),
        (EMPTY, "nothing_to_clear"),
    ],
)
def test_the_screen_is_checked_again_right_before_the_key(world: RemoteWorld, second: str, code: str) -> None:
    show(world, DRAFT, captureSequence={"%1": [DRAFT, second]})
    assert clear(world) == refused(code)
    assert pressed(world) == []


def test_a_session_that_starts_working_between_the_checks_is_not_cleared(
    world: RemoteWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = os.path.join(world.home, ".claude", "sessions", "2001.json")

    def busy(_: float) -> None:
        with open(registry, "w", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid(), "sessionId": SESSION_A, "status": "busy", "tmux": "0:@0.%1"}, handle)

    monkeypatch.setattr(d, "_pause", busy)
    assert clear(world) == refused("not_idle")
    assert pressed(world) == []


def test_a_failed_key_is_tmux_failed(world: RemoteWorld) -> None:
    show(world, DRAFT, failOn=["send-keys"])
    assert clear(world) == refused("tmux_failed")


def test_the_test_only_switch_skips_the_waits(world: RemoteWorld, no_waits: list[float]) -> None:
    show(world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22", d.TEST_CLEAR_NO_WAIT: "1"}
    env["SSH_ORIGINAL_COMMAND"] = f"switchboard-remote clear %1 {CLAUDE_PID} {SESSION_A} {DRAFT_HASH}"
    assert d.handle(FORCED, env)["cleared"] is True
    assert no_waits == []


# --- the screen helpers --------------------------------------------------------------------------


def test_draft_helpers() -> None:
    assert d.input_draft(DRAFT) == (["fix the parser and then run the tests"], False)
    assert d.input_draft(EMPTY) is None
    assert d.input_draft(HINT) is None
    assert d.input_draft(TALL) == ([f"typed row {n}" for n in range(36, 44)], True)
    multi = box("❯ first line", "  second line")
    assert d.input_draft(multi) == (["first line", "second line"], False)
    assert d.draft_hash(EMPTY) is None
    assert d.draft_hash(DRAFT) == d.draft_hash(DRAFT.replace("and then", "and  then")), "whitespace collapsed"
    assert d.draft_hash(DRAFT) != d.draft_hash(multi)
    assert d.shows_empty_input(EMPTY)
    assert d.shows_empty_input(HINT)
    assert not d.shows_empty_input(DRAFT)
    assert not d.shows_empty_input(TALL)
    assert not d.shows_empty_input("⏺ Done.\n"), "no box is no evidence of an empty input"
    assert d.shows_exit_armed(box("❯ ", below="  Press Ctrl-C again to exit"))
    assert d.shows_exit_armed(box("❯ ", below="  Press Ctrl-D again to exit"))
    assert not d.shows_exit_armed(DRAFT)
    paste = box("❯ [Pasted text #1 +60 lines]", below="  paste again to expand")
    assert d.input_draft(paste) == (["[Pasted text #1 +60 lines]"], False)
