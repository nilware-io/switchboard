"""Spawn one `claude -p` run, parse stream-json, timeout and cancel.

- `loop.subprocess_exec(bin, *args, cwd=..., env=..., stdin/stdout/stderr=PIPE,
  start_new_session=True)`: a new session and process group, no shell.
- The prompt is written to stdin, then stdin is closed (ignore BrokenPipeError).
- Stop (timeout or abort): SIGTERM to the process group (`os.killpg`), SIGKILL after `kill_grace_ms`.
  After a normal exit, whatever is left in the group also gets SIGTERM, later SIGKILL (the SIGKILL
  timer must not keep anything waiting).
- Settle when the claude process exits; stdout then gets at most 2 s to drain (a background child
  may hold the pipe open).
- stdout lines longer than MAX_LINE_BYTES are dropped (only counted, never logged).
- Never raises; every failure is described in the RunResult.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import os
import re
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from ..config import ClaudeConfig, PermissionMode
from ..sessions import clip_words
from ..text import clip
from ..urls import parse_url
from .access import ISOLATED_ENV, RunAccess, isolated_access
from .prompt import SPOKEN_ANSWER_PROMPT

ENV_ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "PATH",
    "LANG",
    "LC_ALL",
    "USER",
    "SHELL",
    "TMPDIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
)
"""Copied from the server's environment when set."""

FIXED_ENV: Mapping[str, str] = {"DISABLE_AUTOUPDATER": "1"}
"""Always set for the child (isolated runs also get ISOLATED_ENV)."""

STDERR_TAIL_BYTES = 4096
PROGRESS_KEEP = 3
DEFAULT_KILL_GRACE_MS = 10_000
STDOUT_DRAIN_MS = 2000
MAX_LINE_BYTES = 8 * 1024 * 1024
"""Longest stdout line that is parsed; longer ones are dropped."""
RESULT_MAX_CHARS = 200_000
"""The result text is cut to this length."""
TOOL_USES_KEEP = 50
"""How many tool_use blocks (name and input) are kept in `RunResult.tool_uses`."""
LARGE_USER_LINE_BYTES = 64 * 1024
"""A `user` event line longer than this (usually a big tool_result) is not parsed as a whole: only
its tool_use ids and is_error flags are looked for."""

_TYPE_USER_RE = re.compile(rb'^\s*\{\s*"type"\s*:\s*"user"')
_TOOL_RESULT_RE = re.compile(rb'"tool_use_id"\s*:\s*"([^"\\]{1,200})"')
_IS_ERROR_RE = re.compile(rb'"is_error"\s*:\s*(true|false)')

RunOutcome = Literal["success", "error", "timeout", "aborted"]


@dataclass(frozen=True)
class ToolUse:
    """One tool_use block from the stream-json output."""

    name: str
    input: Mapping[str, Any]
    id: str | None = field(default=None, compare=False)
    """The tool_use id, used to match its tool_result."""
    is_error: bool | None = None
    """Its tool_result: True with is_error true (for example a denied permission), False without,
    None while no tool_result came back."""


@dataclass
class RunOptions:
    claude: ClaudeConfig
    cwd: str
    """Project directory (realpath inside an allowed root)."""
    prompt: str
    """Written to stdin, never to argv."""
    mcp_config_path: str
    """Strict MCP config of an isolated run; ignored when `access` is set."""
    timeout_ms: int
    base_env: Mapping[str, str]
    """Environment the allowlisted variables are copied from (normally `os.environ`)."""
    resume_session_id: str | None = None
    on_progress: Callable[[list[str]], None] | None = None
    """Called with the last progress summaries whenever a tool_use block arrives; exceptions are ignored."""
    abort: asyncio.Event | None = None
    """Setting it stops the run; already set before start: outcome aborted, nothing spawned."""
    kill_grace_ms: int | None = None
    """Delay between SIGTERM and SIGKILL, default 10 s."""
    system_prompt: str | None = None
    """Defaults to SPOKEN_ANSWER_PROMPT."""
    fork_session: bool = False
    """With `resume_session_id`: continue in a new session id, leaving the original untouched."""
    permission_mode: PermissionMode | None = None
    """Overrides `claude.permission_mode` for this run."""
    tools: Sequence[str] | None = None
    """Restricts the built-in tools (`--tools`); an empty list means no tools."""
    session_name: str | None = None
    """Display name for the run (`--name`), seen by other sessions as the sender name."""
    max_turns: int | None = None
    """Overrides `claude.max_turns` for this run."""
    access: RunAccess | None = None
    """MCP config, setting sources and `--settings`; default isolated with `mcp_config_path`."""
    replace_system_prompt: bool = False
    """`--system-prompt` (replaces Claude Code's own) instead of `--append-system-prompt`."""
    task_settings: bool = True
    """False leaves out `claude.model`, `claude.effort` and `claude.extraArgs` (the owner's task
    settings), for runs that are not tasks (the remote summariser)."""
    model: str | None = None
    """`--model` for this run, overriding `claude.model`."""
    no_session_persistence: bool = False
    """`--no-session-persistence`: nothing of the run is written to a transcript."""


