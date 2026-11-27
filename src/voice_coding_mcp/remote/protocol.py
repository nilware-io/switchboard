"""Requests to and responses from the dispatcher (docs/SWITCHBOARD.md, "The dispatcher").

The command string is built only here, from a fixed verb set and arguments that match a
shell-neutral charset (`ARG_RE`), each also `shlex.quote`d. Responses are parsed strictly and with
size caps: a remote machine is not trusted to answer sensibly.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import shlex
from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, ValidationError

from ..usage.records import UsageBatch, UsageStreamError, parse_lines

DISPATCHER_PROGRAM = "switchboard-remote"
VERBS: tuple[str, ...] = ("version", "list", "capture", "send", "usage", "keys", "resize", "attachments", "attachment")
ARG_RE = re.compile(r"[A-Za-z0-9%_.-]{1,64}", re.ASCII)
PANE_ID_RE = re.compile(r"%[0-9]{1,6}", re.ASCII)
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_USAGE_RESPONSE_BYTES = 64 * 1024 * 1024
"""The streamed answer of `usage` (JSON lines)."""
ATTACHMENT_CAPS: dict[str, int] = {"image": 3_750_000, "audio": 8 * 1024 * 1024, "video": 16 * 1024 * 1024}
"""The hub's own size limits per kind (the same as the dispatcher's `ATTACHMENT_CAPS`)."""
ATTACHMENT_CHUNK_BYTES = 1024 * 1024
MAX_ATTACHMENT_CHUNKS = 16
MAX_ATTACHMENT_RESPONSE_BYTES = 24 * 1024 * 1024
"""The streamed answer of `attachment`: the largest file (16 MiB) as base64 in chunk documents."""
MAX_ATTACHMENTS = 50
ATTACHMENT_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
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
    allow_attachments: bool = Field(default=False, alias="allowAttachments")
    """Dispatcher 0.7.0 and newer."""


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


class UsageResponse(BaseModel):
    """The parsed JSON lines of `usage` (built with `model_construct`, already checked)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    batch: UsageBatch


AttachmentKind = Literal["image", "audio", "video"]


class AttachmentInfo(_Response):
    name: Annotated[str, Field(pattern=ATTACHMENT_NAME_RE)]
    kind: AttachmentKind
    mime: Annotated[str, Field(max_length=32)]
    size: int = Field(ge=1)
    mtime: int | None = None
    """Epoch milliseconds."""


class AttachmentsResponse(_Response):
    attachments: Annotated[list[AttachmentInfo], Field(max_length=MAX_ATTACHMENTS)]
    skipped: int = Field(default=0, ge=0)
    """Files in the outbox that are not listed: not media, too large, not a plain file, or past 50."""
    outbox: bool = True
    """False: the machine has no outbox directory."""


class _AttachmentHeader(_Response):
    stream: Literal["attachment"]
    name: Annotated[str, Field(pattern=ATTACHMENT_NAME_RE)]
    kind: AttachmentKind
    mime: Annotated[str, Field(max_length=32)]
    size: int = Field(ge=1)
    mtime: int | None = None
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    chunks: int = Field(ge=1, le=MAX_ATTACHMENT_CHUNKS)


class AttachmentResponse(BaseModel):
    """One attachment as the dispatcher sent it, reassembled and checked against its own header
    (size, chunk count, SHA-256, the hub's size cap). Not yet checked to be media: the tool runs
    `dispatcher.media_check` on `data` before anything reaches the client."""

    model_config = ConfigDict(frozen=True)
    name: str
    kind: AttachmentKind
    mime: str
    mtime: int | None
    data: bytes


def response_cap(verb: str) -> int:
    if verb == "usage":
        return MAX_USAGE_RESPONSE_BYTES
    return MAX_ATTACHMENT_RESPONSE_BYTES if verb == "attachment" else MAX_RESPONSE_BYTES


def parse_attachment(header: dict[str, Any], lines: Sequence[bytes]) -> AttachmentResponse:
    """The attachment from its header document and the chunk lines after it. Raises
    ResponseError("bad_response") for anything but exactly the announced chunks, in order, then
    `{"done": true}`, adding up to the announced size and SHA-256."""
    try:
        head = _AttachmentHeader.model_validate(header)
    except ValidationError:
        raise ResponseError("bad_response") from None
    cap = ATTACHMENT_CAPS[head.kind]
    if head.size > cap or head.chunks != -(-head.size // ATTACHMENT_CHUNK_BYTES):
        raise ResponseError("bad_response")
    documents = [line for line in lines if line.strip()]
    if len(documents) != head.chunks + 1:
        raise ResponseError("bad_response")
    parts: list[bytes] = []
    total = 0
    for index, line in enumerate(documents):
        try:
            data: Any = json.loads(line)
        except ValueError:
            raise ResponseError("bad_response") from None
        if index == head.chunks:
            if data != {"done": True}:
                raise ResponseError("bad_response")
            break
        if not isinstance(data, dict) or set(data) != {"chunk", "data"} or data["chunk"] != index:
            raise ResponseError("bad_response")
        text = data["data"]
        if not isinstance(text, str) or len(text) > 4 * (ATTACHMENT_CHUNK_BYTES // 3 + 1):
            raise ResponseError("bad_response")
        try:
            part = base64.b64decode(text.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise ResponseError("bad_response") from None
        total += len(part)
        if total > head.size or (index < head.chunks - 1 and len(part) != ATTACHMENT_CHUNK_BYTES):
            raise ResponseError("bad_response")
        parts.append(part)
    blob = b"".join(parts)
    if len(blob) != head.size or hashlib.sha256(blob).hexdigest() != head.sha256:
        raise ResponseError("bad_response")
    return AttachmentResponse(name=head.name, kind=head.kind, mime=head.mime, mtime=head.mtime, data=blob)


RESPONSE_MODELS: dict[str, type[_Response]] = {
    "version": VersionResponse,
    "list": ListResponse,
    "capture": CaptureResponse,
    "send": SendResponse,
    "keys": KeysResponse,
    "resize": ResizeResponse,
    "attachments": AttachmentsResponse,
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
    if verb in ("usage", "attachment"):
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
    if verb == "attachment":
        return parse_attachment(data, rest)
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
