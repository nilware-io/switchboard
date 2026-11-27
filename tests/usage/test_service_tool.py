"""`usage_report` and `voice-coding-mcp usage` end to end: the real ssh transport on the fake ssh, the
real dispatcher reading a fake remote Claude Code home, the hub's own home, caches and tasks."""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import claude_config
from tests.remote.helpers import RemoteWorld, machine
from tests.test_cli import run_cli
from tests.usage.helpers import MIN, S1, S2, S3, SECRET_TEXT, T0, assistant, user, write_transcript
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote.transport import make_transport
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions
from voice_coding_mcp.usage.cache import HUB, cache_path, load_cache
from voice_coding_mcp.usage.compute import parse_period
from voice_coding_mcp.usage.service import UsageService
from voice_coding_mcp.usage.tool import UsageTool, UsageToolOptions

HOUR = 60 * MIN
NOW = T0 + 2 * HOUR
FORCED = ["--sessions", "fpga", "--allow-usage"]


def config_data(tmp: Path, world: RemoteWorld, **usage: Any) -> dict[str, Any]:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    hub_home = tmp / "hub-claude"
    hub_home.mkdir(exist_ok=True)
    claude = claude_config(str(tmp / "record.jsonl"))
    return {
        "publicUrl": "https://vcm.example.com",
        "stateDir": str(tmp / "state"),
        "projectRoots": [str(projects)],
        "claude": {"bin": claude.bin, "env": dict(claude.env), "configDir": str(hub_home)},
        "tasks": {"syncWaitSeconds": 5, "maxConcurrent": 1},
        "remote": {
            "machines": [
                machine("dev", world=world, sessions=["fpga"]),
                machine("buildbox", world=world, host="example-host", user="bot", sessions=["api"]),
                machine("quiet", world=world, host="quiet-host", user="bot", sessions=["x"], allowUsage=False),
            ],
            "ssh": {"bin": world.ssh, "commandTimeoutSeconds": 8},
        },
        "usage": usage,
    }


@dataclass
class Ctx:
    tmp: Path
    world: RemoteWorld
    config: Config
    manager: TaskManager
    service: UsageService
    tool: UsageTool
    clock: list[int]

    @property
    def remote_claude(self) -> Path:
        return Path(self.world.home) / ".claude"

    @property
    def hub_claude(self) -> Path:
        return self.tmp / "hub-claude"


@pytest.fixture
async def ctx(tmp_path: Path) -> AsyncIterator[Ctx]:
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    world.configure(modes={"example-host": "refused"})
    config = parse_config(config_data(tmp_path, world), home_dir="/nonexistent-home-dir")
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
    clock = [NOW]
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    transports = {m.name: make_transport(m, config.remote, config.state_dir, env) for m in config.remote.machines}
    service = UsageService(config, transports, silent_logger, now=lambda: clock[0], tz=UTC, env=env)
    tool = UsageTool(
        UsageToolOptions(
            service=service, manager=manager, logger=silent_logger, sync_wait_seconds=5, now=lambda: clock[0]
        )
    )
    yield Ctx(tmp_path, world, config, manager, service, tool, clock)
    await manager.shutdown("test finished")


def seed(c: Ctx) -> None:
    write_transcript(
        c.remote_claude,
        S1,
        [
            user(T0),
            assistant(T0 + 10 * MIN, "m1", tokens=(1_000_000, 100_000, 0, 0)),
            *(assistant(T0 + i * 10 * MIN, f"m{i}", tokens=(0, 0, 0, 0)) for i in range(2, 7)),
        ],
    )
    write_transcript(
        c.hub_claude, S2, [user(T0, cwd="/srv/projects/alpha"), user(T0 + 10 * MIN, cwd="/srv/projects/alpha")]
    )


async def test_first_report_refreshes_and_answers(ctx: Ctx) -> None:
    seed(ctx)
    answer = await ctx.tool.usage_report("today", None, None)
    assert not answer.is_error
    assert answer.text == (
        "Today: 1.2 active hours in 2 sessions, 46.67 EUR at 40 per hour. "
        "API-equivalent token cost about 7.5 dollars: 1 million input, 100 thousand output. "
        'Most time: "fpgadc", 1 hour. '
        "buildbox could not be read (the connection was refused); the numbers are without it."
    )
    assert SECRET_TEXT not in answer.text
    calls = ctx.world.ssh_calls()
    assert [c["argv"][-1] for c in calls if c["argv"][-2] == "localhost"] == [
        f"switchboard-remote usage {(NOW // 1000) - 400 * 86400} 512"
    ]
    assert not any("quiet-host" in c["argv"] for c in calls), "allowUsage false is never asked"
    for name in ("dev", HUB):
        assert load_cache(ctx.config.state_dir, name).refreshed_ms == NOW
    assert not os.path.exists(cache_path(ctx.config.state_dir, "buildbox"))  # noqa: ASYNC240
    assert SECRET_TEXT not in Path(cache_path(ctx.config.state_dir, "dev")).read_text(encoding="utf-8")  # noqa: ASYNC240