@dataclass
class RunResult:
    ok: bool = False
    """True only for a result event with is_error false and exit code 0."""
    outcome: RunOutcome = "error"
    exit_code: int | None = None
    exit_signal: str | None = None
    """Signal name, e.g. "SIGTERM"."""
    session_id: str | None = None
    result: str | None = None
    """Text of the result event, when there was one."""
    is_error: bool | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    permission_denials: list[Any] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    """The result event's `usage` object (token counts), when it had one."""
    progress: list[str] = field(default_factory=list)
    tool_uses: list[ToolUse] = field(default_factory=list)
    """The first TOOL_USES_KEEP tool calls of the run, with their input (used to verify a relay)."""
    dropped_lines: int = 0
    """stdout lines dropped for being longer than MAX_LINE_BYTES."""
    stderr_tail: str = ""
    """Last 4 KB of stderr."""
    resume_missing: bool = False
    """The run used --resume and failed because the session does not exist (and did no work)."""
    error: str | None = None
    """Spawn error or a short failure description."""


def build_args(options: RunOptions) -> list[str]:
    """argv for one run, without the executable."""
    claude = options.claude
    permission_mode = options.permission_mode
    system_prompt = options.system_prompt
    args = [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        permission_mode if permission_mode is not None else claude.permission_mode,
        "--permission-prompts",
        "none",
        *run_access(options).args(),
        "--system-prompt" if options.replace_system_prompt else "--append-system-prompt",
        system_prompt if system_prompt is not None else SPOKEN_ANSWER_PROMPT,
    ]
    model = options.model or (claude.model if options.task_settings else None)
    if model:
        args += ["--model", model]
    if claude.effort and options.task_settings:
        args += ["--effort", claude.effort]
    turns = options.max_turns if options.max_turns is not None else claude.max_turns
    if turns is not None:
        args += ["--max-turns", str(turns)]
    if options.tools is not None:
        args += ["--tools", ",".join(options.tools)]
    if options.session_name:
        args += ["--name", options.session_name]
    if options.resume_session_id:
        args += ["--resume", options.resume_session_id]
        if options.fork_session:
            args.append("--fork-session")
    if options.no_session_persistence:
        args.append("--no-session-persistence")
    if options.task_settings:
        args += claude.extra_args
    return args


def run_access(options: RunOptions) -> RunAccess:
    return options.access if options.access is not None else isolated_access(options.claude, options.mcp_config_path)


def build_env(claude: ClaudeConfig, base_env: Mapping[str, str], *, isolated: bool = True) -> dict[str, str]:
    """Environment built from scratch: allowlist, CLAUDE_CONFIG_DIR, ISOLATED_ENV (isolated runs
    only) and FIXED_ENV, then `claude.env`."""
    env: dict[str, str] = {}
    for key in ENV_ALLOWLIST:
        value = base_env.get(key)
        if value is not None:
            env[key] = value
    if claude.config_dir:
        env["CLAUDE_CONFIG_DIR"] = claude.config_dir
    if isolated:
        env.update(ISOLATED_ENV)
    env.update(FIXED_ENV)
    env.update(claude.env)
    return env


def _basename(path: str) -> str:
    """`os.path.basename`, but trailing slashes are ignored."""
    return os.path.basename(path.rstrip("/"))


def summarize_tool_use(name: str, tool_input: object) -> str:
    """ "Edit config.py", "Bash npm test", "Grep TODO": file basename, else command/pattern (40
    characters, one line, "…"), else URL hostname, else query or description."""
    data: Mapping[str, object] = tool_input if isinstance(tool_input, dict) else {}

    def string(key: str) -> str | None:
        value = data.get(key)
        return value if isinstance(value, str) and value else None

    target: str | None = None
    file = string("file_path") or string("notebook_path") or string("path")
    command, pattern, url, query, description = (
        string("command"),
        string("pattern"),
        string("url"),
        string("query"),
        string("description"),
    )
    if file:
        target = _basename(file)
    elif command:
        target = clip_words(command, 40)
    elif pattern:
        target = clip_words(pattern, 40)
    elif url:
        parsed = parse_url(url)
        target = parsed.hostname if parsed is not None else clip_words(url, 40)
    elif query:
        target = clip_words(query, 40)
    elif description:
        target = clip_words(description, 40)
    return f"{name} {target}" if target else name


