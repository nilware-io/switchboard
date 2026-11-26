"""Task lifecycle, ids, queue, per-project sessions, persistence and waiting.

State files:
- `stateDir/tasks/<id>.json`: one `Task` per file, written synchronously on every state change and
  at most every PROGRESS_SAVE_MS for progress (failures only logged; the first write at enqueue
  must succeed, or the start fails).
- `stateDir/task-counter.json`: `{"nextId": n}`.
- `stateDir/projects.json`: `{"projects": {projectPath: {"name", "lastSessionId", "lastActivityAt"}}}`.
- `stateDir/mcp-config.json`: `{"mcpServers": claude.mcp_servers}`, written by `create`, for run_task
  (strict when `claude.access` is isolated; see `access.py`).
- `stateDir/mcp-config-none.json`: `{"mcpServers": {}}`, for the ask and send runs (no MCP servers).
- `stateDir/session-answers/<id>.json`: the Stop hook input of a task's Remote Control session (a
  session task, or a task reopened with resume_task_in_remote_control), whose
  `last_assistant_message` is its latest answer; deleted with the task file.

Concurrency model: one asyncio event loop. Starts, cancels and every state file write are
synchronous, so the limit checks and the queue cannot change between them. Runs are `asyncio.Task`s
kept in a set; waiters are Events per task id; aborting a run sets its `asyncio.Event`. Lookups
(`get`, `latest`, `list_tasks`, `queue_position`, `known_session_ids`) are synchronous.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, ValidationError, ValidationInfo, field_validator

from ..clock import DAY_MS, HOUR_MS, MINUTE_MS, SECOND_MS, elapsed_minutes, now_ms, parse_iso, to_iso
from ..config import ClaudeConfig, SessionsConfig, TasksConfig
from ..json_file import StateModel, ensure_dir, read_json, read_json_object, write_json_atomic
from ..log import Logger, create_logger, silent_logger
from ..notify import Notifier, NotifyTask
from ..projects import MatchOk, Project, list_projects, match_project, normalize_name
from ..sessions import (
    ASK_SESSION_PROMPT,
    WHITESPACE_RE,
    LiveSession,
    ReplyWatchOptions,
    ReplyWatchResult,
    SessionInfo,
    claude_home_dir,
    file_size,
    rc_disconnected,
    read_live_sessions,
    relay_input,
    relay_prompt,
    valid_session_id,
    watch_for_reply,
)
from ..text import ELLIPSIS, clip, plural
from .access import task_access
from .prompt import SPOKEN_ANSWER_PROMPT
from .runner import RESULT_MAX_CHARS, RunOptions, RunResult, ToolUse, run_claude

MAX_PROMPT_CHARS = 20_000
VOICE_ANSWER_LIMIT = 2000
LOAD_LAST = 50
MEMORY_KEEP = 200
STDERR_LOG_CHARS = 1000
PRUNE_INTERVAL_MS = HOUR_MS
"""Task files older than `keep_days` are deleted at startup and then this often."""
PROGRESS_SAVE_MS = 5 * SECOND_MS
"""Progress alone rewrites a running task's file at most this often (the state in memory is current)."""
ASK_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")
"""Built-in tools of an ask_session_readonly run (besides plan mode)."""

TaskKind = Literal["task", "ask", "send", "remote"]
"""task: run_task; ask: ask_session_readonly (read-only fork); send: send_message_to_session (relay plus
reply watch); remote: a background job of the Switchboard remote tools (`start_job`)."""

TaskStatus = Literal["queued", "running", "done", "failed", "cancelled", "timeout"]

TERMINAL_STATUSES: tuple[TaskStatus, ...] = ("done", "failed", "cancelled", "timeout")

TASK_OPTIONAL_KEYS = frozenset(
    {
        "target_session_id",
        "target_session_name",
        "remote_session_id",
        "resume_skipped",
        "remote_control",
        "bg_id",
        "session_state",
        "title",
        "name",
        "handed_over_at",
        "desktop_session_id",
        "session_answer",
    }
)
"""Fields written only when set: pass to `Task.to_json(exclude_none_fields=...)`."""

TASK_FILE_RE = re.compile(r"(\d+)\.json", re.ASCII)
"""Name of a task file in `stateDir/tasks` (`fullmatch`; group 1 is the id)."""
_SENT_RE = re.compile(r"^SENT\b", re.ASCII)
_FAILED_PREFIX_RE = re.compile(r"^FAILED:?\s*")


def _clip_tail(text: str, limit: int) -> str:
    trimmed = text.strip()
    return ELLIPSIS + trimmed[-(limit - 1) :] if len(trimmed) > limit else trimmed


@dataclass(frozen=True)
class _LoadContext:
    """Pydantic validation context of a state file load: names the file in the invalid session id warning."""

    logger: Logger
    source: str
    """Names the file in the warning, followed by the field name."""


_CODE_LOGGER: Logger = create_logger()
"""Warns about invalid session ids in models built in code (no `_LoadContext`)."""


def _checked_session_id(value: object, info: ValidationInfo) -> object:
    """A session id that is not UUID-shaped becomes None with a warning that never includes the value:
    only such ids may reach `--resume`. A load (`_LoadContext`) warns on its logger and names its
    file; models built in code warn on `_CODE_LOGGER`."""
    context = info.context
    if isinstance(context, _LoadContext):
        logger, source = context.logger, f"{context.source} {info.field_name}"
    else:
        logger, source = _CODE_LOGGER, f"task {info.field_name}"
    task_id = info.data.get("id")
    return valid_session_id(value, logger, source, task_id if isinstance(task_id, int) else None)


SessionId = Annotated[str | None, BeforeValidator(_checked_session_id)]
"""A session id field: invalid ids become None, whether loaded from a state file or built in code."""

_BG_ID_RE = re.compile(r"[0-9a-f]{8}", re.ASCII)


def valid_bg_id(value: object) -> str | None:
    """Only an id8 (8 lowercase hex digits) may reach `claude stop`; anything else becomes None."""
    return value if isinstance(value, str) and _BG_ID_RE.fullmatch(value) else None


def _checked_bg_id(value: object) -> object:
    return valid_bg_id(value)


BgId = Annotated[str | None, BeforeValidator(_checked_bg_id)]

SessionState = Literal["working", "approval", "input", "idle", "disconnected"]
"""A running session task: working, waiting for a permission approval, waiting for another answer,
or idle (its turn is done)."""

SESSION_LOST_MS = 60 * SECOND_MS
"""A session task whose session never showed up in the live registry counts as ended after this."""
MAX_ANSWER_FILE_BYTES = 1024 * 1024
"""Larger Stop hook files are ignored."""


class ProjectState(StateModel):
    """One project in projects.json: the session a later run_task resumes."""

    name: str | None = None
    last_session_id: SessionId = None
    last_activity_at: str | None = None


PROJECT_OPTIONAL_KEYS = frozenset({"name", "last_session_id", "last_activity_at"})


class ProjectsFile(StateModel):
    """projects.json, by project path. Entries that are not objects are dropped at load."""

    projects: dict[str, ProjectState] = Field(default_factory=dict)

    @field_validator("projects", mode="before")
    @classmethod
    def _objects_only(cls, value: object) -> object:
        if not isinstance(value, dict):
            return {}
        return {path: state for path, state in value.items() if isinstance(state, dict)}

    def to_file(self) -> dict[str, Any]:
        data = self.to_json()
        data["projects"] = {
            path: state.to_json(exclude_none_fields=PROJECT_OPTIONAL_KEYS) for path, state in self.projects.items()
        }
        return data


