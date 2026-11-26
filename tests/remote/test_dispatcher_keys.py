"""The dispatcher's `keys` and `resize` verbs against the fake tmux: strict request validation (only
key names from `KEY_NAMES`, never free text, modifiers or tmux syntax), the send opt-ins, rechecks of
the pane, its process and Claude Code session, the binding to the menu or permission dialog the owner
heard, and the fixed argv shapes of every tmux call. Screens are synthetic."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import DISPATCHER, SESSION_A, SESSION_B, RemoteWorld, pane
from tests.remote.menu_fixtures import capture, menu_lines
from voice_coding_mcp.remote import dispatcher as d

SESSIONS = ["--sessions", "fpga,notes,work-*"]
FORCED = [*SESSIONS, "--allow-send"]
CLAUDE_PID = 9_100_101
SHELL_PID = 9_100_102
MENU = capture(menu_lines(), rows=30, alternate=False)
MENU_HASH = d.menu_hash(MENU)
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
    "  Esc to cancel\n"
)
DIALOG_HASH = d.dialog_hash(DIALOG)


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path)
    world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", SHELL_PID, window_index=1, window_name="shell", command="bash"),
            pane("private", "%3", 9_100_103, command="claude"),
        ],
        {"%1": MENU, "%2": "$ make\nok\n$ ", "%3": MENU},
    )
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="waiting", waitingFor="user answer", tmux="0:@0.%1")
    return world


def call(world: RemoteWorld, request: str, forced: list[str] | None = None) -> dict[str, Any]:
    env = {**world.tmux_env(), "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22"}
    env["SSH_ORIGINAL_COMMAND"] = f"switchboard-remote {request}"
    return d.handle(forced if forced is not None else FORCED, env)


def keys(world: RemoteWorld, *names: str, bound: str | None = None, forced: list[str] | None = None) -> dict[str, Any]:
    mode = bound if bound is not None else f"menu {MENU_HASH}"
    return call(world, f"keys %1 {CLAUDE_PID} {SESSION_A} {mode} {' '.join(names)}", forced)


def tmux_argvs(world: RemoteWorld) -> list[list[str]]:
    return [c["argv"] for c in world.tmux_calls()]


def pressed(world: RemoteWorld) -> list[list[str]]:
    return [argv for argv in tmux_argvs(world) if argv[:1] == ["send-keys"]]


def assert_fixed_shapes(world: RemoteWorld, pane_id: str = "%1") -> None:
    for argv in tmux_argvs(world):
        if argv[:1] == ["list-panes"]:
            assert argv == ["list-panes", "-a", "-F", d.LIST_FORMAT]
        elif argv[:1] == ["capture-pane"]:
            assert argv == ["capture-pane", "-p", "-J", "-t", pane_id, "-S", f"-{d.SEND_CHECK_LINES}"]
        elif argv[:1] == ["send-keys"]:
            assert argv[:3] == ["send-keys", "-t", pane_id]
            assert "-l" not in argv
            assert 1 <= len(argv[3:]) <= d.MAX_KEYS
            assert all(key in d.KEY_NAMES for key in argv[3:])
        elif argv[:1] == ["resize-window"]:
            assert argv[:3] == ["resize-window", "-t", pane_id]
            assert argv[3] == "-x"
            assert argv[5] == "-y"
        else:
            raise AssertionError(f"unexpected tmux call {argv}")


# --- the happy path ----------------------------------------------------------------------------


def test_presses_the_keys_in_one_send_keys_call(world: RemoteWorld) -> None:
    out = keys(world, "Down", "Down", "Enter")
    assert out == {"protocol": 1, "ok": True, "sent": True, "keys": 3}
    assert pressed(world) == [["send-keys", "-t", "%1", "Down", "Down", "Enter"]]
    assert_fixed_shapes(world)
    scenario = json.loads(Path(world.scenario).read_text(encoding="utf-8"))
    assert scenario["keys"] == [{"pane": "%1", "keys": ["Down", "Down", "Enter"]}]
    assert not any(argv[:1] in (["load-buffer"], ["paste-buffer"]) for argv in tmux_argvs(world))


@pytest.mark.parametrize("name", d.KEY_NAMES)
def test_every_allowed_key_name(world: RemoteWorld, name: str) -> None:
    assert keys(world, name)["ok"] is True
    assert pressed(world) == [["send-keys", "-t", "%1", name]]


def test_version_lists_the_new_verbs(world: RemoteWorld) -> None:
    out = call(world, "version")
    assert "keys" in out["verbs"]
    assert "resize" in out["verbs"]
    assert d.VERSION == "0.5.0"


# --- request validation ------------------------------------------------------------------------

BAD_KEYS = [
    "C-c",
    "M-x",
    "c-c",
    "C-m",
    "^C",
    "Space",
    "BSpace",
    "enter",
    "ENTER",
    "0",
    "10",
    "yes",
    "send-keys",
    "kill-server",
    "q",
    "Y",
    "-t",
    "-l",
    "%2",
]


@pytest.mark.parametrize("name", BAD_KEYS)
def test_unknown_key_names_are_bad_requests(world: RemoteWorld, name: str) -> None:
    assert keys(world, name) == {"protocol": 1, "ok": False, "error": "bad_request"}
    assert keys(world, "Down", name, "Enter")["error"] == "bad_request"
    assert pressed(world) == []


@pytest.mark.parametrize(
    "request_line",
    [
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu {MENU_HASH}",  # no keys
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu {MENU_HASH} " + " ".join(["Down"] * (d.MAX_KEYS + 1)),
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu Enter",  # hash missing
        f"keys %1 {CLAUDE_PID} {SESSION_A} message none Enter",
        f"keys %1 {CLAUDE_PID} {SESSION_A} answer xyz Enter",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu {'A' * 24} Enter",
        f"keys %1 {CLAUDE_PID} not-a-uuid menu none Enter",
        f"keys 1 {CLAUDE_PID} {SESSION_A} menu none Enter",
        f"keys %1 -5 {SESSION_A} menu none Enter",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none 'Down Enter'",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none Enter ; send-keys -t %2 C-c",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none Enter \\; kill-server",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none $(id)",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none '#{{pane_id}}'",
        f"keys %1 {CLAUDE_PID} {SESSION_A} menu none 'Enter\n'",
    ],
)
def test_malformed_keys_requests(world: RemoteWorld, request_line: str) -> None:
    assert call(world, request_line) == {"protocol": 1, "ok": False, "error": "bad_request"}
    assert pressed(world) == []


def test_keys_need_allow_send(world: RemoteWorld) -> None:
    assert keys(world, "Enter", forced=SESSIONS) == {"protocol": 1, "ok": False, "error": "not_allowed"}
    assert tmux_argvs(world) == []


# --- rechecks ----------------------------------------------------------------------------------


def test_pane_pid_and_session_are_rechecked(world: RemoteWorld) -> None:
    assert call(world, f"keys %9 {CLAUDE_PID} {SESSION_A} menu {MENU_HASH} Enter")["error"] == "gone"
    assert call(world, f"keys %3 9100103 {SESSION_A} menu {MENU_HASH} Enter")["error"] == "gone", "not allowed"
    assert call(world, f"keys %1 1234 {SESSION_A} menu {MENU_HASH} Enter")["error"] == "changed"
    assert call(world, f"keys %1 {CLAUDE_PID} {SESSION_B} menu {MENU_HASH} Enter")["error"] == "changed"
    assert call(world, f"keys %2 {SHELL_PID} none menu none Enter")["error"] == "not_claude"
    assert pressed(world) == []


def test_a_shell_pane_only_with_send_to_any_pane(world: RemoteWorld) -> None:
    out = call(world, f"keys %2 {SHELL_PID} none menu none y Enter", [*FORCED, "--send-to-any-pane"])
    assert out["ok"] is True
    assert pressed(world) == [["send-keys", "-t", "%2", "y", "Enter"]]


def test_a_changed_menu_is_refused(world: RemoteWorld) -> None:
    world.set_panes(
        [pane("fpga", "%1", CLAUDE_PID, command="claude")],
        {"%1": capture(menu_lines(selected=2), rows=30, alternate=False)},
    )
    assert keys(world, "Enter") == {"protocol": 1, "ok": False, "error": "menu_changed"}
    assert keys(world, "Enter", bound="menu none")["error"] == "menu_changed", "a menu the owner did not hear"
    assert pressed(world) == []


def test_a_menu_that_closed_is_refused(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": EMPTY_BOX})
    assert keys(world, "Enter")["error"] == "menu_changed"
    assert keys(world, "Escape", bound="menu none")["ok"] is True


def test_without_a_menu_draft_and_vim_are_refused(world: RemoteWorld) -> None:
    draft = EMPTY_BOX.replace("│ >                │", "│ > half typed     │")
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": draft})
    assert keys(world, "Enter", bound="menu none")["error"] == "draft_present"
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": EMPTY_BOX + "-- NORMAL --\n"})
    assert keys(world, "Enter", bound="menu none")["error"] == "vim_normal"
    assert pressed(world) == []


@pytest.mark.parametrize("mode_key", d.MODE_KEYS)
def test_tab_and_shift_tab_need_a_bound_menu_or_dialog(world: RemoteWorld, mode_key: str) -> None:
    """Shift+Tab at an idle Claude Code input cycles its permission mode (accept edits, bypass):
    never with no menu bound, whatever the opt-ins."""
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": EMPTY_BOX})
    everything = [*FORCED, "--allow-permission-answers", "--send-to-any-pane"]
    assert keys(world, mode_key, bound="menu none") == {"protocol": 1, "ok": False, "error": "mode_keys"}
    assert keys(world, "Down", mode_key, "Enter", bound="menu none", forced=everything)["error"] == "mode_keys"
    shell = call(world, f"keys %2 {SHELL_PID} none menu none {mode_key}", everything)
    assert shell["error"] == "mode_keys"
    assert pressed(world) == []
    # Into a menu the owner heard, and into a permission dialog, they still go.
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": MENU})
    assert keys(world, mode_key)["ok"] is True
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": DIALOG})
    assert keys(world, mode_key, bound=f"answer {DIALOG_HASH}", forced=everything)["ok"] is True
    assert pressed(world) == [["send-keys", "-t", "%1", mode_key]] * 2


def test_a_permission_dialog_needs_answer_mode_and_the_opt_in(world: RemoteWorld) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": DIALOG})
    assert DIALOG_HASH is not None
    # Read back as a menu: never into a permission dialog.
    assert keys(world, "Enter", bound=f"menu {d.menu_hash(DIALOG)}")["error"] == "permission_prompt"
    # Answer mode without --allow-permission-answers.
    assert keys(world, "Down", "Enter", bound=f"answer {DIALOG_HASH}")["error"] == "permission_prompt"
    allowed = [*FORCED, "--allow-permission-answers"]
    assert keys(world, "Down", "Enter", bound=f"answer {'0' * 24}", forced=allowed)["error"] == "dialog_changed"
    assert pressed(world) == []
    assert keys(world, "Down", "Enter", bound=f"answer {DIALOG_HASH}", forced=allowed)["ok"] is True
    assert pressed(world) == [["send-keys", "-t", "%1", "Down", "Enter"]]


def test_answer_mode_without_a_dialog_is_refused(world: RemoteWorld) -> None:
    allowed = [*FORCED, "--allow-permission-answers"]
    assert keys(world, "1", bound=f"answer {DIALOG_HASH}", forced=allowed)["error"] == "dialog_changed"
    assert pressed(world) == []


def test_a_registry_permission_wait_counts_as_a_dialog(world: RemoteWorld) -> None:
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="waiting", waitingFor="permission", tmux="0:@0.%1")
    assert keys(world, "Enter")["error"] == "permission_prompt"


def test_tmux_failure(world: RemoteWorld) -> None:
    world.set_panes(
        [pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": MENU}, failOn=["send-keys", "resize-window"]
    )
    assert keys(world, "Enter")["error"] == "tmux_failed"
    assert call(world, f"resize %1 {CLAUDE_PID} 120 40")["error"] == "tmux_failed"


# --- resize ------------------------------------------------------------------------------------


def test_resize_runs_resize_window(world: RemoteWorld) -> None:
    out = call(world, f"resize %1 {CLAUDE_PID} 120 40")
    assert out == {"protocol": 1, "ok": True, "resized": True, "columns": 120, "rows": 40}
    assert [argv for argv in tmux_argvs(world) if argv[0] == "resize-window"] == [
        ["resize-window", "-t", "%1", "-x", "120", "-y", "40"]
    ]
    assert_fixed_shapes(world)


@pytest.mark.parametrize(
    "args",
    [
        "%1 9100101 79 40",
        "%1 9100101 301 40",
        "%1 9100101 120 23",
        "%1 9100101 120 121",
        "%1 9100101 120",
        "%1 9100101 120 40 1",
        "%1 9100101 -120 40",
        "%1 9100101 120x40",
        "1 9100101 120 40",
        "%1 9100101 1e3 40",
    ],
)
def test_resize_validation(world: RemoteWorld, args: str) -> None:
    assert call(world, f"resize {args}") == {"protocol": 1, "ok": False, "error": "bad_request"}
    assert tmux_argvs(world) == []


def test_resize_policy_and_rechecks(world: RemoteWorld) -> None:
    assert call(world, f"resize %1 {CLAUDE_PID} 120 40", SESSIONS)["error"] == "not_allowed"
    assert call(world, "resize %1 1234 120 40")["error"] == "changed"
    assert call(world, "resize %3 9100103 120 40")["error"] == "gone"
    assert call(world, "resize %9 1 120 40")["error"] == "gone"
    assert not any(argv[0] == "resize-window" for argv in tmux_argvs(world))


def test_resize_a_pane_without_claude_code_only_with_send_to_any_pane(world: RemoteWorld) -> None:
    assert call(world, f"resize %2 {SHELL_PID} 120 40")["error"] == "not_claude"
    assert not any(argv[0] == "resize-window" for argv in tmux_argvs(world))
    out = call(world, f"resize %2 {SHELL_PID} 120 40", [*FORCED, "--send-to-any-pane"])
    assert out["ok"] is True
    assert [argv for argv in tmux_argvs(world) if argv[0] == "resize-window"] == [
        ["resize-window", "-t", "%2", "-x", "120", "-y", "40"]
    ]


def test_the_script_answers_keys_like_handle(world: RemoteWorld) -> None:
    """The deployed file, run as a forced command, answers the same (stdlib only, Python 3.8 syntax)."""
    env = {
        **world.tmux_env(),
        "SSH_CONNECTION": "127.0.0.1 1 127.0.0.1 22",
        "SSH_ORIGINAL_COMMAND": f"switchboard-remote keys %1 {CLAUDE_PID} {SESSION_A} menu {MENU_HASH} Down Enter",
    }
    done = subprocess.run(
        [sys.executable, "-I", DISPATCHER, *FORCED], env=env, capture_output=True, timeout=30, check=True
    )
    assert json.loads(done.stdout) == {"protocol": 1, "ok": True, "sent": True, "keys": 2}
