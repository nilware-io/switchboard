"""switchboard-connect (`remote/connector.py`, deployed as `deploy/remote/switchboard-connect`):
argument checks, the ssh argv and environments, the instance lock, cross-wired pipes with a fake
ssh, reconnect reasons, backoff growth, cap, jitter and reset, the long waits, SIGTERM, and the
whole chain connector, fake ssh, relay, hub."""

from __future__ import annotations

import ast
import asyncio
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import FIXTURES, LogCapture
from tests.remote.agent_helpers import agent_machine, agents_section, socket_dir, wait_until
from tests.remote.helpers import DISPATCHER, FAKE_TMUX, RemoteWorld, pane
from voice_coding_mcp.remote import connector as c
from voice_coding_mcp.remote.agents import AgentHub
from voice_coding_mcp.remote.config import RemoteConfig

REPO = Path(__file__).resolve().parents[2]
CONNECTOR = Path(c.__file__)
FAKE_SSH_OUT = str(FIXTURES / "fake_ssh_out.py")


class Machine:
    """Key files, a fake ssh wrapper with its settings, and the dispatcher command line."""

    def __init__(self, root: Path, world: RemoteWorld) -> None:
        self.root = root
        self.world = world
        self.identity = root / "switchboard_relay"
        self.identity.write_text("key", encoding="utf-8")
        self.identity.chmod(0o600)
        self.known_hosts = root / "known_hosts"
        self.known_hosts.write_text("switchboard-hub ssh-ed25519 AAAA\n", encoding="utf-8")
        self.known_hosts.chmod(0o644)
        self.ssh = root / "ssh"
        self.settings_path = root / "fake-ssh-out.json"
        self.log = root / "ssh.jsonl"
        self.out = root / "hub-side.jsonl"
        self.ssh.write_text(
            "#!/bin/sh\n"
            f"FAKE_SSH_OUT_CONFIG={shlex.quote(str(self.settings_path))}\n"
            "export FAKE_SSH_OUT_CONFIG\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(FAKE_SSH_OUT)} "$@"\n',
            encoding="utf-8",
        )
        self.ssh.chmod(0o755)
        self.configure(mode="hub", bye="shutdown")

    def configure(self, **settings: Any) -> None:
        data = {"log": str(self.log), "out": str(self.out), **settings}
        self.settings_path.write_text(json.dumps(data), encoding="utf-8")

    def argv(self, extra: dict[str, str] | None = None, policy: list[str] | None = None) -> list[str]:
        options = {
            "--host": "hub.example.org",
            "--user": "sbrelay",
            "--identity": str(self.identity),
            "--known-hosts": str(self.known_hosts),
            "--ssh": str(self.ssh),
            **(extra or {}),
        }
        flat = [item for pair in options.items() for item in pair]
        return [*flat, "--", sys.executable, "-I", DISPATCHER, "--serve", *(policy or ["--sessions", "fpga"])]

    def options(self, **extra: str) -> c.Options:
        return c.parse_args(self.argv(extra), os.getuid())

    def env(self) -> dict[str, str]:
        return {
            **self.world.tmux_env(),
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "SSH_AUTH_SOCK": "/tmp/agent.sock",
            "SSH_CONNECTION": "198.51.100.1 1 192.0.2.1 22",
            "SECRET_TOKEN": "x",
        }

    def connector(self, **kwargs: Any) -> c.Connector:
        return c.Connector(self.options(), self.env(), log=io.StringIO(), **kwargs)

    def hub_side(self) -> list[Any]:
        if not self.out.exists():
            return []
        return [json.loads(line) for line in self.out.read_text(encoding="utf-8").splitlines() if line]

    def ssh_calls(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    world = RemoteWorld.create(tmp_path)
    world.set_panes([pane("fpga", "%1", 9_500_001, command="claude")], {"%1": "hello"})
    return Machine(tmp_path, world)


# --- arguments -------------------------------------------------------------------------------------


def test_good_arguments(machine: Machine) -> None:
    options = machine.options(**{"--port": "2222", "--max-backoff": "600"})
    assert (options.host, options.port, options.user, options.max_backoff) == ("hub.example.org", 2222, "sbrelay", 600)
    assert options.command[3] == "--serve"


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"--host": "-oProxyCommand=x"}, "--host"),
        ({"--host": "user@hub"}, "--host"),
        ({"--host": "hub example"}, "--host"),
        ({"--user": "Root"}, "--user"),
        ({"--port": "0"}, "--port"),
        ({"--port": "70000"}, "--port"),
        ({"--port": "22a"}, "--port"),
        ({"--max-backoff": "10"}, "--max-backoff"),
        ({"--max-backoff": "4000"}, "--max-backoff"),
        ({"--identity": "relative/key"}, "--identity"),
        ({"--identity": "/no/such/key"}, "does not exist"),
        ({"--known-hosts": "/a b"}, "--known-hosts"),
        ({"--ssh": "ssh"}, "--ssh"),
    ],
)
def test_bad_arguments(machine: Machine, change: dict[str, str], message: str) -> None:
    with pytest.raises(c.UsageError, match=message):
        machine.options(**change)


