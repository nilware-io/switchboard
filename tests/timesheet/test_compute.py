"""Timesheet time rules: owner versus agent sessions, sidechains, idle gaps, unions, the overlap
split, voice calls, renames, copies, day and week boundaries in the owner's time zone, rounding."""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime
from typing import Any

import pytest

from voice_coding_mcp.timesheet.compute import (
    VOICE_PROJECT,
    Source,
    VoiceEvent,
    compute_time,
    is_agent_driven,
    round_minutes,
    transcript_pieces,
)
from voice_coding_mcp.timesheet.service import parse_timesheet_period, period_bounds
from voice_coding_mcp.usage.compute import Period
from voice_coding_mcp.usage.records import Record, Transcript

MIN = 60_000
HOUR = 60 * MIN
ZURICH = zoneinfo.ZoneInfo("Europe/Zurich")
T0 = int(datetime(2026, 11, 17, 9, 0, tzinfo=ZURICH).timestamp() * 1000)
"""Tuesday 2026-11-17 09:00 in Zurich."""
GAP = 15 * MIN
S1 = "22222222-0000-4000-8000-000000000001"
S2 = "22222222-0000-4000-8000-000000000002"


def u(t: int, *, other: bool = False, project: str | None = None) -> Record:
    return Record(t=t, kind="u", other_origin=other, project=project)


def a(t: int, msg: str | None = None, *, side: bool = False, project: str | None = None) -> Record:
    return Record(t=t, kind="a", msg_id=msg, sidechain=side, project=project)


def transcript(
    records: list[Record], *, tid: str = S1, project: str = "alpha", entrypoint: str | None = "cli"
) -> Transcript:
    return Transcript(id=tid, size=1, mtime=1, project=project, records=records, entrypoint=entrypoint)


def week() -> Period:
    return parse_timesheet_period("2026-11-16..2026-11-22", T0, ZURICH)


def minutes(result: Any, project: str, day: str, kind: str) -> float:
    entry = result.days.get(project, {}).get(day)
    if entry is None:
        return 0.0
    return float(entry.owner_ms if kind == "owner" else entry.agent_ms) / MIN


def run(sources: list[Source], voice: list[VoiceEvent] | None = None, **kw: Any) -> Any:
    return compute_time(
        sources,
        voice or [],
        kw.pop("period", week()),
        idle_gap_ms=GAP,
        owner_overlap=kw.pop("overlap", "split"),
        tz=ZURICH,
        **kw,
    )


