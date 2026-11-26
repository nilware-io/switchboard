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

DISPATCHER_PROGRAM = "switchboard-remote"
VERBS: tuple[str, ...] = ("version", "list", "capture", "send")
ARG_RE = re.compile(r"[A-Za-z0-9%_.-]{1,64}", re.ASCII)
PANE_ID_RE = re.compile(r"%[0-9]{1,6}", re.ASCII)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
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
    claude: ClaudeInfo | None = None


class ListResponse(_Response):
    panes: Annotated[list[PaneInfo], Field(max_length=MAX_PANES)]
    truncated: bool = False


class CaptureResponse(_Response):
    text: Annotated[str, _capped(CAPTURE_MAX_CHARS)]
    pane_id: str = Field(alias="paneId")
    session: Name
    window: int | None = None


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


RESPONSE_MODELS: dict[str, type[_Response]] = {
    "version": VersionResponse,
    "list": ListResponse,
    "capture": CaptureResponse,
    "send": SendResponse,
}


class ResponseError(ValueError):
    """Not a dispatcher response: `code` is `dispatcher_missing` (no JSON at all) or `bad_response`."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def parse_response(verb: str, stdout: bytes) -> _Response | str:
    """The response model, or the dispatcher's own error code (a refusal). Raises ResponseError."""
    if len(stdout) > MAX_RESPONSE_BYTES:
        raise ResponseError("bad_response")
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
    try:
        return RESPONSE_MODELS[verb].model_validate(data)
    except ValidationError:
        raise ResponseError("bad_response") from None