_RESUME_MISSING_RE = re.compile(r"No conversation found with session ID")
"""Exact stderr text of `claude --resume <id>` when the session does not exist."""


def _last_stderr_line(stderr: str) -> str | None:
    lines = [line.strip() for line in stderr.split("\n")]
    lines = [line for line in lines if line]
    return clip_words(lines[-1], 200) if lines else None


def _first_error(errors: object) -> str | None:
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if isinstance(first, str):
        return first.strip() or None
    if isinstance(first, dict) and isinstance(first.get("message"), str):
        message: str = first["message"]
        return message.strip() or None
    return None


def _tail_text(data: bytes) -> str:
    """The last STDERR_TAIL_BYTES of stderr as text, without a cut multibyte character at the start."""
    tail = data[-STDERR_TAIL_BYTES:]
    if len(data) > STDERR_TAIL_BYTES:
        start = 0
        while start < len(tail) and start < 3 and 0x80 <= tail[start] < 0xC0:
            start += 1
        tail = tail[start:]
    return tail.decode("utf-8", "replace")


class _RunProtocol(asyncio.SubprocessProtocol):
    """Receives the child's pipes and exit; the run logic lives in `_Run`."""

    def __init__(self, run: _Run) -> None:
        self._run = run

    def pipe_data_received(self, fd: int, data: bytes) -> None:
        if fd == 1:
            self._run.on_stdout(data)
        elif fd == 2:
            self._run.on_stderr(data)

    def pipe_connection_lost(self, fd: int, exc: Exception | None) -> None:
        if fd == 1:
            self._run.on_stdout_done()

    def process_exited(self) -> None:
        self._run.on_exit()


