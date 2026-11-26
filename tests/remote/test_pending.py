"""`pending_input`: the screen heuristics, and the tool end to end on the fake ssh, the real dispatcher
and the fake tmux, plus the hub's own registry."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import SESSION_A, SESSION_B, SESSION_C, RemoteWorld, pane
from tests.remote.test_tools import Ctx, build, populate
from voice_coding_mcp.remote.pending import CAPTURE_LINES, MAX_CAPTURES, ScreenAsk, screen_ask

DIALOG = "\n".join(
    [
        "⏺ I fixed the parser and all 41 tests pass.",
        "╭───────────────────────────────────────────╮",
        "│ Bash command                              │",
        "│                                           │",
        "│   git push origin main                    │",
        "│   Push the branch                         │",
        "│                                           │",
        "│ Do you want to proceed?                   │",
        "│ ❯ 1. Yes                                  │",
        "│   2. Yes, and don't ask again for git push │",
        "│   3. No, and tell Claude what to do differently (esc) │",
        "╰───────────────────────────────────────────╯",
        "",
        "",
    ]
)
EDIT_DIALOG = "\n".join(
    [
        "────────────────────────────",
        " Edit file",
        "   1 - old line",
        "   1 + new line",
        " Do you want to make this edit to parser.py?",
        " ❯ 1. Yes",
        "   2. Yes, allow all edits during this session (shift+tab)",
        "   3. No, and tell Claude what to do differently (esc)",
    ]
)
QUESTION = "\n".join(
    [
        "⏺ The old endpoint is still used by two clients.",
        "  Should I keep the old endpoint for one more release?",
        "╭──────────────────────────────╮",
        "│ > yes do                     │",
        "╰──────────────────────────────╯",
        "  ? for shortcuts",
    ]
)


# --- heuristics ---------------------------------------------------------------------------------


def test_a_permission_dialog_is_named_by_its_first_lines() -> None:
    assert screen_ask(DIALOG) == ScreenAsk("permission", "Bash command git push origin main")


def test_a_specific_dialog_question_is_quoted_as_is() -> None:
    assert screen_ask(EDIT_DIALOG) == ScreenAsk("permission", "Do you want to make this edit to parser.py?")


def test_numbered_yes_and_no_options_are_a_dialog() -> None:
    assert screen_ask("Allow this?\n 1. Yes\n 2. No\n") == ScreenAsk("permission", "Allow this?")
    # Nothing to quote between the box edge and the options.
    assert screen_ask("────\n 1. Yes\n 2. No\n") == ScreenAsk("permission", None)
    assert screen_ask("1. Yes, it builds\nnothing else") is None


def test_the_last_assistant_line_above_the_input_box_ending_in_a_question_mark() -> None:
    assert screen_ask(QUESTION) == ScreenAsk("question", "Should I keep the old endpoint for one more release?")
    without_box = "⏺ Which branch should I use?"
    assert screen_ask(without_box) == ScreenAsk("question", "Which branch should I use?")


def test_no_question_when_the_last_line_says_something_else() -> None:
    assert screen_ask("⏺ Should I go on?\n⏺ Done, all tests pass.\n╭──╮\n│ > │\n╰──╯") is None
    assert screen_ask("") is None
    assert screen_ask("\n\n\n") is None


def test_only_the_tail_of_the_screen_counts() -> None:
    old = DIALOG + "\n" + "\n".join(f"line {i}" for i in range(CAPTURE_LINES))
    assert screen_ask(old) is None


def test_questions_are_redacted_and_clipped() -> None:
    secret = "Bash command\n  curl -H 'Authorization: Bearer abcdefghijklmnop' example\nDo you want to proceed?\n 1. Yes\n 2. No"
    ask = screen_ask(secret)
    assert ask is not None
    assert ask.text is not None
    assert "abcdefghijklmnop" not in ask.text
    assert "[secret]" in ask.text
    long = "⏺ " + "why " * 60 + "?"
    asked = screen_ask(long)
    assert asked is not None
    assert asked.text is not None
    assert len(asked.text) == 100


# --- the tool -------------------------------------------------------------------------------------


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    world = RemoteWorld.create(tmp_path)
    populate(world)
    world.configure(modes={"example-host": "connect_timeout"})
    c = await build(tmp_path, world, summary=False)
    yield c
    await c.manager.shutdown("test finished")


async def test_registry_waits_on_every_machine(ctx: Ctx) -> None:
    answer = await ctx.tools.pending_input(None)
    assert not answer.is_error
    assert answer.text == (
        "2 sessions need you.\n"
        'On dev, work-api asks for permission: "permission prompt".\n'
        'On dev, notes asks: "user answer".\n'
        "Approve it at the keyboard or in the Claude app.\n"
        "buildbox could not be checked: the connection timed out."
    )
    # Only the two waiting panes were captured, with the short tail; never the busy one or a shell.
    captures = [call["argv"] for call in ctx.world.tmux_calls() if "capture-pane" in call["argv"]]
    assert sorted(argv[argv.index("-t") + 1] for argv in captures) == ["%3", "%4"]
    assert all(argv[-1] == f"-{CAPTURE_LINES}" for argv in captures)
    # The Claude Code session name ("billing fix") is not how a pane is named here.
    assert "billing fix" not in answer.text


def _world_with(tmp: Path, panes: list[dict[str, Any]], texts: dict[str, str], **forced: Any) -> RemoteWorld:
    world = RemoteWorld.create(tmp, **forced)
    world.set_panes(panes, texts)
    world.configure(modes={"example-host": "connect_timeout"})
    return world


async def test_screens_of_idle_panes_and_the_dialog_title_of_a_waiting_one(tmp_path: Path) -> None:
    world = _world_with(
        tmp_path,
        [
            pane("fpga", "%1", 9_999_801, command="claude"),
            pane("notes", "%2", 9_999_802, command="claude"),
            pane("work-a", "%3", 9_999_803, command="claude"),
            pane("work-b", "%4", 9_999_804, command="claude"),
        ],
        {"%1": DIALOG, "%2": QUESTION, "%3": "⏺ Done.", "%4": DIALOG},
    )
    pid = os.getpid()
    world.register("2001", pid, sessionId=SESSION_A, status="waiting", waitingFor="permission prompt", tmux="0:@0.%1")
    world.register("2002", pid, sessionId=SESSION_B, status="idle", tmux="1:@1.%2")
    world.register("2003", pid, sessionId=SESSION_C, status="idle", tmux="2:@2.%3")
    world.register("2004", pid, sessionId="11111111-0000-4000-8000-00000000000d", status="busy", tmux="3:@3.%4")
    c = await build(tmp_path, world, summary=False)
    try:
        answer = await c.tools.pending_input("dev")
        assert answer.text == (
            "2 sessions need you.\n"
            'On dev, fpga asks for permission: "Bash command git push origin main".\n'
            'On dev, notes asks: "Should I keep the old endpoint for one more release?"'
            ".\nApprove it at the keyboard or in the Claude app."
        )
        # A busy pane is never captured, whatever its screen shows.
        captured = {
            call["argv"][call["argv"].index("-t") + 1] for call in world.tmux_calls() if "capture-pane" in call["argv"]
        }
        assert captured == {"%1", "%2", "%3"}
    finally:
        await c.manager.shutdown("test finished")


async def test_nobody_waiting(tmp_path: Path) -> None:
    world = _world_with(tmp_path, [pane("fpga", "%1", 9_999_801, command="claude")], {"%1": "⏺ Done."})
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="idle", tmux="0:@0.%1")
    c = await build(tmp_path, world, summary=False)
    try:
        assert (await c.tools.pending_input("dev")).text == "Nobody is waiting for you."
        everywhere = await c.tools.pending_input(None)
        assert everywhere.text == (
            "Nobody is waiting for you on the machines I could check.\n"
            "buildbox could not be checked: the connection timed out."
        )
    finally:
        await c.manager.shutdown("test finished")


async def test_at_most_six_captures_per_machine_and_the_rest_is_reported(tmp_path: Path) -> None:
    count = MAX_CAPTURES + 2
    panes = [pane(f"work-{i}", f"%{i + 1}", 9_999_800 + i, command="claude") for i in range(count)]
    world = _world_with(tmp_path, panes, {f"%{i + 1}": "⏺ Done." for i in range(count)})
    for i in range(count):
        world.register(
            str(3000 + i),
            os.getpid(),
            sessionId=f"11111111-0000-4000-8000-0000000001{i:02d}",
            status="idle",
            tmux=f"{i}:@{i}.%{i + 1}",
        )
    c = await build(tmp_path, world, summary=False)
    try:
        answer = await c.tools.pending_input("dev")
        assert (
            answer.text
            == "Nobody is waiting for you on the machines I could check.\nSome sessions on dev could not be checked."
        )
        captures = [call for call in world.tmux_calls() if "capture-pane" in call["argv"]]
        assert len(captures) == MAX_CAPTURES
    finally:
        await c.manager.shutdown("test finished")


async def test_the_hub_allow_list_hides_waiting_sessions_the_dispatcher_allows(tmp_path: Path) -> None:
    world = _world_with(
        tmp_path,
        [pane("private", "%1", 9_999_801, command="claude"), pane("fpga", "%2", 9_999_802, command="claude")],
        {"%1": DIALOG},
        forced=["--sessions", "*"],
    )
    world.register(
        "2001", os.getpid(), sessionId=SESSION_A, status="waiting", waitingFor="permission prompt", tmux="0:@0.%1"
    )
    world.register("2002", os.getpid(), sessionId=SESSION_B, status="idle", tmux="1:@1.%2")
    c = await build(tmp_path, world, summary=False, dev_sessions=["fpga"])
    try:
        assert (await c.tools.pending_input("dev")).text == "Nobody is waiting for you."
        assert not any("%1" in call["argv"] for call in world.tmux_calls() if "capture-pane" in call["argv"])
    finally:
        await c.manager.shutdown("test finished")


async def test_waiting_for_text_is_cleaned_and_redacted(tmp_path: Path) -> None:
    world = _world_with(tmp_path, [pane("fpga", "%1", 9_999_801, command="claude")], {})
    world.register(
        "2001",
        os.getpid(),
        sessionId=SESSION_A,
        status="waiting",
        waitingFor="use token=hunter2secret\u202e now\x1b[31m?",
        tmux="0:@0.%1",
    )
    c = await build(tmp_path, world, summary=False)
    try:
        answer = await c.tools.pending_input("dev")
        # The dispatcher drops the escape and bidi characters; the hub redacts the token.
        assert answer.text == '1 session needs you.\nOn dev, fpga asks: "use token=[secret] now[31m?".'
    finally:
        await c.manager.shutdown("test finished")


async def test_two_claude_panes_in_a_session_are_named_by_window(tmp_path: Path) -> None:
    world = _world_with(
        tmp_path,
        [
            pane("fpga", "%1", 9_999_801, command="claude"),
            pane("fpga", "%2", 9_999_802, window_index=1, command="claude"),
        ],
        {},
    )
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="busy", tmux="0:@0.%1")
    world.register("2002", os.getpid(), sessionId=SESSION_B, status="waiting", waitingFor="your answer", tmux="0:@1.%2")
    c = await build(tmp_path, world, summary=False)
    try:
        answer = await c.tools.pending_input("dev")
        assert answer.text == '1 session needs you.\nOn dev, fpga window 1 asks: "your answer".'
    finally:
        await c.manager.shutdown("test finished")


def _write_local(home: Path, name: str, data: dict[str, Any]) -> None:
    sessions = home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{name}.json").write_text(json.dumps({"pid": os.getpid(), **data}), encoding="utf-8")


async def test_local_sessions_outside_tmux_from_the_hub_registry(tmp_path: Path) -> None:
    world = _world_with(tmp_path, [], {})
    projects = tmp_path / "projects"
    (projects / "alpha").mkdir(parents=True)
    hub = tmp_path / "hub-claude"
    alpha = str(projects / "alpha")
    ids = [f"22222222-0000-4000-8000-00000000000{i}" for i in range(6)]
    _write_local(
        hub,
        "1",
        {
            "sessionId": ids[0],
            "cwd": alpha,
            "status": "waiting",
            "waitingFor": "permission prompt",
            "name": "billing fix",
        },
    )
    _write_local(
        hub, "2", {"sessionId": ids[1], "cwd": alpha, "status": "waiting", "waitingFor": "answer", "tmux": "0:@0.%9"}
    )
    _write_local(
        hub, "3", {"sessionId": ids[2], "cwd": alpha, "status": "waiting", "waitingFor": "x", "entrypoint": "sdk-cli"}
    )
    _write_local(hub, "4", {"sessionId": ids[3], "cwd": str(tmp_path), "status": "waiting", "waitingFor": "outside"})
    _write_local(hub, "5", {"sessionId": ids[4], "cwd": alpha, "status": "idle"})
    c = await build(tmp_path, world, summary=False, claude_home=str(hub))
    try:
        answer = await c.tools.pending_input(None)
        assert answer.text == (
            "1 session needs you.\n"
            'On this machine, session "billing fix" in alpha asks for permission: "permission prompt".\n'
            "Approve it at the keyboard or in the Claude app.\n"
            "buildbox could not be checked: the connection timed out."
        )
        # One machine asked for: the hub's own registry is not part of it.
        assert (await c.tools.pending_input("dev")).text == "Nobody is waiting for you."
    finally:
        await c.manager.shutdown("test finished")


async def test_unknown_machine(ctx: Ctx) -> None:
    answer = await ctx.tools.pending_input("laptop")
    assert answer.is_error
    assert answer.text == 'No machine matches "laptop". Machines: dev, buildbox.'


async def test_a_slow_machine_does_not_hold_the_answer_past_the_deadline(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world)
    world.configure(modes={"example-host": "timeout"})
    c = await build(tmp_path, world, summary=False, sync_wait=5)
    try:
        started = time.monotonic()
        answer = await c.tools.pending_input(None)
        assert time.monotonic() - started < 4.5
        assert answer.text.startswith("2 sessions need you.\n")
        assert answer.text.endswith("buildbox could not be checked: the connection timed out.")
    finally:
        await c.manager.shutdown("test finished")