async def test_fresh_caches_answer_without_reading(ctx: Ctx) -> None:
    seed(ctx)
    await ctx.tool.usage_report("today", "dev", None)
    before = len(ctx.world.ssh_calls())
    ctx.clock[0] += 5 * MIN
    answer = await ctx.tool.usage_report("today", "dev", None)
    assert answer.text.startswith("Today on dev: 1 active hour in 1 session, 40 EUR at 40 per hour.")
    assert len(ctx.world.ssh_calls()) == before
    ctx.clock[0] += 6 * MIN
    await ctx.tool.usage_report("today", "dev", None)
    assert len(ctx.world.ssh_calls()) == before + 1


async def test_incremental_refresh_sends_known_files(ctx: Ctx) -> None:
    seed(ctx)
    await ctx.tool.usage_report("today", "dev", None)
    write_transcript(ctx.remote_claude, S3, [user(T0 + 90 * MIN)])
    ctx.clock[0] += 11 * MIN
    await ctx.tool.usage_report("today", "dev", None)
    last = ctx.world.ssh_calls()[-1]
    known = json.loads(last["stdin"])
    assert [item["id"] for item in known] == [S1]
    assert sorted(load_cache(ctx.config.state_dir, "dev").files) == [S1, S3]
    os.unlink(ctx.remote_claude / "projects" / "-home-USER-projects-fpgadc" / f"{S1}.jsonl")
    ctx.clock[0] += 11 * MIN
    await ctx.tool.usage_report("today", "dev", None)
    assert sorted(load_cache(ctx.config.state_dir, "dev").files) == [S3]


async def test_failed_machine_uses_its_old_cache(ctx: Ctx) -> None:
    seed(ctx)
    await ctx.tool.usage_report("today", "dev", None)
    ctx.world.configure(modes={"localhost": "auth"})
    ctx.clock[0] += 3 * HOUR
    answer = await ctx.tool.usage_report("today", "dev", None)
    assert answer.text.endswith("dev could not be read (the key was refused); I used its records from 3 hours ago.")
    assert "1 active hour" in answer.text


async def test_dispatcher_without_allow_usage_is_named(ctx: Ctx) -> None:
    ctx.world.configure(forced=["--sessions", "fpga"])
    answer = await ctx.tool.usage_report("today", "dev", None)
    assert answer.text == (
        "Today on dev: no Claude Code activity. dev could not be read (its switchboard-remote does not allow usage, add --allow-usage); the numbers are without it."
    )


async def test_machine_and_project_filters(ctx: Ctx) -> None:
    seed(ctx)
    answer = await ctx.tool.usage_report("today", None, "alph")
    assert answer.text.startswith('Today, project "alpha": 10 active minutes in 1 session, 6.67 EUR')
    missing = await ctx.tool.usage_report("today", None, "zebra")
    assert missing.is_error
    assert missing.text == 'I found no project called "zebra" in the usage records. Projects: alpha, fpgadc.'
    quiet = await ctx.tool.usage_report("today", "quiet", None)
    assert (quiet.is_error, quiet.text) == (True, "Usage reading is off for quiet.")
    unknown = await ctx.tool.usage_report("today", "nowhere", None)
    assert unknown.is_error


async def test_bad_period(ctx: Ctx) -> None:
    answer = await ctx.tool.usage_report("the other day", None, None)
    assert answer.is_error
    assert answer.text.startswith("I did not understand the period. Say today, yesterday")


async def test_slow_machine_turns_into_a_task(ctx: Ctx) -> None:
    seed(ctx)
    ctx.world.configure(modes={"example-host": "refused", "localhost": "slow"}, sleep=3)
    started = time.monotonic()
    answer = await ctx.tool.usage_report("today", None, None)
    assert time.monotonic() - started < 3
    assert answer.text.startswith("I am reading the usage records. That is task ")
    task_id = int(answer.text.split("task ")[1].split(";")[0])
    task = await ctx.manager.wait(task_id, 8000)
    assert task is not None
    assert task.status == "done", task
    assert task.kind == "remote"
    assert task.result is not None
    assert task.result.startswith("Today: 1.2 active hours")
    assert task.prompt == "usage report"


