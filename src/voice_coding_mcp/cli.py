"""Entry point: serve, pair, unpair, status, revoke-all, remote-check, remote-dispatcher,
remote-connector, agent-relay, audit, usage, set-public-url (argparse; usage errors exit 2).

`serve` runs uvicorn programmatically (`uvicorn.Server(...).serve()` in a task, own SIGTERM/SIGINT
handling: first signal stops gracefully within 20 s, a second one exits at once with code 1). With
`--pid-file` it holds a locked pid file for its lifetime (see `pid_file`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
import socket
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn

import uvicorn

from .auth import OAuthStore, close_pairing, get_pairing, open_pairing
from .auth.text import strip_control_chars
from .clock import SECOND_MS, ago, now_ms
from .config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config, parse_config
from .http import CreateAppOptions, create_app
from .json_file import ensure_dir, loads_json, write_json_atomic
from .log import Logger, as_log_level, create_logger, silent_logger
from .mcp import SERVER_VERSION
from .notify import HttpNotifier, create_notifier
from .remote import relay
from .remote.agents import CONTROL_SOCKET_NAME, control_request, lint_authorized_keys
from .remote.audit import audit_path, format_audit_table, read_audit
from .remote.check import check_machine
from .remote.config import remote_warnings
from .remote.inventory import Inventory
from .remote.transport import DISPATCHER_PATH, make_transport
from .sessions import claude_home_dir
from .tasks.manager import TaskManager, TaskManagerOptions, load_tasks
from .usage.cache import HUB
from .usage.compute import PERIOD_HELP, Grouping, PeriodError, parse_period
from .usage.report import format_csv, format_table
from .usage.service import ProjectProblem, UsageService, failure_reason, spoken_source

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

SHUTDOWN_TIMEOUT_MS = 20_000

_DESCRIPTION = "Remote MCP server that runs Claude Code tasks for the Claude apps."

_EPILOG = f"""--config <path> may come before or after the command (default: $VCM_CONFIG or {DEFAULT_CONFIG_PATH}).

environment:
  VCM_CONFIG         config file path, when --config is not given
  VCM_LOG_LEVEL      debug, info (default), warn or error (serve only)

