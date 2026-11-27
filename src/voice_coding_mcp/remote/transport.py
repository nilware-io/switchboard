"""How the hub reaches a machine's dispatcher: over SSH, or as a local child process
(docs/SWITCHBOARD.md, "Transports").

`Transport.call` never raises. Every failure maps to a fixed code that the speech layer turns into
words: `timeout`, `host_key`, `auth`, `refused`, `dispatcher_missing`, `bad_response`,
`refused_by_policy` (the dispatcher said no; its own code is in `detail`) or `other`; an `agent`
machine (`agents.AgentTransport`) adds `not_connected` (nothing was sent) and `disconnected` (the
connection closed after the request went out). Processes run
from argv (never a shell) in a new session and are killed as a process group on timeout and when
the call is cancelled; stdout is capped and stderr is kept only as a short tail for the error
mapping, never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from .config import MachineConfig, RemoteConfig, known_hosts_path
from .protocol import MAX_RESPONSE_BYTES, ResponseError, build_command, parse_response, request_tokens, response_cap
from .ssh import build_ssh_argv, ssh_env

if TYPE_CHECKING:
    from .agents import AgentHub

TransportError = Literal[
    "timeout",
    "host_key",
    "auth",
    "refused",
    "dispatcher_missing",
    "bad_response",
    "refused_by_policy",
    "other",
    "not_connected",
    "disconnected",
]

DISPATCHER_PATH = str(Path(__file__).resolve().parent / "dispatcher.py")
"""The bundled dispatcher (also `deploy/remote/switchboard-remote`)."""
STDERR_TAIL_BYTES = 2048
KILL_GRACE_SECONDS = 1.0
LOCAL_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMUX_TMPDIR", "CLAUDE_CONFIG_DIR")
"""What the local dispatcher gets from the server's environment."""
TEST_ENV_RE = re.compile(r"SWITCHBOARD_REMOTE_TEST\w*|FAKE_TMUX_\w+")
"""Test hooks passed on to a local dispatcher when the server itself has them."""


@dataclass(frozen=True)
class TransportResult:
    ok: bool
    data: BaseModel | None = None
    """The parsed response (protocol model of the verb) when ok."""
    error: TransportError | None = None
    detail: str | None = None
    """With `refused_by_policy`: the dispatcher's error code (`gone`, `bad_request`, ...)."""
    elapsed_ms: int = 0


class Transport(Protocol):
    machine: MachineConfig

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult: ...


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: bytes
    stderr_tail: str
    timed_out: bool = False
    overflow: bool = False
    spawn_error: bool = False