async def test_hub_home_is_not_counted_twice_with_a_local_machine(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    data = config_data(tmp_path, world)
    data["remote"]["machines"] = [
        {"name": "dev", "transport": "local", "sessions": ["fpga"], "claudeHome": str(tmp_path / "hub-claude")}
    ]
    config = parse_config(data, home_dir="/nonexistent-home-dir")
    service = UsageService(config, {}, silent_logger, env={"HOME": str(tmp_path)})
    assert service.sources() == ["dev"]
    data["remote"]["machines"][0]["claudeHome"] = str(tmp_path / "other")
    other = UsageService(
        parse_config(data, home_dir="/nonexistent-home-dir"), {}, silent_logger, env={"HOME": str(tmp_path)}
    )
    assert other.sources() == ["dev", HUB]


async def test_summariser_runs_count_as_switchboard(ctx: Ctx) -> None:
    usage = Path(ctx.config.state_dir) / "usage"
    usage.mkdir(parents=True, exist_ok=True)
    (usage / "summaries.jsonl").write_text(
        '{"time":"2025-11-24T16:05:00.000Z","model":"haiku","input":100,"output":10,"costUsd":0.5}\n', encoding="utf-8"
    )
    report, _ = ctx.service.report([HUB], parse_period("today", NOW, UTC))
    assert [(r.key, r.cost_usd, r.sessions) for r in report.rows] == [(("switchboard",), 0.5, 0)]


# --- CLI -------------------------------------------------------------------------------------------


def write_config(tmp: Path, world: RemoteWorld, **usage: Any) -> str:
    path = tmp / "config.json"
    path.write_text(json.dumps(config_data(tmp, world, **usage)), encoding="utf-8")
    return str(path)


async def test_cli_usage_csv_and_exit_code(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    world.configure(modes={"example-host": "refused"})
    path = write_config(tmp_path, world, hourlyRate=50, currency="CHF")
    now = int(time.time() * 1000)
    write_transcript(Path(world.home) / ".claude", S1, [user(now - 2 * HOUR + i * 10 * MIN) for i in range(4)])
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    code, io = await run_cli("usage", "--config", path, "--period", "this month", "--csv", env=env)
    assert code == 1, io.stderr
    lines = io.stdout.splitlines()
    assert lines[0].startswith("project,sessions,active_hours,billable_hours,amount,currency,")
    assert lines[1].startswith("fpgadc,1,0.50,0.50,25.00,CHF,")
    assert lines[-1].startswith("total,1,0.50,0.50,25.00,CHF,")
    assert "Reading usage records from dev, buildbox, this machine" in io.stderr
    assert "warning: buildbox could not be read: the connection was refused" in io.stderr

    cached_code, cached = await run_cli(
        "usage", "--config", path, "--cached", "--by", "session", "--machine", "dev", env=env
    )
    assert cached_code == 0, cached.stderr
    assert "Reading" not in cached.stderr
    assert cached.stdout.splitlines()[1].split()[:3] == ["machine", "project", "session"]
    assert cached.stdout.splitlines()[2].split()[:4] == ["dev", "fpgadc", S1, "1"]


async def test_cli_usage_argument_errors(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    path = write_config(tmp_path, world)
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    code, io = await run_cli("usage", "--config", path, "--period", "someday", env=env)
    assert code == 2
    assert io.stderr.startswith("Unknown period someday.")
    code, io = await run_cli("usage", "--config", path, "--machine", "quiet", env=env)
    assert code == 2
    assert "No machine called quiet with usage reading on" in io.stderr
    code, io = await run_cli("usage", "--config", path, "--by", "week", env=env)
    assert code == 2


async def test_cli_usage_leaves_agent_machines_to_the_hub(tmp_path: Path) -> None:
    # The CLI holds no agent connection: an agent machine is reported from its cache, with a note
    # that is not a failure (the exit code stays 0).
    world = RemoteWorld.create(tmp_path, forced=FORCED)
    data = config_data(tmp_path, world)
    data["remote"]["machines"] = [
        machine("dev", world=world, sessions=["fpga"]),
        {"name": "laptop", "transport": "agent", "sessions": ["fpga"], "allowUsage": True},
    ]
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    code, io = await run_cli("usage", "--config", str(path), "--period", "this month", "--csv", env=env)
    assert code == 0, io.stderr
    assert "Reading usage records from dev, this machine ..." in io.stderr
    assert "warning: laptop connects to the hub, so only the hub reads it; its cached records are used" in io.stderr
    assert "not connected" not in io.stderr
    code, io = await run_cli("usage", "--config", str(path), "--machine", "laptop", env=env)
    assert code == 0, io.stderr
    assert "Reading" not in io.stderr


async def test_agent_transcripts_do_not_change_usage(ctx: Ctx) -> None:
    seed(ctx)
    await ctx.service.refresh(["dev", HUB])
    before, _ = ctx.service.report(["dev", HUB], parse_period("today", NOW, UTC))
    agents = ctx.remote_claude / "projects" / "-home-USER-projects-fpgadc" / S1 / "subagents"
    agents.mkdir(parents=True)
    line = assistant(T0 + 90 * MIN, "agent-msg", tokens=(5, 5, 5, 5), isSidechain=True)
    (agents / "agent-a1.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
    await ctx.service.refresh(["dev", HUB])
    assert any(t.is_agent_file for t in load_cache(ctx.config.state_dir, "dev").files.values())
    after, _ = ctx.service.report(["dev", HUB], parse_period("today", NOW, UTC))
    assert after.total == before.total