class _Run:
    def __init__(self, options: RunOptions, loop: asyncio.AbstractEventLoop) -> None:
        self.options = options
        self.loop = loop
        self.state = RunResult()
        self.grace_s = (options.kill_grace_ms if options.kill_grace_ms is not None else DEFAULT_KILL_GRACE_MS) / 1000
        self.done: asyncio.Future[RunResult] = loop.create_future()
        self.transport: asyncio.SubprocessTransport | None = None
        self.pid: int | None = None
        self.stopping: Literal["timeout", "aborted"] | None = None
        self.settled = False
        self.saw_result = False
        self.saw_tool_use = False
        self.result_subtype: str | None = None
        self.result_errors: object = None
        self.child_exited = False
        self.stdout_done = False
        self.buffer = bytearray()
        """The unfinished last stdout line."""
        self.dropping = False
        """The current stdout line is too long and is skipped up to its newline."""
        self.stderr = b""
        self.timeout_handle: asyncio.TimerHandle | None = None
        self.kill_handle: asyncio.TimerHandle | None = None
        self.drain_handle: asyncio.TimerHandle | None = None
        self.abort_task: asyncio.Task[Any] | None = None

    # process group -------------------------------------------------------------------------

    def kill_group(self, sig: signal.Signals) -> None:
        if self.pid is None:
            return
        with contextlib.suppress(OSError):  # group already gone
            os.killpg(self.pid, sig)

    def terminate_group(self) -> None:
        """SIGTERM to the process group, SIGKILL after the grace period (the timer keeps nothing waiting)."""
        self.kill_group(signal.SIGTERM)
        self.kill_handle = self.loop.call_later(self.grace_s, self.kill_group, signal.SIGKILL)

    def stop(self, why: Literal["timeout", "aborted"]) -> None:
        if self.stopping or self.settled or self.child_exited:
            return
        self.stopping = why
        self.terminate_group()

    # stream-json ---------------------------------------------------------------------------

    def handle_event(self, event: dict[str, Any]) -> None:
        state = self.state
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init" and isinstance(event.get("session_id"), str):
            state.session_id = event["session_id"]
        elif kind == "assistant":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            changed = False
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    self.saw_tool_use = True
                    if len(state.tool_uses) < TOOL_USES_KEEP:
                        tool_input = block.get("input")
                        use_id = block.get("id")
                        state.tool_uses.append(
                            ToolUse(
                                name=block["name"],
                                input=tool_input if isinstance(tool_input, dict) else {},
                                id=use_id if isinstance(use_id, str) else None,
                            )
                        )
                    state.progress.append(summarize_tool_use(block["name"], block.get("input")))
                    if len(state.progress) > PROGRESS_KEEP:
                        state.progress.pop(0)
                    changed = True
            if changed and self.options.on_progress is not None:
                with contextlib.suppress(Exception):  # a broken listener must not break the run
                    self.options.on_progress(list(state.progress))
        elif kind == "user":
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    self.mark_result(block.get("tool_use_id"), is_error=block.get("is_error") is True)
        elif kind == "result":
            self.saw_result = True
            if isinstance(event.get("session_id"), str):
                state.session_id = event["session_id"]
            result = event.get("result")
            state.result = clip(result, RESULT_MAX_CHARS) if isinstance(result, str) else None
            subtype = event.get("subtype")
            self.result_subtype = subtype if isinstance(subtype, str) else None
            self.result_errors = event.get("errors")
            is_error = event.get("is_error")
            state.is_error = is_error if isinstance(is_error, bool) else None
            state.cost_usd = _number(event.get("total_cost_usd"))
            state.duration_ms = _number(event.get("duration_ms"))
            state.num_turns = _number(event.get("num_turns"))
            denials = event.get("permission_denials")
            state.permission_denials = denials if isinstance(denials, list) else []
            usage = event.get("usage")
            state.usage = usage if isinstance(usage, dict) else None

    def mark_result(self, use_id: object, *, is_error: bool) -> None:
        if not isinstance(use_id, str):
            return
        uses = self.state.tool_uses
        for i, use in enumerate(uses):
            if use.id == use_id:
                uses[i] = dataclasses.replace(use, is_error=is_error)

    def handle_line(self, raw: bytes) -> None:
        line = raw.strip()
        if not line.startswith(b"{"):
            return
        if len(line) > LARGE_USER_LINE_BYTES and _TYPE_USER_RE.match(line) and self.scan_large_user_line(line):
            return
        try:
            event = json.loads(line)
        except ValueError:
            return
        if isinstance(event, dict):
            self.handle_event(event)

    def scan_large_user_line(self, line: bytes) -> bool:
        """A big `user` event (usually a long tool_result) only matters for the results of the kept
        tool_uses, so its tool_use ids and is_error flags are read from the raw bytes instead of
        parsing it all. False when the layout is unusual (a flag missing or not after its id, an
        escaped id): the caller then parses the line."""
        kept = {use.id for use in self.state.tool_uses if use.id is not None}
        if not kept:
            return True
        matches = list(_TOOL_RESULT_RE.finditer(line))
        if len(matches) != line.count(b'"tool_use_id"'):
            return False
        found: list[tuple[str, bool]] = []
        for i, match in enumerate(matches):
            end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
            flag = _IS_ERROR_RE.search(line, match.end(), end)
            if flag is None:
                return False
            found.append((match.group(1).decode("utf-8", "replace"), flag.group(1) == b"true"))
        for use_id, is_error in found:
            if use_id in kept:
                self.mark_result(use_id, is_error=is_error)
        return True

    def on_stdout(self, data: bytes) -> None:
        # Only the new bytes are searched for a newline, and an unfinished line never grows past
        # MAX_LINE_BYTES: the rest of an oversized line is skipped up to its newline.
        if self.settled:
            return
        start = 0
        while (nl := data.find(b"\n", start)) != -1:
            piece = data[start:nl]
            start = nl + 1
            if self.dropping:
                self.dropping = False
                continue
            if self.buffer:
                self.buffer += piece
                line = bytes(self.buffer)
                self.buffer = bytearray()
            else:
                line = piece
            if len(line) > MAX_LINE_BYTES:
                self.state.dropped_lines += 1
                continue
            self.handle_line(line)
        if start < len(data) and not self.dropping:
            self.buffer += data[start:]
            if len(self.buffer) > MAX_LINE_BYTES:
                self.buffer = bytearray()
                self.dropping = True
                self.state.dropped_lines += 1

    def on_stderr(self, data: bytes) -> None:
        if self.settled:
            return
        self.stderr += data
        if len(self.stderr) > STDERR_TAIL_BYTES * 2:
            self.stderr = self.stderr[-STDERR_TAIL_BYTES:]

    # settling ------------------------------------------------------------------------------

    def on_stdout_done(self) -> None:
        self.stdout_done = True
        if self.child_exited:
            self.finish()

    def on_exit(self) -> None:
        # Settle when the claude process exits, not when stdout closes: a background process it
        # started can inherit stdout and keep it open for hours.
        self.child_exited = True
        if self.stdout_done:
            self.finish()
        else:
            self.drain_handle = self.loop.call_later(STDOUT_DRAIN_MS / 1000, self.finish)

    def finish(self) -> None:
        if self.settled:
            return
        self.settled = True
        for handle in (self.timeout_handle, self.drain_handle):
            if handle is not None:
                handle.cancel()
        if self.abort_task is not None:
            self.abort_task.cancel()
        if self.buffer and not self.dropping:
            self.handle_line(bytes(self.buffer))
        self.buffer = bytearray()
        # Background processes started by the run may still hold the pipes or keep running: stop
        # listening and terminate whatever is left in the process group. The SIGKILL timer keeps
        # nothing waiting.
        if not self.stopping:
            self.terminate_group()
        returncode = self.transport.get_returncode() if self.transport is not None else None
        if self.transport is not None:
            self.transport.close()

        state = self.state
        options = self.options
        state.stderr_tail = _tail_text(self.stderr)
        code: int | None = None
        sig: str | None = None
        if returncode is not None and returncode < 0:
            try:
                sig = signal.Signals(-returncode).name
            except ValueError:
                sig = str(-returncode)
        else:
            code = returncode
        state.exit_code = code
        state.exit_signal = sig
        state.ok = not self.stopping and self.saw_result and state.is_error is False and code == 0
        if self.stopping:
            state.outcome = self.stopping
        else:
            state.outcome = "success" if state.ok else "error"

        # Retrying without --resume repeats the run, so only when this run did no work at all.
        if not state.ok and options.resume_session_id and not self.stopping:
            state.resume_missing = (
                _RESUME_MISSING_RE.search(state.stderr_tail) is not None
                and not self.saw_tool_use
                and (state.num_turns is None or state.num_turns == 0)
            )

        exit_text = str(code) if code is not None else (sig if sig is not None else "null")
        if self.stopping == "timeout":
            state.error = f"timed out after {round(options.timeout_ms / 1000)} s"
        elif self.stopping == "aborted":
            state.error = "cancelled"
        elif not state.ok:
            stderr_line = _last_stderr_line(state.stderr_tail)
            if state.is_error and state.result:
                state.error = clip_words(state.result, 300)
            elif state.is_error:
                detail = _first_error(self.result_errors) or stderr_line
                kind = (
                    self.result_subtype.replace("_", " ")
                    if self.result_subtype and self.result_subtype != "success"
                    else None
                )
                state.error = clip_words(
                    ": ".join(p for p in (kind, detail) if p) or "Claude Code reported an error", 300
                )
            elif not self.saw_result:
                state.error = f"no result (exit {exit_text})" + (f": {stderr_line}" if stderr_line else "")
            else:
                state.error = f"exit code {exit_text}" + (f": {stderr_line}" if stderr_line else "")
        if not self.done.done():
            self.done.set_result(state)

    def watch_abort(self, abort: asyncio.Event) -> None:
        async def waiter() -> None:
            await abort.wait()
            self.stop("aborted")

        self.abort_task = self.loop.create_task(waiter())


