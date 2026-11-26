"""Every remote tool over the agent transport, end to end: RemoteTools with its AgentHub on a
socket, the real relay (in a thread, pipes for the SSH channel) and the real dispatcher in serve
mode (a child process on the fake tmux). Also the watcher's pushes, the spoken "not connected",
and `voice-coding-mcp status` and `remote-check` through the hub's control socket."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FakeClock, LogCapture, claude_config
from tests.remote.agent_helpers import MachineLink, agent_machine, agents_section, socket_dir, wait_until
from tests.remote.helpers import SESSION_A, SESSION_B, RemoteWorld, pane
from tests.remote.menu_fixtures import capture, menu_lines
from tests.test_cli import run_cli
from tests.usage.helpers import S1, assistant, user, write_transcript
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.audit import read_audit
from voice_coding_mcp.remote.tools import Caller, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.watcher import Watcher, WatcherOptions
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions
from voice_coding_mcp.usage.service import UsageService

T0 = 1_764_160_000_000
OWNER = Caller(fingerprint="len=36 sha256=0a0b0c0d", name="Claude")
CLAUDE_PID = 9_400_001
EMPTY_BOX = (
    "⏺ Done. All 41 tests pass.\n╭──────────────────╮\n│ >                │\n╰──────────────────╯\n  ? for shortcuts\n"
)
POLICY = ["--sessions", "fpga,notes,work-*", "--allow-send", "--allow-usage"]
MENU = capture(menu_lines(), rows=30, alternate=False)


@dataclass
class Ctx:
    world: RemoteWorld
    tools: RemoteTools
    config: Config
    config_path: str
    clock: FakeClock
    log: LogCapture
    socket: str
    links: list[MachineLink]
    manager: TaskManager

    def connect(self, policy: list[str] | None = None) -> MachineLink:
        link = MachineLink(self.world, self.socket, "laptop", POLICY if policy is None else policy)
        self.links.append(link)
        return link

    async def connected(self, policy: list[str] | None = None) -> MachineLink:
        assert self.tools.agents is not None
        before = self.tools.agents.live("laptop")
        link = self.connect(policy)
        await wait_until(lambda: self.tools.agents.live("laptop") not in (None, before))
        return link

    def later(self, seconds: float = 5) -> None:
        self.clock.t += int(seconds * 1000)

    def tmux(self, verb: str) -> list[dict[str, Any]]:
        return [c for c in self.world.tmux_calls() if c["argv"][:1] == [verb]]


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    with socket_dir() as directory:
        world = RemoteWorld.create(tmp_path)
        world.set_panes(
            [
                pane("fpga", "%1", CLAUDE_PID, command="claude"),
                pane("notes", "%2", 9_400_002, command="claude"),
                pane("private", "%3", 9_400_003),
            ],
            {"%1": EMPTY_BOX, "%2": "Do you want to proceed?\n❯ 1. Yes\n  2. No\n", "%3": "secret stuff"},
        )
        world.register("2001", os.getpid(), sessionId=SESSION_A, name="fpga", status="idle", tmux="0:@0.%1")
        world.register(
            "2002",
            os.getpid(),
            sessionId=SESSION_B,
            name="notes",
            status="waiting",
            waitingFor="user answer",
            tmux="1:@1.%2",
        )
        projects = tmp_path / "projects"
        projects.mkdir()
        claude = claude_config(str(tmp_path / "record.jsonl"))
        data = {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(directory / "state"),
            "projectRoots": [str(projects)],
            "claude": {"bin": claude.bin, "env": dict(claude.env), "configDir": str(tmp_path / "hub-claude")},
            "tasks": {"syncWaitSeconds": 20, "maxConcurrent": 1},
            "remote": {
                "machines": [agent_machine("laptop", allowSend=True)],
                "agents": agents_section(directory),
                "summary": {"enabled": False},
            },
            "watch": {"intervalSeconds": 60, "notifyUnreachable": True},
            "notify": {"url": "https://ntfy.example.com/topic"},
        }
        config = parse_config(data, home_dir="/nonexistent-home-dir")
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(data), encoding="utf-8")
        manager = await TaskManager.create(
            TaskManagerOptions(
                state_dir=config.state_dir,
                project_roots=config.project_roots,
                claude=config.claude,
                tasks=config.tasks,
                logger=LogCapture().logger,
                kill_grace_ms=200,
            )
        )
        clock = FakeClock(T0)
        log = LogCapture()
        ids = itertools.count(4172, 1111)
        tools = RemoteTools(
            RemoteToolsOptions(
                config=config,
                manager=manager,
                logger=log.logger,
                base_env={"PATH": os.environ["PATH"], "HOME": str(tmp_path)},
                now=clock,
                claude_home=str(tmp_path / "hub-claude"),
                new_confirmation_id=lambda: str(next(ids)),
            )
        )
        await tools.start()
        assert tools.agents is not None
        assert tools.agents.problem is None, tools.agents.problem
        context = Ctx(
            world=world,
            tools=tools,
            config=config,
            config_path=str(config_path),
            clock=clock,
            log=log,
            socket=str(directory / "hub.sock"),
            links=[],
            manager=manager,
        )
        try:
            yield context
        finally:
            await tools.stop()
            for link in context.links:
                link.stop()
            await manager.shutdown("test over")


async def test_not_connected_is_spoken_short(ctx: Ctx) -> None:
    assert (await ctx.tools.list_remote_sessions(None)).text == "laptop: not connected right now."
    answer = await ctx.tools.remote_status("laptop", "fpga", None)
    assert answer.text == "laptop is not connected right now."
    assert answer.is_error
    pending = await ctx.tools.pending_input("laptop")
    assert "laptop is not connected right now." in pending.text
    answer = await ctx.tools.remote_send("laptop", "fpga", "hello", OWNER)
    assert answer.text == "laptop is not connected right now."


async def test_list_status_and_pending_over_the_agent_transport(ctx: Ctx) -> None:
    await ctx.connected()
    listed = (await ctx.tools.list_remote_sessions(None)).text
    assert listed.startswith("laptop: 2 sessions. fpga, Claude Code idle. notes, Claude Code waiting for your answer.")
    assert "private" not in listed
    status = (await ctx.tools.remote_status("laptop", "fpga", None)).text
    assert "All 41 tests pass" in status
    pending = (await ctx.tools.pending_input(None)).text
    assert pending == '1 session needs you.\nOn laptop, notes asks: "Do you want to proceed?".'
    # The capture never asked tmux about the private session's pane.
    assert not any("%3" in c["argv"] for c in ctx.world.tmux_calls() if c["argv"][:1] == ["capture-pane"])


async def test_remote_send_and_confirm_over_the_agent_transport(ctx: Ctx) -> None:
    await ctx.connected()
    text = 'run "the tests" and $(report)'
    readback = await ctx.tools.remote_send("laptop", "fpga", text, OWNER)
    assert readback.text.startswith(f'Ready to send to fpga on laptop: "{text}".'), readback.text
    assert ctx.tmux("load-buffer") == []
    ctx.later()
    confirmed = await ctx.tools.remote_send_confirm("4172", False, OWNER)
    assert confirmed.text == "Sent to fpga on laptop."
    assert [c["stdin"] for c in ctx.tmux("load-buffer")] == [text]
    assert not any(text in " ".join(c["argv"]) for c in ctx.world.tmux_calls())
    (line,) = read_audit(ctx.config.state_dir)[0]
    assert (line["outcome"], line["machine"], line["paneId"]) == ("sent", "laptop", "%1")


async def test_send_refused_by_the_machines_own_policy(ctx: Ctx) -> None:
    await ctx.connected(["--sessions", "fpga,notes"])
    readback = await ctx.tools.remote_send("laptop", "fpga", "hello", OWNER)
    assert not readback.is_error
    ctx.later()
    confirmed = await ctx.tools.remote_send_confirm("4172", False, OWNER)
    assert confirmed.is_error
    assert ctx.tmux("load-buffer") == []
    (line,) = read_audit(ctx.config.state_dir)[0]
    assert line["outcome"] == "not_sent:not_allowed"


async def test_keys_and_resize_over_the_agent_transport(ctx: Ctx) -> None:
    ctx.world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude", height=30)], {"%1": MENU})
    await ctx.connected()
    readback = await ctx.tools.remote_send_keys("laptop", "fpga", ["Down", "Enter"], OWNER)
    assert readback.text.startswith("Ready to press down and enter in fpga on laptop."), readback.text
    ctx.later()
    assert (
        await ctx.tools.remote_send_confirm("4172", False, OWNER)
    ).text == "Pressed down and enter in fpga on laptop."
    assert [c["argv"] for c in ctx.tmux("send-keys")] == [["send-keys", "-t", "%1", "Down", "Enter"]]
    readback = await ctx.tools.remote_resize("laptop", "fpga", 120, 40, OWNER)
    assert not readback.is_error, readback.text
    ctx.later()
    resized = await ctx.tools.remote_send_confirm("5283", False, OWNER)
    assert resized.text.startswith("Resized the window of fpga on laptop to 120 by 40."), resized.text
    assert [c["argv"] for c in ctx.tmux("resize-window")] == [["resize-window", "-t", "%1", "-x", "120", "-y", "40"]]
    events = [line["event"] for line in read_audit(ctx.config.state_dir)[0]]
    assert events == ["remote_send_keys", "remote_resize"]
    assert d.menu_hash(MENU) is not None


async def test_a_send_whose_connection_drops_is_unknown(ctx: Ctx, monkeypatch: pytest.MonkeyPatch) -> None:
    await ctx.connected()
    await ctx.tools.remote_send("laptop", "fpga", "run the tests", OWNER)
    ctx.later()
    assert ctx.tools.agents is not None
    conn = ctx.tools.agents.live("laptop")
    assert conn is not None
    real = conn.send

    async def send_then_drop(frame: dict[str, Any]) -> bool:
        ok = await real(frame)
        if frame.get("verb") == "send":
            # The request is out; the connection goes away before any answer can come back.
            asyncio.get_running_loop().create_task(conn.close("eof"))
        return ok

    monkeypatch.setattr(conn, "send", send_then_drop)
    confirmed = await ctx.tools.remote_send_confirm("4172", False, OWNER)
    assert "I do not know whether it arrived" in confirmed.text
    (line,) = read_audit(ctx.config.state_dir)[0]
    assert line["outcome"] == "unknown"


async def test_usage_over_the_agent_transport(ctx: Ctx) -> None:
    write_transcript(ctx.world.home + "/.claude", S1, [user(T0 - 3_600_000), assistant(T0 - 3_000_000)])
    await ctx.connected()
    service = UsageService(
        ctx.config,
        ctx.tools.transports,
        LogCapture().logger,
        now=ctx.clock,
        hub_claude_home=str(ctx.world.root / "none"),
    )
    outcomes = await service.refresh(["laptop"])
    assert outcomes["laptop"].ok, outcomes
    sessions, _ = service.sessions(["laptop"])
    assert [(s.machine, s.id, len(s.records)) for s in sessions] == [("laptop", S1, 2)]


async def test_the_watcher_says_not_connected_and_connected_again(ctx: Ctx) -> None:
    pushed: list[str] = []

    async def send(text: str) -> None:
        pushed.append(text)

    watcher = Watcher(
        WatcherOptions(
            watch=ctx.config.watch,
            check=ctx.tools.collect_pending,
            send=send,
            state_dir=ctx.config.state_dir,
            logger=LogCapture().logger,
            now=ctx.clock,
        )
    )
    for _ in range(3):
        await watcher.tick()
    assert "Machine laptop is not connected." in pushed
    await ctx.connected()
    await watcher.tick()
    assert pushed[-1] == "Machine laptop is connected again."


async def test_a_replaced_connection_keeps_the_machine_working(ctx: Ctx) -> None:
    first = await ctx.connected()
    await ctx.connected()
    assert first.dispatcher.wait(20) == d.EXIT_REPLACED
    listed = (await ctx.tools.list_remote_sessions("laptop")).text
    assert listed.startswith("laptop: 2 sessions.")


async def test_status_and_remote_check_through_the_control_socket(ctx: Ctx) -> None:
    code, io = await run_cli("status", "--config", ctx.config_path)
    assert code == 0, io.stderr
    assert "Agent machines:\n  - laptop: not connected" in io.stdout
    await ctx.connected()
    code, io = await run_cli("status", "--config", ctx.config_path)
    assert code == 0
    line = next(x for x in io.stdout.splitlines() if x.startswith("  - laptop:"))
    assert line.startswith("  - laptop: connected since ")
    assert f"dispatcher {d.VERSION}, send allowed, usage allowed, from 203.0.113.5" in line
    code, io = await run_cli("remote-check", "laptop", "--config", ctx.config_path)
    assert code == 0, io.stdout
    lines = io.stdout.splitlines()
    assert lines[0] == "laptop (agent: connects out to the hub)"
    assert lines[1].startswith(f"  switchboard-remote {d.VERSION}, verbs version, list")
    assert lines[2].startswith("  laptop: 2 sessions.")
    assert lines[3].startswith("relay keys (/etc/ssh/switchboard-relay/authorized_keys):")


async def test_status_and_remote_check_without_a_running_hub(ctx: Ctx) -> None:
    await ctx.tools.stop()
    code, io = await run_cli("status", "--config", ctx.config_path)
    assert code == 0
    assert "Agent machines: the hub is not running, so none is connected" in io.stdout
    code, io = await run_cli("remote-check", "--config", ctx.config_path)
    assert code == 1
    assert "  not checked: the hub is not running (only the hub holds agent connections)" in io.stdout
