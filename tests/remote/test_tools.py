"""`list_remote_sessions` and `remote_status` end to end: RemoteTools with the real ssh transport on the
fake ssh, the real dispatcher on the fake tmux, and the summariser on the fake claude."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import claude_config, read_records
from tests.remote.helpers import SESSION_A, SESSION_B, SESSION_C, RemoteWorld, machine, pane
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote.tools import RemoteTools, RemoteToolsOptions
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions


@dataclass
class Ctx:
    world: RemoteWorld
    tools: RemoteTools
    manager: TaskManager
    config: Config
    record: str


async def build(
    tmp: Path,
    world: RemoteWorld,
    *,
    summary: bool = True,
    sync_wait: int = 20,
    dev_sessions: list[str] | None = None,
    **remote: Any,
) -> Ctx:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    record = str(tmp / "record.jsonl")
    claude = claude_config(record)
    config = parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(tmp / "state"),
            "projectRoots": [str(projects)],
            "claude": {"bin": claude.bin, "env": dict(claude.env)},
            "tasks": {"syncWaitSeconds": sync_wait, "maxConcurrent": 1},
            "remote": {
                "machines": [
                    machine("dev", world=world, sessions=dev_sessions or ["fpga", "notes", "work-*", "private"]),
                    machine("buildbox", world=world, host="example-host", user="bot", sessions=["api"]),
                ],
                "ssh": {"bin": world.ssh, "commandTimeoutSeconds": 8},
                "summary": {"enabled": summary, "timeoutSeconds": 10},
                **remote,
            },
        },
        home_dir="/nonexistent-home-dir",
    )
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=silent_logger,
            kill_grace_ms=200,
        )
    )
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp)}
    tools = RemoteTools(RemoteToolsOptions(config=config, manager=manager, logger=silent_logger, base_env=env))
    return Ctx(world=world, tools=tools, manager=manager, config=config, record=record)


def populate(world: RemoteWorld, texts: dict[str, str] | None = None) -> None:
    world.set_panes(
        [
            pane("fpga", "%1", 9_999_801, command="claude"),
            pane("fpga", "%2", 9_999_802, window_index=1, window_name="logs", command="tail"),
            pane("notes", "%3", 9_999_803, command="claude"),
            pane("work-api", "%4", 9_999_804, command="claude"),
            pane("work-shell", "%5", 9_999_805, command="bash"),
            pane("private", "%6", 9_999_806),
        ],
        texts
        or {
            "%1": "Ran 41 tests\nAll 41 tests passed\n╭──╮\n│ > │\n╰──╯\n? for shortcuts",
            "%5": "$ make\nbuild finished in 3 minutes\nexport TOKEN=abcdef123456\n$ ",
            "%6": "private stuff",
        },
    )
    pid = os.getpid()
    world.register("2001", pid, sessionId=SESSION_A, name="fpga", status="busy", tmux="0:@0.%1")
    world.register(
        "2002", pid, sessionId=SESSION_B, name="notes", status="waiting", waitingFor="user answer", tmux="1:@2.%3"
    )
    world.register(
        "2003",
        pid,
        sessionId=SESSION_C,
        name="billing fix",
        status="waiting",
        waitingFor="permission prompt",
        tmux="2:@3.%4",
    )


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    world = RemoteWorld.create(tmp_path)
    populate(world)
    world.configure(modes={"example-host": "connect_timeout"})
    c = await build(tmp_path, world, summary=False)
    yield c
    await c.manager.shutdown("test finished")


async def test_list_all_machines(ctx: Ctx) -> None:
    answer = await ctx.tools.list_remote_sessions(None)
    assert not answer.is_error
    assert answer.text == (
        "2 machines.\n"
        "dev: 4 sessions. fpga, Claude Code working. notes, Claude Code waiting for your answer. "
        'work-api, Claude Code "billing fix" waiting for your permission. work-shell, no Claude Code.\n'
        "buildbox: not reachable, the connection timed out."
    )
    # The dispatcher's forced command does not allow "private", whatever the hub config says.
    assert "private" not in answer.text


async def test_list_one_machine_by_spoken_name(ctx: Ctx) -> None:
    answer = await ctx.tools.list_remote_sessions("build box")
    assert answer.text == "buildbox: not reachable, the connection timed out."
    missing = await ctx.tools.list_remote_sessions("laptop")
    assert missing.is_error
    assert missing.text == 'No machine matches "laptop". Machines: dev, buildbox.'


async def test_many_sessions_are_cut(ctx: Ctx) -> None:
    ctx.world.set_panes([pane(f"work-{i}", f"%{i}", 9_999_700 + i) for i in range(11)])
    answer = await ctx.tools.list_remote_sessions("dev")
    assert answer.text.startswith("dev: 11 sessions. work-0, no Claude Code.")
    assert answer.text.endswith("work-7, no Claude Code. And 3 more.")


async def test_two_claude_panes_in_one_session(ctx: Ctx) -> None:
    ctx.world.set_panes([pane("fpga", "%1", 9_999_901), pane("fpga", "%2", 9_999_902, window_index=1)])
    ctx.world.register("2004", os.getpid(), sessionId=SESSION_B, status="idle", tmux="0:@1.%2")
    answer = await ctx.tools.list_remote_sessions("dev")
    assert answer.text == "dev: 1 session. fpga, 2 Claude Code panes: one working, one idle."


async def test_status_heuristic(ctx: Ctx) -> None:
    answer = await ctx.tools.remote_status("dev", "fpga", None)
    assert answer.text == 'dev, fpga: Claude Code is working. The screen says: "All 41 tests passed"'
    shell = await ctx.tools.remote_status("dev", "work shell", None)
    assert shell.text == 'dev, work-shell: no Claude Code here. The screen says: "export TOKEN=[secret]"'
    named = await ctx.tools.remote_status("dev", "billing fix", 20)
    assert named.text == 'dev, work-api: Claude Code "billing fix" is waiting for your permission. The screen is empty.'
    assert ctx.world.tmux_calls()[-1]["argv"][-1] == "-20"


async def test_status_target_problems(ctx: Ctx) -> None:
    missing = await ctx.tools.remote_status("dev", "private", None)
    assert missing.is_error
    assert missing.text == 'dev has no session called "private". Its sessions: fpga, notes, work-api, work-shell.'
    which = await ctx.tools.remote_status("dev", None, None)
    assert which.text == "Which session on dev? Its sessions: fpga, notes, work-api, work-shell."
    machine_missing = await ctx.tools.remote_status(None, "fpga", None)
    assert machine_missing.text == "Which machine? dev, buildbox."
    down = await ctx.tools.remote_status("buildbox", "api", None)
    assert down.text == "buildbox is not reachable: the connection timed out."


async def test_a_pane_that_disappears_between_list_and_capture(ctx: Ctx) -> None:
    await ctx.tools.remote_status("dev", "notes", None)
    calls = len(ctx.world.ssh_calls())
    ctx.world.set_panes([pane("fpga", "%1", 9_999_801)])
    gone = await ctx.tools.remote_status("dev", "notes", None)
    # The list came from the 15 s cache (one ssh call, the capture), and the capture found no pane.
    assert len(ctx.world.ssh_calls()) == calls + 1
    assert gone.text == "dev, notes: that pane is gone. Ask me to list the sessions again."


@pytest.fixture
async def wide(tmp_path: Path) -> AsyncIterator[Ctx]:
    """The dispatcher allows every session (forced `--sessions '*'`); the hub allows fewer."""
    world = RemoteWorld.create(tmp_path, forced=["--sessions", "*"])
    populate(world)
    world.configure(modes={"example-host": "connect_timeout"})
    c = await build(tmp_path, world, summary=False, dev_sessions=["fpga", "notes", "work-*"])
    yield c
    await c.manager.shutdown("test finished")


async def test_the_hub_allow_list_filters_what_the_dispatcher_allows(wide: Ctx) -> None:
    # The dispatcher reports "private"; only the hub's own list keeps it out.
    answer = await wide.tools.list_remote_sessions(None)
    assert "private" not in answer.text
    assert answer.text.startswith("2 machines.\ndev: 4 sessions. fpga, ")
    private = await wide.tools.remote_status("dev", "private", None)
    missing = await wide.tools.remote_status("dev", "nosuch", None)
    assert private.is_error
    assert missing.is_error
    assert private.text == missing.text.replace('"nosuch"', '"private"')
    assert private.text == 'dev has no session called "private". Its sessions: fpga, notes, work-api, work-shell.'
    # Not even by window target or by Claude Code session name, and nothing was captured.
    assert (await wide.tools.remote_status("dev", "private:0", None)).text == private.text.replace(
        '"private"', '"private:0"'
    )
    assert not any("capture-pane" in call["argv"] for call in wide.world.tmux_calls())


async def test_a_pane_that_moved_to_a_session_only_the_dispatcher_allows(wide: Ctx) -> None:
    await wide.tools.remote_status("dev", "notes", None)  # caches the list
    moved = pane("private", "%3", 9_999_803, command="claude")
    wide.world.set_panes([moved], {"%3": "the private screen"})
    answer = await wide.tools.remote_status("dev", "notes", None)
    assert answer.text == "dev, notes: that pane is gone. Ask me to list the sessions again."
    assert "private screen" not in answer.text


async def test_summary_answers_in_time_and_is_cached(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(
        world, {"%1": "Running the build\nRESULT It is building the parser and waits for nothing. key token=s3cr3t"}
    )
    c = await build(tmp_path, world)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == (
            "dev, fpga: Claude Code is working. A summary of the screen: "
            '"It is building the parser and waits for nothing. key token=[secret]"'
        )
        again = await c.tools.remote_status("dev", "fpga", None)
        assert again.text == answer.text
        runs = read_records(c.record)
        assert len(runs) == 1
        assert "--no-session-persistence" in runs[0]["argv"]
        assert "s3cr3t" not in runs[0]["stdin"]
        assert "token=[secret]" in runs[0]["stdin"]
    finally:
        await c.manager.shutdown("test finished")


async def test_slow_summary_becomes_a_task(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": "Working\nSLEEP 1500\nRESULT Finished the refactor."})
    c = await build(tmp_path, world, sync_wait=4)
    try:
        started = time.monotonic()
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert time.monotonic() - started < 4
        assert answer.text.startswith("dev, fpga: I am still reading it. That is task ")
        task_id = int(answer.text.split("task ")[1].split(";")[0])
        done = await c.manager.wait(task_id, 10_000)
        assert done is not None
        assert done.kind == "remote"
        assert done.status == "done"
        assert done.result == 'dev, fpga: Claude Code is working. A summary of the screen: "Finished the refactor."'
        assert done.title == "status of fpga on dev"
    finally:
        await c.manager.shutdown("test finished")


async def test_no_free_slot_answers_with_the_heuristic(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": "Compiling\nRESULT never used"})
    c = await build(tmp_path, world)
    try:
        lease = c.manager.claim_slot("other")
        assert lease is not None
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == 'dev, fpga: Claude Code is working. The screen says: "RESULT never used"'
        assert read_records(c.record) == []
        lease.release()
    finally:
        await c.manager.shutdown("test finished")


async def test_failed_summary_falls_back_to_the_heuristic(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": "Compiling\nFAIL"})
    c = await build(tmp_path, world)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == 'dev, fpga: Claude Code is working. The screen says: "FAIL"'
    finally:
        await c.manager.shutdown("test finished")


async def test_slow_machines_do_not_hold_the_answer_past_the_deadline(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world)
    world.configure(modes={"example-host": "timeout"})
    c = await build(tmp_path, world, summary=False, sync_wait=5)
    try:
        started = time.monotonic()
        answer = await c.tools.list_remote_sessions(None)
        assert time.monotonic() - started < 4.5
        assert answer.text.endswith("buildbox: not reachable, the connection timed out.")
        assert "dev: 4 sessions." in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_no_machines(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    c = await build(tmp_path, world, summary=False, machines=[])
    try:
        assert (await c.tools.list_remote_sessions(None)).text == "No machines are configured."
        assert (await c.tools.remote_status(None, None, None)).text == "No machines are configured."
    finally:
        await c.manager.shutdown("test finished")