exit codes: 0 success, 1 error, 2 usage or config error.
"""

MAX_PAIRING_MINUTES = 24 * 60


@dataclass(frozen=True)
class CliIo:
    out: Callable[[str], None]
    err: Callable[[str], None]
    env: Mapping[str, str]


class UsageError(Exception):
    """A command line argparse refused; `usage` is the usage line of the (sub)command, `prog` its name."""

    def __init__(self, message: str, usage: str = "", prog: str = "voice-coding-mcp") -> None:
        super().__init__(message)
        self.usage = usage
        self.prog = prog


class EarlyExitError(Exception):
    """argparse finished early with status 0 (`-h`, `--version`); `text` is what it printed."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser that raises instead of printing and exiting, so `main` decides where the
    text goes and returns the exit code."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("formatter_class", argparse.RawDescriptionHelpFormatter)
        super().__init__(*args, **kwargs)
        self._printed: list[str] = []

    def _print_message(self, message: str, file: object = None) -> None:
        self._printed.append(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        if status != 0:
            raise UsageError(message or "", self.format_usage(), self.prog)
        raise EarlyExitError("".join(self._printed) + (message or ""))

    def error(self, message: str) -> NoReturn:
        raise UsageError(message, self.format_usage(), self.prog)


def _minutes(text: str) -> int:
    """argparse type of `pair --minutes`: a whole number from 1 to MAX_PAIRING_MINUTES."""
    try:
        minutes = int(text)
    except ValueError:
        minutes = 0
    if not 1 <= minutes <= MAX_PAIRING_MINUTES:
        raise argparse.ArgumentTypeError(f"must be a whole number between 1 and {MAX_PAIRING_MINUTES}, got {text}")
    return minutes


def _limit(text: str) -> int:
    """argparse type of `audit --limit`: a whole number from 1 to 100000."""
    try:
        value = int(text)
    except ValueError:
        value = 0
    if not 1 <= value <= 100_000:
        raise argparse.ArgumentTypeError(f"must be a whole number between 1 and 100000, got {text}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="voice-coding-mcp", description=_DESCRIPTION, epilog=_EPILOG)
    parser.add_argument("-v", "--version", action="version", version=SERVER_VERSION)
    config_help = "config file (default: $VCM_CONFIG or the built-in path)"
    parser.add_argument("--config", metavar="PATH", help=config_help)
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        # SUPPRESS: when --config is given only before the command, the subcommand keeps that value.
        sub.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help=config_help)
        return sub

    serve = command("serve", "run the server (started by systemd on a VPS or by deploy/mac/vcm-mac on a Mac)")
    serve.add_argument(
        "--pid-file",
        metavar="PATH",
        help="write the server pid to PATH and hold an exclusive lock on it while running; "
        "refuse to start when another process holds the lock",
    )
    pair = command("pair", "open a pairing window and print a one-time code")
    pair.add_argument(
        "--minutes",
        type=_minutes,
        metavar="N",
        help=f"window length, 1 to {MAX_PAIRING_MINUTES} (default: oauth.pairingMinutes)",
    )
    command("unpair", "close the pairing window")
    command("status", "show the pairing window, connected clients and task counts")
    revoke = command("revoke-all", "revoke every client and token; the Claude connector must pair again")
    revoke.add_argument("-y", "--yes", action="store_true", help="confirm")
    check = command("remote-check", "call the dispatcher of each remote machine (or one) and show what it sees")
    check.add_argument("machine", nargs="?", help="only this machine (its name in remote.machines)")
    command("remote-dispatcher", "print the switchboard-remote dispatcher script, for installing it on a machine")
    command(
        "remote-connector",
        "print the switchboard-connect script, for installing it on a machine that connects out to the hub",
    )
    agent_relay = command(
        "agent-relay",
        "the relay between one machine's SSH connection and the hub (the forced command of the relay account)",
    )
    agent_relay.add_argument("--machine", required=True, help="the agent machine this key belongs to")
    agent_relay.add_argument("--socket", help=f"the hub's relay socket (default: {relay.DEFAULT_SOCKET})")
    audit = command("audit", "print the remote_send audit log (stateDir/audit.jsonl) as a table")
    audit.add_argument("--limit", type=_limit, metavar="N", help="only the last N lines (default: all)")
    usage = command("usage", "active hours, billable amount and token cost per project, session or day")
    usage.add_argument("--period", metavar="P", help=f"{PERIOD_HELP} (default: this month)")
    usage.add_argument(
        "--machine", metavar="M", help=f"only this machine ({HUB} for the server's own Claude Code home)"
    )
    usage.add_argument("--project", metavar="X", help="only this project (folder name)")
    usage.add_argument("--by", choices=("project", "session", "day"), default="project", help="rows (default: project)")
    usage.add_argument("--csv", action="store_true", help="CSV with a header row instead of a table")
    usage.add_argument("--cached", action="store_true", help="use the cached records, do not read the machines")
    public_url = command(
        "set-public-url", "set publicUrl in the config file (deploy/linux/sb-linux on calls it); restart to apply"
    )
    public_url.add_argument("url", help="the new publicUrl, an https origin such as https://example-host.ts.net")
    return parser


@dataclass(frozen=True)
class ParsedArgs:
    command: str
    config: str | None = None
    minutes: int | None = None
    yes: bool = False
    pid_file: str | None = None
    machine: str | None = None
    limit: int | None = None
    period: str | None = None
    project: str | None = None
    by: str = "project"
    csv: bool = False
    cached: bool = False
    url: str | None = None
    socket: str | None = None


def parse_args(argv: Sequence[str]) -> ParsedArgs:
    """Parses the command line. Raises UsageError, or EarlyExitError for `-h` and `--version`."""
    ns = build_parser().parse_args(list(argv))
    return ParsedArgs(
        command=ns.command,
        config=ns.config,
        minutes=getattr(ns, "minutes", None),
        yes=getattr(ns, "yes", False),
        pid_file=getattr(ns, "pid_file", None),
        machine=getattr(ns, "machine", None),
        limit=getattr(ns, "limit", None),
        period=getattr(ns, "period", None),
        project=getattr(ns, "project", None),
        by=getattr(ns, "by", "project"),
        csv=getattr(ns, "csv", False),
        cached=getattr(ns, "cached", False),
        url=getattr(ns, "url", None),
        socket=getattr(ns, "socket", None),
    )


def format_time(ms: float) -> str:
    """Local time in a fixed en-US style (medium date, short time), for example `Nov 22, 2026, 3:05 PM`.
    The month and AM/PM names are those of the C locale, which Python uses unless `setlocale` is called."""
    dt = datetime.fromtimestamp(ms / SECOND_MS)
    return f"{dt:%b} {dt.day}, {dt:%Y}, {dt.hour % 12 or 12}:{dt:%M %p}"


def format_ago(ms: float | None, now: float) -> str:
    """`clock.ago` of a timestamp, or "never"."""
    return "never" if ms is None else ago(now - ms)


def _new_store(config: Config) -> OAuthStore:
    return OAuthStore(
        config.state_dir,
        access_token_hours=config.oauth.access_token_hours,
        refresh_token_days=config.oauth.refresh_token_days,
    )


def _task_counts(state_dir: str) -> dict[str, int]:
    """Counts task files by status without a TaskManager (which would fail running tasks)."""
    counts: dict[str, int] = {}

    def unreadable(_task_id: int, _error: Exception) -> None:
        counts["unreadable"] = counts.get("unreadable", 0) + 1

    for task in load_tasks(state_dir, on_error=unreadable):
        counts[task.status] = counts.get(task.status, 0) + 1
    return counts


async def _cmd_pair(config: Config, args: ParsedArgs, io: CliIo) -> int:
    minutes = args.minutes if args.minutes is not None else config.oauth.pairing_minutes
    opened = await open_pairing(config.state_dir, minutes)
    io.out(
        "\n".join(
            [
                f"Pairing code: {opened.code}",
                f"Valid until {format_time(opened.expires_at)} ({minutes:g} min), 5 attempts.",
                "",
                "In the Claude app: Settings > Connectors > Add custom connector, with this URL:",
                f"  {config.resource_url}",
                "Then click Connect and enter the code on the page that opens.",
                "",
            ]
        )
    )
    return EXIT_OK


async def _cmd_unpair(config: Config, io: CliIo) -> int:
    window = await get_pairing(config.state_dir)
    await close_pairing(config.state_dir)
    io.out("Pairing window closed.\n" if window else "No pairing window was open.\n")
    return EXIT_OK


_STATUS_ORDER = ("running", "queued", "done", "failed", "timeout", "cancelled", "unreadable")


async def _cmd_status(config: Config, io: CliIo) -> int:
    now = now_ms()
    lines = [f"Connector URL: {config.resource_url}"]
    pairing = await get_pairing(config.state_dir, now)
    lines.append(
        f"Pairing window: open until {format_time(pairing.expires_at)}, {pairing.attempts_left} attempt(s) left"
        if pairing
        else "Pairing window: closed"
    )
    status = await _new_store(config).status()
    lines.append(f"Clients: {len(status.clients)}, live authorizations: {status.live_grants}")
    for client in status.clients:
        # Names and hosts come from clients: strip control characters before they reach the terminal.
        hosts = [strip_control_chars(host) for host in client.redirect_hosts]
        redirect = f", redirect {' '.join(hosts)}" if hosts else ""
        lines.append(
            f"  - {strip_control_chars(client.client_name)} ({client.kind.upper()}{redirect}): {client.live_grants} live, "
            f"created {format_time(client.created_at)}, last used {format_ago(client.last_used_at, now)}"
        )
    counts = _task_counts(config.state_dir)
    total = sum(counts.values())
    parts = [f"{counts[s]} {s}" for s in _STATUS_ORDER if counts.get(s)]
    lines.append(f"Tasks: {total}{f' ({", ".join(parts)})' if parts else ''}")
    lines += await _agent_status_lines(config, now)
    io.out("\n".join(lines) + "\n")
    return EXIT_OK


def _control_path(config: Config) -> str:
    return os.path.join(config.state_dir, CONTROL_SOCKET_NAME)


def _clean(value: object) -> str:
    return strip_control_chars(str(value))[:200]


async def _agent_status_lines(config: Config, now: int) -> list[str]:
    """`Agent machines:` and a line per machine, from the running hub's control socket."""
    names = [m.name for m in config.remote.machines if m.transport == "agent"]
    if not names:
        return []
    answer = await control_request(_control_path(config), {"t": "status"})
    if answer is None or not isinstance(answer.get("machines"), list):
        return ["Agent machines: the hub is not running, so none is connected"]
    lines = ["Agent machines:"]
    if answer.get("problem"):
        lines.append(f"  warning: the relay socket is not served: {_clean(answer['problem'])}")
    for entry in answer["machines"]:
        if not isinstance(entry, dict):
            continue
        name = _clean(entry.get("name"))
        since = entry.get("sinceMs")
        if entry.get("connected") and isinstance(since, int):
            peer = f", from {_clean(entry['peer'])}" if entry.get("peer") else ""
            line = (
                f"  - {name}: connected since {format_time(since)} ({format_ago(since, now)}), "
                f"dispatcher {_clean(entry.get('version'))}, send {'allowed' if entry.get('allowSend') else 'off'}, "
                f"usage {'allowed' if entry.get('allowUsage') else 'off'}, "
                f"attachments {'allowed' if entry.get('allowAttachments') else 'off'}{peer}"
            )
        else:
            last = entry.get("lastDisconnectMs")
            when = (
                f", last connection ended {format_ago(last, now)} ({_clean(entry.get('lastDisconnectReason'))})"
                if isinstance(last, int)
                else ""
            )
            line = f"  - {name}: not connected{when}"
        replaced = entry.get("replacementsLastHour")
        if isinstance(replaced, int) and replaced:
            line += f", replaced {replaced} time(s) in the last hour"
        lines.append(line)
        warnings = entry.get("warnings")
        if isinstance(warnings, list):
            lines += [f"    warning: {_clean(w)}" for w in warnings]
    return lines


async def _cmd_revoke_all(config: Config, args: ParsedArgs, io: CliIo) -> int:
    if not args.yes:
        io.err("revoke-all removes every client and token; the Claude connector must pair again. Run it with --yes.\n")
        return EXIT_USAGE
    result = await _new_store(config).revoke_all()
    io.out(f"Revoked {result.clients} client(s) and {result.grants} authorization(s).\n")
    return EXIT_OK


class _LogBridge(logging.Handler):
    """Forwards warnings and errors of the SDK and uvicorn (stdlib logging) to our JSON logger.
    Only the message and the exception type and text, never request data."""

    def __init__(self, logger: Logger, msg: str) -> None:
        super().__init__(logging.WARNING)
        self._logger = logger
        self._msg = msg

    def emit(self, record: logging.LogRecord) -> None:
        try:
            error = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                exc = record.exc_info[1]
                error = f"{error}: {type(exc).__name__}: {exc}"
            log = self._logger.error if record.levelno >= logging.ERROR else self._logger.warn
            log(self._msg, {"logger": record.name, "error": error})
        except Exception:
            self.handleError(record)


def _bridge_library_logs(logger: Logger) -> None:
    for name, msg in (("mcp", "mcp error"), ("uvicorn", "http server error")):
        lib = logging.getLogger(name)
        for handler in [h for h in lib.handlers if isinstance(h, _LogBridge)]:
            lib.removeHandler(handler)
        lib.addHandler(_LogBridge(logger, msg))
        lib.setLevel(logging.WARNING)
        lib.propagate = False


class _Server(uvicorn.Server):
    """uvicorn without its own signal handling: `serve` (or the test) decides when to stop."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


def _bind(host: str, port: int) -> socket.socket:
    """A listening socket on the first resolved address of `host` (SO_REUSEADDR)."""
    family, kind, proto, _, address = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[
        0
    ]
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.listen(511)
        sock.setblocking(False)
    except BaseException:
        sock.close()
        raise
    return sock


class RunningServer:
    """A started server: `url` (http://host:port actually bound) and `stop()`."""

    url: str

    def __init__(
        self,
        *,
        url: str,
        server: uvicorn.Server,
        task: asyncio.Task[None],
        manager: TaskManager,
        notifier: HttpNotifier | None,
        logger: Logger,
    ) -> None:
        self.url = url
        self._server = server
        self._task = task
        self._manager = manager
        self._notifier = notifier
        self._logger = logger
        self._stopping: asyncio.Task[None] | None = None

    async def stop(self, reason: str = "server stopped") -> None:
        """Stops accepting, shuts the task manager down and closes connections (at most 20 s). Idempotent."""
        if self._stopping is None:
            self._stopping = asyncio.ensure_future(self._stop(reason))
        await asyncio.shield(self._stopping)

    async def _stop(self, reason: str) -> None:
        self._logger.info("server stopping", {"reason": reason})
        # uvicorn stops accepting, closes idle connections and lets running requests finish; the
        # manager fails queued tasks and stops running ones, which ends waiting tool calls.
        self._server.should_exit = True

        async def graceful() -> None:
            await _shutdown(self._manager, self._notifier, reason)
            await asyncio.shield(self._task)

        try:
            await asyncio.wait_for(graceful(), SHUTDOWN_TIMEOUT_MS / 1000)
        except TimeoutError:
            self._logger.warn("shutdown timed out, closing connections")
            self._server.force_exit = True
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 2)
            except TimeoutError:
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
        self._logger.info("server stopped")


async def _shutdown(manager: TaskManager, notifier: HttpNotifier | None, reason: str) -> None:
    """Shuts the task manager down, then closes the notification client."""
    await manager.shutdown(reason)
    if notifier is not None:
        await notifier.aclose()


async def start_server(config: Config, logger: Logger, env: Mapping[str, str] | None = None) -> RunningServer:
    """Starts the server (used by `serve` and by tests). Port 0 binds a free port."""
    environ: Mapping[str, str] = env if env is not None else os.environ
    ensure_dir(config.state_dir)
    has_credentials = (
        environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        or config.claude.env.get("CLAUDE_CODE_OAUTH_TOKEN")
        or config.claude.env.get("ANTHROPIC_API_KEY")
        or config.claude.config_dir
    )
    if not has_credentials:
        logger.info(
            "CLAUDE_CODE_OAUTH_TOKEN is not set; Claude Code will use the credentials of the logged-in user"
            " (expected on a Mac; on a server run `claude setup-token`)"
        )
    _bridge_library_logs(logger)
    store = _new_store(config)
    notifier = create_notifier(config.notify, logger)
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            sessions=config.sessions,
            logger=logger,
            notifier=notifier,
            base_env=environ,
            log_prompts=config.log_prompts,
            public_url=config.public_url,
        )
    )
    try:
        sock = _bind(config.listen.host, config.listen.port)
    except BaseException:
        await _shutdown(manager, notifier, "server failed to start")
        raise
    app = create_app(
        CreateAppOptions(
            config=config,
            logger=logger,
            store=store,
            manager=manager,
            send_text=notifier.send_text if notifier is not None else None,
        )
    )
    server = _Server(
        uvicorn.Config(
            app,
            proxy_headers=False,
            access_log=False,
            log_config=None,
            server_header=False,
            lifespan="on",
            ws="none",
            timeout_graceful_shutdown=SHUTDOWN_TIMEOUT_MS // 1000,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    # uvicorn has no startup event to wait on.
    while not server.started and not task.done():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    if task.done():
        sock.close()
        await _shutdown(manager, notifier, "server failed to start")
        error = task.exception() if not task.cancelled() else None
        raise RuntimeError(f"server failed to start{f': {error}' if error else ''}")
    address = sock.getsockname()
    host = f"[{address[0]}]" if sock.family == socket.AF_INET6 else address[0]
    url = f"http://{host}:{address[1]}"
    logger.info("server started", {"version": SERVER_VERSION, "listen": url, "publicUrl": config.public_url})
    return RunningServer(url=url, server=server, task=task, manager=manager, notifier=notifier, logger=logger)


PID_FILE_RETRY_SECONDS = 1.0
"""How long `serve --pid-file` retries a held lock before it reports another server."""


class PidFileLockedError(Exception):
    """Another process holds the lock on the pid file (a server is running)."""


@contextlib.contextmanager
def pid_file(path: str) -> Iterator[None]:
    """Writes this process's pid to `path` (mode 0600, parent directory created 0700) and holds an
    exclusive `flock` on it until the block ends, then removes the file. A pid file is stale exactly
    when its lock is free: a crash leaves the file, but the kernel drops the lock. Raises
    PidFileLockedError when another process holds the lock."""
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    # A liveness probe (`vcm-mac status` runs `lockf -t 0` on the file) holds the lock for a moment,
    # so a held lock is retried briefly before it counts as another server.
    deadline = time.monotonic() + PID_FILE_RETRY_SECONDS
    while True:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = os.read(fd, 32).decode("ascii", "replace").strip()
            os.close(fd)
            if time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            running = f" (pid {holder})" if holder.isdigit() else ""
            raise PidFileLockedError(f"another server holds the pid file {path}{running}") from None
        except BaseException:
            os.close(fd)
            raise
        # The previous holder may have removed the file between our open and flock: then we hold a
        # lock on an unlinked file and must start over with the path as it is now.
        try:
            same = os.path.samestat(os.fstat(fd), os.stat(path))
        except FileNotFoundError:
            same = False
        if same:
            break
        os.close(fd)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode("ascii"))
        yield
    finally:
        # Removed while still locked, so nobody locks the old file after we let go.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        os.close(fd)


async def _cmd_serve(config: Config, args: ParsedArgs, io: CliIo) -> int:
    with contextlib.ExitStack() as stack:
        if args.pid_file is not None:
            try:
                stack.enter_context(pid_file(args.pid_file))
            except PidFileLockedError as err:
                io.err(f"Error: {err}\n")
                return EXIT_ERROR
        return await _serve(config, io)


async def _serve(config: Config, io: CliIo) -> int:
    logger = create_logger(as_log_level(io.env.get("VCM_LOG_LEVEL")))
    try:
        running = await start_server(config, logger, io.env)
    except Exception as err:
        logger.error("server failed to start", {"error": err})
        return EXIT_ERROR
    loop = asyncio.get_running_loop()
    done: asyncio.Future[int] = loop.create_future()
    signals = 0

    def on_signal(sig: signal.Signals) -> None:
        nonlocal signals
        signals += 1
        if signals > 1:
            logger.warn("second signal, exiting now", {"signal": sig.name})
            sys.stdout.flush()
            os._exit(EXIT_ERROR)

        async def stop() -> None:
            await running.stop(f"server stopped ({sig.name})")
            if not done.done():
                done.set_result(EXIT_OK)

        loop.create_task(stop())  # noqa: RUF006 (awaited through `done`)

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, on_signal, sig)
    try:
        return await done
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