def test_an_identity_readable_by_others_is_refused(machine: Machine) -> None:
    machine.identity.chmod(0o640)
    with pytest.raises(c.UsageError, match="readable by others"):
        machine.options()


def test_a_known_hosts_file_writable_by_others_is_refused(machine: Machine) -> None:
    machine.known_hosts.chmod(0o666)
    with pytest.raises(c.UsageError, match="writable by others"):
        machine.options()


def test_a_symlinked_identity_is_refused(machine: Machine) -> None:
    link = machine.root / "link"
    link.symlink_to(machine.identity)
    with pytest.raises(c.UsageError, match="not a regular file"):
        machine.options(**{"--identity": str(link)})


@pytest.mark.parametrize(
    "tail",
    [
        [],
        ["python3", "-I", "switchboard-remote", "--serve"],
        ["/usr/bin/python3", "-I", "/x/switchboard-remote", "--sessions", "fpga"],
    ],
)
def test_the_dispatcher_command_must_be_absolute_and_serve(machine: Machine, tail: list[str]) -> None:
    argv = machine.argv()
    argv = [*argv[: argv.index("--") + 1], *tail]
    with pytest.raises(c.UsageError, match="--serve"):
        c.parse_args(argv, os.getuid())


@pytest.mark.parametrize(
    "argv",
    [
        ["--host", "hub", "--user", "sbrelay"],
        ["--bogus", "x", "--"],
        ["--host", "a", "--host", "b", "--"],
    ],
)
def test_malformed_command_lines(argv: list[str]) -> None:
    with pytest.raises(c.UsageError):
        c.parse_args(argv, os.getuid())


def test_missing_required_options(machine: Machine) -> None:
    argv = machine.argv()
    index = argv.index("--known-hosts")
    with pytest.raises(c.UsageError, match="--known-hosts is required"):
        c.parse_args(argv[:index] + argv[index + 2 :], os.getuid())


