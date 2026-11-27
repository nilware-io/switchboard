"""CLI: `remote-dispatcher` and `remote-check` (fake ssh)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests.helpers import claude_config
from tests.remote.helpers import DISPATCHER, SESSION_A, RemoteWorld, machine, pane
from tests.test_cli import run_cli
from voice_coding_mcp.remote.audit import AuditLog
from voice_coding_mcp.remote.send import ConfirmationStore


def write_config(root: Path, world: RemoteWorld, machines: list[dict[str, Any]]) -> str:
    projects = root / "projects"
    (projects / "alpha-app").mkdir(parents=True, exist_ok=True)
    claude_home = root / "claude-home"
    claude_home.mkdir(exist_ok=True)
    claude = claude_config(str(root / "record.jsonl"))
    data = {
        "publicUrl": "https://vcm.example.com",
        "stateDir": str(root / "state"),
        "projectRoots": [str(projects)],
        "claude": {"bin": claude.bin, "env": dict(claude.env), "configDir": str(claude_home)},
        "remote": {"machines": machines, "ssh": {"bin": world.ssh}},
    }
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


async def test_remote_dispatcher_prints_the_script_without_a_config() -> None:
    code, io = await run_cli("remote-dispatcher", "--config", "/nonexistent/config.json")
    assert code == 0
    assert io.stdout == Path(DISPATCHER).read_text(encoding="utf-8")  # noqa: ASYNC240
    assert io.stdout.startswith("#!/usr/bin/env python3\n")


async def test_remote_check_reports_each_machine(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    world.set_panes([pane("fpga", "%1", 9_999_001, command="claude"), pane("notes", "%2", 9_999_002)])
    world.register("3001", os.getpid(), sessionId=SESSION_A, status="idle", tmux="0:@0.%1")
    world.configure(modes={"example-host": "auth"})
    key = tmp_path / "key"
    key.write_text("k", encoding="utf-8")
    key.chmod(0o600)
    path = write_config(
        tmp_path,
        world,
        [
            machine("dev", world=world, identity=str(key)),
            machine("buildbox", world=world, identity=str(key), host="example-host", user="bot", sessions=["*"]),
        ],
    )
    (tmp_path / "claude-home" / "settings.json").write_text('{"hooks": {"Stop": []}}', encoding="utf-8")
    code, io = await run_cli("remote-check", "--config", path, env={"PATH": os.environ["PATH"]})
    assert code == 1, io.stderr
    assert io.stdout == (
        "dev (ssh owner@localhost:22)\n"
        "  switchboard-remote 0.9.0, verbs version, list, capture, send, usage, keys, resize, attachments, attachment, "
        "health, send off, usage off, attachments off\n"
        "  dev: 2 sessions. fpga, Claude Code idle. notes, no Claude Code.\n"
        "buildbox (ssh bot@example-host:22)\n"
        "  warning: remote machine buildbox: sessions allows every tmux session (*); list the session names instead\n"
        "  not reachable: the key was refused\n"
        "warning: the server user's Claude Code settings.json defines hooks; the remote summariser loads user settings, so they run for every summary\n"
    )
    code, io = await run_cli("remote-check", "dev", "--config", path, env={"PATH": os.environ["PATH"]})
    assert code == 0
    assert io.stdout.startswith("dev (ssh owner@localhost:22)\n")
    assert "buildbox" not in io.stdout
    code, io = await run_cli("remote-check", "laptop", "--config", path)
    assert code == 2
    assert io.stderr == "No machine called laptop. Configured: dev, buildbox.\n"


async def test_remote_check_without_machines(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    code, io = await run_cli("remote-check", "--config", write_config(tmp_path, world, []))
    assert (code, io.stdout) == (0, "No remote machines are configured (remote.machines).\n")


async def test_audit_prints_the_send_log_as_a_table(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    path = write_config(tmp_path, world, [])
    code, io = await run_cli("audit", "--config", path)
    assert (code, io.stdout) == (0, "No sends were audited yet.\n")
    store = ConfirmationStore(max_pending=2, confirm_seconds=120, min_confirm_seconds=0, now=lambda: 1_764_160_000_000)
    log = AuditLog(str(tmp_path / "state"), now=lambda: 1_764_160_012_000)
    for outcome, text in (("sent", "push the branch"), ("not_sent:gone", "run the tests")):
        item, _ = store.add(
            client="len=36 sha256=1a2b3c4d",
            client_name="Claude\x1b[31m",
            machine="dev",
            session="fpga",
            place="fpga on dev",
            pane_id="%3",
            pane_pid=42,
            session_id=None,
            text=text,
            risky=["push"] if "push" in text else [],
            permission_answer=False,
        )
        log.record(item, outcome=outcome, risky_ok="push" in text)
    with open(log.path, "a", encoding="utf-8") as handle:  # noqa: ASYNC230
        handle.write("garbage\n")
    code, io = await run_cli("audit", "--config", path)
    assert code == 0
    lines = io.stdout.splitlines()
    assert lines[0].split() == [
        "time",
        "event",
        "machine",
        "session",
        "pane",
        "outcome",
        "risky",
        "risky",
        "ok",
        "chars",
        "keys",
        "kind",
        "bytes",
        "client",
    ]
    assert lines[1].split() == [
        "2025-11-26T12:26:52.000Z",
        "remote_send",
        "dev",
        "fpga",
        "%3",
        "sent",
        "push",
        "yes",
        "15",
        "-",
        "-",
        "-",
        "Claude?[31m",
    ]
    assert lines[2].split()[5:] == ["not_sent:gone", "-", "no", "13", "-", "-", "-", "Claude?[31m"]
    assert "push the branch" not in io.stdout
    assert io.stderr.startswith("warning: 1 unreadable line(s) in ")
    code, io = await run_cli("audit", "--limit", "1", "--config", path)
    assert len(io.stdout.splitlines()) == 2
    assert "not_sent:gone" in io.stdout
    code, io = await run_cli("audit", "--limit", "0", "--config", path)
    assert code == 2
    assert "must be a whole number between 1 and 100000" in io.stderr