def test_owner_session_counts_from_the_prompt_through_the_answers() -> None:
    t = transcript([u(T0), a(T0 + 5 * MIN), a(T0 + 12 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 12
    assert minutes(result, "alpha", "2026-11-17", "agent") == 0


def test_idle_gap_splits_and_a_lone_event_is_one_minute() -> None:
    t = transcript([u(T0), a(T0 + 10 * MIN), u(T0 + 40 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 11


def test_chain_without_an_owner_prompt_is_agent_time() -> None:
    t = transcript([u(T0, other=True), a(T0 + 5 * MIN), a(T0 + 9 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 0
    assert minutes(result, "alpha", "2026-11-17", "agent") == 9


def test_autonomous_lead_in_is_agent_and_the_answer_before_the_prompt_starts_owner_time() -> None:
    t = transcript([a(T0), a(T0 + 6 * MIN), a(T0 + 10 * MIN), u(T0 + 14 * MIN), a(T0 + 20 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-17", "agent") == 6
    assert minutes(result, "alpha", "2026-11-17", "owner") == 10


@pytest.mark.parametrize("entrypoint", ["sdk-cli", "sdk-ts", "sdk-py"])
def test_headless_runs_are_agent_time(entrypoint: str) -> None:
    t = transcript([u(T0), a(T0 + 30 * MIN)], entrypoint=entrypoint)
    assert is_agent_driven(t)
    result = run([Source("local", t)])
    # Two chains (30 minutes apart), one minute each.
    assert minutes(result, "alpha", "2026-11-17", "agent") == 2
    assert minutes(result, "alpha", "2026-11-17", "owner") == 0


def test_agent_transcripts_and_sidechain_lines_are_agent_time() -> None:
    session = transcript([u(T0), a(T0 + 2 * MIN), a(T0 + 3 * MIN, side=True), a(T0 + 8 * MIN, side=True)])
    agent = transcript([u(T0 + MIN), a(T0 + 14 * MIN)], tid=f"{S1}:a1", entrypoint="cli")
    assert is_agent_driven(agent)
    result = run([Source("dev", session), Source("dev", agent)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 2
    # Sidechain 3..8 and the agent transcript 1..14 united.
    assert minutes(result, "alpha", "2026-11-17", "agent") == 13


def test_parallel_sessions_of_one_project_count_once() -> None:
    one = transcript([u(T0), a(T0 + 10 * MIN)])
    two = transcript([u(T0 + 5 * MIN), a(T0 + 15 * MIN)], tid=S2)
    result = run([Source("dev", one), Source("buildbox", two)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 15


def test_owner_time_in_two_projects_at_once_is_split_or_full() -> None:
    one = transcript([u(T0), a(T0 + 10 * MIN)], project="alpha")
    two = transcript([u(T0), a(T0 + 10 * MIN)], tid=S2, project="beta")
    # Two machines: lines at the same moment on one machine would be copies.
    split = run([Source("dev", one), Source("buildbox", two)])
    assert minutes(split, "alpha", "2026-11-17", "owner") == 5
    assert minutes(split, "beta", "2026-11-17", "owner") == 5
    assert split.wall_owner_ms == 10 * MIN
    assert split.days["alpha"]["2026-11-17"].owner_overlap_ms == 10 * MIN
    full = run([Source("dev", one), Source("buildbox", two)], overlap="full")
    assert minutes(full, "alpha", "2026-11-17", "owner") == 10
    assert minutes(full, "beta", "2026-11-17", "owner") == 10


def test_agent_time_is_never_split() -> None:
    one = transcript([a(T0), a(T0 + 10 * MIN)], project="alpha", entrypoint="sdk-cli")
    two = transcript([a(T0), a(T0 + 10 * MIN)], tid=S2, project="beta", entrypoint="sdk-cli")
    result = run([Source("dev", one), Source("buildbox", two)])
    assert minutes(result, "alpha", "2026-11-17", "agent") == 10
    assert minutes(result, "beta", "2026-11-17", "agent") == 10


def test_time_between_events_goes_to_the_later_events_project() -> None:
    t = transcript([u(T0), a(T0 + 4 * MIN, project="beta"), a(T0 + 10 * MIN)], project="alpha")
    pieces = transcript_pieces(t, t.records, GAP)
    assert [(p.end - p.start, p.project, p.kind) for p in pieces] == [
        (4 * MIN, "beta", "owner"),
        (6 * MIN, "alpha", "owner"),
    ]


def test_work_project_of_the_transcript_wins_over_its_folder() -> None:
    t = Transcript(
        id=S1, size=1, mtime=1, project="model", work_project="shrike4-rtl", records=[u(T0)], entrypoint="cli"
    )
    result = run([Source("dev", t)])
    assert minutes(result, "shrike4-rtl", "2026-11-17", "owner") == 1


def test_rename_unites_folders_of_one_project() -> None:
    one = transcript([u(T0), a(T0 + 10 * MIN)], project="switchboard")
    two = transcript([u(T0 + 5 * MIN), a(T0 + 12 * MIN)], tid=S2, project="switchboard-timesheet")
    result = run(
        [Source("dev", one), Source("dev", two)], rename=lambda f: "switchboard" if f.startswith("switchboard") else f
    )
    assert set(result.days) == {"switchboard"}
    assert minutes(result, "switchboard", "2026-11-17", "owner") == 12


def test_copied_history_of_a_forked_session_counts_once() -> None:
    original = transcript([u(T0), a(T0 + 10 * MIN, "m1")])
    fork = transcript([u(T0), a(T0 + 10 * MIN, "m1"), a(T0 + 30 * MIN, "m2")], tid=S2, entrypoint="sdk-cli")
    result = run([Source("dev", original), Source("dev", fork)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 10
    # Only the fork's own line (one minute) is agent time, not the copied history.
    assert minutes(result, "alpha", "2026-11-17", "agent") == 1


def test_voice_calls_are_owner_time_of_their_project_or_voice() -> None:
    voice = [
        VoiceEvent(T0, T0 + 20_000, "alpha"),
        VoiceEvent(T0 + 3 * MIN, T0 + 4 * MIN, "alpha"),
        VoiceEvent(T0 + 60 * MIN, T0 + 60 * MIN + 5_000, None),
    ]
    result = run([], voice)
    assert minutes(result, "alpha", "2026-11-17", "owner") == 4
    assert minutes(result, VOICE_PROJECT, "2026-11-17", "owner") == 1


def test_voice_and_session_time_of_one_project_are_united() -> None:
    t = transcript([u(T0), a(T0 + 10 * MIN)])
    result = run([Source("dev", t)], [VoiceEvent(T0 + 2 * MIN, T0 + 3 * MIN, "alpha")])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 10


def test_midnight_in_the_owners_zone_splits_days() -> None:
    late = int(datetime(2026, 11, 17, 23, 50, tzinfo=ZURICH).timestamp() * 1000)
    t = transcript([u(late), a(late + 10 * MIN), a(late + 20 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-17", "owner") == 10
    assert minutes(result, "alpha", "2026-11-18", "owner") == 10


def test_week_boundaries_clip_monday_to_sunday_in_the_owners_zone() -> None:
    sunday_night = int(datetime(2026, 11, 22, 23, 55, tzinfo=ZURICH).timestamp() * 1000)
    t = transcript([u(sunday_night), a(sunday_night + 10 * MIN), a(sunday_night + 15 * MIN)])
    result = run([Source("dev", t)])
    assert minutes(result, "alpha", "2026-11-22", "owner") == 5
    assert "2026-11-23" not in result.days["alpha"]
    period = week()
    assert period_bounds(period, ZURICH) == ("2026-11-16_2026-11-22", "2026-11-16", "2026-11-22")
    assert period.start_ms == int(datetime(2026, 11, 16, tzinfo=ZURICH).timestamp() * 1000)


def test_default_period_is_last_week_monday_to_sunday() -> None:
    wednesday = int(datetime(2026, 11, 25, 12, 0, tzinfo=ZURICH).timestamp() * 1000)
    period = parse_timesheet_period(None, wednesday, ZURICH)
    assert period_bounds(period, ZURICH)[0] == "2026-11-16_2026-11-22"
    assert period_bounds(parse_timesheet_period("2026-W47", wednesday, ZURICH), ZURICH)[0] == "2026-11-16_2026-11-22"
    assert period_bounds(parse_timesheet_period("2026-11-16_2026-11-22", wednesday, ZURICH), ZURICH)[0] == (
        "2026-11-16_2026-11-22"
    )
    utc_period = parse_timesheet_period(None, wednesday, UTC)
    assert utc_period.start_ms - period.start_ms == HOUR


@pytest.mark.parametrize(
    ("ms", "step", "mode", "expected"),
    [
        (7 * MIN + 29_000, 0, "nearest", 7),
        (7 * MIN + 30_000, 0, "nearest", 8),
        (7 * MIN, 15, "nearest", 0),
        (8 * MIN, 15, "nearest", 15),
        (1 * MIN, 15, "up", 15),
        (15 * MIN, 15, "up", 15),
        (15 * MIN + 1, 15, "up", 30),
        (52 * MIN, 6, "nearest", 54),
    ],
)
def test_rounding(ms: int, step: int, mode: Any, expected: int) -> None:
    assert round_minutes(ms, step, mode) == expected


def test_unattributed_voice_time_does_not_take_from_projects() -> None:
    t = transcript([u(T0), a(T0 + 10 * MIN)])
    voice = [
        VoiceEvent(T0 + 5 * MIN, T0 + 5 * MIN + 1_000, None),
        VoiceEvent(T0 + 12 * MIN, T0 + 12 * MIN + 1_000, None),
    ]
    result = run([Source("dev", t)], voice)
    assert minutes(result, "alpha", "2026-11-17", "owner") == 10
    # The voice chain from 5 to 12 minutes and 1 second counts only after 10 minutes.
    assert result.days[VOICE_PROJECT]["2026-11-17"].owner_ms == 2 * MIN + 1_000
    assert result.wall_owner_ms == 12 * MIN + 1_000
