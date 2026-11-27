"""Usage records: the compact, text-free lines the dispatcher's `usage` verb streams, read again on
the hub (docs/SWITCHBOARD.md, "The dispatcher", `usage`).

The transcript parser itself is the dispatcher's (`dispatcher.usage_documents`), which the hub also
runs on its own Claude Code home, so machines and the hub cannot parse transcripts differently. A
machine is not trusted to answer sensibly: `parse_documents` checks every line strictly.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

from ..clock import parse_iso
from ..remote.dispatcher import ENTRYPOINT_RE, USAGE_FILE_ID_RE, usage_documents

MAX_TOKENS = 10**12
MAX_TIME_MS = 10**14
NAME_MAX = 64
_TEXT_RE = re.compile(r"[^\x00-\x1f\x7f-\x9f]{1,64}")
SUMMARY_PROJECT = "switchboard"
"""Project name of the remote summariser's runs (`stateDir/usage/summaries.jsonl`)."""
MAX_SUMMARY_BYTES = 64 * 1024 * 1024


class UsageStreamError(ValueError):
    """Usage lines that are not a valid answer of the `usage` verb."""


@dataclass(frozen=True, slots=True)
class Record:
    t: int
    """Epoch milliseconds."""
    kind: Literal["a", "u"]
    """`a` an assistant message (with token counts), `u` a prompt of the session's driver."""
    model: str | None = None
    msg_id: str | None = None
    req_id: str | None = None
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    cost_usd: float | None = None
    """A cost already known (the summariser's `result` event), used instead of the price table."""
    other_origin: bool = False
    """A `u` line the owner did not type (a task notification, a compact summary), dispatcher 0.10.0."""
    sidechain: bool = False
    """An `a` line of a subagent inside a session's own transcript."""
    project: str | None = None
    """The work project of this line's `cwd` when it differs from the transcript's (0.10.0)."""


@dataclass
class Transcript:
    id: str
    size: int
    mtime: int
    project: str = ""
    skipped: bool = False
    """Larger than `usage.maxTranscriptMegabytes`: not read."""
    records: list[Record] = field(default_factory=list)
    entrypoint: str | None = None
    """How the session was started (`cli`, `sdk-cli` for `claude -p`, ...), dispatcher 0.10.0."""
    work_project: str = ""
    """The repository the latest `cwd` lies in, when it differs from `project` (0.10.0)."""

    @property
    def is_agent_file(self) -> bool:
        """A subagent or workflow agent transcript (`<session id>:<agent id>`)."""
        return ":" in self.id


@dataclass
class UsageBatch:
    files: list[Transcript] = field(default_factory=list)
    gone: list[str] = field(default_factory=list)


def _int(value: object, limit: int = MAX_TOKENS) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= limit:
        raise UsageStreamError("bad number")
    return value


def _text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _TEXT_RE.fullmatch(value):
        raise UsageStreamError("bad text")
    return value


def record_from(data: object) -> Record:
    if not isinstance(data, dict):
        raise UsageStreamError("record is not an object")
    kind = data.get("k")
    t = _int(data.get("t"), MAX_TIME_MS)
    project = _text(data.get("p"))
    if kind == "u":
        origin = data.get("o")
        if origin not in (None, "n"):
            raise UsageStreamError("bad origin")
        return Record(t=t, kind="u", other_origin=origin == "n", project=project)
    if kind != "a":
        raise UsageStreamError("bad record kind")
    side = data.get("sc")
    if side not in (None, 0, 1, True, False):
        raise UsageStreamError("bad sidechain flag")
    return Record(
        sidechain=bool(side),
        project=project,
        t=t,
        kind="a",
        model=_text(data.get("m")),
        msg_id=_text(data.get("id")),
        req_id=_text(data.get("rq")),
        input=_int(data.get("in", 0)),
        output=_int(data.get("out", 0)),
        cache_write_5m=_int(data.get("cw5", 0)),
        cache_write_1h=_int(data.get("cw1", 0)),
        cache_read=_int(data.get("cr", 0)),
    )