class Task(StateModel):
    """One task file. Times are ISO strings (`clock.to_iso`)."""

    id: int
    kind: TaskKind = "task"
    """Missing in task files written before sessions existed."""
    target_session_id: SessionId = None
    """ask/send: the session asked or messaged."""
    target_session_name: str | None = None
    project: str
    project_path: str
    prompt: str
    status: TaskStatus
    new_session: bool
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    resumed_session_id: SessionId = None
    """Session the run resumed, if any."""
    session_id: SessionId = None
    """Session id reported by Claude Code."""
    result: str | None = None
    error: str | None = None
    progress: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    permission_denials: int = 0
    exit_code: int | None = None
    remote_session_id: SessionId = None
    """Session opened for Remote Control by resume_task_in_remote_control (may be a copy with a new id)."""
    resume_skipped: bool | None = None
    """True when the project session was not resumed because it is open in Remote Control."""
    remote_control: bool | None = None
    """run_task(remote_control=true): the task runs as a Remote Control session (`claude --bg`), not
    as a headless run. It is "running" while the session process lives, then "done" (or "cancelled"
    after cancel_task); `session_id` and `remote_session_id` are the session's id."""
    bg_id: BgId = None
    """The session's id8 as `claude --bg` printed it (what `claude stop` takes)."""
    session_state: SessionState | None = None
    """A running session task's state as last seen in the live registry."""
    title: str | None = None
    """Short label the voice model made up for the task (`sanitize_title`); prompt-derived text,
    logged only with logPrompts."""
    name: str | None = None
    """The task's session name, `<title or project> (voice <id>)`, set when the task is created.
    Missing in task files written before titles existed (`session_name` falls back)."""
    handed_over_at: str | None = None
    """When the task's stopped Remote Control session was handed over to the Claude desktop app
    (`claude://resume`); cleared when the server opens the session again."""
    desktop_session_id: SessionId = None
    """The session the desktop app took over with that handoff (it owns the transcript now)."""
    session_answer: str | None = None
    """A finished task reopened with resume_task_in_remote_control: the latest answer of that
    session in the Claude app (its Stop hook); `result` keeps the headless answer."""

    @property
    def is_session_task(self) -> bool:
        """A session task (run_task with remote_control) whose session is still running."""
        return bool(self.remote_control) and self.status == "running"


def live_bg_session(task: Task, live: Mapping[str, LiveSession]) -> tuple[str, LiveSession] | None:
    """(session id, registry entry) of the task's live background session opened for Remote
    Control, connected or not: its `remote_session_id`, else the one entry starting with its
    `bg_id`. None when it is not live or the entry is not the server's (`LiveSession.is_bg`)."""
    sid = task.remote_session_id
    if sid is None and task.bg_id:
        matches = [s for s in live if s.startswith(task.bg_id)]
        sid = matches[0] if len(matches) == 1 else None
    entry = live.get(sid) if sid else None
    return (sid, entry) if sid and entry is not None and entry.is_bg else None


StartFailureReason = Literal[
    "not_found",
    "ambiguous",
    "empty_prompt",
    "prompt_too_long",
    "queue_full",
    "shutting_down",
    "state_error",
    "session_not_running",
    "session_unreachable",
]


@dataclass(frozen=True)
class Started:
    """A task that was queued (or started), also the result of a successful cancel (`Cancelled`)."""

    task: Task


@dataclass(frozen=True)
class StartFailed:
    """`candidates` is set for not_found/ambiguous, `session` for session_not_running/session_unreachable."""

    reason: StartFailureReason
    project: str
    candidates: list[str] = field(default_factory=list)
    session: str | None = None


StartResult = Started | StartFailed


Cancelled = Started


@dataclass(frozen=True)
class CancelFailed:
    reason: Literal["not_found", "already_finished"]
    task: Task | None = None
    """Set for already_finished."""


CancelResult = Cancelled | CancelFailed


@dataclass(frozen=True)
class StartItem:
    project: str
    prompt: str
    new_session: bool = False
    title: str | None = None


@dataclass
class TaskManagerOptions:
    state_dir: str
    project_roots: Sequence[str]
    claude: ClaudeConfig
    tasks: TasksConfig
    logger: Logger
    sessions: SessionsConfig | None = None
    """Default: 7 days, 10 minutes reply wait."""
    notifier: Notifier | None = None
    base_env: Mapping[str, str] | None = None
    """Environment for the allowlist, default `os.environ`."""
    now: Callable[[], int] | None = None
    """Clock in epoch ms, for tests."""
    run: Callable[[RunOptions], Awaitable[RunResult]] | None = None
    """Runner, for tests (default `run_claude`)."""
    kill_grace_ms: int | None = None
    """SIGTERM to SIGKILL delay passed to the runner."""
    watch_reply: Callable[[ReplyWatchOptions], Awaitable[ReplyWatchResult]] | None = None
    """Reply watcher for send_message_to_session, for tests (default `watch_for_reply`)."""
    reply_poll_ms: int | None = None
    """Transcript poll interval for send_message_to_session."""
    live_session_ids: Callable[[], set[str]] | None = None
    """Ids of running Claude Code sessions, for tests (default: the live registry)."""
    log_prompts: bool = False
    """When false, stderr of a failed run is logged only as its length (it may quote the prompt)."""
    prune_interval_ms: int | None = None
    """How often old task files are pruned, default PRUNE_INTERVAL_MS (for tests)."""
    public_url: str | None = None
    """The server's public URL; `claude.access` desktop denies it to task runs (required there)."""
    home_dir: str | None = None
    """Where `.claude.json` is looked for without `claude.configDir`, default `~` (for tests)."""


def is_terminal(status: TaskStatus) -> bool:
    return status in TERMINAL_STATUSES


def task_file_ids(tasks_dir: str) -> list[int]:
    """Ids of the task files in `tasks_dir`, unsorted. Raises OSError."""
    return [int(m.group(1)) for name in os.listdir(tasks_dir) if (m := TASK_FILE_RE.fullmatch(name))]


def read_task_file(path: str, task_id: int, logger: Logger = silent_logger) -> Task:
    """One task file as it is, except that invalid session ids become None (warned on `logger`).
    Raises OSError or ValueError (also pydantic's ValidationError)."""
    data = read_json(path, None)
    if not isinstance(data, dict) or type(data.get("id")) is not int or data["id"] != task_id:
        raise ValueError("not the task file of its id")
    if not isinstance(data.get("progress"), list):
        data["progress"] = []
    return Task.model_validate(data, context=_LoadContext(logger, "task file"))


def load_tasks(
    state_dir: str,
    ids: Sequence[int] | None = None,
    on_error: Callable[[int, Exception], None] | None = None,
    logger: Logger = silent_logger,
) -> list[Task]:
    """The task files of `ids` (default: all, by id), as they are: no restart recovery. Unreadable
    files are skipped after `on_error(task_id, error)`."""
    tasks_dir = os.path.join(state_dir, "tasks")
    if ids is None:
        try:
            ids = sorted(task_file_ids(tasks_dir))
        except OSError:
            return []
    tasks: list[Task] = []
    for task_id in ids:
        try:
            tasks.append(read_task_file(os.path.join(tasks_dir, f"{task_id}.json"), task_id, logger))
        except (OSError, ValueError) as err:
            if on_error is not None:
                on_error(task_id, err)
    return tasks


@dataclass
class _SessionSpec:
    """Not persisted: what an ask/send run needs besides the task itself."""

    session: SessionInfo
    address: str | None
    """send: `uds:<socket>` address of the target."""


@dataclass
class _Running:
    """An active (running or reply-watching) task, a remote job, or a slot lease (negative key)."""

    lock_key: str
    abort: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str | None = None
    holds_slot: bool = True
    """False while a send task only waits for the reply (no process runs)."""


def _copy(task: Task) -> Task:
    return task.model_copy(deep=True)


@dataclass
class SlotLease:
    """A run slot held outside the queue (`TaskManager.claim_slot`), for example by the remote
    summariser's `claude -p` run. `abort` is set at shutdown; `release` frees the slot (idempotent)."""

    abort: asyncio.Event
    release: Callable[[], None]


JobFn = Callable[[asyncio.Event], Awaitable[str]]
"""A remote job: gets the task's abort event, returns the spoken result (raising fails the task)."""