def _settings_hooks(config: Config) -> bool:
    """True when the server user's Claude Code settings define hooks (the summariser loads them)."""
    path = os.path.join(claude_home_dir(config.claude.config_dir), "settings.json")
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(1024 * 1024).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and bool(data.get("hooks"))


async def _cmd_remote_check(config: Config, args: ParsedArgs, io: CliIo) -> int:
    machines = list(config.remote.machines)
    if args.machine is not None:
        machines = [m for m in machines if m.name == args.machine]
        if not machines:
            names = ", ".join(m.name for m in config.remote.machines) or "none"
            io.err(f"No machine called {args.machine}. Configured: {names}.\n")
            return EXIT_USAGE
    if not machines:
        io.out("No remote machines are configured (remote.machines).\n")
        return EXIT_OK
    transports = {m.name: make_transport(m, config.remote, config.state_dir, io.env) for m in machines}
    inventory = Inventory(config.remote, transports, silent_logger)
    warnings = remote_warnings(config.remote)
    failed = False
    lines: list[str] = []
    for machine in machines:
        if machine.transport == "agent":
            lines.append(f"{machine.name} (agent: connects out to the hub)")
            answer = await control_request(_control_path(config), {"t": "check", "machine": machine.name})
            if answer is None:
                failed = True
                lines.append("  not checked: the hub is not running (only the hub holds agent connections)")
                continue
            got = answer.get("lines")
            lines += [_clean(line) for line in got if isinstance(line, str)] if isinstance(got, list) else []
            failed = failed or answer.get("ok") is not True
            continue
        where = (
            "local dispatcher"
            if machine.transport == "local"
            else f"ssh {machine.user}@{machine.host}:{machine.port}"
            + ("" if machine.dispatcher == "forced" else f", dispatcher {machine.dispatcher}")
        )
        lines.append(f"{machine.name} ({where})")
        machine_lines, ok = await check_machine(inventory, machine.name, warnings)
        lines += machine_lines
        failed = failed or not ok
    if any(m.transport == "agent" for m in machines):
        lines += _authorized_keys_lines(config)
    if _settings_hooks(config):
        lines.append(
            "warning: the server user's Claude Code settings.json defines hooks; the remote summariser loads user settings, so they run for every summary"
        )
    io.out("\n".join(lines) + "\n")
    return EXIT_ERROR if failed else EXIT_OK


