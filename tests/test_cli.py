"""The CLI: arguments, commands, output texts, exit codes, and `serve` with a graceful SIGTERM in a
real process."""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import socket
import stat
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.helpers import claude_config, create_grant
from voice_coding_mcp.auth import OAuthStore, get_pairing
from voice_coding_mcp.auth.store import NewClient, NewGrant
from voice_coding_mcp.cli import (
    EXIT_ERROR,
    EXIT_OK,
    EXIT_USAGE,
    CliIo,
    EarlyExitError,
    PidFileLockedError,
    UsageError,
    _bind,
    build_parser,
    format_ago,
    format_time,
    main,
    parse_args,
    pid_file,
    start_server,
)
from voice_coding_mcp.config import load_config
from voice_coding_mcp.log import create_logger, silent_logger
from voice_coding_mcp.mcp import SERVER_VERSION
from voice_coding_mcp.tasks.manager import Task, TaskStatus

PUBLIC_URL = "https://vcm.example.com"


class Io:
    def __init__(self, env: dict[str, str] | None = None) -> None:
        self.out: list[str] = []
        self.err: list[str] = []
        self.io = CliIo(out=self.out.append, err=self.err.append, env=env or {})

    @property
    def stdout(self) -> str:
        return "".join(self.out)

    @property
    def stderr(self) -> str:
        return "".join(self.err)


