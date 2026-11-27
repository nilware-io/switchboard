"""The owner's voice activity: when each Switchboard tool call ran and which project it targeted
(docs/SWITCHBOARD.md, "Timesheet"). One JSON line per call in `stateDir/timesheets/voice.jsonl`
(mode 0600): `{"t": start ms, "e": end ms, "tool": name, "p": project folder name or null}`. No
prompt, no answer, no screen text, no session name.

A tool implementation names the project it targeted with `attribute(folder)` while the call runs (a
context variable carries it); `VoiceLog.wrap` records the call when it returns.
"""

from __future__ import annotations

import contextvars
import functools
import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from ..clock import now_ms
from ..json_file import ensure_dir
from ..log import Logger

VOICE_FILE = "voice.jsonl"
MAX_VOICE_BYTES = 64 * 1024 * 1024
_NAME_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f/]{1,64}")
_TOOL_RE = re.compile(r"[a-z_]{1,64}")
_current: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("timesheet_attribution", default=None)

P = ParamSpec("P")
R = TypeVar("R")


def timesheet_dir(state_dir: str) -> str:
    return os.path.join(state_dir, "timesheets")


def voice_path(state_dir: str) -> str:
    return os.path.join(timesheet_dir(state_dir), VOICE_FILE)


def attribute(folder: str | None) -> None:
    """Names the project folder the running tool call targets (several calls: several projects)."""
    names = _current.get()
    if names is None or not folder:
        return
    name = os.path.basename(folder.rstrip("/")) if "/" in folder else folder
    if _NAME_RE.fullmatch(name) and name not in names:
        names.append(name)


@dataclass(frozen=True)
class VoiceCall:
    start_ms: int
    end_ms: int
    tool: str
    project: str | None


class VoiceLog:
    def __init__(self, state_dir: str, logger: Logger, *, now: Callable[[], int] | None = None) -> None:
        self._state_dir = state_dir
        self._logger = logger
        self._now = now or now_ms

    @property
    def path(self) -> str:
        return voice_path(self._state_dir)

    def append(self, call: VoiceCall) -> None:
        line = {"t": call.start_ms, "e": call.end_ms, "tool": call.tool, "p": call.project}
        data = (json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            ensure_dir(timesheet_dir(self._state_dir))
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab") as handle:
                handle.write(data)
        except OSError as err:
            self._logger.warn("voice activity not recorded", {"error": type(err).__name__})

    def wrap(self, tool: str, fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        """`fn` recording one line per call: its start and end, and every project it attributed (one
        line each), or one line without a project."""

        @functools.wraps(fn)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            names: list[str] = []
            token = _current.set(names)
            start = self._now()
            try:
                return await fn(*args, **kwargs)
            finally:
                _current.reset(token)
                end = max(self._now(), start)
                targets: list[str | None] = list(names) or [None]
                for project in targets:
                    self.append(VoiceCall(start_ms=start, end_ms=end, tool=tool, project=project))

        return wrapper


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 10**14 else None


def read_voice(state_dir: str, start_ms: int, end_ms: int) -> tuple[list[VoiceCall], int]:
    """The calls overlapping [start_ms, end_ms), and how many lines could not be read."""
    try:
        with open(voice_path(state_dir), "rb") as handle:
            raw = handle.read(MAX_VOICE_BYTES)
    except OSError:
        return [], 0
    out: list[VoiceCall] = []
    bad = 0
    for chunk in raw.split(b"\n"):
        if not chunk.strip():
            continue
        try:
            data: Any = json.loads(chunk)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if not isinstance(data, dict):
            bad += 1
            continue
        start, end, tool, project = _int(data.get("t")), _int(data.get("e")), data.get("tool"), data.get("p")
        if start is None or end is None or end < start or not isinstance(tool, str) or not _TOOL_RE.fullmatch(tool):
            bad += 1
            continue
        if project is not None and (not isinstance(project, str) or not _NAME_RE.fullmatch(project)):
            bad += 1
            continue
        if end < start_ms or start >= end_ms:
            continue
        out.append(VoiceCall(start_ms=start, end_ms=end, tool=tool, project=project))
    return out, bad