def _authorized_keys_lines(config: Config) -> list[str]:
    """The lint of the relay account's authorized_keys, when this user can read it."""
    path = config.remote.agents.authorized_keys_file
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read(1024 * 1024)
    except OSError:
        return [f"relay keys ({path}): not readable here, not checked"]
    agents = [m.name for m in config.remote.machines if m.transport == "agent"]
    problems = lint_authorized_keys(text, agents)
    if not problems:
        return [f"relay keys ({path}): ok"]
    return [f"relay keys ({path}):"] + [f"  problem: {_clean(p)}" for p in problems]


def _cmd_audit(config: Config, args: ParsedArgs, io: CliIo) -> int:
    rows, bad = read_audit(config.state_dir, args.limit)
    if not rows and not bad:
        io.out("No sends were audited yet.\n")
        return EXIT_OK
    io.out(format_audit_table(rows, with_text=any("text" in row for row in rows)))
    if bad:
        io.err(f"warning: {bad} unreadable line(s) in {audit_path(config.state_dir)}\n")
    return EXIT_OK


async def _cmd_usage(config: Config, args: ParsedArgs, io: CliIo) -> int:
    transports = {m.name: make_transport(m, config.remote, config.state_dir, io.env) for m in config.remote.machines}
    service = UsageService(config, transports, silent_logger, env=io.env)
    try:
        period = parse_period(args.period, now_ms())
    except PeriodError:
        io.err(f"Unknown period {args.period}. Use {PERIOD_HELP}.\n")
        return EXIT_USAGE
    names = service.sources(args.machine)
    if args.machine is not None and not names:
        usable = ", ".join(service.sources()) or "none"
        io.err(f"No machine called {args.machine} with usage reading on. Usable: {usable}.\n")
        return EXIT_USAGE
    if not names:
        io.err("No machine has usage reading on.\n")
        return EXIT_ERROR
    outcomes = None
    # An agent machine's connection is held by the hub's process, so only the hub reads it; this
    # process reports its cached records (refreshed by the hub's own usage reads).
    agent_names = {m.name for m in config.remote.machines if m.transport == "agent"}
    hub_only = [n for n in names if n in agent_names]
    readable = [n for n in names if n not in agent_names]
    if not args.cached and readable:
        io.err(f"Reading usage records from {', '.join(spoken_source(n) for n in readable)} ...\n")
        outcomes = await service.refresh(readable)
    project = None
    if args.project is not None:
        found = service.match_project(names, args.project)
        if isinstance(found, ProjectProblem):
            known = ", ".join(found.candidates) or "none"
            io.err(
                f"{'Several projects match' if found.ambiguous else 'No project matches'} {args.project}: {known}.\n"
            )
            return EXIT_USAGE
        project = found
    by: Grouping = "session" if args.by == "session" else "day" if args.by == "day" else "project"
    report, context = service.report(names, period, project=project, by=by)
    notes = []
    if context.skipped:
        notes.append(f"{context.skipped} transcript(s) larger than usage.maxTranscriptMegabytes were skipped")
    failed = [o for o in (outcomes or {}).values() if not o.ok]
    notes += [
        f"{spoken_source(o.name)} could not be read: {failure_reason(o)}; its cached records are used" for o in failed
    ]
    if not args.cached:
        notes += [
            f"{spoken_source(n)} connects to the hub, so only the hub reads it; its cached records are used"
            for n in hub_only
        ]
    if args.csv:
        io.out(format_csv(report))
        for note in notes:
            io.err(f"warning: {note}\n")
    else:
        io.out(format_table(report, notes))
    return EXIT_ERROR if failed else EXIT_OK


