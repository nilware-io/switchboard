"""Transports end to end: the hub's ssh argv through the fake ssh to the real dispatcher and the fake
tmux, error mapping, timeouts and caps; and the local transport."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from tests.helpers import is_alive
from tests.remote.helpers import RemoteWorld, pane
from voice_coding_mcp.remote.config import MachineConfig, RemoteConfig, SshConfig
from voice_coding_mcp.remote.protocol import CaptureResponse, ListResponse, VersionResponse
from voice_coding_mcp.remote.transport import (
    LocalTransport,
    SshTransport,
    make_transport,
    map_ssh_failure,
    run_process,
)


@pytest.fixture
def world(tmp_path: Path) -> RemoteWorld:
    w = RemoteWorld.create(tmp_path)
    w.set_panes([pane("fpga", "%1", 1, command="claude"), pane("private", "%2", 2)], {"%1": "hello\nworld"})
    return w


def ssh_machine(world: RemoteWorld, **keys: object) -> MachineConfig:
    return MachineConfig.model_validate(
        {
            "name": "dev",
            "host": "localhost",
            "user": "owner",
            "identityFile": "/home/switchboard/.ssh/switchboard_dev",
            "knownHostsFile": str(world.root / "known_hosts"),
            "sessions": ["fpga", "private"],
            **keys,
        }
    )


def transport(world: RemoteWorld, **keys: object) -> SshTransport:
    remote = RemoteConfig(ssh=SshConfig(bin=world.ssh, command_timeout_seconds=5))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(world.root), "SSH_AUTH_SOCK": "/tmp/agent.sock", "SECRET": "x"}
    built = make_transport(ssh_machine(world, **keys), remote, str(world.root / "state"), env)
    assert isinstance(built, SshTransport)
    return built


async def test_list_over_ssh_with_the_forced_command(world: RemoteWorld) -> None:
    result = await transport(world).call("list", timeout_seconds=10)
    assert result.ok, result
    assert isinstance(result.data, ListResponse)
    # The forced command allows fpga,notes,work-* only, whatever the hub's config says.
    assert [p.pane_id for p in result.data.panes] == ["%1"]
    call = world.ssh_calls()[0]
    assert call["argv"][-2:] == ["localhost", "switchboard-remote list"]
    assert "SSH_AUTH_SOCK" not in call["env"]
    assert "SECRET" not in call["env"]
    assert result.elapsed_ms > 0


async def test_capture_and_version(world: RemoteWorld) -> None:
    t = transport(world)
    captured = await t.call("capture", ["%1", "1"], timeout_seconds=10)
    assert isinstance(captured.data, CaptureResponse)
    assert captured.data.text == "world\n"
    version = await t.call("version", timeout_seconds=10)
    assert isinstance(version.data, VersionResponse)
    assert version.data.verbs == ["version", "list", "capture"]
    gone = await t.call("capture", ["%2", "1"], timeout_seconds=10)
    assert (gone.ok, gone.error, gone.detail) == (False, "refused_by_policy", "gone")


async def test_explicit_dispatcher_path(world: RemoteWorld) -> None:
    world.configure(explicit="/home/owner/bin/switchboard-remote")
    t = transport(world, dispatcher="/home/owner/bin/switchboard-remote")
    refused = await t.call("list", timeout_seconds=10)
    assert (refused.error, refused.detail) == ("refused_by_policy", "no_policy")
    policy = Path(world.home) / ".config" / "switchboard-remote" / "policy.json"
    policy.parent.mkdir(parents=True)
    policy.write_text('{"sessions": ["private"]}', encoding="utf-8")
    listed = await t.call("list", timeout_seconds=10)
    assert isinstance(listed.data, ListResponse)
    assert [p.session for p in listed.data.panes] == ["private"]
    assert world.ssh_calls()[-1]["argv"][-1] == "/home/owner/bin/switchboard-remote list"


@pytest.mark.parametrize(
    ("mode", "error"),
    [
        ("hostkey", "host_key"),
        ("auth", "auth"),
        ("refused", "refused"),
        ("connect_timeout", "timeout"),
        ("no_dispatcher", "dispatcher_missing"),
        ("garbage", "dispatcher_missing"),
    ],
)
async def test_failures_map_to_fixed_codes(world: RemoteWorld, mode: str, error: str) -> None:
    world.configure(modes={"localhost": mode})
    result = await transport(world).call("list", timeout_seconds=10)
    assert (result.ok, result.error) == (False, error)


async def test_a_dropped_hardening_option_fails(world: RemoteWorld, monkeypatch: pytest.MonkeyPatch) -> None:
    t = transport(world)
    real = t.argv

    def weakened(verb: str, args: object = ()) -> list[str]:
        argv = real(verb)
        index = argv.index("IdentityAgent=none")
        return argv[: index - 1] + argv[index + 1 :]

    monkeypatch.setattr(t, "argv", weakened)
    result = await t.call("list", timeout_seconds=10)
    assert (result.ok, result.error) == (False, "other")


async def test_timeout_kills_the_process_group(world: RemoteWorld) -> None:
    world.configure(modes={"localhost": "timeout"})
    started = time.monotonic()
    result = await transport(world).call("list", timeout_seconds=0.5)
    assert (result.ok, result.error) == (False, "timeout")
    assert time.monotonic() - started < 5
    assert world.ssh_calls()  # it did start


async def test_bad_requests_never_spawn(world: RemoteWorld) -> None:
    result = await transport(world).call("capture", ["%1;id", "10"], timeout_seconds=5)
    assert (result.ok, result.error) == (False, "other")
    assert world.ssh_calls() == []


async def test_spawn_failure(world: RemoteWorld, tmp_path: Path) -> None:
    remote = RemoteConfig(ssh=SshConfig(bin=str(tmp_path / "no-ssh")))
    t = make_transport(ssh_machine(world), remote, str(tmp_path), {})
    result = await t.call("list", timeout_seconds=5)
    assert (result.ok, result.error) == (False, "other")


async def test_run_process_caps_stdout_and_kills(tmp_path: Path) -> None:
    script = "import sys,time\nsys.stdout.write('x'*200000)\nsys.stdout.flush()\ntime.sleep(30)"
    started = time.monotonic()
    result = await run_process([sys.executable, "-c", script], dict(os.environ), timeout_seconds=20, stdout_cap=1000)
    assert result.overflow
    assert time.monotonic() - started < 10


async def test_run_process_kills_leftover_children_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r},'w').write(str(p.pid))\n"
        "time.sleep(60)"
    )
    result = await run_process([sys.executable, "-c", script], dict(os.environ), timeout_seconds=1.5)
    assert result.timed_out
    child = int(pid_file.read_text())
    for _ in range(100):
        if not is_alive(child):
            break
        await asyncio.sleep(0.05)
    assert not is_alive(child)


def test_ssh_stderr_mapping() -> None:
    assert map_ssh_failure("@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@") == "host_key"
    assert map_ssh_failure("ssh: Could not resolve hostname nowhere: Name or service not known") == "refused"
    assert map_ssh_failure("ssh: connect to host x port 22: No route to host") == "refused"
    assert map_ssh_failure("kex_exchange_identification: read: Connection reset") == "other"


async def test_local_transport_runs_the_bundled_dispatcher(world: RemoteWorld) -> None:
    machine = MachineConfig(name="dev", transport="local", sessions=["private"], claude_home="/nonexistent/claude")
    env = {**world.tmux_env(), "PATH": "/usr/bin:/bin", "UNRELATED": "1"}
    local = LocalTransport(machine, env)
    assert local.argv("list")[1:3] == ["-I", local.argv("list")[2]]
    assert local.policy_args() == [
        "--policy-from-hub",
        "--sessions",
        "private",
        "--allow-usage",
        "--claude-home",
        "/nonexistent/claude",
    ]
    result = await local.call("list", timeout_seconds=10)
    assert isinstance(result.data, ListResponse), result
    assert [p.session for p in result.data.panes] == ["private"]
    assert "UNRELATED" not in world.tmux_calls()[0]["env"]


async def test_local_transport_missing_python(world: RemoteWorld, tmp_path: Path) -> None:
    machine = MachineConfig(name="dev", transport="local", sessions=["fpga"])
    local = LocalTransport(machine, {}, python=str(tmp_path / "no-python"))
    result = await local.call("version", timeout_seconds=5)
    assert (result.ok, result.error) == (False, "dispatcher_missing")