class TaskManager:
    """Use `await TaskManager.create(options)`; the constructor is private."""

    def __init__(self, options: TaskManagerOptions) -> None:
        self._opts = options
        self._now: Callable[[], int] = options.now or now_ms
        self._tasks_dir = os.path.join(options.state_dir, "tasks")
        self._counter_path = os.path.join(options.state_dir, "task-counter.json")
        self._projects_path = os.path.join(options.state_dir, "projects.json")
        self._mcp_config_path = os.path.join(options.state_dir, "mcp-config.json")
        self._no_mcp_config_path = os.path.join(options.state_dir, "mcp-config-none.json")
        self._answers_dir = os.path.join(options.state_dir, "session-answers")
        self._tasks: dict[int, Task] = {}
        self._queue: list[int] = []
        self._active: dict[int, _Running] = {}
        """Running tasks and send tasks waiting for a reply (those hold their lock but no slot)."""
        self._waiters: dict[int, set[asyncio.Event]] = {}
        self._runs: set[asyncio.Task[None]] = set()
        self._notifications: set[asyncio.Task[None]] = set()
        self._specs: dict[int, _SessionSpec] = {}
        """Every queued or active ask/send task has one."""
        self._projects = ProjectsFile()
        self._next_id = 1
        self._prune_handle: asyncio.TimerHandle | None = None
        self._shutting_down = False
        self._reopening: dict[int, int] = {}
        """Session tasks whose session resume_task_in_remote_control is replacing (`reopening`)."""
        self._next_lease = -1
        """Keys of slot leases in `_active` are negative, so they never collide with task ids."""

    @classmethod
    async def create(cls, options: TaskManagerOptions) -> TaskManager:
        """Writes the MCP configs, prunes task files older than `keep_days` (by mtime, again every hour),
        loads the last 50 and marks those left queued/running as failed ("server restarted")."""
        manager = cls(options)
        manager._init()
        manager._schedule_prune(asyncio.get_running_loop())
        return manager

    @property
    def mcp_config_path(self) -> str:
        return self._mcp_config_path

    def session_answer_path(self, task_id: int) -> str:
        """Where the Stop hook of a session task writes its input (with `last_assistant_message`)."""
        return os.path.join(self._answers_dir, f"{task_id}.json")

    def _init(self) -> None:
        opts = self._opts
        logger = opts.logger
        ensure_dir(opts.state_dir)
        ensure_dir(self._tasks_dir)
        ensure_dir(self._answers_dir)
        write_json_atomic(self._mcp_config_path, {"mcpServers": opts.claude.mcp_servers})
        write_json_atomic(self._no_mcp_config_path, {"mcpServers": {}})
        raw_projects: Any = read_json(self._projects_path, {})
        try:
            # Invalid lastSessionIds are dropped here: only UUID-shaped ids may reach `--resume`.
            self._projects = ProjectsFile.model_validate(raw_projects, context=_LoadContext(logger, "projects.json"))
        except ValidationError:
            self._projects = ProjectsFile()
        counter: Any = read_json(self._counter_path, {})
        raw_next = counter.get("nextId") if isinstance(counter, dict) else None
        self._next_id = raw_next if type(raw_next) is int and raw_next >= 1 else 1
        ids = self._prune_files()

        def unreadable(task_id: int, err: Exception) -> None:
            error = str(err).split("\n")[0] if isinstance(err, ValidationError) else err
            logger.warn("task file unreadable", {"taskId": task_id, "error": error})

        for task in load_tasks(opts.state_dir, sorted(ids)[-LOAD_LAST:], unreadable, logger):
            self._tasks[task.id] = task
            if task.status == "queued" or (task.status == "running" and not task.remote_control):
                task.status = "failed"
                task.error = "server restarted"
                task.finished_at = to_iso(self._now())
                self._save(task)
        logger.info("tasks loaded", {"count": len(self._tasks), "nextId": self._next_id})

    def _prune_files(self) -> list[int]:
        """Deletes task files older than `keep_days` (by mtime) unless their task is still active, and
        returns the ids of the files left. Seen ids also move the counter past them."""
        cutoff = self._now() - self._opts.tasks.keep_days * DAY_MS
        ids: list[int] = []
        try:
            found = task_file_ids(self._tasks_dir)
        except OSError as err:
            self._opts.logger.warn("task files unreadable", {"error": err})
            return ids
        for task_id in found:
            self._next_id = max(self._next_id, task_id + 1)
            path = self._task_path(task_id)
            active = (task := self._tasks.get(task_id)) is not None and not is_terminal(task.status)
            try:
                if not active and os.stat(path).st_mtime_ns / 1_000_000 < cutoff:
                    os.unlink(path)
                    with contextlib.suppress(OSError):
                        os.unlink(self.session_answer_path(task_id))
                    continue
            except OSError:
                continue
            ids.append(task_id)
        return ids

    def _schedule_prune(self, loop: asyncio.AbstractEventLoop) -> None:
        interval = self._opts.prune_interval_ms if self._opts.prune_interval_ms is not None else PRUNE_INTERVAL_MS
        self._prune_handle = loop.call_later(interval / SECOND_MS, self._prune_tick, loop)

    def _prune_tick(self, loop: asyncio.AbstractEventLoop) -> None:
        self._prune_handle = None
        if self._shutting_down:
            return
        try:
            self._prune_files()
        except Exception as err:
            self._opts.logger.warn("task file pruning failed", {"error": err})
        self._schedule_prune(loop)

    def _task_path(self, task_id: int) -> str:
        return os.path.join(self._tasks_dir, f"{task_id}.json")

    @staticmethod
    def _task_json(task: Task) -> dict[str, Any]:
        return task.to_json(exclude_none_fields=TASK_OPTIONAL_KEYS)

    def _save(self, task: Task) -> None:
        """Writes the task file; failures are only logged. Writes are synchronous, so the writes of
        one task can never overtake each other."""
        try:
            write_json_atomic(self._task_path(task.id), self._task_json(task))
        except Exception as err:
            self._opts.logger.error("task save failed", {"taskId": task.id, "error": err})

    def _save_projects(self) -> None:
        try:
            write_json_atomic(self._projects_path, self._projects.to_file())
        except Exception as err:
            self._opts.logger.error("projects save failed", {"error": err})

    def _now_iso(self) -> str:
        return to_iso(self._now())

    @staticmethod
    def _lock_key(task: Task) -> str:
        """Runs with the same key never overlap: the project for run_task, the target session for ask/send."""
        return f"session:{task.target_session_id}" if task.kind != "task" else task.project_path

    def _slots_used(self) -> int:
        return sum(1 for running in self._active.values() if running.holds_slot)

    def _locked(self, lock_key: str) -> bool:
        return any(running.lock_key == lock_key for running in self._active.values())

    def _can_start_now(self, lock_key: str) -> bool:
        if self._slots_used() >= self._opts.tasks.max_concurrent or self._locked(lock_key):
            return False
        return not any(self._lock_key(self._tasks[qid]) == lock_key for qid in self._queue)

    def _check_prompt(self, prompt: str, project: str) -> str | StartFailed:
        """The trimmed prompt, or why no task can start with it."""
        if self._shutting_down:
            return StartFailed(reason="shutting_down", project=project)
        text = prompt.strip()
        if not text:
            return StartFailed(reason="empty_prompt", project=project)
        if len(text) > MAX_PROMPT_CHARS:
            return StartFailed(reason="prompt_too_long", project=project)
        return text

    async def start(
        self, project_name: str, prompt: str, *, new_session: bool = False, title: str | None = None
    ) -> StartResult:
        """Validates, resolves the project and queues a task (it starts at once when a slot is free).
        `title`: the voice model's short label for the session name (sanitized here)."""
        return self._start(list_projects(self._opts.project_roots), project_name, prompt, new_session, title)

    async def start_many(self, items: Sequence[StartItem]) -> list[StartResult]:
        """Starts several tasks in order; each result is independent."""
        projects = list_projects(self._opts.project_roots)
        return [self._start(projects, item.project, item.prompt, item.new_session, item.title) for item in items]

    def _resolve(
        self, projects: Sequence[Project], project_name: str, prompt: str
    ) -> tuple[Project, str] | StartFailed:
        """The matched project and the trimmed prompt, or why no task can start."""
        text = self._check_prompt(prompt, project_name)
        if isinstance(text, StartFailed):
            return text
        resolved = match_project(projects, project_name)
        if not isinstance(resolved, MatchOk):
            return StartFailed(reason=resolved.reason, project=project_name, candidates=list(resolved.candidates))
        return resolved.item, text

    def _start(
        self, projects: Sequence[Project], project_name: str, prompt: str, new_session: bool, title: str | None
    ) -> StartResult:
        resolved = self._resolve(projects, project_name, prompt)
        if isinstance(resolved, StartFailed):
            return resolved
        project, text = resolved
        return self._enqueue("task", project, text, new_session=new_session, title=title)

    async def start_session(self, kind: Literal["ask", "send"], session: SessionInfo, prompt: str) -> StartResult:
        """ask: answers from a forked, read-only copy. send: relays into the running session and waits
        for its reply."""
        text = self._check_prompt(prompt, session.project)
        if isinstance(text, StartFailed):
            return text
        address: str | None = None
        if kind == "send":
            if session.live is None:
                return StartFailed(reason="session_not_running", project=session.project, session=session.name)
            if not session.live.socket_path:
                return StartFailed(reason="session_unreachable", project=session.project, session=session.name)
            address = f"uds:{session.live.socket_path}"
        return self._enqueue(
            kind,
            Project(name=session.project, path=session.cwd),
            text,
            new_session=False,
            spec=_SessionSpec(session=session, address=address),
        )

    def start_remote(self, project_name: str, prompt: str, *, title: str | None = None) -> StartResult:
        """Creates a running session task (run_task with remote_control): no queue and no run slot,
        the caller launches the session and reports back with `remote_launched` or `remote_failed`.
        Synchronous, like `_enqueue`."""
        resolved = self._resolve(list_projects(self._opts.project_roots), project_name, prompt)
        if isinstance(resolved, StartFailed):
            return resolved
        project, text = resolved
        now = self._now_iso()
        label = sanitize_title(title)
        task = Task(
            id=self._next_id,
            title=label,
            name=format_session_name(label or project.name, self._next_id),
            project=project.name,
            project_path=project.path,
            prompt=text,
            status="running",
            new_session=True,
            created_at=now,
            started_at=now,
            remote_control=True,
            session_state="working",
        )
        if not self._write_new(task):
            return StartFailed(reason="state_error", project=project.name)
        self._opts.logger.info(
            "session task created", {"taskId": task.id, "project": project.name, "promptChars": len(text)}
        )
        return Started(task=_copy(task))

    def claim_slot(self, lock_key: str) -> SlotLease | None:
        """A run slot now, outside the queue, or None when every slot is taken, the key is locked or
        a task waits in the queue (queued tasks keep their turn). Synchronous, like `_enqueue`."""
        if self._shutting_down or self._queue or not self._can_start_now(lock_key):
            return None
        key = self._next_lease
        self._next_lease -= 1
        running = self._active[key] = _Running(lock_key=lock_key)

        def release() -> None:
            if self._active.get(key) is running:
                del self._active[key]
                self._pump()

        return SlotLease(abort=running.abort, release=release)

    def start_job(self, *, label: str, machine: str, lock_key: str, job: JobFn) -> StartResult:
        """A `remote` task that runs `job` in the background (no queue, no run slot of its own: a
        job that runs `claude -p` takes one with `claim_slot`). `label` names it ("status of fpga on
        dev") and is all that is stored as its prompt. While a job with the same `lock_key` runs, that
        job's task is returned instead of starting another."""
        if self._shutting_down:
            return StartFailed(reason="shutting_down", project=machine)
        for task_id, running in self._active.items():
            existing = self._tasks.get(task_id)
            if running.lock_key == lock_key and existing is not None and existing.kind == "remote":
                return Started(task=_copy(existing))
        now = self._now_iso()
        label = sanitize_title(label) or "remote job"
        task = Task(
            id=self._next_id,
            kind="remote",
            title=label,
            project=machine,
            project_path=lock_key,
            prompt=label,
            status="running",
            new_session=False,
            created_at=now,
            started_at=now,
        )
        if not self._write_new(task):
            return StartFailed(reason="state_error", project=machine)
        running = self._active[task.id] = _Running(lock_key=lock_key, holds_slot=False)
        self._opts.logger.info("remote job started", {"taskId": task.id, "machine": machine})
        run = asyncio.get_running_loop().create_task(self._run_job(task, running, job))
        self._runs.add(run)
        run.add_done_callback(self._runs.discard)
        return Started(task=_copy(task))

    async def _run_job(self, task: Task, running: _Running, job: JobFn) -> None:
        work = asyncio.ensure_future(job(running.abort))
        stop = asyncio.ensure_future(running.abort.wait())
        try:
            await asyncio.wait({work, stop}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop.cancel()
        if not work.done():
            work.cancel()
            with contextlib.suppress(BaseException):
                await work
        task.finished_at = self._now_iso()
        if work.cancelled():
            task.status = "failed" if running.cancel_reason else "cancelled"
            task.error = running.cancel_reason or "cancelled"
        elif (error := work.exception()) is not None:
            task.status = "failed"
            task.error = str(error) or type(error).__name__
        else:
            task.status = "done"
            task.result = work.result()
        self._active.pop(task.id, None)
        self._save(task)
        self._opts.logger.info("remote job finished", {"taskId": task.id, "status": task.status})
        self._finished(task)
        self._pump()

    def _write_new(self, task: Task) -> bool:
        """Claims the task id and writes the new task file; False (logged) when that fails."""
        self._next_id += 1
        try:
            write_json_atomic(self._counter_path, {"nextId": self._next_id})
            # Direct write: _save() only logs failures, and a task that is not on disk must not start.
            write_json_atomic(self._task_path(task.id), self._task_json(task))
        except Exception as err:
            self._opts.logger.error(
                "task state write failed", {"taskId": task.id, "project": task.project, "error": err}
            )
            return False
        self._tasks[task.id] = task
        self._trim_memory()
        return True

    def remote_launched(self, task_id: int, *, session_id: str | None, bg_id: str | None) -> None:
        """The session of a session task was started (`session_id` None while not found in the registry)."""
        task = self._tasks.get(task_id)
        if task is None or not task.remote_control:
            return
        task.bg_id = valid_bg_id(bg_id)
        sid = valid_session_id(session_id, self._opts.logger, "remote control", task_id) if session_id else None
        if sid:
            task.session_id = task.remote_session_id = sid
        task.handed_over_at = task.desktop_session_id = None
        self._save(task)

    def remote_failed(self, task_id: int, error: str) -> None:
        """The session of a session task could not be started."""
        task = self._tasks.get(task_id)
        if task is not None and task.is_session_task:
            self._end_remote(task, "failed", error)

    def session_closed(self, task_id: int, reason: Literal["cancel", "idle"]) -> None:
        """`RemoteControl.close` stopped the task's session (`claude stop`). A running session task
        ends: "cancelled" by cancel_task, "done" by the idle close (its turn was over). A no-op for
        any other task."""
        task = self._tasks.get(task_id)
        if task is None or not task.is_session_task:
            return
        self._read_answer(task)
        if reason == "idle":
            self._end_remote(task, "done", None)
        else:
            self._end_remote(task, "cancelled", "cancelled")

    def _end_remote(self, task: Task, status: TaskStatus, error: str | None) -> None:
        task.status = status
        task.error = error
        task.session_state = None
        task.finished_at = self._now_iso()
        self._save(task)
        self._opts.logger.info("session task ended", {"taskId": task.id, "status": status})
        self._wake(task.id)

    def remote_task_ids(self) -> list[int]:
        """Running session tasks."""
        return [t.id for t in self._tasks.values() if t.is_session_task]

    @contextlib.contextmanager
    def reopening(self, task_id: int) -> Iterator[None]:
        """While resume_task_in_remote_control replaces a running session task's stale session (stop,
        then launch again, then `remote_launched` with the new ids), `refresh_remote` leaves the task
        alone: the old session leaving the registry does not end it."""
        self._reopening[task_id] = self._reopening.get(task_id, 0) + 1
        try:
            yield
        finally:
            left = self._reopening[task_id] - 1
            if left:
                self._reopening[task_id] = left
            else:
                del self._reopening[task_id]

    def refresh_remote(self, live: Mapping[str, LiveSession], task_ids: Sequence[int] | None = None) -> None:
        """Updates tasks (default: the running session tasks) from the live registry and their Stop
        hook file. A running session task gets its state, last answer, and "done" once the session
        process is gone; tasks being reopened (`reopening`) are skipped. A finished task reopened
        with resume_task_in_remote_control (it has a `remote_session_id`) gets the latest answer of
        that session (`session_answer`, or `result` for a session task, whose result is its
        session's answer anyway)."""
        for task_id in task_ids if task_ids is not None else self.remote_task_ids():
            task = self._tasks.get(task_id)
            if task is None:
                continue
            if not task.is_session_task:
                if is_terminal(task.status) and task.remote_session_id and self._read_answer(task):
                    self._save(task)
                continue
            if task_id in self._reopening:
                continue
            entry = self._find_live(task, live)
            changed = self._read_answer(task)
            if entry is not None:
                state = session_state(entry, self._now())
                if state != task.session_state:
                    task.session_state = state
                    changed = True
            elif task.session_id or self._age_ms(task) > SESSION_LOST_MS:
                self._end_remote(task, "done", None)
                continue
            if changed:
                self._save(task)

    def _age_ms(self, task: Task) -> int:
        started = parse_iso(task.started_at)
        return self._now() - started if started is not None else SESSION_LOST_MS + 1

    def _find_live(self, task: Task, live: Mapping[str, LiveSession]) -> LiveSession | None:
        """The task's session in the registry: by id, else (id not known yet) by its id8."""
        if task.session_id:
            return live.get(task.session_id)
        if not task.bg_id:
            return None
        matches = [sid for sid in live if sid.startswith(task.bg_id)]
        if len(matches) != 1:
            return None
        sid = valid_session_id(matches[0], self._opts.logger, "live registry", task.id)
        if not sid:
            return None
        task.session_id = task.remote_session_id = sid
        return live[sid]

    def _read_answer(self, task: Task) -> bool:
        """Copies `last_assistant_message` from the Stop hook file into `task.result` (a session task)
        or `task.session_answer` (a reopened task); True when it changed. The file is read
        defensively: missing, too large, invalid or of another session (neither the task's session
        nor its Remote Control session) means no answer."""
        data = read_json_object(self.session_answer_path(task.id), MAX_ANSWER_FILE_BYTES)
        if data is None:
            return False
        message = data.get("last_assistant_message")
        hook_session = data.get("session_id")
        own = {sid for sid in (task.session_id, task.remote_session_id) if sid}
        if not isinstance(message, str) or (own and hook_session not in own):
            return False
        message = message.strip()[:RESULT_MAX_CHARS]
        if task.remote_control:
            if not message or message == task.result:
                return False
            task.result = message
            return True
        if not message or message == task.session_answer:
            return False
        task.session_answer = message
        return True

    def _enqueue(
        self,
        kind: TaskKind,
        project: Project,
        text: str,
        *,
        new_session: bool,
        spec: _SessionSpec | None = None,
        title: str | None = None,
    ) -> StartResult:
        """Synchronous from the limit check to the queue insert, so concurrent starts cannot exceed the
        limits or overtake each other."""
        label = sanitize_title(title)
        task = Task(
            id=self._next_id,
            kind=kind,
            title=label,
            name=format_session_name(label or project.name, self._next_id),
            target_session_id=spec.session.id if spec else None,
            target_session_name=spec.session.name if spec else None,
            project=project.name,
            project_path=project.path,
            prompt=text,
            status="queued",
            new_session=new_session,
            created_at=self._now_iso(),
        )
        if not self._can_start_now(self._lock_key(task)) and len(self._queue) >= self._opts.tasks.max_queued:
            return StartFailed(reason="queue_full", project=project.name)

        task_id = task.id
        if not self._write_new(task):
            return StartFailed(reason="state_error", project=project.name)
        if spec is not None:
            self._specs[task_id] = spec
        self._queue.append(task_id)
        self._opts.logger.info(
            "task queued", {"taskId": task_id, "kind": kind, "project": project.name, "promptChars": len(text)}
        )
        self._pump()
        return Started(task=_copy(task))

    def _pump(self) -> None:
        if self._shutting_down:
            return
        i = 0
        while i < len(self._queue) and self._slots_used() < self._opts.tasks.max_concurrent:
            task = self._tasks[self._queue[i]]
            if self._locked(self._lock_key(task)):
                i += 1
                continue
            self._queue.pop(i)
            running = self._begin(task)
            run = asyncio.get_running_loop().create_task(self._execute(task, running))
            self._runs.add(run)
            run.add_done_callback(self._runs.discard)

    def _session_to_resume(self, task: Task) -> str | None:
        if task.kind == "ask":
            return task.target_session_id
        if task.kind == "send" or task.new_session:
            return None
        state = self._projects.projects.get(task.project_path)
        if state is None or not state.last_session_id:
            return None
        activity_ms = parse_iso(state.last_activity_at)
        if activity_ms is None:
            return None
        age = self._now() - activity_ms
        return state.last_session_id if 0 <= age <= self._opts.tasks.resume_within_hours * HOUR_MS else None

    def _begin(self, task: Task) -> _Running:
        """The synchronous start of a run: claims the slot and the lock and marks the task running."""
        running = self._active[task.id] = _Running(lock_key=self._lock_key(task))
        task.status = "running"
        task.started_at = self._now_iso()
        resume = self._session_to_resume(task)
        if resume and task.kind == "task" and self._open_in_remote_control(resume):
            # Resuming would make two processes write one conversation: start a fresh one instead.
            task.resume_skipped = True
            resume = None
        task.resumed_session_id = resume
        self._save(task)
        self._opts.logger.info(
            "task started",
            {
                "taskId": task.id,
                "kind": task.kind,
                "project": task.project,
                "resume": task.resumed_session_id is not None,
            },
        )
        return running

    def _open_in_remote_control(self, session_id: str) -> bool:
        """The session, or a Remote Control session opened from it (or handed over to the desktop
        app), is running: as the server's bg session, in the desktop app or in a terminal."""
        ids = {session_id}
        for other in self._tasks.values():
            related = {other.session_id, other.remote_session_id, other.desktop_session_id} - {None}
            if session_id in related and (other.remote_session_id or other.desktop_session_id):
                ids.update(sid for sid in related if sid)
        try:
            live = (
                self._opts.live_session_ids()
                if self._opts.live_session_ids is not None
                else set(read_live_sessions(claude_home_dir(self._opts.claude.config_dir)))
            )
        except Exception:
            return False
        return not ids.isdisjoint(live)

    def _run_overrides(self, task: Task, spec: _SessionSpec | None) -> dict[str, Any]:
        """The RunOptions fields that depend on the task kind, including the prompt."""
        # ask and send never get MCP servers: only the built-in tools they need.
        if task.kind == "ask":
            return {
                "prompt": task.prompt,
                "mcp_config_path": self._no_mcp_config_path,
                "fork_session": True,
                "permission_mode": "plan",
                "tools": list(ASK_TOOLS),
                "system_prompt": f"{SPOKEN_ANSWER_PROMPT}\n{ASK_SESSION_PROMPT}",
            }
        if task.kind == "send" and spec is not None:
            return {
                "prompt": relay_input(spec.session.name, task.prompt, f"MESSAGE {secrets.token_hex(6)}"),
                "mcp_config_path": self._no_mcp_config_path,
                "tools": ["SendMessage"],
                "session_name": _relay_sender(task.id),
                "max_turns": 3,
                "system_prompt": relay_prompt(spec.address or ""),
            }
        # The session name (a deliberate short label, never the prompt) titles the conversation.
        return {"prompt": task.prompt, "mcp_config_path": self._mcp_config_path, "session_name": session_name(task)}

    async def _execute(self, task: Task, running: _Running) -> None:
        opts = self._opts
        logger = opts.logger
        spec = self._specs[task.id] if task.kind != "task" else None
        overrides = self._run_overrides(task, spec)
        # The target transcript size before the relay runs: the reply is looked for after it.
        reply_offset = file_size(spec.session.transcript_path) if task.kind == "send" and spec else 0
        run = opts.run or run_claude
        progress_saved_at: int | None = None

        def on_progress(progress: list[str]) -> None:
            nonlocal progress_saved_at
            task.progress = progress
            now = self._now()
            if progress_saved_at is None or now - progress_saved_at >= PROGRESS_SAVE_MS:
                progress_saved_at = now
                self._save(task)

        async def run_once(resume_session_id: str | None) -> RunResult:
            return await run(
                RunOptions(
                    claude=opts.claude,
                    cwd=task.project_path,
                    resume_session_id=resume_session_id,
                    timeout_ms=opts.tasks.timeout_minutes * MINUTE_MS,
                    base_env=opts.base_env if opts.base_env is not None else os.environ,
                    abort=running.abort,
                    kill_grace_ms=opts.kill_grace_ms,
                    on_progress=on_progress,
                    **overrides,
                )
            )

        try:
            if task.kind == "ask" and task.resumed_session_id is None:
                raise RuntimeError("the session id is not valid")
            if task.kind == "task":
                # ask and send keep the strict, empty MCP config of their overrides in every mode.
                overrides["access"] = await asyncio.to_thread(
                    task_access,
                    opts.claude,
                    project_path=task.project_path,
                    public_url=opts.public_url,
                    mcp_config_path=self._mcp_config_path,
                    home_dir=opts.home_dir,
                )
            result = await run_once(task.resumed_session_id)
            if (
                task.kind == "task"
                and not result.ok
                and result.resume_missing
                and task.resumed_session_id
                and not running.abort.is_set()
            ):
                logger.warn("resume failed, retrying without --resume", {"taskId": task.id, "project": task.project})
                task.resumed_session_id = None
                task.progress = []
                result = await run_once(None)
        except Exception as err:
            result = RunResult(progress=task.progress, error=str(err))

        if task.kind == "ask" and not result.ok and result.resume_missing:
            result = dataclasses.replace(result, error="the session transcript was not found")
        if task.kind == "send" and spec and result.outcome != "aborted" and not running.abort.is_set():
            # Also after a failed relay run: a verified delivery still counts as sent.
            result = await self._await_reply(task, spec, result, reply_offset=reply_offset, running=running)

        self._apply_result(task, result, running)
        self._active.pop(task.id, None)
        self._specs.pop(task.id, None)
        self._save(task)
        logger.info(
            "task finished",
            {
                "taskId": task.id,
                "project": task.project,
                "status": task.status,
                "durationMs": result.duration_ms,
                "costUsd": result.cost_usd,
                "exitCode": result.exit_code,
            },
        )
        if result.dropped_lines:
            logger.warn("task output lines too long, dropped", {"taskId": task.id, "count": result.dropped_lines})
        if not result.ok and result.stderr_tail:
            # stderr may quote the prompt or file contents: its text is logged only with logPrompts.
            stderr_fields: dict[str, Any] = (
                {"stderrTail": _clip_tail(result.stderr_tail, STDERR_LOG_CHARS)}
                if opts.log_prompts
                else {"stderrChars": len(result.stderr_tail)}
            )
            logger.warn("task stderr", {"taskId": task.id, **stderr_fields})
        self._finished(task)
        self._pump()

    def _apply_result(self, task: Task, result: RunResult, running: _Running) -> None:
        """Copies the run result into the task, sets its final status and remembers the project session."""
        # A run flagged resume-missing reports the requested (missing) session id: never keep it.
        session_id = (
            None
            if result.resume_missing
            else valid_session_id(result.session_id, self._opts.logger, "run result", task.id)
        )
        task.session_id = session_id
        task.result = result.result
        task.progress = result.progress
        task.cost_usd = result.cost_usd
        task.duration_ms = result.duration_ms
        task.num_turns = result.num_turns
        task.permission_denials = len(result.permission_denials)
        task.exit_code = result.exit_code
        task.finished_at = self._now_iso()
        if result.ok:
            task.status = "done"
            task.error = None
        elif result.outcome == "timeout":
            task.status = "timeout"
            task.error = result.error
        elif result.outcome == "aborted":
            task.status = "failed" if running.cancel_reason else "cancelled"
            task.error = running.cancel_reason or "cancelled"
        else:
            task.status = "failed"
            task.error = result.error or "failed"

        if session_id and task.kind == "task":
            self._projects.projects[task.project_path] = ProjectState(
                name=task.project, last_session_id=session_id, last_activity_at=task.finished_at
            )
            self._save_projects()

    async def _await_reply(
        self, task: Task, spec: _SessionSpec, relay: RunResult, *, reply_offset: int, running: _Running
    ) -> RunResult:
        """send_message_to_session after the relay run: checks that it sent the message, then frees the
        run slot (no process runs while waiting) and follows the target transcript for the reply.
        A relay run that failed afterwards (an error, max turns, no clear answer) still counts as sent
        when its one SendMessage to the target with the original text got a successful result. An
        explicit FAILED answer always wins: the message then counts as not delivered."""
        name = spec.session.name
        verdict = _relay_verdict(relay, spec.address or "", task.prompt)
        if isinstance(verdict, _RawFailure):
            return relay
        if isinstance(verdict, _NotDelivered):
            return dataclasses.replace(
                relay, ok=False, outcome="error", error=f"could not deliver the message to {name}: {verdict.reason}"
            )
        if not verdict.confirmed:
            self._opts.logger.warn(
                "relay run did not confirm, but the message was delivered",
                {"taskId": task.id, "outcome": relay.outcome},
            )
            relay = dataclasses.replace(relay, ok=True, outcome="success", error=None)
        # Free the concurrency slot; keep the session lock so replies cannot get mixed up.
        running.holds_slot = False
        self._pump()
        task.progress = ["message sent, waiting for it to be read"]
        self._save(task)
        watch = self._opts.watch_reply or watch_for_reply

        def on_arrived() -> None:
            task.progress = [f"{name} is working on the message"]
            self._save(task)

        mins = (self._opts.sessions or SessionsConfig()).reply_wait_minutes
        try:
            reply = await watch(
                ReplyWatchOptions(
                    transcript_path=spec.session.transcript_path,
                    offset=reply_offset,
                    from_name=_relay_sender(task.id),
                    timeout_ms=mins * MINUTE_MS,
                    abort=running.abort,
                    poll_ms=self._opts.reply_poll_ms,
                    on_arrived=on_arrived,
                )
            )
        except Exception as err:
            return dataclasses.replace(
                relay,
                ok=False,
                outcome="error",
                error=f"the message was sent, but reading the reply failed: {err}",
            )
        waited = f"{mins:g} minute{'' if mins == 1 else 's'}"
        if reply.aborted:
            return dataclasses.replace(relay, ok=False, outcome="aborted", error="cancelled")
        if not reply.arrived:
            return dataclasses.replace(
                relay,
                result=f"The message was sent, but {name} has not picked it up within {waited}. It may be busy, or it may be waiting for the owner to approve the message in the desktop app because it runs in a different permission mode.",
            )
        if not reply.finished:
            partial = f" Its latest words: {reply.reply}" if reply.reply else ""
            return dataclasses.replace(
                relay, result=f"{name} received the message and is still working on it after {waited}.{partial}"
            )
        return dataclasses.replace(
            relay,
            result=f"{name} replied: {reply.reply}" if reply.reply else f"{name} read the message but wrote no reply.",
        )

    def known_session_ids(self) -> set[str]:
        """Session ids the server's own runs created or used (left out of list_sessions)."""
        ids: set[str] = set()
        for task in self._tasks.values():
            if task.session_id:
                ids.add(task.session_id)
            if task.kind != "ask" and task.resumed_session_id:
                ids.add(task.resumed_session_id)
            if task.remote_session_id:
                ids.add(task.remote_session_id)
        ids.update(state.last_session_id for state in self._projects.projects.values() if state.last_session_id)
        return ids

    def session_busy(self, session_id: str, *, exclude_task: int | None = None) -> bool:
        """True while a queued or running task (other than `exclude_task`) uses the session or may
        still resume it (a queued run_task in a project whose last session it is)."""
        for task in self._tasks.values():
            if is_terminal(task.status) or task.id == exclude_task:
                continue
            if session_id in (task.session_id, task.resumed_session_id, task.target_session_id):
                return True
            if task.status == "queued" and task.kind == "task" and not task.new_session:
                state = self._projects.projects.get(task.project_path)
                if state is not None and state.last_session_id == session_id:
                    return True
        return False

    def set_remote_session(
        self, task_id: int, session_id: str, *, bg_id: str | None = None, launched: bool = False
    ) -> None:
        """Remembers the session opened for Remote Control for a task (persisted in its file), and
        the id8 the launcher printed (what `claude stop` takes) when given. `launched`: the server
        started that session, so a desktop handoff of the task no longer holds the latest conversation."""
        task = self._tasks.get(task_id)
        if task is None:
            return
        bg = valid_bg_id(bg_id) if bg_id is not None else task.bg_id
        clear = launched and task.handed_over_at is not None
        if task.remote_session_id == session_id and task.bg_id == bg and not clear:
            return
        if valid_session_id(session_id, self._opts.logger, "remote control", task_id) is None:
            return
        task.remote_session_id = session_id
        task.bg_id = bg
        if clear:
            task.handed_over_at = task.desktop_session_id = None
        self._save(task)

    def handed_over(self, task_id: int, session_id: str) -> None:
        """The task's stopped session `session_id` was handed over to the Claude desktop app."""
        task = self._tasks.get(task_id)
        if task is None or valid_session_id(session_id, self._opts.logger, "desktop handoff", task_id) is None:
            return
        task.handed_over_at = self._now_iso()
        task.desktop_session_id = session_id
        self._save(task)

    def remote_session_names(self) -> dict[str, str]:
        """Names of the sessions opened for Remote Control, by session id (`session_name` of their task)."""
        return {sid: session_name(t) for sid, t in self.remote_session_tasks().items()}

    def remote_session_tasks(self) -> dict[str, Task]:
        """The tasks (copies) of the sessions opened for Remote Control, by session id."""
        return {t.remote_session_id: _copy(t) for t in self._tasks.values() if t.remote_session_id}

    def _wake(self, task_id: int) -> bool:
        """Wakes the waiters of a task; True when there were any."""
        waiters = self._waiters.pop(task_id, None)
        for event in waiters or ():
            event.set()
        return bool(waiters)

    def _finished(self, task: Task) -> None:
        someone_waiting = self._wake(task.id)
        notifier = self._opts.notifier
        if not someone_waiting and notifier is not None and not self._shutting_down:
            note = NotifyTask(id=task.id, project=task.project, status=task.status, result=task.result)
            sent = asyncio.get_running_loop().create_task(self._notify(notifier, note))
            self._notifications.add(sent)
            sent.add_done_callback(self._notifications.discard)

    @staticmethod
    async def _notify(notifier: Notifier, note: NotifyTask) -> None:
        with contextlib.suppress(Exception):  # notifiers never raise; a broken one must not matter
            await notifier(note)

    def _trim_memory(self) -> None:
        if len(self._tasks) <= MEMORY_KEEP:
            return
        for task_id, task in list(self._tasks.items()):
            if len(self._tasks) <= MEMORY_KEEP:
                break
            if is_terminal(task.status):
                del self._tasks[task_id]

    async def wait(self, task_id: int, ms: float) -> Task | None:
        """Waits up to `ms` for the task to finish. Returns a copy of its state, None for an unknown id."""
        task = self._tasks.get(task_id)
        if task is None:
            return None
        if is_terminal(task.status) or ms <= 0:
            return _copy(task)
        event = asyncio.Event()
        waiters = self._waiters.setdefault(task_id, set())
        waiters.add(event)
        try:
            await asyncio.wait_for(event.wait(), ms / SECOND_MS)
        except TimeoutError:
            pass
        finally:
            waiters.discard(event)
            if not waiters and self._waiters.get(task_id) is waiters:
                del self._waiters[task_id]
        return self.get(task_id)

    async def wait_all(self, task_ids: Sequence[int], ms: float) -> list[Task | None]:
        """Waits up to `ms` in total for all tasks."""
        await asyncio.gather(*(self.wait(task_id, ms) for task_id in task_ids))
        return [self.get(task_id) for task_id in task_ids]

    def get(self, task_id: int) -> Task | None:
        task = self._tasks.get(task_id)
        return _copy(task) if task is not None else None

    def latest(self, project: str | None = None) -> Task | None:
        """The most recent task, optionally for one project (by name)."""
        best: Task | None = None
        for task in self._tasks.values():
            if project and task.project != project:
                continue
            if best is None or task.id > best.id:
                best = task
        return _copy(best) if best is not None else None

    def list_tasks(self, project: str | None = None, limit: int | None = None) -> list[Task]:
        """Recent tasks, newest first. Default limit 10, clamped to 1..50."""
        clamped = max(1, min(limit if limit is not None else 10, LOAD_LAST))
        tasks = [task for task in self._tasks.values() if not project or task.project == project]
        tasks.sort(key=lambda task: task.id, reverse=True)
        return [_copy(task) for task in tasks[:clamped]]

    def queue_position(self, task_id: int) -> int | None:
        """Position in the queue (1-based), or None when not queued."""
        return self._queue.index(task_id) + 1 if task_id in self._queue else None

    async def cancel(self, task_id: int) -> CancelResult:
        """Cancels a queued task at once; aborts a running one and waits up to 5 s for it to stop."""
        task = self._tasks.get(task_id)
        if task is None:
            return CancelFailed(reason="not_found")
        if is_terminal(task.status):
            return CancelFailed(reason="already_finished", task=_copy(task))
        if task_id in self._queue:
            self._queue.remove(task_id)
            self._opts.logger.info("task cancelled", {"taskId": task_id, "wasRunning": False})
            self._end_queued(task, "cancelled", "cancelled")
            return Cancelled(task=_copy(task))
        running = self._active.get(task_id)
        if running is not None:
            self._opts.logger.info("task cancelled", {"taskId": task_id, "wasRunning": True})
            running.abort.set()
            after = await self.wait(task_id, 5 * SECOND_MS)
            return Cancelled(task=after or _copy(task))
        return CancelFailed(reason="already_finished", task=_copy(task))

    def _end_queued(self, task: Task, status: TaskStatus, error: str) -> None:
        """Ends a task taken out of the queue (it never ran)."""
        self._specs.pop(task.id, None)
        task.status = status
        task.error = error
        task.finished_at = self._now_iso()
        self._save(task)
        self._wake(task.id)

    async def shutdown(self, reason: str) -> None:
        """Stops accepting tasks, fails queued ones, aborts running ones and waits for them and for
        pending writes."""
        self._shutting_down = True
        if self._prune_handle is not None:
            self._prune_handle.cancel()
            self._prune_handle = None
        queued, self._queue = self._queue, []
        for task_id in queued:
            self._end_queued(self._tasks[task_id], "failed", reason)
        for running in self._active.values():
            running.cancel_reason = reason
            running.abort.set()
        # Task file writes are synchronous, so nothing else is pending once the runs are done.
        await asyncio.gather(*list(self._runs), return_exceptions=True)


def session_state(live: LiveSession, now: int) -> SessionState:
    """The registry's `status` ("busy", "waiting" with `waitingFor`, "idle") as a SessionState;
    "disconnected" when the process lives but has lost Remote Control (`rc_disconnected`)."""
    if rc_disconnected(live, now):
        return "disconnected"
    if live.status == "idle":
        return "idle"
    if live.status == "waiting":
        return "approval" if "permission" in (live.waiting_for or "").lower() else "input"
    return "working"


def _relay_sender(task_id: int) -> str:
    """Display name of a send task's relay run, which the reply watcher looks for."""
    return f"vcm-relay-{task_id}"


@dataclass(frozen=True)
class _Delivered:
    confirmed: bool
    """The relay run also succeeded and answered SENT; otherwise the verified send rescues it."""


@dataclass(frozen=True)
class _NotDelivered:
    reason: str
    """Spoken-style, without the message text."""


@dataclass(frozen=True)
class _RawFailure:
    """The relay run failed and nothing proves a delivery: its own error stands."""


def _relay_verdict(relay: RunResult, address: str, message: str) -> _Delivered | _NotDelivered | _RawFailure:
    """Whether the relay delivered the message. An explicit FAILED answer always means not delivered;
    a run that failed (an error, max turns, no clear answer) still counts as delivered when its one
    SendMessage to the target with the original text got a successful tool_result."""
    send = _relay_send(relay, address, message)
    answer = (relay.result or "").strip()
    verified = isinstance(send, ToolUse) and send.is_error is False and not _FAILED_PREFIX_RE.match(answer)
    if not relay.ok and not verified:
        return _RawFailure()
    if isinstance(send, str):
        return _NotDelivered(send)
    sent_answer = _SENT_RE.match(answer) is not None
    if verified:
        return _Delivered(confirmed=relay.ok and sent_answer)
    if not sent_answer:
        why = _FAILED_PREFIX_RE.sub("", answer, count=1) or "the relay did not confirm sending"
        return _NotDelivered(why[:200])
    return _Delivered(confirmed=True)


def _relay_send(relay: RunResult, address: str, message: str) -> ToolUse | str:
    """The relay run's one SendMessage with the target address and the original text, or why there
    is none. Denied or failed attempts do not count as sent. Spoken-style, without the message text."""
    denied = {
        d["tool_use_id"]
        for d in relay.permission_denials
        if isinstance(d, dict) and isinstance(d.get("tool_use_id"), str)
    }
    attempts = [use for use in relay.tool_uses if use.name == "SendMessage"]
    sends = [use for use in attempts if use.is_error is not True and (use.id is None or use.id not in denied)]
    if not sends:
        if relay.permission_denials:
            return "the relay was not allowed to send it, so check the permissions"
        if attempts:
            return "the relay tried but could not send it"
        return "the relay did not send it"
    if len(sends) > 1:
        return f"the relay sent it {len(sends)} times instead of once, so check the session"
    send = sends[0]
    if send.input.get("to") != address:
        return "the relay addressed it to a different session, so check your sessions"
    text = send.input.get("message")
    if not isinstance(text, str) or text.strip() != message.strip():
        return "the relay changed the text, so the session may have received a different message"
    return send


def _name_list(names: Sequence[str], limit: int = 15) -> str:
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _format_failure(item: StartFailed) -> str:
    match item.reason:
        case "not_found":
            return (
                f'No project matches "{item.project}". Available projects: {_name_list(item.candidates)}.'
                if item.candidates
                else f'No project matches "{item.project}" and no projects are available.'
            )
        case "ambiguous":
            return f'"{item.project}" matches several projects: {_name_list(item.candidates)}. Which one?'
        case "empty_prompt":
            return "The task is empty. Say what Claude Code should do."
        case "prompt_too_long":
            return f"The task is too long (limit {MAX_PROMPT_CHARS} characters)."
        case "queue_full":
            return "Too many tasks are waiting. Try again later or cancel a task."
        case "shutting_down":
            return "The server is shutting down. Try again in a minute."
        case "state_error":
            return "The server could not save the task. Nothing was started."
        case "session_not_running":
            return f"Session {item.session} is not running, so it cannot receive a message. Ask about it with ask_session_readonly instead."
        case "session_unreachable":
            return f"Session {item.session} is running but does not accept messages from other sessions."


NAME_MARKER = "voice"
"""Marks the sessions this server names: `<label> (voice <task id>)`. Never translated."""
LEGACY_NAME_PREFIX = "vcm-"
"""Prefix of the session names of tasks created before titles existed (`vcm-<slug>-<id>`)."""
TITLE_MAX_CHARS = 60

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def sanitize_title(value: str | None) -> str | None:
    """A voice-made title fit for argv and display: control and format characters (newlines,
    bidi controls) become spaces, whitespace runs collapse, leading `-` (would read as a flag) and
    spaces are stripped, and it is cut to TITLE_MAX_CHARS at a word boundary when possible. None
    when nothing is left."""
    if value is None:
        return None
    chars = [" " if unicodedata.category(c) in ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp") else c for c in value]
    text = WHITESPACE_RE.sub(" ", "".join(chars)).strip().lstrip("- ").strip()
    if len(text) > TITLE_MAX_CHARS:
        cut = text[: TITLE_MAX_CHARS + 1]
        space = cut.rfind(" ")
        text = (cut[:space] if space >= TITLE_MAX_CHARS // 2 else text[:TITLE_MAX_CHARS]).rstrip()
    return text or None


def format_session_name(label: str, task_id: int) -> str:
    """`<label> (voice <task id>)`, the label sanitized like a title ("project" when nothing is left)."""
    return f"{sanitize_title(label) or 'project'} ({NAME_MARKER} {task_id})"


def _legacy_session_name(task: Task) -> str:
    slug = _SLUG_RE.sub("-", normalize_name(task.project)).strip("-")[:24].strip("-") or "project"
    return f"{LEGACY_NAME_PREFIX}{slug}-{task.id}"


def session_name(task: Task) -> str:
    """The task's session name (`Task.name`): `<title> (voice <id>)`, or `<project> (voice <id>)`
    without a title, never derived from the prompt. A task file from before titles existed keeps
    its old `vcm-<project>-<id>` name when it already had a Remote Control session (that is the name
    the app shows), else gets the new format."""
    if task.name:
        return task.name
    if task.remote_control or task.remote_session_id:
        return _legacy_session_name(task)
    return format_session_name(task.project, task.id)


def task_head(task: Task) -> str:
    """How a spoken answer names a task: "Task 3 for session billing-fix" or "Task 3 in alpha"."""
    if task.target_session_name:
        return f"Task {task.id} for session {task.target_session_name}"
    if task.kind == "remote":
        return f"Task {task.id} ({task.title or task.prompt})"
    return f"Task {task.id} in {task.project}"


def format_for_voice(item: Task | StartResult, *, now: int | None = None, queue_position: int | None = None) -> str:
    """Short plain-text tool response for a task or a failed start."""
    if isinstance(item, Started):
        return format_for_voice(item.task, now=now, queue_position=queue_position)
    if isinstance(item, StartFailed):
        return _format_failure(item)

    task = item
    if task.remote_control and task.status in ("running", "done", "cancelled"):
        return _format_session_task(task)
    if task.session_answer and is_terminal(task.status):
        return _format_continued(task)
    current = now if now is not None else now_ms()
    head = task_head(task)
    if task.resume_skipped:
        head += " (in a new conversation, because the previous one is open in the Claude app)"
    match task.status:
        case "queued":
            position = f", position {queue_position}" if queue_position else ""
            return f"{head} is waiting in the queue{position}. Ask for task {task.id} later."
        case "running":
            start = parse_iso(task.started_at)
            mins = elapsed_minutes(current - start) if start is not None else None
            if mins is None:
                time = ""
            elif mins < 1:
                time = " for less than a minute"
            else:
                time = f" for {plural(mins, 'minute')}"
            steps = f" Latest steps: {'; '.join(task.progress)}." if task.progress else ""
            return f"{head} is still running{time}.{steps} Ask for task {task.id} again in a while."
        case "done":
            if not task.result or not task.result.strip():
                return f"{head} is done, without a text answer."
            answer = task.result.strip()
            note = (
                " (Answer shortened; the full text is saved in the task file.)"
                if len(answer) > VOICE_ANSWER_LIMIT
                else ""
            )
            answer = clip(answer, VOICE_ANSWER_LIMIT)
            return f"{head} is done. {answer}{note}"
        case "timeout":
            return f"{head} timed out and was stopped."
        case "cancelled":
            return f"{head} was cancelled."
        case "failed":
            reason = clip(task.error, 300) if task.error else "unknown error"
            text = f"{head} failed: {reason}."
            return text[:-1] if text.endswith("..") else text


LIST_ANSWER_LIMIT = 200
"""How much of a reopened session's answer list_tasks quotes."""


def _format_continued(task: Task) -> str:
    """A finished task the owner continued in its reopened session: that session's latest answer."""
    answer = (task.session_answer or "").strip()
    note = " (Answer shortened; the full text is in the Claude app.)" if len(answer) > VOICE_ANSWER_LIMIT else ""
    return (
        f"{task_head(task)} was continued in session {session_name(task)} in the Claude app."
        f" Its latest answer there: {clip(answer, VOICE_ANSWER_LIMIT)}{note}"
    )


def continued_note(task: Task) -> str:
    """list_tasks: the latest answer of a finished task's reopened session, short; empty without one."""
    if task.remote_control or not task.session_answer or not is_terminal(task.status):
        return ""
    answer = clip(task.session_answer.strip(), LIST_ANSWER_LIMIT).rstrip(".")
    return f"; latest answer in the Claude app: {answer}"


def _answer(task: Task) -> str:
    """The last answer for a spoken response, clipped (empty without one)."""
    if not task.result or not task.result.strip():
        return ""
    answer = task.result.strip()
    note = " (Answer shortened; the full text is in the Claude app.)" if len(answer) > VOICE_ANSWER_LIMIT else ""
    return f" Its latest answer: {clip(answer, VOICE_ANSWER_LIMIT)}{note}"


def session_task_state(task: Task) -> str:
    """A few words on a session task's state, for list_tasks."""
    name = session_name(task)
    if task.status == "cancelled":
        return f"stopped, session {name}"
    if task.status != "running":
        return f"done, session {name} has ended"
    match task.session_state:
        case "disconnected":
            return f"session {name} lost its connection to the Claude app"
        case "approval":
            return f"waiting for your approval in session {name}"
        case "input":
            return f"waiting for your answer in session {name}"
        case "idle":
            return f"finished its turn in session {name}"
        case _:
            return f"working in session {name}"


def _format_session_task(task: Task) -> str:
    head = task_head(task)
    name = session_name(task)
    app = f"In the Claude app, open Code and pick {name}"
    answer = _answer(task)
    if task.status == "cancelled":
        return f"{head} was stopped; session {name} has ended and its conversation is kept.{answer}"
    if task.status == "done":
        # A handed-over session gets its hint from the caller (where it is, how to continue).
        hint = "" if task.handed_over_at else f" To continue it, ask to resume task {task.id} in the Claude app."
        return f"{head} is done; session {name} has ended.{answer}{hint}"
    match task.session_state:
        case "disconnected":
            return (
                f"{head}: session {name} lost its connection to the Claude app and no longer shows there.{answer}"
                f" Ask to resume task {task.id} in the Claude app to reconnect it, or cancel task {task.id} to close it."
            )
        case "approval":
            return f"{head} is waiting for your approval in session {name}. {app} to approve it.{answer}"
        case "input":
            return f"{head} is waiting for your answer in session {name}. {app} to reply.{answer}"
        case "idle":
            body = answer or " It wrote no text answer."
            return f"{head} finished its turn in session {name}.{body} {app} to continue it."
        case _:
            return f"{head} is working in session {name}.{answer} {app} to watch it."