def _cmd_set_public_url(config_path: str, config: Config, url: str, io: CliIo) -> int:
    """Rewrites `publicUrl` in the config file (atomically, mode 0600, other keys and their order kept)
    after checking that the file with the new value is still a valid config. Prints `changed: ...` or
    `unchanged: ...` as its first word, which `deploy/linux/sb-linux` reads."""
    want = url.rstrip("/")
    if want == config.public_url:
        io.out(f"unchanged: publicUrl is already {want}\n")
        return EXIT_OK
    path = os.path.realpath(config_path)
    with open(path, encoding="utf-8") as handle:
        data = loads_json(handle.read())
    if not isinstance(data, dict):
        raise ConfigError(f"{config_path}: the config is not a JSON object")
    data = {**data, "publicUrl": want}
    try:
        parse_config(data)
    except ConfigError as err:
        io.err(f"{err}\n")
        return EXIT_USAGE
    write_json_atomic(path, data)
    io.out(f"changed: publicUrl is now {want} (restart the server to apply it)\n")
    return EXIT_OK


def _cmd_remote_dispatcher(io: CliIo) -> int:
    with open(DISPATCHER_PATH, encoding="utf-8") as handle:
        io.out(handle.read())
    return EXIT_OK


CONNECTOR_PATH = os.path.join(os.path.dirname(DISPATCHER_PATH), "connector.py")


