"""The dispatcher's `send` verb against the fake tmux: the text only ever reaches tmux on the stdin of
`load-buffer`, every argv has a fixed shape, and every recheck (pane, pid, Claude Code session,
text rules, permission dialog, draft, vim mode, the race guard before Enter) refuses before typing."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import DISPATCHER, SESSION_A, SESSION_B, RemoteWorld, pane
from voice_coding_mcp.remote import dispatcher as d

SESSIONS = ["--sessions", "fpga,notes,work-*"]
FORCED = [*SESSIONS, "--allow-send"]
CLAUDE_PID = 9_100_001
SHELL_PID = 9_100_002
EMPTY_BOX = (
    "⏺ Done. All 41 tests pass.\n╭──────────────────╮\n│ >                │\n╰──────────────────╯\n  ? for shortcuts\n"
)
DIALOG = (
    "╭──────────────────────────────╮\n"
    "│ Bash command                 │\n"
    "│   git push origin main       │\n"
    "│ Do you want to proceed?      │\n"
    "│ ❯ 1. Yes                     │\n"
    "│   2. No, and tell Claude     │\n"
    "╰──────────────────────────────╯\n"
)
BUFFER_RE = re.compile(r"sb-[0-9a-f]{16}")


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", SHELL_PID, window_index=1, window_name="shell", command="bash"),
            pane("private", "%3", 9_100_003, command="claude"),
        ],
        {"%1": EMPTY_BOX, "%2": "$ make\nok\n$ ", "%3": EMPTY_BOX},
    )
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="idle", tmux="0:@0.%1")
    return world


def send(
    world: RemoteWorld,
    text: str | bytes,
    args: str = f"%1 {CLAUDE_PID} {SESSION_A}",
    forced: list[str] | None = None,
) -> dict[str, Any]:
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    env["SSH_ORIGINAL_COMMAND"] = f"switchboard-remote send {args}"
    data = text.encode("utf-8") if isinstance(text, str) else text
    return d.handle(forced if forced is not None else FORCED, env, data)


def tmux_argvs(world: RemoteWorld) -> list[list[str]]:
    return [call["argv"] for call in world.tmux_calls()]


def assert_fixed_shapes(world: RemoteWorld, pane_id: str = "%1") -> None:
    """Every tmux call has one of the fixed shapes of the send verb: no argument comes from the text."""
    for argv in tmux_argvs(world):
        if argv[:1] == ["list-panes"]:
            assert argv == ["list-panes", "-a", "-F", d.LIST_FORMAT]
        elif argv[:1] == ["capture-pane"]:
            assert argv == ["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{d.SEND_CHECK_LINES}"]
        elif argv[:1] == ["load-buffer"]:
            assert argv[:2] == ["load-buffer", "-b"]
            assert BUFFER_RE.fullmatch(argv[2])
            assert argv[3:] == ["-"]
        elif argv[:1] == ["paste-buffer"]:
            assert argv[:4] == ["paste-buffer", "-p", "-d", "-b"]
            assert BUFFER_RE.fullmatch(argv[4])
            assert argv[5:] == ["-t", pane_id]
        elif argv[:1] == ["send-keys"]:
            assert argv == ["send-keys", "-t", pane_id, "Enter"]
        elif argv[:1] == ["delete-buffer"]:
            assert argv[:2] == ["delete-buffer", "-b"]
            assert BUFFER_RE.fullmatch(argv[2])
        else:
            raise AssertionError(f"unexpected tmux call {argv}")


def stdin_of(world: RemoteWorld, verb: str) -> list[str]:
    return [call["stdin"] for call in world.tmux_calls() if call["argv"][:1] == [verb]]


def typed(world: RemoteWorld) -> bool:
    return any(argv[:1] in (["load-buffer"], ["paste-buffer"], ["send-keys"]) for argv in tmux_argvs(world))


# --- the happy path and injection attempts ------------------------------------------------------


def test_sends_through_load_buffer_stdin_bracketed_paste_and_enter(world: RemoteWorld) -> None:
    out = send(world, "run the tests please")
    assert out == {"protocol": 1, "ok": True, "sent": True, "bytes": 20, "enter": True}
    verbs = [argv[0] for argv in tmux_argvs(world)]
    assert verbs.index("load-buffer") < verbs.index("paste-buffer") < verbs.index("send-keys")
    assert verbs[-1] == "send-keys"
    assert stdin_of(world, "load-buffer") == ["run the tests please"]
    loaded = next(argv for argv in tmux_argvs(world) if argv[0] == "load-buffer")
    pasted = next(argv for argv in tmux_argvs(world) if argv[0] == "paste-buffer")
    assert loaded[2] == pasted[4], "the pasted buffer is the loaded one, deleted by -d"
    assert_fixed_shapes(world)
    scenario = json.loads(Path(world.scenario).read_text(encoding="utf-8"))
    assert scenario["pasted"] == [{"pane": "%1", "text": "run the tests please"}]


INJECTIONS = [
    "say \"hi\" and 'bye'",
    "$(rm -rf ~)",
    "`id`",
    "a; rm -rf / ; b",
    "a && reboot || shutdown",
    "first line\nsecond line\n\nfourth",
    "C-c",
    "Enter",
    "Escape",
    "send-keys -t %2 C-c Enter",
    "-t %3",
    "'; tmux kill-server; '",
    "show #{pane_id} and #(id)",
    "%1",
    "\\x1b[201~ literal backslashes",
    "x" * 8000,
    "€" * 2000,
]


@pytest.mark.parametrize("text", INJECTIONS, ids=lambda t: repr(t[:20]))
def test_hostile_text_is_typed_verbatim_and_never_reaches_an_argv(world: RemoteWorld, text: str) -> None:
    out = send(world, text)
    assert out["ok"] is True, out
    assert out["bytes"] == len(text.encode("utf-8"))
    assert stdin_of(world, "load-buffer") == [text]
    assert_fixed_shapes(world)
    for call in world.tmux_calls():
        if call["argv"][0] != "load-buffer":
            assert call["stdin"] == ""


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("", "empty"),
        ("   \n  ", "empty"),
        ("\x1b[201~rm -rf /\x1b[200~", "escape ends the bracketed paste"),
        ("stop\x03", "ctrl-c"),
        ("a\rb", "carriage return"),
        ("a\tb", "tab"),
        ("abc\u202edef", "bidi override"),
        ("a\u200bb", "zero width space"),
        ("a\u2028b", "line separator"),
        ("a\x9bb", "C1 CSI"),
        ("e\u0301", "not NFC"),
        ("\ue000", "private use"),
        ("!rm -rf /", "bash mode"),
        ("   !ls", "bash mode after whitespace"),
        ("#remember my password", "memory"),
        ("/login", "slash command"),
        ("/compact", "slash command not allowed"),
    ],
)
def test_text_rules_refuse_before_anything_is_typed(world: RemoteWorld, text: str, why: str) -> None:
    out = send(world, text)
    assert out == {"protocol": 1, "ok": False, "error": "bad_text"}, why
    assert not typed(world)


def test_oversized_and_invalid_utf8_stdin_is_bad_text(world: RemoteWorld) -> None:
    assert send(world, b"x" * (d.MAX_SEND_BYTES + 1))["error"] == "bad_text"
    assert send(world, b"\xff\xfe")["error"] == "bad_text"
    assert send(world, "x" * d.MAX_SEND_BYTES)["ok"] is True


def test_allowed_slash_commands_need_the_dispatcher_list(world: RemoteWorld) -> None:
    forced = [*FORCED, "--slash-commands", "compact,clear"]
    assert send(world, "/compact", forced=forced)["ok"] is True
    assert send(world, "/compact keep the parser notes", forced=forced)["ok"] is True
    assert send(world, "/compactx", forced=forced)["error"] == "bad_text"
    assert send(world, "/login", forced=forced)["error"] == "bad_text"


def test_policy_flags_in_the_request_are_refused(world: RemoteWorld) -> None:
    env = {**world.tmux_env(), "SSH_ORIGINAL_COMMAND": f"switchboard-remote --allow-send send %1 {CLAUDE_PID} none"}
    assert d.handle(SESSIONS, env, b"hi") == {"protocol": 1, "ok": False, "error": "bad_request"}
    assert not typed(world)


def test_sending_needs_allow_send(world: RemoteWorld) -> None:
    assert send(world, "hello", forced=SESSIONS) == {"protocol": 1, "ok": False, "error": "not_allowed"}
    assert tmux_argvs(world) == []


# --- the pane it goes to ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (f"%9 {CLAUDE_PID} {SESSION_A}", "gone"),
        (f"%3 9100003 {SESSION_A}", "gone"),  # outside the allow-list: like a missing pane
        (f"%1 4242 {SESSION_A}", "changed"),  # another process in the pane
        (f"%1 {CLAUDE_PID} {SESSION_B}", "changed"),  # Claude Code restarted in the same shell
        (f"%1 {CLAUDE_PID} none", "changed"),  # Claude Code now runs where the hub saw none
        (f"%2 {SHELL_PID} {SESSION_A}", "changed"),  # Claude Code is gone from the pane
        (f"%2 {SHELL_PID} none", "not_claude"),  # a plain shell without --send-to-any-pane
    ],
)
def test_the_pane_is_rechecked(world: RemoteWorld, args: str, error: str) -> None:
    assert send(world, "hello", args) == {"protocol": 1, "ok": False, "error": error}
    assert not typed(world)


def test_a_plain_shell_gets_single_lines_only_with_send_to_any_pane(world: RemoteWorld) -> None:
    forced = [*FORCED, "--send-to-any-pane"]
    assert send(world, "make test", f"%2 {SHELL_PID} none", forced)["ok"] is True
    assert send(world, "make\nrm -rf /", f"%2 {SHELL_PID} none", forced)["error"] == "bad_text"
    assert_fixed_shapes(world, "%2")


# --- what the screen shows ----------------------------------------------------------------------


def test_a_permission_dialog_on_screen_is_refused(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": "⏺ I will push now.\n" + DIALOG})
    assert send(world, "1")["error"] == "permission_prompt"
    assert send(world, "yes")["error"] == "permission_prompt"
    assert not typed(world)


def test_a_registry_permission_wait_is_refused(world: RemoteWorld) -> None:
    world.register(
        "2001", os.getpid(), sessionId=SESSION_A, status="waiting", waitingFor="permission prompt", tmux="0:@0.%1"
    )
    assert send(world, "go ahead")["error"] == "permission_prompt"
    assert not typed(world)


def test_permission_answers_are_a_choice_digit_without_enter(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": DIALOG})
    forced = [*FORCED, "--allow-permission-answers"]
    assert send(world, "yes", forced=forced)["error"] == "permission_prompt"
    assert send(world, "12", forced=forced)["error"] == "permission_prompt"
    assert not typed(world)
    out = send(world, "1", forced=forced)
    assert out == {"protocol": 1, "ok": True, "sent": True, "bytes": 1, "enter": False}
    assert stdin_of(world, "load-buffer") == ["1"]
    assert not any(argv[0] == "send-keys" for argv in tmux_argvs(world))


def test_race_guard_a_dialog_that_opens_after_the_paste_gets_no_enter(world: RemoteWorld) -> None:
    world.set_panes(
        [pane("fpga", "%1", CLAUDE_PID, command="claude")],
        {"%1": EMPTY_BOX},
        afterPaste={"text": {"%1": "⏺ Running the tests.\n" + DIALOG}},
    )
    assert send(world, "run the tests") == {"protocol": 1, "ok": False, "error": "pasted_no_enter"}
    verbs = [argv[0] for argv in tmux_argvs(world)]
    assert "paste-buffer" in verbs
    assert "send-keys" not in verbs


def test_race_guard_also_watches_the_registry(world: RemoteWorld) -> None:
    registry = os.path.join(world.home, ".claude", "sessions", "2001.json")
    waiting = {
        "pid": os.getpid(),
        "sessionId": SESSION_A,
        "status": "waiting",
        "waitingFor": "permission prompt",
        "tmux": "0:@0.%1",
    }
    world.set_panes(
        [pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": EMPTY_BOX}, afterPaste={"write": {registry: waiting}}
    )
    assert send(world, "run the tests")["error"] == "pasted_no_enter"
    assert "send-keys" not in [argv[0] for argv in tmux_argvs(world)]


def test_a_draft_in_the_input_box_is_not_submitted(world: RemoteWorld) -> None:
    draft = "⏺ Done.\n╭────────────────────╮\n│ > half typed idea  │\n╰────────────────────╯\n"
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": draft})
    assert send(world, "run the tests")["error"] == "draft_present"
    assert not typed(world)


def test_the_empty_box_hint_is_not_a_draft(world: RemoteWorld) -> None:
    hint = '⏺ Done.\n╭──────────────────────────────╮\n│ > Try "fix lint errors"      │\n╰──────────────────────────────╯\n'
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": hint})
    assert send(world, "run the tests")["ok"] is True


def test_vim_normal_mode_is_refused(world: RemoteWorld) -> None:
    screen = EMPTY_BOX + "  -- NORMAL --\n"
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": screen})
    assert send(world, "run the tests")["error"] == "vim_normal"
    assert not typed(world)


@pytest.mark.parametrize(
    ("step", "error", "deleted"),
    [
        ("load-buffer", "tmux_failed", False),
        ("paste-buffer", "tmux_failed", True),
        ("send-keys", "pasted_no_enter", False),
    ],
)
def test_tmux_failures(world: RemoteWorld, step: str, error: str, deleted: bool) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": EMPTY_BOX}, failOn=[step])
    assert send(world, "hello")["error"] == error
    assert any(argv[0] == "delete-buffer" for argv in tmux_argvs(world)) is deleted


def test_the_script_reads_the_text_from_its_stdin(world: RemoteWorld) -> None:
    env = {
        **world.tmux_env(),
        "PATH": "/usr/bin:/bin",
        "SSH_ORIGINAL_COMMAND": f"switchboard-remote send %1 {CLAUDE_PID} {SESSION_A}",
    }
    done = subprocess.run(
        [sys.executable, "-I", DISPATCHER, *FORCED],
        env=env,
        input=b"it's $(fine)\nreally",
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["sent"] is True
    assert stdin_of(world, "load-buffer") == ["it's $(fine)\nreally"]


@pytest.mark.parametrize(
    ("screen", "dialog"),
    [
        (DIALOG, True),
        ("Do you want to make this edit to parser.py?\n❯ 1. Yes\n  2. No\n", True),
        ("⏺ Pick one:\n 1. Yes, rewrite it\n 2. No, keep it\n> ", True),  # broader than the hub on purpose
        ("⏺ All done. Do you want to see the diff?\n> ", False),
        ("⏺ 1. Yes is the answer\n> ", False),
        (EMPTY_BOX, False),
    ],
)
def test_dialog_heuristic_errs_on_the_side_of_a_dialog(screen: str, dialog: bool) -> None:
    assert d.shows_dialog(screen) is dialog