def _file_id(value: object) -> str:
    if not isinstance(value, str) or not USAGE_FILE_ID_RE.fullmatch(value):
        raise UsageStreamError("bad file id")
    return value.lower()


def _project(value: object) -> str:
    if not isinstance(value, str):
        raise UsageStreamError("bad project")
    return "".join(ch for ch in value if ch.isprintable())[:NAME_MAX]


def parse_documents(documents: Iterable[object]) -> UsageBatch:
    """The batch the usage lines (after the header document) describe. Raises UsageStreamError
    for anything out of shape, also when the final `done` line is missing (a cut answer)."""
    batch = UsageBatch()
    current: Transcript | None = None
    expected = 0
    done = False
    for document in documents:
        if done:
            raise UsageStreamError("lines after done")
        if not isinstance(document, dict):
            raise UsageStreamError("line is not an object")
        if "t" in document:
            if current is None or expected <= 0:
                raise UsageStreamError("record outside a file")
            current.records.append(record_from(document))
            expected -= 1
            continue
        if expected:
            raise UsageStreamError("file has fewer records than announced")
        if document.get("done") is True:
            gone = document.get("gone", [])
            if not isinstance(gone, list):
                raise UsageStreamError("bad gone list")
            batch.gone = [_file_id(item) for item in gone]
            done = True
            continue
        if "file" not in document:
            raise UsageStreamError("unknown line")
        current = Transcript(
            id=_file_id(document.get("file")),
            size=_int(document.get("size"), 2**53),
            mtime=_int(document.get("mtime"), MAX_TIME_MS),
        )
        if document.get("skipped") is True:
            current.skipped = True
            expected = 0
        else:
            current.project = _project(document.get("project", ""))
            current.work_project = _project(document.get("wp", ""))
            entrypoint = document.get("ep")
            if entrypoint is not None and (not isinstance(entrypoint, str) or not ENTRYPOINT_RE.fullmatch(entrypoint)):
                raise UsageStreamError("bad entrypoint")
            current.entrypoint = entrypoint
            expected = _int(document.get("records"))
        batch.files.append(current)
    if not done:
        raise UsageStreamError("no done line")
    return batch


def parse_lines(lines: Iterable[bytes]) -> UsageBatch:
    """`parse_documents` over JSON lines (blank lines ignored)."""

    def documents() -> Iterable[object]:
        for line in lines:
            if line.strip():
                try:
                    yield json.loads(line)
                except (ValueError, RecursionError):
                    raise UsageStreamError("line is not JSON") from None

    return parse_documents(documents())


def scan_local(home: str, since_ms: int, max_bytes: int, known: dict[str, tuple[int, int]]) -> UsageBatch:
    """The hub's own Claude Code home, read with the dispatcher's parser (blocking; run in a thread)."""
    return parse_documents(usage_documents(home, since_ms, max_bytes, known))


def _cost(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def read_summaries(state_dir: str, since_ms: int = 0) -> list[Record]:
    """The remote summariser's runs (`stateDir/usage/summaries.jsonl`): tokens and known cost, no text.
    Unreadable lines are skipped."""
    path = os.path.join(state_dir, "usage", "summaries.jsonl")
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SUMMARY_BYTES)
    except OSError:
        return []
    out: list[Record] = []
    for line in raw.split(b"\n"):
        try:
            data = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        t = parse_iso(data.get("time")) if isinstance(data.get("time"), str) else None
        if t is None or t < since_ms:
            continue
        cost = data.get("costUsd")

        def count(name: str, values: dict[str, object] = data) -> int:
            value = values.get(name)
            return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_TOKENS else 0

        model = data.get("model")
        out.append(
            Record(
                t=t,
                kind="a",
                model=model if isinstance(model, str) and _TEXT_RE.fullmatch(model) else None,
                input=count("input"),
                output=count("output"),
                cache_write_5m=count("cacheWrite"),
                cache_read=count("cacheRead"),
                cost_usd=_cost(cost),
            )
        )
    return out
