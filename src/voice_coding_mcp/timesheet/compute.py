"""Owner time and agent time per project and day (docs/SWITCHBOARD.md, "Timesheet", "Time rules").

- A transcript is agent-driven when it is an agent transcript (a subagent or workflow agent, id
  `<session>:<agent>`) or a headless run (entrypoint `sdk-*`: `claude -p`, the hub's own tasks).
  Every other transcript is owner-driven (an interactive session: terminal, IDE, desktop app).
- Events are a transcript's prompts and assistant lines. A chain is a run of events at most
  `idleGapMinutes` apart. In an owner-driven transcript a chain with a prompt the owner typed (not a
  task notification, not a compact summary) is owner time from the event just before that first
  prompt (the answer the owner was reading) to its end; the events before that are agent time. A
  chain without such a prompt, every chain of an agent-driven transcript and the sidechain lines of a
  session are agent time. Every part lasts at least one minute.
- The time between two events belongs to the project of the later event (its own `cwd`'s
  repository); a lone event's minute to its own project.
- Voice: every Switchboard tool call of the owner is an event pair (start, end) of the project it
  targeted (`(voice)` when none); chains as above, all owner time.
- Per project and day: owner time is the union of the owner parts and voice chains, agent time the
  union of the agent parts. With `ownerOverlap` `split` a moment of owner time in several projects is
  divided equally between them, so owner hours never exceed wall-clock hours; `full` counts it in
  each. Agent time is never split.
- Copies (a resumed or forked session repeats earlier lines with their times) count once: per
  machine, assistant lines by message id, other lines by kind and time; the session that started
  first keeps them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta, tzinfo
from itertools import pairwise
from typing import Literal

from ..clock import MINUTE_MS
from ..usage.compute import Interval, Period, _day_start, clip, local_datetime, union
from ..usage.records import Record, Transcript

Kind = Literal["owner", "agent"]
UNKNOWN_PROJECT = "(unknown)"
VOICE_PROJECT = "(voice)"
MIN_PART_MS = MINUTE_MS


@dataclass(frozen=True)
class Source:
    machine: str
    transcript: Transcript


@dataclass(frozen=True)
class Piece:
    start: int
    end: int
    project: str
    kind: Kind


@dataclass(frozen=True)
class VoiceEvent:
    start: int
    end: int
    project: str | None


@dataclass
class DayTime:
    owner_ms: float = 0.0
    agent_ms: float = 0.0
    owner_overlap_ms: float = 0.0
    """Owner time this project shared with other projects at the same moment (before splitting)."""


@dataclass
class TimeResult:
    days: dict[str, dict[str, DayTime]] = field(default_factory=dict)
    """project -> day (YYYY-MM-DD) -> times."""
    wall_owner_ms: int = 0
    """Wall-clock owner time over all projects (each moment once)."""


def is_agent_driven(transcript: Transcript) -> bool:
    return transcript.is_agent_file or (transcript.entrypoint or "").startswith("sdk")


def _project(record: Record, base: str) -> str:
    return record.project or base


def _owner_prompt(record: Record) -> bool:
    return record.kind == "u" and not record.other_origin


def _parts(events: Sequence[tuple[int, str]], kind: Kind) -> list[Piece]:
    """Pieces of one part of a chain: between two events the later event's project; at least one
    minute (the last event's project gets the padding)."""
    out: list[Piece] = []
    for (a, _), (b, project) in pairwise(events):
        if b > a:
            out.append(Piece(a, b, project, kind))
    start, end = events[0][0], events[-1][0]
    if end - start < MIN_PART_MS:
        out.append(Piece(end, start + MIN_PART_MS, events[-1][1], kind))
    return out


def _chains(events: list[tuple[int, str, bool]], idle_gap_ms: int) -> list[list[tuple[int, str, bool]]]:
    ordered = sorted(events, key=lambda e: e[0])
    chains: list[list[tuple[int, str, bool]]] = []
    for event in ordered:
        if chains and event[0] - chains[-1][-1][0] <= idle_gap_ms:
            chains[-1].append(event)
        else:
            chains.append([event])
    return chains


def transcript_pieces(transcript: Transcript, records: Iterable[Record], idle_gap_ms: int) -> list[Piece]:
    base = transcript.work_project or transcript.project or UNKNOWN_PROJECT
    agent_driven = is_agent_driven(transcript)
    main: list[tuple[int, str, bool]] = []
    side: list[tuple[int, str, bool]] = []
    for record in records:
        event = (record.t, _project(record, base), _owner_prompt(record))
        (side if record.sidechain and not agent_driven else main).append(event)
    out: list[Piece] = []
    for chain in _chains(side, idle_gap_ms):
        out += _parts([(t, p) for t, p, _ in chain], "agent")
    for chain in _chains(main, idle_gap_ms):
        events = [(t, p) for t, p, _ in chain]
        first = next((i for i, e in enumerate(chain) if e[2]), None) if not agent_driven else None
        if first is None:
            out += _parts(events, "agent")
            continue
        owner_from = max(first - 1, 0)
        if owner_from >= 1:
            out += _parts(events[:owner_from], "agent")
        out += _parts(events[owner_from:], "owner")
    return out


def voice_pieces(calls: Iterable[VoiceEvent], idle_gap_ms: int) -> list[Piece]:
    by_project: dict[str, list[tuple[int, str, bool]]] = {}
    for call in calls:
        project = call.project or VOICE_PROJECT
        by_project.setdefault(project, []).extend([(call.start, project, True), (call.end, project, True)])
    out: list[Piece] = []
    for events in by_project.values():
        for chain in _chains(events, idle_gap_ms):
            out += _parts([(t, p) for t, p, _ in chain], "owner")
    return out


def deduplicate(sources: Sequence[Source]) -> list[list[Record]]:
    """Each source's records without the copies another source (started earlier) also has."""
    started = [min((r.t for r in s.transcript.records), default=0) for s in sources]
    keeper: dict[tuple[object, ...], int] = {}
    for index, source in enumerate(sources):
        for record in source.transcript.records:
            if record.kind == "a" and record.msg_id is not None:
                key: tuple[object, ...] = (source.machine, "a", record.msg_id, record.req_id or "")
            else:
                key = (source.machine, record.kind, record.t)
            seen = keeper.get(key)
            if seen is None or (started[index], index) < (started[seen], seen):
                keeper[key] = index
    out: list[list[Record]] = []
    for index, source in enumerate(sources):
        kept: list[Record] = []
        for record in source.transcript.records:
            if record.kind == "a" and record.msg_id is not None:
                key = (source.machine, "a", record.msg_id, record.req_id or "")
            else:
                key = (source.machine, record.kind, record.t)
            if keeper.get(key) == index:
                kept.append(record)
        out.append(kept)
    return out


def days_of(period: Period, tz: tzinfo | None) -> list[tuple[str, int, int]]:
    """(YYYY-MM-DD, start ms, end ms) of each local day of the period (DST aware)."""
    out: list[tuple[str, int, int]] = []
    day = local_datetime(period.start_ms, tz).date()
    while True:
        start = _day_start(day, tz)
        if start >= period.end_ms:
            return out
        end = min(_day_start(day + timedelta(days=1), tz), period.end_ms)
        out.append((day.isoformat(), max(start, period.start_ms), end))
        day += timedelta(days=1)


def _split(owner: dict[str, list[Interval]]) -> tuple[dict[str, float], dict[str, float], int]:
    """Owner ms per project with shared moments divided equally, the shared ms per project, and the
    wall-clock total."""
    edges: list[tuple[int, int, str]] = []
    for project, intervals in owner.items():
        for a, b in intervals:
            edges.append((a, 1, project))
            edges.append((b, -1, project))
    edges.sort(key=lambda e: (e[0], e[1]))
    share = dict.fromkeys(owner, 0.0)
    shared = dict.fromkeys(owner, 0.0)
    active: dict[str, int] = {}
    wall = 0
    last: int | None = None
    for moment, step, project in edges:
        if last is not None and moment > last and active:
            span = moment - last
            wall += span
            for name in active:
                share[name] += span / len(active)
                if len(active) > 1:
                    shared[name] += span
        active[project] = active.get(project, 0) + step
        if active[project] <= 0:
            del active[project]
        last = moment
    return share, shared, wall


def compute_time(
    sources: Sequence[Source],
    voice: Iterable[VoiceEvent],
    period: Period,
    *,
    idle_gap_ms: int,
    owner_overlap: Literal["split", "full"],
    tz: tzinfo | None,
    rename: Callable[[str], str] | None = None,
) -> TimeResult:
    """`rename` maps a folder name to the configured project it belongs to before the unions, so
    two folders of one project (a clone and a worktree) count each moment once."""
    pieces: list[Piece] = []
    for source, records in zip(sources, deduplicate(sources), strict=True):
        pieces += transcript_pieces(source.transcript, records, idle_gap_ms)
    if rename is not None:
        voice = [VoiceEvent(v.start, v.end, rename(v.project) if v.project else None) for v in voice]
    pieces += voice_pieces(voice, idle_gap_ms)
    owner: dict[str, list[Interval]] = {}
    agent: dict[str, list[Interval]] = {}
    for piece in pieces:
        project = rename(piece.project) if rename is not None else piece.project
        (owner if piece.kind == "owner" else agent).setdefault(project, []).append((piece.start, piece.end))
    result = TimeResult()
    for label, start, end in days_of(period, tz):
        day_owner = {p: union(clip(i, start, end)) for p, i in owner.items()}
        day_owner = {p: i for p, i in day_owner.items() if i}
        share, shared, wall = _split(day_owner)
        result.wall_owner_ms += wall
        for project, intervals in day_owner.items():
            entry = result.days.setdefault(project, {}).setdefault(label, DayTime())
            entry.owner_ms = share[project] if owner_overlap == "split" else float(sum(b - a for a, b in intervals))
            entry.owner_overlap_ms = shared[project]
        for project, intervals in agent.items():
            clipped = union(clip(intervals, start, end))
            if clipped:
                entry = result.days.setdefault(project, {}).setdefault(label, DayTime())
                entry.agent_ms = float(sum(b - a for a, b in clipped))
    return result


def round_minutes(ms: float, step: int, mode: Literal["nearest", "up"]) -> int:
    """Whole minutes of `ms`, rounded to `step` minutes (0: to the nearest minute)."""
    minutes = ms / MINUTE_MS
    if step <= 0:
        return math.floor(minutes + 0.5)
    if mode == "up":
        return math.ceil(minutes / step - 1e-9) * step
    return math.floor(minutes / step + 0.5) * step
