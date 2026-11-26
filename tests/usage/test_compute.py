"""Usage computation: periods, active intervals, idle gaps, overlap, dedup, tokens by type, prices."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from tests.usage.helpers import MIN, T0
from voice_coding_mcp.usage.compute import (
    ComputeOptions,
    Period,
    PeriodError,
    SessionUsage,
    active_intervals,
    compute,
    parse_period,
    union,
)
from voice_coding_mcp.usage.config import PriceConfig
from voice_coding_mcp.usage.prices import BUILT_IN, PRICES_AS_OF, PriceTable
from voice_coding_mcp.usage.records import Record

HOUR = 60 * MIN
DAY_START = T0 - 16 * HOUR
"""2025-11-24T00:00:00Z, a Monday."""
WHOLE = Period(DAY_START - 30 * 24 * HOUR, DAY_START + 30 * 24 * HOUR, "Test")


def options(**keys: object) -> ComputeOptions:
    base: dict[str, object] = {
        "idle_gap_ms": 15 * MIN,
        "overlap": "union",
        "hourly_rate": 40.0,
        "currency": "EUR",
        "prices": PriceTable(),
        "tz": UTC,
    }
    base.update(keys)
    return ComputeOptions(**base)  # type: ignore[arg-type]


def u(t: int) -> Record:
    return Record(t=t, kind="u")


def a(t: int, msg: str | None = None, model: str = "claude-opus-4-8", **tokens: int) -> Record:
    return Record(t=t, kind="a", model=model, msg_id=msg, req_id="r" if msg else None, **tokens)


def session(records: list[Record], *, project: str = "fpgadc", sid: str = "s1", machine: str = "dev") -> SessionUsage:
    return SessionUsage(machine=machine, id=sid, project=project, records=records)


# --- periods ---------------------------------------------------------------------------------------


NOW = T0 + 3 * 24 * HOUR  # Thursday 2025-11-27 16:00 UTC


@pytest.mark.parametrize(
    ("text", "start", "end", "label"),
    [
        ("today", "2025-11-27", "2025-11-28", "Today"),
        (" Yesterday ", "2025-11-26", "2025-11-27", "Yesterday"),
        ("this week", "2025-11-24", "2025-12-01", "This week"),
        ("last_week", "2025-11-17", "2025-11-24", "Last week"),
        (None, "2025-11-01", "2025-12-01", "This month"),
        ("last month", "2025-10-01", "2025-11-01", "Last month"),
        ("2024-12", "2024-12-01", "2025-01-01", "December 2024"),
        ("2025-11-03", "2025-11-03", "2025-11-04", "November 3, 2025"),
        ("2025-11-01..2025-11-15", "2025-11-01", "2025-11-16", "From November 1, 2025 to November 15, 2025"),
    ],
)
def test_parse_period(text: str | None, start: str, end: str, label: str) -> None:
    period = parse_period(text, NOW, UTC)
    assert period.label == label
    assert period.start_ms == int(datetime.fromisoformat(start).replace(tzinfo=UTC).timestamp() * 1000)
    assert period.end_ms == int(datetime.fromisoformat(end).replace(tzinfo=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    "text", ["next week", "2025-13", "2025-02-30", "2025-11-15..2025-11-01", "1969-01", "fortnight"]
)
def test_bad_periods(text: str) -> None:
    with pytest.raises(PeriodError):
        parse_period(text, NOW, UTC)


def test_periods_use_the_hub_time_zone() -> None:
    athens = timezone(timedelta(hours=2))
    # 23:30 UTC on Sunday is already Monday in UTC+2.
    now = DAY_START - 30 * MIN
    assert parse_period("today", now, athens).start_ms == DAY_START - 2 * HOUR
    assert parse_period("this week", now, athens).start_ms == DAY_START - 2 * HOUR
    assert parse_period("this week", now, UTC).start_ms == DAY_START - 7 * 24 * HOUR


def test_a_day_across_a_dst_change_is_23_hours() -> None:
    zoneinfo = pytest.importorskip("zoneinfo")
    try:
        zone = zoneinfo.ZoneInfo("Europe/Athens")
    except zoneinfo.ZoneInfoNotFoundError:
        pytest.skip("no time zone data")
    period = parse_period("2025-03-30", T0, zone)
    assert period.end_ms - period.start_ms == 23 * HOUR


# --- intervals -------------------------------------------------------------------------------------


def test_active_intervals_join_within_the_idle_gap() -> None:
    times = [T0, T0 + 10 * MIN, T0 + 25 * MIN, T0 + 41 * MIN, T0 + 100 * MIN]
    assert active_intervals(times, 15 * MIN) == [
        (T0, T0 + 25 * MIN),
        (T0 + 41 * MIN, T0 + 42 * MIN),
        (T0 + 100 * MIN, T0 + 101 * MIN),
    ]


def test_a_gap_of_exactly_the_idle_gap_joins() -> None:
    assert active_intervals([T0, T0 + 15 * MIN], 15 * MIN) == [(T0, T0 + 15 * MIN)]
    assert active_intervals([T0, T0 + 15 * MIN + 1], 15 * MIN) == [
        (T0, T0 + MIN),
        (T0 + 15 * MIN + 1, T0 + 16 * MIN + 1),
    ]


def test_union_merges_overlaps_and_touching_intervals() -> None:
    assert union([(5, 9), (1, 3), (3, 4), (8, 12)]) == [(1, 4), (5, 12)]


def test_hours_and_amount_of_one_session() -> None:
    report = compute(
        [session([u(T0), a(T0 + 30 * MIN, "m1"), a(T0 + 90 * MIN, "m2")])], WHOLE, options(idle_gap_ms=60 * MIN)
    )
    assert report.total.billable_ms == 90 * MIN
    assert report.total.amount == 60.0
    assert report.total.sessions == 1


def test_isolated_event_counts_one_minute() -> None:
    report = compute([session([u(T0)])], WHOLE, options())
    assert report.total.billable_ms == MIN
    assert report.total.amount == round(40 / 60, 2)


def test_intervals_are_clipped_at_the_period_edges() -> None:
    events = [u(DAY_START - 10 * MIN), u(DAY_START), u(DAY_START + 10 * MIN)]
    today = Period(DAY_START, DAY_START + 24 * HOUR, "Today")
    report = compute([session(events)], today, options())
    assert report.total.billable_ms == 10 * MIN
    before = Period(DAY_START - 24 * HOUR, DAY_START, "Yesterday")
    assert compute([session(events)], before, options()).total.billable_ms == 10 * MIN


def test_union_counts_parallel_sessions_once_and_sum_counts_each() -> None:
    one = session([u(T0), u(T0 + 60 * MIN)], sid="s1")
    two = session([u(T0 + 30 * MIN), u(T0 + 90 * MIN)], sid="s2")
    gap = options(idle_gap_ms=60 * MIN)
    assert compute([one, two], WHOLE, gap).total.billable_ms == 90 * MIN
    summed = compute([one, two], WHOLE, options(idle_gap_ms=60 * MIN, overlap="sum"))
    assert summed.total.billable_ms == 120 * MIN
    assert summed.total.active_ms == 120 * MIN


def test_projects_bill_separately_even_at_the_same_time() -> None:
    gap = options(idle_gap_ms=60 * MIN)
    report = compute(
        [
            session([u(T0), u(T0 + 60 * MIN)], project="alpha"),
            session([u(T0), u(T0 + 30 * MIN)], project="beta", sid="s2"),
        ],
        WHOLE,
        gap,
    )
    assert [(r.key, r.billable_ms, r.amount) for r in report.rows] == [
        (("alpha",), 60 * MIN, 40.0),
        (("beta",), 30 * MIN, 20.0),
    ]
    assert report.total.billable_ms == 90 * MIN
    assert report.total.amount == 60.0


def test_total_amount_is_the_sum_of_rounded_project_amounts() -> None:
    sessions = [session([u(T0 + i * HOUR)], project=f"p{i}", sid=str(i)) for i in range(3)]
    report = compute(sessions, WHOLE, options(hourly_rate=1.0))
    assert [r.amount for r in report.rows] == [0.02, 0.02, 0.02]
    assert report.total.amount == 0.06


# --- tokens ----------------------------------------------------------------------------------------


def test_tokens_by_type_are_deduplicated_by_message_and_request() -> None:
    records = [
        a(T0, "m1", input=10, output=5, cache_read=100),
        a(T0 + 1, "m1", input=10, output=50, cache_read=100),
        a(T0 + 2, "m2", input=1, cache_write_5m=7, cache_write_1h=3),
        a(T0 + 3, None, input=2),
        a(T0 + 4, None, input=2),
    ]
    report = compute([session(records)], WHOLE, options())
    t = report.total.tokens
    assert (t.input, t.output, t.cache_write_5m, t.cache_write_1h, t.cache_read) == (15, 50, 7, 3, 100)


def test_copied_messages_count_once_for_the_session_that_had_them_first() -> None:
    original = session([u(T0), a(T0 + MIN, "m1", input=100)], sid="old")
    resumed = session(
        [a(T0 + MIN, "m1", input=100), u(T0 + 5 * HOUR), a(T0 + 5 * HOUR + MIN, "m2", input=1)], sid="new"
    )
    report = compute([resumed, original], WHOLE, options(), by="session")
    by_id = {r.key[2]: r for r in report.rows}
    assert by_id["old"].tokens.input == 100
    assert by_id["new"].tokens.input == 1
    assert report.total.tokens.input == 101


def test_same_message_id_on_two_machines_is_two_messages() -> None:
    report = compute(
        [session([a(T0, "m1", input=5)]), session([a(T0, "m1", input=5)], machine="buildbox")], WHOLE, options()
    )
    assert report.total.tokens.input == 10


def test_tokens_count_in_the_period_of_their_time() -> None:
    period = Period(T0, T0 + HOUR, "x")
    report = compute(
        [session([a(T0 - 1, "m0", input=1), a(T0, "m1", input=2), a(T0 + HOUR, "m2", input=4)])], period, options()
    )
    assert report.total.tokens.input == 2


def test_cost_uses_the_built_in_prices() -> None:
    record = a(
        T0,
        "m1",
        model="claude-opus-4-8",
        input=1_000_000,
        output=1_000_000,
        cache_write_5m=1_000_000,
        cache_write_1h=1_000_000,
        cache_read=1_000_000,
    )
    report = compute([session([record])], WHOLE, options())
    assert report.total.cost_usd == pytest.approx(5 + 25 + 6.25 + 10 + 0.5)
    assert report.unpriced_models == []


def test_unpriced_models_are_reported_not_guessed() -> None:
    report = compute(
        [
            session(
                [
                    a(T0, "m1", model="claude-opus-4-1-20250805", input=5),
                    a(T0, "m2", model="<synthetic>"),
                    a(T0, "m3", model="claude-opus-4-8", input=1_000_000),
                ]
            )
        ],
        WHOLE,
        options(),
    )
    assert report.unpriced_models == ["claude-opus-4-1-20250805"]
    assert report.total.cost_usd == pytest.approx(5.0)


def test_known_cost_wins_and_token_only_sessions_add_no_time() -> None:
    summaries = SessionUsage(
        "local",
        "summaries",
        "switchboard",
        [Record(t=T0, kind="a", model="haiku", input=10, cost_usd=0.25)],
        activity=False,
    )
    report = compute([summaries], WHOLE, options())
    assert report.total.billable_ms == 0
    assert report.total.sessions == 0
    assert report.total.cost_usd == 0.25
    assert report.rows[0].key == ("switchboard",)


# --- prices ----------------------------------------------------------------------------------------


def test_price_table_matches() -> None:
    table = PriceTable()
    assert table.price("claude-opus-4-8") == BUILT_IN["claude-opus-4-8"]
    assert table.price("claude-haiku-4-5-20251001") == BUILT_IN["claude-haiku-4-5"]
    assert table.price("claude-opus-4-6[1m]") == BUILT_IN["claude-opus-4-6"]
    assert table.price("claude-opus-5-5") == BUILT_IN["claude-opus-5-5"]
    assert table.price("claude-opus-5") == BUILT_IN["claude-opus-5"]
    assert table.price("claude-opus-5-7") is None, "a newer model is not priced like an older one"
    assert table.price("claude-fable-5-2") == BUILT_IN["claude-fable-5-2"]
    assert table.price(None) is None
    assert PRICES_AS_OF == "2026-11-03"


def test_built_in_cache_writes_follow_the_published_multipliers() -> None:
    for price in BUILT_IN.values():
        assert price.cache_write_5m == pytest.approx(price.input * 1.25)
        assert price.cache_write_1h == pytest.approx(price.input * 2)
        assert price.cache_read <= price.input * 0.1 + 1e-9


def test_configured_prefixes_win_longest_first() -> None:
    table = PriceTable(
        {
            "claude-": PriceConfig(input=1, output=1),
            "claude-opus-": PriceConfig(input=2, output=3, cache_read=0.5),
        }
    )
    opus = table.price("claude-opus-4-1")
    assert opus is not None
    assert (opus.input, opus.output, opus.cache_write_5m, opus.cache_write_1h, opus.cache_read) == (2, 3, 2.5, 4, 0.5)
    other = table.price("claude-sonnet-4-5")
    assert other is not None
    assert other.input == 1
    assert table.price("gpt-x") is None


# --- grouping --------------------------------------------------------------------------------------


def test_by_day_splits_at_local_midnight() -> None:
    events = [u(DAY_START - 30 * MIN), u(DAY_START - 20 * MIN), u(DAY_START - 5 * MIN), u(DAY_START + 10 * MIN)]
    report = compute([session([*events, a(DAY_START + MIN, "m1", input=3)])], WHOLE, options(), by="day")
    assert [(r.key, r.billable_ms, r.tokens.input) for r in report.rows] == [
        (("2025-11-23",), 30 * MIN, 0),
        (("2025-11-24",), 10 * MIN, 3),
    ]
    assert report.total.billable_ms == 40 * MIN


def test_by_session_rows() -> None:
    report = compute(
        [session([u(T0), u(T0 + 10 * MIN)], sid="a"), session([u(T0)], sid="b", project="", machine="buildbox")],
        WHOLE,
        options(),
        by="session",
    )
    assert [r.key for r in report.rows] == [("dev", "fpgadc", "a"), ("buildbox", "(unknown)", "b")]


def test_empty_report() -> None:
    report = compute([session([u(T0)])], Period(0, 1, "x"), options())
    assert report.rows == []
    assert report.total.billable_ms == 0
    assert report.total.amount == 0