def _number(value: object) -> Any:
    """The value when it is a number, else None (bool is not a number)."""
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


async def run_claude(options: RunOptions) -> RunResult:
    """Runs one `claude -p` process. Never raises; failures are described in the result."""
    if options.abort is not None and options.abort.is_set():
        return RunResult(outcome="aborted", error="cancelled before start")

    args = build_args(options)
    env = build_env(options.claude, options.base_env, isolated=run_access(options).isolated)
    loop = asyncio.get_running_loop()
    run = _Run(options, loop)
    try:
        transport, _protocol = await loop.subprocess_exec(
            lambda: _RunProtocol(run),
            options.claude.bin,
            *args,
            cwd=options.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as err:
        return RunResult(error=f"spawn failed: {err}")
    run.transport = transport
    run.pid = transport.get_pid()

    stdin = transport.get_pipe_transport(0)
    if isinstance(stdin, asyncio.WriteTransport):
        try:
            stdin.write(options.prompt.encode("utf-8"))
            stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the child exited without reading stdin

    run.timeout_handle = loop.call_later(options.timeout_ms / 1000, run.stop, "timeout")
    if options.abort is not None:
        run.watch_abort(options.abort)
    try:
        return await asyncio.shield(run.done)
    except asyncio.CancelledError:
        run.stop("aborted")
        raise