async def run_process(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    timeout_seconds: float,
    stdout_cap: int = MAX_RESPONSE_BYTES,
    stdin: bytes | None = None,
) -> ProcessResult:
    """Runs argv with stdin from /dev/null (or the `stdin` bytes, then closed), stdout capped, in its
    own process group. Never raises."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError:
        return ProcessResult(returncode=None, stdout=b"", stderr_tail="", spawn_error=True)
    assert process.stdout is not None
    assert process.stderr is not None
    out = bytearray()
    err = bytearray()
    overflow = False

    async def read_out() -> None:
        nonlocal overflow
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            out.extend(chunk)
            if len(out) > stdout_cap:
                overflow = True
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                return

    async def read_err() -> None:
        assert process.stderr is not None
        while chunk := await process.stderr.read(65536):
            err.extend(chunk)
            if len(err) > STDERR_TAIL_BYTES * 2:
                del err[: len(err) - STDERR_TAIL_BYTES]

    async def write_in() -> None:
        if stdin is None or process.stdin is None:
            return
        # A peer that exits without reading (a refusal before the text) closes the pipe early.
        with contextlib.suppress(ConnectionError, OSError):
            process.stdin.write(stdin)
            await process.stdin.drain()
        with contextlib.suppress(ConnectionError, OSError):
            process.stdin.close()

    timed_out = False
    try:
        async with asyncio.timeout(timeout_seconds):
            await asyncio.gather(read_out(), read_err(), write_in())
            if not overflow:
                await process.wait()
    except TimeoutError:
        timed_out = True
    except asyncio.CancelledError:
        # The caller is gone (a watcher tick stopped at shutdown): kill the group at once, without
        # waiting, so no ssh or dispatcher outlives it.
        with contextlib.suppress(OSError):
            os.killpg(process.pid, signal.SIGKILL)
        raise
    if timed_out or overflow or process.returncode is None:
        await _kill_group(process)
    tail = bytes(err[-STDERR_TAIL_BYTES:]).decode("utf-8", "replace")
    return ProcessResult(
        returncode=process.returncode, stdout=bytes(out), stderr_tail=tail, timed_out=timed_out, overflow=overflow
    )


async def _kill_group(process: asyncio.subprocess.Process) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError):
            os.killpg(process.pid, sig)
        try:
            await asyncio.wait_for(process.wait(), KILL_GRACE_SECONDS)
            # The leader is gone; anything left in its group still gets SIGKILL.
            with contextlib.suppress(OSError):
                os.killpg(process.pid, signal.SIGKILL)
            return
        except TimeoutError:
            continue


_SSH_ERRORS: tuple[tuple[re.Pattern[str], TransportError], ...] = (
    (
        re.compile(r"host key verification failed|remote host identification has changed|host key .* is known", re.I),
        "host_key",
    ),
    (re.compile(r"permission denied", re.I), "auth"),
    (re.compile(r"connection timed out|operation timed out|connect.*timed out", re.I), "timeout"),
    (
        re.compile(
            r"connection refused|no route to host|could not resolve hostname|network is unreachable"
            r"|name or service not known|temporary failure in name resolution|connection closed by",
            re.I,
        ),
        "refused",
    ),
)


def map_ssh_failure(stderr_tail: str) -> TransportError:
    """The error code of an ssh exit 255 from its stderr."""
    for pattern, code in _SSH_ERRORS:
        if pattern.search(stderr_tail):
            return code
    return "other"


def interpret(verb: str, result: ProcessResult, *, via_ssh: bool, started: float) -> TransportResult:
    elapsed = round((time.monotonic() - started) * 1000)

    def failed(code: TransportError, detail: str | None = None) -> TransportResult:
        return TransportResult(ok=False, error=code, detail=detail, elapsed_ms=elapsed)

    if result.spawn_error:
        return failed("other" if via_ssh else "dispatcher_missing")
    if result.timed_out:
        return failed("timeout")
    if result.overflow:
        return failed("bad_response")
    if via_ssh and result.returncode == 255:
        return failed(map_ssh_failure(result.stderr_tail))
    silent = not result.stdout.strip()
    if silent and "Traceback (most recent call last)" in result.stderr_tail:
        # The dispatcher ran and crashed (its traceback may mention "No such file" too).
        return failed("other")
    missing = result.returncode == 127 or re.search(r"command not found|no such file", result.stderr_tail, re.I)
    if missing and silent:
        return failed("dispatcher_missing")
    if silent and result.returncode != 0:
        # A non-zero exit without output and without a sign of a missing program: a crash, not a
        # missing dispatcher.
        return failed("other")
    try:
        parsed = parse_response(verb, result.stdout)
    except ResponseError as err:
        return failed("bad_response" if err.code == "bad_response" else "dispatcher_missing")
    if isinstance(parsed, str):
        return failed("refused_by_policy", parsed)
    return TransportResult(ok=True, data=parsed, elapsed_ms=elapsed)


class SshTransport:
    """The dispatcher on another machine (or the owner's account on localhost) over OpenSSH."""

    def __init__(self, machine: MachineConfig, remote: RemoteConfig, known_hosts: str, base_env: Mapping[str, str]):
        self.machine = machine
        self._remote = remote
        self._known_hosts = known_hosts
        self._env = ssh_env(base_env)

    def argv(self, verb: str, args: Sequence[str] = ()) -> list[str]:
        command = build_command(self.machine.dispatcher, verb, args)
        return build_ssh_argv(self._remote.ssh, self.machine, self._known_hosts, command)

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        started = time.monotonic()
        try:
            argv = self.argv(verb, args)
        except ValueError:
            return TransportResult(ok=False, error="other")
        result = await run_process(
            argv, self._env, timeout_seconds=timeout_seconds, stdout_cap=response_cap(verb), stdin=stdin
        )
        return interpret(verb, result, via_ssh=True, started=started)


class LocalTransport:
    """The bundled dispatcher as a child process of the server, with the machine's settings as policy."""

    def __init__(
        self,
        machine: MachineConfig,
        base_env: Mapping[str, str],
        *,
        python: str | None = None,
        slash_commands: Sequence[str] = (),
    ):
        self.machine = machine
        self.slash_commands = list(slash_commands)
        """`remote.send.slashCommands`: the hub's list is the dispatcher's policy too."""
        self._python = python or sys.executable
        self._env = {k: v for k, v in base_env.items() if k in LOCAL_ENV_KEYS or TEST_ENV_RE.fullmatch(k)}

    def policy_args(self) -> list[str]:
        machine = self.machine
        args = ["--policy-from-hub", "--sessions", ",".join(machine.sessions)]
        flags = (
            (machine.allow_usage, "--allow-usage"),
            (machine.allow_send, "--allow-send"),
            (machine.allow_permission_answers, "--allow-permission-answers"),
            (machine.send_to_any_pane, "--send-to-any-pane"),
            (machine.allow_attachments, "--allow-attachments"),
        )
        args += [flag for enabled, flag in flags if enabled]
        if self.slash_commands:
            args += ["--slash-commands", ",".join(self.slash_commands)]
        if machine.claude_home:
            args += ["--claude-home", machine.claude_home]
        return args

    def argv(self, verb: str, args: Sequence[str] = ()) -> list[str]:
        return [self._python, "-I", DISPATCHER_PATH, *self.policy_args(), "--", *request_tokens(verb, args)]

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        started = time.monotonic()
        try:
            argv = self.argv(verb, args)
        except ValueError:
            return TransportResult(ok=False, error="other")
        result = await run_process(
            argv, self._env, timeout_seconds=timeout_seconds, stdout_cap=response_cap(verb), stdin=stdin
        )
        return interpret(verb, result, via_ssh=False, started=started)


class OfflineTransport:
    """An `agent` machine where no hub holds its connection (the CLI runs in another process than
    the hub): every call is `not_connected`."""

    def __init__(self, machine: MachineConfig) -> None:
        self.machine = machine

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        return TransportResult(ok=False, error="not_connected")


def make_transport(
    machine: MachineConfig,
    remote: RemoteConfig,
    state_dir: str,
    base_env: Mapping[str, str],
    agents: AgentHub | None = None,
) -> Transport:
    if machine.transport == "local":
        return LocalTransport(machine, base_env, slash_commands=remote.send.slash_commands)
    if machine.transport == "agent":
        return agents.transport(machine) if agents is not None else OfflineTransport(machine)
    return SshTransport(machine, remote, known_hosts_path(machine, state_dir), base_env)