def free_port() -> int:
    """A port that was free a moment ago (the config does not allow port 0)."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def write_config(root: Path, **extra: Any) -> str:
    projects = root / "projects"
    (projects / "alpha-app").mkdir(parents=True, exist_ok=True)
    claude = claude_config(str(root / "record.jsonl"))
    data = {
        "publicUrl": PUBLIC_URL,
        "stateDir": str(root / "state"),
        "projectRoots": [str(projects)],
        "claude": {"bin": claude.bin, "env": dict(claude.env)},
        **extra,
    }
    path = root / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


async def run_cli(*argv: str, env: dict[str, str] | None = None) -> tuple[int, Io]:
    io = Io(env)
    return await main(list(argv), io.io), io


# --- arguments ------------------------------------------------------------------------------------


def test_parses_flags_before_and_after_the_command() -> None:
    args = parse_args(["--config", "/x.json", "pair", "--minutes=5"])
    assert (args.command, args.config, args.minutes) == ("pair", "/x.json", 5)
    args = parse_args(["revoke-all", "-y", "--config=/y.json"])
    assert (args.command, args.config, args.yes) == ("revoke-all", "/y.json", True)
    assert parse_args(["--config", "/a.json", "status", "--config", "/b.json"]).config == "/b.json"
    assert parse_args(["status"]).config is None
    assert parse_args(["pair", "--minutes", " 15 "]).minutes == 15
    assert parse_args(["pair", "--minutes", "1440"]).minutes == 1440
    assert parse_args(["pair"]).minutes is None
    assert not parse_args(["revoke-all"]).yes
    args = parse_args(["serve", "--pid-file", "/run/vcm.pid", "--config", "/c.json"])
    assert (args.command, args.pid_file, args.config) == ("serve", "/run/vcm.pid", "/c.json")
    assert parse_args(["serve"]).pid_file is None


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "the following arguments are required: <command>"),
        (["--config"], "argument --config: expected one argument"),
        (["status", "--config"], "argument --config: expected one argument"),
        (["pair", "--minutes", "0"], "argument --minutes: must be a whole number between 1 and 1440, got 0"),
        (["pair", "--minutes", "1441"], "argument --minutes: must be a whole number between 1 and 1440, got 1441"),
        (["pair", "--minutes", "abc"], "argument --minutes: must be a whole number between 1 and 1440, got abc"),
        (["pair", "--minutes", "1.5"], "argument --minutes: must be a whole number between 1 and 1440, got 1.5"),
        (["pair", "--minutes", "0x10"], "argument --minutes: must be a whole number between 1 and 1440, got 0x10"),
        (["pair", "--minutes"], "argument --minutes: expected one argument"),
        (["--force"], "the following arguments are required: <command>"),
        (["status", "--force"], "unrecognized arguments: --force"),
        (["pair", "status"], "unrecognized arguments: status"),
        (["status", "--minutes", "5"], "unrecognized arguments: --minutes 5"),
        (["pair", "--pid-file", "/x.pid"], "unrecognized arguments: --pid-file /x.pid"),
        (["serve", "--pid-file"], "argument --pid-file: expected one argument"),
    ],
)
def test_rejects_bad_arguments(argv: list[str], message: str) -> None:
    with pytest.raises(UsageError) as err:
        parse_args(argv)
    assert str(err.value) == message


def test_rejects_unknown_command() -> None:
    with pytest.raises(UsageError) as err:
        parse_args(["frobnicate"])
    # Python 3.12 patch releases differ in whether argparse quotes the choices.
    assert str(err.value).replace("'", "") == (
        "argument <command>: invalid choice: frobnicate (choose from serve, pair, unpair, status, revoke-all, remote-check, remote-dispatcher, remote-connector, agent-relay, audit, incidents, usage, timesheet, set-public-url)"
    )


def test_help_and_version_end_parsing_early() -> None:
    with pytest.raises(EarlyExitError) as done:
        parse_args(["-v"])
    assert done.value.text == f"{SERVER_VERSION}\n"
    with pytest.raises(EarlyExitError) as done:
        parse_args(["-h"])
    assert done.value.text == build_parser().format_help()
    with pytest.raises(EarlyExitError) as done:
        parse_args(["serve", "--help"])
    assert "--pid-file PATH" in done.value.text


async def test_help_version_and_usage_errors() -> None:
    code, io = await run_cli("--version")
    assert (code, io.stdout) == (EXIT_OK, f"{SERVER_VERSION}\n")
    code, io = await run_cli("--help")
    assert (code, io.stdout) == (EXIT_OK, build_parser().format_help())
    for command in ("serve", "pair", "unpair", "status", "revoke-all"):
        code, io = await run_cli(command, "-h")
        assert code == EXIT_OK
        assert io.stdout.startswith(f"usage: voice-coding-mcp {command} [-h] [--config PATH]")
    code, io = await run_cli()
    assert code == EXIT_USAGE
    assert io.stderr == (
        f"{build_parser().format_usage()}voice-coding-mcp: error: the following arguments are required: <command>\n"
    )
    code, io = await run_cli("pair", "--minutes", "0")
    assert code == EXIT_USAGE
    assert io.stderr.startswith("usage: voice-coding-mcp pair ")
    assert io.stderr.endswith(
        "voice-coding-mcp pair: error: argument --minutes: must be a whole number between 1 and 1440, got 0\n"
    )
    code, io = await run_cli("status", "--bogus")
    assert (code, io.stderr.splitlines()[-1]) == (
        EXIT_USAGE,
        "voice-coding-mcp: error: unrecognized arguments: --bogus",
    )


async def test_config_errors_are_usage_errors(real_tmp: Path) -> None:
    code, io = await run_cli("status", "--config", str(real_tmp / "missing.json"))
    assert code == EXIT_USAGE
    assert io.stderr.startswith(f"Cannot read config file {real_tmp / 'missing.json'}")
    path = write_config(real_tmp)
    code, io = await run_cli("status", env={"VCM_CONFIG": path})
    assert code == EXIT_OK, io.stderr


# --- commands -------------------------------------------------------------------------------------


async def test_pair_unpair_and_status(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    state = str(real_tmp / "state")
    code, io = await run_cli("status", "--config", path)
    assert code == EXIT_OK
    assert io.stdout == (
        f"Connector URL: {PUBLIC_URL}/mcp\nPairing window: closed\nClients: 0, live authorizations: 0\nTasks: 0\n"
    )

    code, io = await run_cli("pair", "--config", path, "--minutes", "5")
    assert code == EXIT_OK
    window = await get_pairing(state)
    assert window is not None
    lines = io.stdout.split("\n")
    assert lines[0].startswith("Pairing code: ")
    assert lines[1] == f"Valid until {format_time(window.expires_at)} (5 min), 5 attempts."
    assert lines[2:] == [
        "",
        "In the Claude app: Settings > Connectors > Add custom connector, with this URL:",
        f"  {PUBLIC_URL}/mcp",
        "Then click Connect and enter the code on the page that opens.",
        "",
    ]

    code, io = await run_cli("status", "--config", path)
    assert (
        f"Pairing window: open until {format_time(window.expires_at)}, {window.attempts_left} attempt(s) left\n"
        in io.stdout
    )

    assert (await run_cli("unpair", "--config", path))[1].stdout == "Pairing window closed.\n"
    assert (await run_cli("unpair", "--config", path))[1].stdout == "No pairing window was open.\n"


async def test_status_lists_clients_and_task_counts(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    state = real_tmp / "state"
    store = OAuthStore(str(state))
    client = await store.register_client(
        NewClient(
            client_name="Claude",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            token_endpoint_auth_method="none",
        )
    )
    await create_grant(
        store,
        NewGrant(client_id=client.client.client_id, client_name="Claude", resource=f"{PUBLIC_URL}/mcp", scope="mcp"),
    )
    tasks = state / "tasks"
    tasks.mkdir()
    statuses: list[TaskStatus] = ["done", "done", "failed", "running"]
    for i, status in enumerate(statuses, start=1):
        task = Task(
            id=i, project="alpha", project_path="/p", prompt="x", status=status, new_session=False, created_at=""
        )
        (tasks / f"{i}.json").write_text(json.dumps(task.to_json()), encoding="utf-8")
    (tasks / "5.json").write_text("{broken", encoding="utf-8")
    (tasks / "notes.json").write_text("{}", encoding="utf-8")

    code, io = await run_cli("status", "--config", path)
    assert code == EXIT_OK
    created = format_time(client.client.created_at)
    assert io.stdout.split("\n")[2:] == [
        "Clients: 1, live authorizations: 1",
        f"  - Claude (DCR, redirect claude.ai): 1 live, created {created}, last used just now",
        "Tasks: 5 (1 running, 2 done, 1 failed, 1 unreadable)",
        "",
    ]


async def test_status_strips_control_characters_from_client_strings(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    store = OAuthStore(str(real_tmp / "state"))
    # Stored directly (as an older version could have), bypassing the sanitizing /register.
    await store.register_client(
        NewClient(
            client_name="\x1b]0;owned\x07Evil\u202e\x1b[2J",
            redirect_uris=["https://claude.ai/api/mcp/auth_callback"],
            token_endpoint_auth_method="none",
        )
    )
    await create_grant(
        store,
        NewGrant(
            client_id="https://x.example/cimd", client_name="Cimd\x9b31m", resource=f"{PUBLIC_URL}/mcp", scope="mcp"
        ),
    )
    code, io = await run_cli("status", "--config", path)
    assert code == EXIT_OK
    assert "\x1b" not in io.stdout
    assert "\x07" not in io.stdout
    assert "\x9b" not in io.stdout
    assert "\u202e" not in io.stdout
    assert "  - ]0;ownedEvil[2J (DCR, redirect claude.ai): 0 live" in io.stdout
    assert "  - Cimd31m (CIMD): 1 live" in io.stdout


async def test_revoke_all_needs_yes(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    code, io = await run_cli("revoke-all", "--config", path)
    assert code == EXIT_USAGE
    assert io.stderr == (
        "revoke-all removes every client and token; the Claude connector must pair again. Run it with --yes.\n"
    )
    code, io = await run_cli("revoke-all", "--yes", "--config", path)
    assert (code, io.stdout) == (EXIT_OK, "Revoked 0 client(s) and 0 authorization(s).\n")


async def test_command_errors_exit_1(real_tmp: Path) -> None:
    path = write_config(real_tmp)
    (real_tmp / "state").mkdir()
    (real_tmp / "state" / "pairing.json").write_text("{broken", encoding="utf-8")
    code, io = await run_cli("unpair", "--config", path)
    assert code == EXIT_ERROR
    assert io.stderr.startswith("Error: ")


def test_time_formats() -> None:
    ms = datetime(2026, 1, 5, 9, 7).timestamp() * 1000
    assert format_time(ms) == "Jan 5, 2026, 9:07 AM"
    assert format_time(datetime(2026, 11, 22, 23, 30).timestamp() * 1000) == "Nov 22, 2026, 11:30 PM"
    assert format_time(datetime(2026, 11, 22, 0, 5).timestamp() * 1000) == "Nov 22, 2026, 12:05 AM"
    assert format_time(datetime(2026, 11, 22, 12, 0).timestamp() * 1000) == "Nov 22, 2026, 12:00 PM"
    months = [format_time(datetime(2026, month, 1, 13, 0).timestamp() * 1000).split(" ")[0] for month in range(1, 13)]
    assert months == ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    now = 10_000_000_000
    assert format_ago(None, now) == "never"
    assert format_ago(now - 20_000, now) == "just now"
    assert format_ago(now - 5 * 60_000, now) == "5 minutes ago"
    assert format_ago(now - 60_000, now) == "1 minute ago"
    assert format_ago(now - 119 * 60_000, now) == "2 hours ago"
    assert format_ago(now - 72 * 3_600_000, now) == "3 days ago"


# --- serve ----------------------------------------------------------------------------------------


async def test_start_server_binds_a_free_port_and_stops(real_tmp: Path) -> None:
    config = load_config(write_config(real_tmp, listen={"host": "127.0.0.1", "port": free_port()}))
    lines: list[str] = []
    logger = create_logger("debug", write=lines.append)
    running = await start_server(config, logger, {})
    try:
        assert running.url.startswith("http://127.0.0.1:")
        async with httpx2.AsyncClient() as http:
            res = await http.get(f"{running.url}/healthz")
            assert res.json() == {"ok": True}
    finally:
        await running.stop()
        await running.stop()
    entries = [json.loads(line) for line in lines]
    msgs = [entry["msg"] for entry in entries]
    token_note = next(entry for entry in entries if entry["msg"].startswith("CLAUDE_CODE_OAUTH_TOKEN is not set"))
    assert token_note["level"] == "info"
    assert "credentials of the logged-in user" in token_note["msg"]
    assert msgs.count("server stopping") == 1
    assert msgs[-1] == "server stopped"


async def test_start_server_fails_when_the_port_is_taken(real_tmp: Path) -> None:
    taken = _bind("127.0.0.1", 0)
    try:
        port = taken.getsockname()[1]
        config = load_config(write_config(real_tmp, listen={"host": "127.0.0.1", "port": port}))
        with pytest.raises(OSError, match="in use"):
            await start_server(config, silent_logger, {})
    finally:
        taken.close()


async def test_serve_stops_gracefully_on_sigterm_and_fails_running_tasks(real_tmp: Path) -> None:
    path = write_config(real_tmp, listen={"host": "127.0.0.1", "port": free_port()}, tasks={"syncWaitSeconds": 1})
    state = real_tmp / "state"
    store = OAuthStore(str(state))
    client = await store.register_client(
        NewClient(client_name="Test", redirect_uris=["https://claude.ai/x"], token_endpoint_auth_method="none")
    )
    tokens = await create_grant(
        store,
        NewGrant(client_id=client.client.client_id, client_name="Test", resource=f"{PUBLIC_URL}/mcp", scope="mcp"),
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "voice_coding_mcp",
        "serve",
        "--config",
        path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, "VCM_LOG_LEVEL": "info"},
    )
    assert proc.stdout is not None
    try:
        url = ""
        while not url:
            line = await asyncio.wait_for(proc.stdout.readline(), 15)
            assert line, "server exited before it started"
            record = json.loads(line)
            if record["msg"] == "server started":
                url = record["listen"]
        headers = {
            "authorization": f"Bearer {tokens.access_token}",
            "host": "vcm.example.com",
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "mcp-protocol-version": "2025-11-25",
        }
        call = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "run_task", "arguments": {"project": "alpha-app", "prompt": "SLEEP 60000"}},
        }
        async with httpx2.AsyncClient(base_url=url, timeout=30) as http:
            res = await http.post("/mcp", headers=headers, content=json.dumps(call))
            assert res.status_code == 200
            assert "still running" in res.json()["result"]["content"][0]["text"]
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 20) == EXIT_OK
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    rest = (await proc.stdout.read()).decode()
    msgs = [json.loads(line) for line in rest.splitlines() if line.startswith("{")]
    assert {"msg": "server stopping", "reason": "server stopped (SIGTERM)"}.items() <= next(
        m for m in msgs if m["msg"] == "server stopping"
    ).items()
    assert msgs[-1]["msg"] == "server stopped"
    task = Task.model_validate(json.loads((state / "tasks" / "1.json").read_text(encoding="utf-8")))
    assert task.status == "failed"
    assert task.error == "server stopped (SIGTERM)"


# --- pid file -------------------------------------------------------------------------------------


def lock_is_free(path: Path) -> bool:
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    finally:
        os.close(fd)
    return True


def test_pid_file_is_written_locked_and_removed(real_tmp: Path) -> None:
    path = real_tmp / "run" / "nested" / "vcm.pid"
    with pid_file(str(path)):
        assert path.read_text(encoding="ascii") == f"{os.getpid()}\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert not lock_is_free(path)
        with pytest.raises(PidFileLockedError) as refused, pid_file(str(path)):
            pass
        assert str(refused.value) == f"another server holds the pid file {path} (pid {os.getpid()})"
        assert path.exists()
    assert not path.exists()


def test_a_stale_pid_file_is_taken_over(real_tmp: Path) -> None:
    path = real_tmp / "vcm.pid"
    path.write_text("99999999\nleftover from a crash\n", encoding="ascii")
    path.chmod(0o644)
    with pid_file(str(path)):
        assert path.read_text(encoding="ascii") == f"{os.getpid()}\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not path.exists()


async def test_serve_holds_the_pid_file_and_a_second_serve_is_refused(real_tmp: Path) -> None:
    path = write_config(real_tmp, listen={"host": "127.0.0.1", "port": free_port()})
    pid_path = real_tmp / "run" / "vcm.pid"
    argv = [sys.executable, "-m", "voice_coding_mcp", "serve", "--config", path, "--pid-file", str(pid_path)]
    env = {**os.environ, "VCM_LOG_LEVEL": "info"}
    proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, env=env)
    assert proc.stdout is not None
    try:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), 15)
            assert line, "server exited before it started"
            if json.loads(line)["msg"] == "server started":
                break
        assert pid_path.read_text(encoding="ascii") == f"{proc.pid}\n"
        assert not lock_is_free(pid_path)

        second = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env
        )
        out, err = await asyncio.wait_for(second.communicate(), 15)
        assert second.returncode == EXIT_ERROR
        assert err.decode() == f"Error: another server holds the pid file {pid_path} (pid {proc.pid})\n"
        assert out == b""
        assert pid_path.read_text(encoding="ascii") == f"{proc.pid}\n"

        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(proc.wait(), 20) == EXIT_OK
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    assert not pid_path.exists()
