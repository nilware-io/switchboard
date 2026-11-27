#!/usr/bin/env python3
"""switchboard-connect: keeps a machine connected out to the Switchboard hub.

Standard library only, Python 3.8 or newer, one file. `deploy/remote/switchboard-connect` is a
byte-identical copy of this file; `voice-coding-mcp remote-connector` prints it. Design:
docs/SWITCHBOARD.md, "Outbound agents".

    python3 -I switchboard-connect --host hub.example.org [--port 22] --user sbrelay
        --identity ~/.ssh/switchboard_relay --known-hosts ~/.config/switchboard-connect/known_hosts
        [--max-backoff 300] [--ssh /usr/bin/ssh]
        -- /usr/bin/python3 -I /home/USER/.local/bin/switchboard-remote --serve --sessions fpga,notes

It runs `ssh` to the hub's relay account (no remote command: the hub's forced command decides what
runs there) and the dispatcher in serve mode, and wires ssh's stdout to the dispatcher's stdin and
the dispatcher's stdout to ssh's stdin. The hub only sends requests down that connection; the
dispatcher's policy is its own command line after `--`, here on the machine. When either process
ends, the other is stopped and the connection is opened again after a capped, jittered backoff.
The hub's host key is pinned in the known-hosts file under the alias `switchboard-hub`; this
program never writes that file.
"""

from __future__ import annotations

import contextlib
import fcntl
import ipaddress
import os
import random
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import TextIO

VERSION = "0.7.0"
HOST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
HOST_IPV6_RE = re.compile(r"[0-9A-Fa-f:]{2,39}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,255}")
HOST_KEY_ALIAS = "switchboard-hub"
DEFAULT_SSH = "/usr/bin/ssh"
DEFAULT_PORT = 22
DEFAULT_MAX_BACKOFF = 300
MAX_BACKOFF_RANGE = (30, 3600)
INITIAL_DELAY = 2.0
RESET_AFTER_SECONDS = 60.0
"""A connection that stayed up this long starts the backoff from INITIAL_DELAY again."""
KILL_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.1
DISPATCHER_GRACE_SECONDS = 1.0
"""How long the dispatcher may take to exit on its own after ssh ended."""
STDERR_TAIL_BYTES = 2048
EXIT_REPLACED = 75
"""The dispatcher's exit code after the hub replaced this connection with a newer one."""
EXIT_REFUSED = 76
"""The dispatcher's exit code after the hub refused this connection (unknown machine, protocol)."""
LONG_WAIT_REASONS = ("replaced", "refused_by_hub", "auth", "host_key")
"""Retried after the maximum delay: repeating them sooner changes nothing."""
SSH_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL")
DISPATCHER_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TMUX_TMPDIR", "CLAUDE_CONFIG_DIR", "XDG_RUNTIME_DIR")
TEST_ENV_RE = re.compile(r"SWITCHBOARD_REMOTE_TEST\w*|FAKE_TMUX_\w+")
"""The dispatcher's own test hooks, passed on when this process has them (it honours them only with
SWITCHBOARD_REMOTE_TEST=1)."""
LOCK_NAME = "switchboard-connect.lock"