def _cmd_remote_connector(io: CliIo) -> int:
    with open(CONNECTOR_PATH, encoding="utf-8") as handle:
        io.out(handle.read())
    return EXIT_OK


async def main(argv: Sequence[str], io: CliIo) -> int:
    """Runs one CLI invocation and returns the exit code."""
    try:
        args = parse_args(argv)
    except EarlyExitError as done:
        io.out(done.text)
        return EXIT_OK
    except UsageError as err:
        io.err(f"{err.usage}{err.prog}: error: {err}\n")
        return EXIT_USAGE

    if args.command in ("remote-dispatcher", "remote-connector"):
        # Needs no config: it prints the bundled script.
        try:
            return _cmd_remote_dispatcher(io) if args.command == "remote-dispatcher" else _cmd_remote_connector(io)
        except OSError as err:
            io.err(f"Error: {err}\n")
            return EXIT_ERROR
    if args.command == "agent-relay":
        # Needs no config either; the forced command uses `python -I -m voice_coding_mcp.remote.relay`.
        relay_argv = ["--machine", args.machine or ""] + (["--socket", args.socket] if args.socket else [])
        return relay.run(relay_argv, dict(io.env))

    config_path = args.config or io.env.get("VCM_CONFIG") or DEFAULT_CONFIG_PATH
    try:
        config = load_config(config_path)
    except ConfigError as err:
        io.err(f"{err}\n")
        return EXIT_USAGE
    except Exception as err:
        io.err(f"{type(err).__name__}: {err}\n")
        return EXIT_USAGE

    try:
        match args.command:
            case "serve":
                return await _cmd_serve(config, args, io)
            case "pair":
                return await _cmd_pair(config, args, io)
            case "unpair":
                return await _cmd_unpair(config, io)
            case "status":
                return await _cmd_status(config, io)
            case "revoke-all":
                return await _cmd_revoke_all(config, args, io)
            case "remote-check":
                return await _cmd_remote_check(config, args, io)
            case "audit":
                return _cmd_audit(config, args, io)
            case "usage":
                return await _cmd_usage(config, args, io)
            case "set-public-url":
                return _cmd_set_public_url(config_path, config, args.url or "", io)
    except Exception as err:
        io.err(f"Error: {err}\n")
        return EXIT_ERROR
    return EXIT_USAGE


def run() -> None:
    """Console script entry point (`voice-coding-mcp`)."""

    def out(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def err(text: str) -> None:
        sys.stderr.write(text)
        sys.stderr.flush()

    io = CliIo(out=out, err=err, env=dict(os.environ))
    try:
        code = asyncio.run(main(sys.argv[1:], io))
    except Exception as error:
        err(f"Error: {error}\n")
        code = EXIT_ERROR
    sys.exit(code)