def test_refuses_to_run_as_root(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert c.main(["--host", "hub"]) == 2
    assert capsys.readouterr().err == "switchboard-connect: refusing to run as root\n"


# --- ssh argv and environments ---------------------------------------------------------------------


def test_the_ssh_argv(machine: Machine) -> None:
    assert c.ssh_argv(machine.options(**{"--port": "2222"})) == [
        str(machine.ssh),
        "-F",
        "/dev/null",
        "-T",
        "-e",
        "none",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={machine.known_hosts}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "HostKeyAlias=switchboard-hub",
        "-o",
        "HostKeyAlgorithms=ssh-ed25519",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "HashKnownHosts=no",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "RequestTTY=no",
        "-o",
        "LogLevel=ERROR",
        "-i",
        str(machine.identity),
        "-p",
        "2222",
        "-l",
        "sbrelay",
        "--",
        "hub.example.org",
    ]


def test_the_environments_are_allow_lists(machine: Machine) -> None:
    env = {**machine.env(), "TMUX_TMPDIR": "/tmp/t", "XDG_RUNTIME_DIR": "/run/user/1000", "CLAUDE_CONFIG_DIR": "/c"}
    assert set(c.ssh_env(env)) == {"PATH", "HOME", "LANG"}
    dispatcher = c.dispatcher_env(env)
    assert "SSH_CONNECTION" not in dispatcher
    assert "SSH_AUTH_SOCK" not in dispatcher
    assert "SECRET_TOKEN" not in dispatcher
    assert {"PATH", "HOME", "LANG", "TMUX_TMPDIR", "XDG_RUNTIME_DIR", "CLAUDE_CONFIG_DIR"} <= set(dispatcher)


def test_the_lock_admits_one_instance(tmp_path: Path) -> None:
    path = c.lock_path({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert path == str(tmp_path / c.LOCK_NAME)
    first = c.take_lock(path)
    assert first is not None
    assert c.take_lock(path) is None
    os.close(first)
    second = c.take_lock(path)
    assert second is not None
    os.close(second)
    assert c.lock_path({"HOME": str(tmp_path)}) == str(tmp_path / ".cache" / c.LOCK_NAME)
    assert stat.S_IMODE(os.stat(tmp_path / ".cache").st_mode) == 0o700


@pytest.mark.parametrize(
    ("stderr", "code"),
    [
        ("Host key verification failed.", "host_key"),
        ("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @", "host_key"),
        ("sbrelay@hub: Permission denied (publickey).", "auth"),
        ("ssh: connect to host hub port 22: Connection timed out", "timeout"),
        ("ssh: Could not resolve hostname hub: Name or service not known", "refused"),
        ("something else", "other"),
    ],
)
def test_ssh_failures_are_mapped(stderr: str, code: str) -> None:
    assert c.map_ssh_failure(stderr) == code


# --- one connection ----------------------------------------------------------------------------------


def test_the_pipes_are_cross_wired_to_the_dispatcher(machine: Machine) -> None:
    machine.configure(
        mode="hub",
        requests=[{"verb": "version", "args": []}, {"verb": "list", "args": []}],
        bye="shutdown",
    )
    outcome = machine.connector().attempt()
    assert outcome.reason == "closed"
    hello, version, listed = machine.hub_side()
    assert hello["t"] == "hello"
    assert hello["policy"] == "ok"
    assert version["doc"]["version"] == c.VERSION
    assert [p["paneId"] for p in listed["doc"]["panes"]] == ["%1"]
    (call,) = machine.ssh_calls()
    assert call["argv"] == c.ssh_argv(machine.options())[1:]
    # The wrapper shell adds PWD (and the like); nothing of the connector's own environment leaks.
    assert set(call["env"]) - {"PWD", "SHLVL", "_", "OLDPWD"} <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "FAKE_SSH_OUT_CONFIG",
    }


@pytest.mark.parametrize(("bye", "reason"), [("replaced", "replaced"), ("unknown_machine", "refused_by_hub")])
def test_the_hubs_bye_decides_the_reason(machine: Machine, bye: str, reason: str) -> None:
    machine.configure(mode="hub", bye=bye)
    assert machine.connector().attempt().reason == reason


@pytest.mark.parametrize(("mode", "reason"), [("auth", "auth"), ("hostkey", "host_key"), ("refused", "refused")])
def test_ssh_failures(machine: Machine, mode: str, reason: str) -> None:
    machine.configure(mode=mode)
    outcome = machine.connector().attempt()
    assert outcome.reason == reason
    assert outcome.detail


def test_a_missing_ssh_and_a_missing_dispatcher(machine: Machine) -> None:
    connector = machine.connector()
    connector.options.ssh = str(machine.root / "no-ssh")
    assert connector.attempt().reason == "no_ssh"
    connector = machine.connector()
    connector.options.command = [str(machine.root / "no-python"), "--serve"]
    assert connector.attempt().reason == "no_dispatcher"


def test_a_dying_ssh_stops_the_dispatcher(machine: Machine) -> None:
    machine.configure(mode="exit", exit=0)
    started = time.monotonic()
    assert machine.connector().attempt().reason == "closed"
    assert time.monotonic() - started < 10


# --- backoff -----------------------------------------------------------------------------------------


class Script:
    """A connector whose attempts return scripted outcomes; waits are recorded, never slept."""

    def __init__(self, machine: Machine, outcomes: list[tuple[str, float]], max_backoff: int = 300) -> None:
        self.outcomes = list(outcomes)
        self.ranges: list[tuple[float, float]] = []
        self.log = io.StringIO()
        self.connector = c.Connector(
            machine.options(**{"--max-backoff": str(max_backoff)}),
            machine.env(),
            log=self.log,
            uniform=self.uniform,
            wait=self.wait,
        )
        self.connector.attempt = self.attempt  # type: ignore[method-assign]

    def attempt(self) -> c.Outcome:
        reason, seconds = self.outcomes.pop(0)
        return c.Outcome(reason, seconds)

    def uniform(self, low: float, high: float) -> float:
        self.ranges.append((low, high))
        return high

    def wait(self, _seconds: float) -> bool:
        return not self.outcomes

    def run(self) -> list[float]:
        assert self.connector.run() == 0
        return self.connector.delays


def test_backoff_doubles_from_2_up_to_the_cap_with_jitter(machine: Machine) -> None:
    script = Script(machine, [("refused", 1)] * 8, max_backoff=60)
    assert script.run() == [2, 4, 8, 16, 32, 60, 60, 60]
    assert script.ranges[0] == (1, 2)
    assert all(low == high / 2 for low, high in script.ranges)


def test_a_connection_that_lasted_resets_the_backoff(machine: Machine) -> None:
    script = Script(machine, [("closed", 1), ("closed", 1), ("closed", 1), ("closed", 61), ("closed", 1)])
    assert script.run() == [2, 4, 8, 2, 4]


@pytest.mark.parametrize("reason", ["replaced", "refused_by_hub", "auth", "host_key"])
def test_long_wait_reasons_go_straight_to_the_maximum(machine: Machine, reason: str) -> None:
    script = Script(machine, [(reason, 1), ("closed", 1)], max_backoff=300)
    assert script.run() == [300, 2]


def test_the_log_names_each_change_of_reason_once(machine: Machine) -> None:
    script = Script(machine, [("auth", 1), ("auth", 1), ("auth", 1), ("closed", 1)])
    script.run()
    log = script.log.getvalue()
    assert log.count("connection ended: auth") == 1
    assert log.count("connection ended: closed") == 1


def test_the_real_jitter_stays_in_range(machine: Machine) -> None:
    connector = c.Connector(machine.options(), machine.env(), log=io.StringIO(), wait=lambda _s: True)
    connector.attempt = lambda: c.Outcome("closed", 0)  # type: ignore[method-assign]
    connector.run()
    assert 1 <= connector.delays[0] <= 2


# --- the script --------------------------------------------------------------------------------------


def test_sigterm_stops_both_children_and_exits_0(machine: Machine, tmp_path: Path) -> None:
    machine.configure(mode="hold")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {**machine.env(), "XDG_RUNTIME_DIR": str(runtime)}
    process = subprocess.Popen(
        [sys.executable, "-I", str(CONNECTOR), *machine.argv()], env=env, stderr=subprocess.PIPE, text=True
    )
    deadline = time.monotonic() + 15
    while not machine.hub_side() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert machine.hub_side()[0]["t"] == "hello"
    second = subprocess.run(
        [sys.executable, "-I", str(CONNECTOR), *machine.argv()],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert second.returncode == 1
    assert "another switchboard-connect of this user is running" in second.stderr
    process.send_signal(signal.SIGTERM)
    assert process.wait(15) == 0
    assert process.stderr is not None
    assert "switchboard-connect: stopped" in process.stderr.read()
    process.stderr.close()
    ssh_pid = machine.ssh_calls()[0]["pid"]
    with pytest.raises(ProcessLookupError):
        os.kill(ssh_pid, 0)


def test_the_deploy_copy_is_byte_identical_executable_python_3_8_and_stdlib_only() -> None:
    deployed = REPO / "deploy" / "remote" / "switchboard-connect"
    assert deployed.read_bytes() == CONNECTOR.read_bytes()
    assert os.access(deployed, os.X_OK)
    source = CONNECTOR.read_text(encoding="utf-8")
    assert source.startswith("#!/usr/bin/env python3\n")
    tree = ast.parse(source, feature_version=(3, 8))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    for newer in ("removeprefix", "removesuffix", "strict=True", "match ", "functools.cache("):
        assert newer not in source, newer


# --- the whole chain ---------------------------------------------------------------------------------


async def test_connector_fake_ssh_relay_and_hub(machine: Machine) -> None:
    with socket_dir() as directory:
        remote = RemoteConfig.model_validate(
            {"machines": [agent_machine("laptop")], "agents": agents_section(directory)}
        )
        hub = AgentHub(remote, str(directory), LogCapture().logger)
        await hub.start()
        assert hub.problem is None
        machine.configure(mode="relay", machine="laptop", socket=str(directory / "hub.sock"))
        connector = machine.connector()
        attempt = asyncio.create_task(asyncio.to_thread(connector.attempt))
        try:
            await wait_until(lambda: hub.live("laptop") is not None)
            conn = hub.live("laptop")
            assert conn is not None
            assert conn.peer == "203.0.113.9"
            result = await hub.transport(remote.machines[0]).call("list", timeout_seconds=10)
            assert result.ok, result
        finally:
            await hub.stop()
        outcome = await attempt
        assert outcome.reason == "closed"
        assert FAKE_TMUX  # the dispatcher ran on the fake tmux of `machine.world`
