"""Requests to and responses from the dispatcher (docs/SWITCHBOARD.md, "The dispatcher").

The command string is built only here, from a fixed verb set and arguments that match a
shell-neutral charset (`ARG_RE`), each also `shlex.quote`d. Responses are parsed strictly and with
size caps: a remote machine is not trusted to answer sensibly.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Sequence
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from ..usage.records import UsageBatch, UsageStreamError, parse_lines

DISPATCHER_PROGRAM = "switchboard-remote"
VERBS: tuple[str, ...] = ("version", "list", "capture", "send", "usage", "keys", "resize", "health", "events")
ARG_RE = re.compile(r"[A-Za-z0-9%_.-]{1,64}", re.ASCII)
PANE_ID_RE = re.compile(r"%[0-9]{1,6}", re.ASCII)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_USAGE_RESPONSE_BYTES = 64 * 1024 * 1024
"""The streamed answer of `usage` (JSON lines)."""
USAGE_TIMEOUT_SECONDS = 120
"""Wall clock of one `usage` call (background only)."""
MAX_PANES = 512
NAME_MAX = 64
WAITING_MAX = 200
CAPTURE_MAX_CHARS = 64 * 1024


class BadRequestError(ValueError):
    """A request the hub must never send (unknown verb, argument outside the charset)."""


def request_tokens(verb: str, args: Sequence[str]) -> list[str]:
    """`[verb, *args]` after checking the verb and every argument."""
    if verb not in VERBS:
        raise BadRequestError(f"unknown verb {verb!r}")
    for arg in args:
        if not ARG_RE.fullmatch(arg) or arg.startswith("-"):
            # A leading - could read as an option on an explicit-path machine.
            raise BadRequestError("argument outside the allowed characters")
    return [verb, *args]


def build_command(dispatcher: str, verb: str, args: Sequence[str]) -> str:
    """The SSH command string: `switchboard-remote <verb> <args>` for a forced command, or
    `<path> <verb> <args>` for an explicit dispatcher path."""
    program = DISPATCHER_PROGRAM if dispatcher == "forced" else dispatcher
    return shlex.join([program, *request_tokens(verb, args)])


# --- responses -------------------------------------------------------------------------------


def _capped(limit: int) -> BeforeValidator:
    def cap(value: object) -> object:
        return value[:limit] if isinstance(value, str) else value

    return BeforeValidator(cap)


Name = Annotated[str, _capped(NAME_MAX)]


class _Response(BaseModel):
    """Unknown fields are ignored (a newer dispatcher may add some); strings are capped."""

    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)


class ClaudeInfo(_Response):
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    name: Name | None = None
    status: str | None = Field(default=None, max_length=16)
    waiting_for: Annotated[str | None, _capped(WAITING_MAX)] = Field(default=None, alias="waitingFor")
    status_updated_at: int | None = Field(default=None, alias="statusUpdatedAt")
    started_at: int | None = Field(default=None, alias="startedAt")
    cwd_name: Name | None = Field(default=None, alias="cwdName")


class PaneInfo(_Response):
    session: Name
    window_index: int | None = Field(default=None, alias="windowIndex")
    window_name: Name = Field(default="", alias="windowName")
    pane_index: int | None = Field(default=None, alias="paneIndex")
    pane_id: Annotated[str, Field(pattern=r"^%[0-9]{1,6}$")] = Field(alias="paneId")
    pane_pid: int = Field(alias="panePid", ge=0)
    command: Name = ""
    active: bool = False
    attached: bool = False
    activity: int | None = None
    width: int | None = None
    """Pane size in columns and rows (dispatcher 0.5.0 and newer)."""
    height: int | None = None
    claude: ClaudeInfo | None = None


class ListResponse(_Response):
    panes: Annotated[list[PaneInfo], Field(max_length=MAX_PANES)]
    truncated: bool = False


class CaptureResponse(_Response):
    text: Annotated[str, _capped(CAPTURE_MAX_CHARS)]
    pane_id: str = Field(alias="paneId")
    session: Name
    window: int | None = None
    pane_width: int | None = Field(default=None, alias="paneWidth")
    pane_height: int | None = Field(default=None, alias="paneHeight")
    alternate_on: bool = Field(default=False, alias="alternateOn")
    """The pane shows its alternate screen (a full-screen program), which has no scrollback: only
    the visible rows can be captured (dispatcher 0.5.0 and newer)."""


class VersionResponse(_Response):
    version: Name
    verbs: Annotated[list[Name], Field(max_length=32)]
    allow_send: bool = Field(default=False, alias="allowSend")
    allow_usage: bool = Field(default=False, alias="allowUsage")


class SendResponse(_Response):
    sent: bool
    bytes: int = Field(ge=0)
    enter: bool = True
    """False for a permission answer: the choice number was typed, Enter was not pressed."""


class KeysResponse(_Response):
    sent: bool
    keys: int = Field(ge=0)


class ResizeResponse(_Response):
    resized: bool
    columns: int = Field(ge=0)
    rows: int = Field(ge=0)


HEALTH_NAMES = ("root", "home", "claudeHome", "windows")
"""The file systems `health` reports, by label (never by path)."""
MAX_BYTES = 2**63


class FilesystemInfo(_Response):
    name: Annotated[str, _capped(32)]
    """One of HEALTH_NAMES; others (a newer dispatcher) are ignored by the hub."""
    total_bytes: int | None = Field(default=None, alias="totalBytes", ge=0, le=MAX_BYTES)
    free_bytes: int | None = Field(default=None, alias="freeBytes", ge=0, le=MAX_BYTES)
    read_only: bool = Field(default=False, alias="readOnly")
    same_as: Annotated[str | None, _capped(32)] = Field(default=None, alias="sameAs")
    error: Annotated[str | None, _capped(32)] = None
    """`unavailable`, `timeout` or `not_mounted` (the Windows drive)."""


class HealthResponse(_Response):
    """`health` (dispatcher 0.9.0): numbers and fixed labels only."""

    wsl: bool = False
    filesystems: Annotated[list[FilesystemInfo], Field(max_length=8)] = Field(default_factory=list)
    load: Annotated[list[Annotated[float, Field(ge=0, le=1e6)]], Field(max_length=3)] | None = None
    memory_total_bytes: int | None = Field(default=None, alias="memoryTotalBytes", ge=0, le=MAX_BYTES)
    memory_available_bytes: int | None = Field(default=None, alias="memoryAvailableBytes", ge=0, le=MAX_BYTES)
    uptime_seconds: float | None = Field(default=None, alias="uptimeSeconds", ge=0, le=1e12)


EVENT_KINDS = ("approval_needed", "question", "finished", "failed", "pr_opened", "owner_note")
EVENT_LEVELS = ("info", "warning", "urgent")
EVENT_ID_PATTERN = r"^[0-9]{13}-[0-9]{10}-[0-9a-f]{8}$"
MAX_EVENTS = 16


class SessionEvent(_Response):
    """One event of a machine's `switchboard-hook` spool (`events`, dispatcher 0.11.0). Everything in
    it was written by the machine's sessions: untrusted, checked again by `remote/events.py`. A
    `machine` field, should one appear, is ignored: the machine is the link the answer came on."""

    id: Annotated[str, Field(pattern=EVENT_ID_PATTERN)]
    kind: str = Field(max_length=32)
    time: int = Field(ge=0, le=10**13)
    session_id: str | None = Field(default=None, alias="sessionId", max_length=64)
    session_name: Name | None = Field(default=None, alias="sessionName")
    project: Name | None = None
    pane: str | None = Field(default=None, max_length=8)
    tmux_session: Name | None = Field(default=None, alias="tmuxSession")
    message: Annotated[str | None, _capped(500)] = None
    url: str | None = Field(default=None, max_length=300)
    level: str | None = Field(default=None, max_length=16)


class EventsResponse(_Response):
    events: Annotated[list[SessionEvent], Field(max_length=MAX_EVENTS)] = Field(default_factory=list)
    more: bool = False
    pending: int = Field(default=0, ge=0, le=10**6)
    full: bool = False
    bad: int = Field(default=0, ge=0, le=10**6)


class UsageResponse(BaseModel):
    """The parsed JSON lines of `usage` (built with `model_construct`, already checked)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    batch: UsageBatch