_SSH_ERRORS = (
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
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class UsageError(Exception):
    """A command line this program refuses to run with."""


class Options:
    def __init__(self) -> None:
        self.host = ""
        self.port = DEFAULT_PORT
        self.user = ""
        self.identity = ""
        self.known_hosts = ""
        self.max_backoff = DEFAULT_MAX_BACKOFF
        self.ssh = DEFAULT_SSH
        self.command: list[str] = []


def _host(value: str) -> bool:
    if HOST_NAME_RE.fullmatch(value) and ".." not in value:
        return True
    if HOST_IPV6_RE.fullmatch(value):
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            return False
        return True
    return False


def file_problem(path: str, uid: int, *, private: bool) -> str | None:
    """Why `path` cannot be used as the identity (`private`) or known-hosts file, or None."""
    try:
        info = os.lstat(path)
    except OSError:
        return "does not exist"
    if not stat.S_ISREG(info.st_mode):
        return "is not a regular file"
    if info.st_uid != uid:
        return "is not owned by this user"
    if private and info.st_mode & 0o077:
        return "is readable by others; use mode 600"
    if not private and info.st_mode & 0o022:
        return "is writable by others"
    return None


def parse_args(argv: list[str], uid: int) -> Options:
    """The options, checked. Raises UsageError."""
    options = Options()
    seen: set[str] = set()
    index = 0
    values = ("--host", "--port", "--user", "--identity", "--known-hosts", "--max-backoff", "--ssh")
    while index < len(argv):
        token = argv[index]
        if token == "--":
            options.command = argv[index + 1 :]
            break
        if token not in values or token in seen or index + 1 >= len(argv):
            raise UsageError(f"unexpected argument {_clean(token)!r}")
        seen.add(token)
        value = argv[index + 1]
        index += 2
        if token == "--host":
            if not _host(value):
                raise UsageError("--host must be a host name or an IP address")
            options.host = value
        elif token == "--user":
            if not USER_RE.fullmatch(value):
                raise UsageError("--user must be a user name")
            options.user = value
        elif token in ("--port", "--max-backoff"):
            if not value.isdigit() or len(value) > 5:
                raise UsageError(f"{token} must be a number")
            number = int(value)
            if token == "--port":
                if not 1 <= number <= 65535:
                    raise UsageError("--port must be 1 to 65535")
                options.port = number
            else:
                if not MAX_BACKOFF_RANGE[0] <= number <= MAX_BACKOFF_RANGE[1]:
                    raise UsageError("--max-backoff must be 30 to 3600 seconds")
                options.max_backoff = number
        else:
            if not PATH_RE.fullmatch(value) or ".." in value.split("/"):
                raise UsageError(f"{token} must be an absolute path of letters, digits and ._/- only")
            if token == "--identity":
                options.identity = value
            elif token == "--known-hosts":
                options.known_hosts = value
            else:
                options.ssh = value
    else:
        raise UsageError("the dispatcher command must follow --")
    for name, value in (("--host", options.host), ("--user", options.user), ("--identity", options.identity)):
        if not value:
            raise UsageError(f"{name} is required")
    if not options.known_hosts:
        raise UsageError("--known-hosts is required (the hub's pinned host key)")
    for name, path, private in (("--identity", options.identity, True), ("--known-hosts", options.known_hosts, False)):
        problem = file_problem(path, uid, private=private)
        if problem:
            raise UsageError(f"{name} {path} {problem}")
    command = options.command
    if not command or not os.path.isabs(command[0]) or "--serve" not in command[1:]:
        raise UsageError("after -- comes the dispatcher's absolute path and its options, with --serve")
    return options


def ssh_argv(options: Options) -> list[str]:
    """No remote command: the hub's forced command decides what runs there."""
    settings = (
        "BatchMode=yes",
        "IdentitiesOnly=yes",
        "IdentityAgent=none",
        "ForwardAgent=no",
        "ForwardX11=no",
        "ClearAllForwardings=yes",
        "PermitLocalCommand=no",
        "StrictHostKeyChecking=yes",
        f"UserKnownHostsFile={options.known_hosts}",
        "GlobalKnownHostsFile=/dev/null",
        f"HostKeyAlias={HOST_KEY_ALIAS}",
        "HostKeyAlgorithms=ssh-ed25519",
        "UpdateHostKeys=no",
        "CheckHostIP=no",
        "HashKnownHosts=no",
        "ConnectTimeout=10",
        "ServerAliveInterval=15",
        "ServerAliveCountMax=3",
        "RequestTTY=no",
        "LogLevel=ERROR",
    )
    argv = [options.ssh, "-F", "/dev/null", "-T", "-e", "none"]
    for setting in settings:
        argv += ["-o", setting]
    return [*argv, "-i", options.identity, "-p", str(options.port), "-l", options.user, "--", options.host]


def ssh_env(env: dict[str, str]) -> dict[str, str]:
    """Built from scratch: never SSH_AUTH_SOCK or anything else of the session."""
    return {key: env[key] for key in SSH_ENV_KEYS if key in env}


def dispatcher_env(env: dict[str, str]) -> dict[str, str]:
    """An allow-list, so starting the connector from an SSH login passes no SSH_CONNECTION on (serve
    mode refuses to run under sshd)."""
    return {key: value for key, value in env.items() if key in DISPATCHER_ENV_KEYS or TEST_ENV_RE.fullmatch(key)}


def map_ssh_failure(stderr_tail: str) -> str:
    for pattern, code in _SSH_ERRORS:
        if pattern.search(stderr_tail):
            return code
    return "other"


def _clean(text: str) -> str:
    return _CONTROL_RE.sub("", text)[:200]


def lock_path(env: dict[str, str]) -> str:
    runtime = env.get("XDG_RUNTIME_DIR") or ""
    if os.path.isabs(runtime) and os.path.isdir(runtime):
        return os.path.join(runtime, LOCK_NAME)
    home = env.get("HOME") or os.path.expanduser("~")
    cache = os.path.join(home, ".cache")
    os.makedirs(cache, mode=0o700, exist_ok=True)
    return os.path.join(cache, LOCK_NAME)


def take_lock(path: str) -> int | None:
    """An exclusive flock on `path`, held while the returned descriptor stays open; None when another
    instance holds it."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


class Outcome:
    def __init__(self, reason: str, seconds: float, detail: str = "") -> None:
        self.reason = reason
        """`closed`, `replaced`, `refused_by_hub`, `host_key`, `auth`, `refused`, `timeout`, `other`,
        `no_ssh`, `no_dispatcher` or `stopped`."""
        self.seconds = seconds
        self.detail = detail


def _signal_group(process: subprocess.Popen, sig: int) -> None:  # type: ignore[type-arg]
    with contextlib.suppress(OSError):
        os.killpg(process.pid, sig)


def stop_process(process: subprocess.Popen) -> None:  # type: ignore[type-arg]
    """SIGTERM to the process group, SIGKILL after KILL_GRACE_SECONDS; reaps it."""
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(KILL_GRACE_SECONDS)
    _signal_group(process, signal.SIGKILL)
    process.wait()


class Connector:
    """The reconnect loop. `wait(seconds)` returns True when it was told to stop; `uniform` and
    `monotonic` are injectable for tests."""

    def __init__(
        self,
        options: Options,
        env: dict[str, str],
        *,
        log: TextIO | None = None,
        uniform: Callable[[float, float], float] = random.uniform,
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self.options = options
        self.env = env
        self.log = log if log is not None else sys.stderr
        self.uniform = uniform
        self.monotonic = monotonic
        self.stopping = threading.Event()
        self.wait = wait or self.stopping.wait
        self.delays: list[float] = []
        """Every wait chosen, for tests and the log."""

    def say(self, message: str) -> None:
        self.log.write(f"switchboard-connect: {message}\n")
        self.log.flush()

    def stop(self) -> None:
        self.stopping.set()

    def attempt(self) -> Outcome:
        """One connection: both processes, cross-wired, until either ends."""
        started = self.monotonic()
        hub_to_dispatcher = os.pipe()
        dispatcher_to_hub = os.pipe()
        try:
            try:
                dispatcher = subprocess.Popen(
                    self.options.command,
                    stdin=hub_to_dispatcher[0],
                    stdout=dispatcher_to_hub[1],
                    env=dispatcher_env(self.env),
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError:
                return Outcome("no_dispatcher", self.monotonic() - started)
            try:
                ssh = subprocess.Popen(
                    ssh_argv(self.options),
                    stdin=dispatcher_to_hub[0],
                    stdout=hub_to_dispatcher[1],
                    stderr=subprocess.PIPE,
                    env=ssh_env(self.env),
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError:
                stop_process(dispatcher)
                return Outcome("no_ssh", self.monotonic() - started)
        finally:
            for fd in (*hub_to_dispatcher, *dispatcher_to_hub):
                os.close(fd)
        tail = bytearray()

        stderr_fd = ssh.stderr.fileno() if ssh.stderr is not None else -1

        def read_stderr() -> None:
            while True:
                try:
                    chunk = os.read(stderr_fd, 4096)
                except OSError:
                    return
                if not chunk:
                    return
                tail.extend(chunk)
                if len(tail) > STDERR_TAIL_BYTES * 2:
                    del tail[: len(tail) - STDERR_TAIL_BYTES]

        reader = threading.Thread(target=read_stderr, daemon=True)
        reader.start()
        while ssh.poll() is None and dispatcher.poll() is None and not self.stopping.is_set():
            time.sleep(POLL_SECONDS)
        if ssh.poll() is not None and not self.stopping.is_set():
            # The relay exits right after forwarding a bye, so ssh can end before the dispatcher has
            # read it: give the dispatcher a moment to exit with its own code (75 or 76).
            with contextlib.suppress(subprocess.TimeoutExpired):
                dispatcher.wait(DISPATCHER_GRACE_SECONDS)
        stop_process(ssh)
        stop_process(dispatcher)
        reader.join(KILL_GRACE_SECONDS)
        if ssh.stderr is not None:
            ssh.stderr.close()
        seconds = self.monotonic() - started
        stderr = bytes(tail[-STDERR_TAIL_BYTES:]).decode("utf-8", "replace")
        first = next((line for line in stderr.splitlines() if line.strip()), "")
        if self.stopping.is_set():
            return Outcome("stopped", seconds)
        if dispatcher.returncode == EXIT_REPLACED:
            return Outcome("replaced", seconds)
        if dispatcher.returncode == EXIT_REFUSED:
            return Outcome("refused_by_hub", seconds, _clean(first))
        if ssh.returncode == 255:
            return Outcome(map_ssh_failure(stderr), seconds, _clean(first))
        return Outcome("closed", seconds, _clean(first))

    def run(self) -> int:
        """Connects again and again until stopped; the exit code (0)."""
        delay = INITIAL_DELAY
        last = ""
        self.say(f"connecting to {self.options.host} as {self.options.user}")
        while not self.stopping.is_set():
            outcome = self.attempt()
            if outcome.reason == "stopped":
                break
            if outcome.seconds >= RESET_AFTER_SECONDS:
                delay = INITIAL_DELAY
            if outcome.reason in LONG_WAIT_REASONS:
                base = float(self.options.max_backoff)
            else:
                base = delay
                delay = min(delay * 2, float(self.options.max_backoff))
            chosen = self.uniform(base / 2, base)
            self.delays.append(chosen)
            if outcome.reason != last:
                detail = f" ({outcome.detail})" if outcome.detail else ""
                self.say(f"connection ended: {outcome.reason}{detail}; retrying in {round(chosen)} s")
                last = outcome.reason
            if self.wait(chosen):
                break
        self.say("stopped")
        return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if os.geteuid() == 0:
        sys.stderr.write("switchboard-connect: refusing to run as root\n")
        return 2
    try:
        options = parse_args(args, os.getuid())
    except UsageError as err:
        sys.stderr.write(f"switchboard-connect: {err}\n")
        return 2
    env = dict(os.environ)
    lock = take_lock(lock_path(env))
    if lock is None:
        sys.stderr.write("switchboard-connect: another switchboard-connect of this user is running\n")
        return 1
    connector = Connector(options, env)

    def on_signal(_sig: int, _frame: object) -> None:
        connector.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        return connector.run()
    finally:
        os.close(lock)


if __name__ == "__main__":
    sys.exit(main())