def response_cap(verb: str) -> int:
    return MAX_USAGE_RESPONSE_BYTES if verb == "usage" else MAX_RESPONSE_BYTES


RESPONSE_MODELS: dict[str, type[_Response]] = {
    "version": VersionResponse,
    "list": ListResponse,
    "capture": CaptureResponse,
    "send": SendResponse,
    "keys": KeysResponse,
    "resize": ResizeResponse,
    "health": HealthResponse,
    "events": EventsResponse,
}


class ResponseError(ValueError):
    """Not a dispatcher response: `code` is `dispatcher_missing` (no JSON at all) or `bad_response`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_response(verb: str, stdout: bytes) -> BaseModel | str:
    """The response model, or the dispatcher's own error code (a refusal). Raises ResponseError."""
    if len(stdout) > response_cap(verb):
        raise ResponseError("bad_response")
    rest: list[bytes] = []
    if verb == "usage":
        first, _, tail = stdout.lstrip().partition(b"\n")
        stdout, rest = first, tail.split(b"\n")
    text = stdout.decode("utf-8", "replace").strip()
    try:
        data: Any = json.loads(text)
    except ValueError:
        raise ResponseError("dispatcher_missing" if not text.startswith("{") else "bad_response") from None
    if not isinstance(data, dict) or data.get("protocol") != 1 or not isinstance(data.get("ok"), bool):
        raise ResponseError("bad_response")
    if not data["ok"]:
        error = data.get("error")
        return error if isinstance(error, str) and ARG_RE.fullmatch(error) else "other"
    if verb == "usage":
        if data.get("stream") != "usage":
            raise ResponseError("bad_response")
        try:
            return UsageResponse.model_construct(batch=parse_lines(rest))
        except UsageStreamError:
            raise ResponseError("bad_response") from None
    try:
        return RESPONSE_MODELS[verb].model_validate(data)
    except ValidationError:
        raise ResponseError("bad_response") from None
