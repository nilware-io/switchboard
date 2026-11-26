"""Active hours, billable hours, tokens by type and API-equivalent cost (docs/SWITCHBOARD.md,
"Usage computation").

- Activity events of a session are its prompts and assistant lines. Consecutive events at most
  `idleGapMinutes` apart form one active interval; every interval lasts at least one minute (an
  isolated event counts as one minute). Intervals are clipped to the period.
- Billable hours of a project: the union of its sessions' intervals (`overlap` `union`, parallel
  sessions count once) or their plain sum (`sum`). The amount is billable hours x `hourlyRate`,
  rounded to cents; the total is the sum of the projects' amounts.
- Tokens are deduplicated per machine by (message id, request id): Claude Code writes one message
  over several lines with the same usage, and a resumed or forked session copies earlier messages.
  The copy that appears first in time keeps them (on a tie, the session that started first). A record counts in the period its time falls in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from typing import Literal

from ..clock import MINUTE_MS
from .prices import PriceTable
from .records import Record

MIN_INTERVAL_MS = MINUTE_MS
UNKNOWN_PROJECT = "(unknown)"
Grouping = Literal["project", "session", "day"]
Overlap = Literal["union", "sum"]

_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


# --- periods -------------------------------------------------------------------------------------


class PeriodError(ValueError):
    pass


@dataclass(frozen=True)
class Period:
    start_ms: int
    end_ms: int
    """Exclusive."""
    label: str
    """Spoken, capitalised: "Last month", "October 2026"."""


def local_datetime(ms: int, tz: tzinfo | None) -> datetime:
    """Naive local wall-clock time of `ms` (`tz`, else the system's local time zone)."""
    if tz is None:
        return datetime.fromtimestamp(ms / 1000)
    return datetime.fromtimestamp(ms / 1000, tz).replace(tzinfo=None)


def epoch_ms(moment: datetime, tz: tzinfo | None) -> int:
    """Epoch milliseconds of a naive local wall-clock time (DST aware)."""
    if tz is None:
        return round(moment.timestamp() * 1000)
    return round(moment.replace(tzinfo=tz).timestamp() * 1000)


def _day_start(day: date, tz: tzinfo | None) -> int:
    return epoch_ms(datetime(day.year, day.month, day.day), tz)


def _month_after(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def _day_words(day: date) -> str:
    return f"{_MONTHS[day.month - 1]} {day.day}, {day.year}"


def _parse_day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise PeriodError(text) from None


_MONTH_RE = re.compile(r"([0-9]{4})-([0-9]{2})")
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
PERIOD_HELP = "today, yesterday, this week, last week, this month, last month, a month like 2026-10, a day like 2026-11-03, or a range like 2026-11-01..2026-11-15"


def parse_period(text: str | None, now_ms: int, tz: tzinfo | None = None) -> Period:
    """The period a spoken or typed name means, in the hub's local time; weeks start on Monday.
    Default: this month. Raises PeriodError."""
    spoken = " ".join((text or "this month").lower().replace("_", " ").split())
    today = local_datetime(now_ms, tz).date()
    monday = today - timedelta(days=today.weekday())
    first = today.replace(day=1)

    def days(start: date, end: date, label: str) -> Period:
        return Period(_day_start(start, tz), _day_start(end, tz), label)

    match spoken:
        case "today":
            return days(today, today + timedelta(days=1), "Today")
        case "yesterday":
            return days(today - timedelta(days=1), today, "Yesterday")
        case "this week":
            return days(monday, monday + timedelta(days=7), "This week")
        case "last week":
            return days(monday - timedelta(days=7), monday, "Last week")
        case "this month":
            return days(first, _month_after(first), "This month")
        case "last month":
            previous = (first - timedelta(days=1)).replace(day=1)
            return days(previous, first, "Last month")
    if ".." in spoken:
        start_text, _, end_text = spoken.partition("..")
        start, end = _parse_day(start_text.strip()), _parse_day(end_text.strip())
        if end < start:
            raise PeriodError(spoken)
        return days(start, end + timedelta(days=1), f"From {_day_words(start)} to {_day_words(end)}")
    if _DAY_RE.fullmatch(spoken):
        day = _parse_day(spoken)
        return days(day, day + timedelta(days=1), _day_words(day))
    found = _MONTH_RE.fullmatch(spoken)
    if found is not None:
        year, month = int(found.group(1)), int(found.group(2))
        if not 1 <= month <= 12 or year < 1970:
            raise PeriodError(spoken)
        start = date(year, month, 1)
        return days(start, _month_after(start), f"{_MONTHS[month - 1]} {year}")
    raise PeriodError(spoken)


# --- intervals -----------------------------------------------------------------------------------

Interval = tuple[int, int]


def active_intervals(times: Iterable[int], idle_gap_ms: int) -> list[Interval]:
    """Runs of events at most `idle_gap_ms` apart, each at least MIN_INTERVAL_MS long."""
    ordered = sorted(times)
    out: list[Interval] = []
    if not ordered:
        return out
    start = end = ordered[0]
    for moment in ordered[1:]:
        if moment - end > idle_gap_ms:
            out.append((start, max(end, start + MIN_INTERVAL_MS)))
            start = moment
        end = moment
    out.append((start, max(end, start + MIN_INTERVAL_MS)))
    return out


def clip(intervals: Iterable[Interval], start: int, end: int) -> list[Interval]:
    out = []
    for a, b in intervals:
        low, high = max(a, start), min(b, end)
        if high > low:
            out.append((low, high))
    return out


def union(intervals: Iterable[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def length(intervals: Iterable[Interval]) -> int:
    return sum(b - a for a, b in intervals)


# --- tokens --------------------------------------------------------------------------------------


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0

    def add(self, other: Tokens | Record) -> None:
        self.input += other.input
        self.output += other.output
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h
        self.cache_read += other.cache_read

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_write_5m + self.cache_write_1h + self.cache_read


def _merge(a: Record, b: Record) -> Record:
    """Two lines of one message: the earlier time, the larger count of each type."""
    return Record(
        t=min(a.t, b.t),
        kind="a",
        model=a.model or b.model,
        msg_id=a.msg_id,
        req_id=a.req_id,
        input=max(a.input, b.input),
        output=max(a.output, b.output),
        cache_write_5m=max(a.cache_write_5m, b.cache_write_5m),
        cache_write_1h=max(a.cache_write_1h, b.cache_write_1h),
        cache_read=max(a.cache_read, b.cache_read),
        cost_usd=a.cost_usd if a.cost_usd is not None else b.cost_usd,
    )


# --- report --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionUsage:
    """One transcript (or the summariser's runs) with its records."""

    machine: str
    id: str
    project: str
    records: Sequence[Record]
    activity: bool = True
    """False for token-only sources (the summariser's runs): they add no active time."""


@dataclass
class Row:
    key: tuple[str, ...]
    """(project), (machine, project, session id) or (day as YYYY-MM-DD)."""
    sessions: int = 0
    active_ms: int = 0
    billable_ms: int = 0
    amount: float = 0.0
    tokens: Tokens = field(default_factory=Tokens)
    cost_usd: float = 0.0


@dataclass
class UsageReport:
    period: Period
    by: Grouping
    rows: list[Row]
    total: Row
    unpriced_models: list[str]
    hourly_rate: float
    currency: str


@dataclass(frozen=True)
class ComputeOptions:
    idle_gap_ms: int
    overlap: Overlap
    hourly_rate: float
    currency: str
    prices: PriceTable
    tz: tzinfo | None = None


@dataclass
class _Session:
    source: SessionUsage
    intervals: list[Interval]
    """Clipped to the period."""
    records: list[Record]
    """Deduplicated assistant records inside the period."""


def _prepare(sessions: Sequence[SessionUsage], period: Period, options: ComputeOptions) -> list[_Session]:
    owner: dict[tuple[str, str, str], tuple[int, Record]] = {}
    plain: list[list[Record]] = [[] for _ in sessions]
    # On a tie (a copy keeps the original time) the session that started first keeps the message.
    started = [min((r.t for r in s.records), default=0) for s in sessions]
    for index, session in enumerate(sessions):
        for record in session.records:
            if record.kind != "a":
                continue
            if record.msg_id is None:
                plain[index].append(record)
                continue
            key = (session.machine, record.msg_id, record.req_id or "")
            seen = owner.get(key)
            if seen is None:
                owner[key] = (index, record)
            else:
                mine, theirs = (record.t, started[index]), (seen[1].t, started[seen[0]])
                keeper = index if mine < theirs else seen[0]
                owner[key] = (keeper, _merge(seen[1], record))
    for index, record in owner.values():
        plain[index].append(record)
    out = []
    for index, session in enumerate(sessions):
        assistant = plain[index]
        times = [r.t for r in assistant] + [r.t for r in session.records if r.kind == "u"]
        intervals = active_intervals(times, options.idle_gap_ms) if session.activity else []
        out.append(
            _Session(
                source=session,
                intervals=clip(intervals, period.start_ms, period.end_ms),
                records=[r for r in assistant if period.start_ms <= r.t < period.end_ms],
            )
        )
    return out


def _cost(record: Record, prices: PriceTable, unpriced: set[str]) -> float:
    if record.cost_usd is not None:
        return record.cost_usd
    price = prices.price(record.model)
    if price is None:
        if record.input + record.output + record.cache_write_5m + record.cache_write_1h + record.cache_read:
            unpriced.add(record.model or "unknown")
        return 0.0
    return (
        record.input * price.input
        + record.output * price.output
        + record.cache_write_5m * price.cache_write_5m
        + record.cache_write_1h * price.cache_write_1h
        + record.cache_read * price.cache_read
    ) / 1_000_000


def _project(session: SessionUsage) -> str:
    return session.project or UNKNOWN_PROJECT


def _billable(groups: Iterable[list[Interval]], overlap: Overlap) -> int:
    """Billable time of one project: the union or the plain sum of its sessions' intervals."""
    lists = list(groups)
    if overlap == "sum":
        return sum(length(intervals) for intervals in lists)
    return length(union(interval for intervals in lists for interval in intervals))


def _amount(billable_ms: int, rate: float) -> float:
    return round(billable_ms / 3_600_000 * rate, 2)


def _days(period: Period, tz: tzinfo | None) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    day = local_datetime(period.start_ms, tz).date()
    while True:
        start = _day_start(day, tz)
        if start >= period.end_ms:
            return out
        end = min(_day_start(day + timedelta(days=1), tz), period.end_ms)
        out.append((day.isoformat(), max(start, period.start_ms), end))
        day += timedelta(days=1)


def compute(
    sessions: Sequence[SessionUsage], period: Period, options: ComputeOptions, by: Grouping = "project"
) -> UsageReport:
    prepared = _prepare(sessions, period, options)
    unpriced: set[str] = set()
    costs = {id(s): sum(_cost(r, options.prices, unpriced) for r in s.records) for s in prepared}
    used = [s for s in prepared if s.intervals or s.records]

    by_project: dict[str, list[_Session]] = {}
    for session in used:
        by_project.setdefault(_project(session.source), []).append(session)
    project_billable = {
        name: _billable((s.intervals for s in group), options.overlap) for name, group in by_project.items()
    }

    def row_of(key: tuple[str, ...], group: Sequence[_Session], billable_ms: int) -> Row:
        row = Row(key=key, billable_ms=billable_ms)
        row.sessions = sum(1 for s in group if s.source.activity)
        row.active_ms = sum(length(s.intervals) for s in group)
        for session in group:
            for record in session.records:
                row.tokens.add(record)
            row.cost_usd += costs[id(session)]
        row.amount = _amount(billable_ms, options.hourly_rate)
        return row

    rows: list[Row]
    if by == "project":
        rows = [row_of((name,), group, project_billable[name]) for name, group in by_project.items()]
        rows.sort(key=lambda r: (-r.billable_ms, -r.cost_usd, r.key))
    elif by == "session":
        rows = [row_of((s.source.machine, _project(s.source), s.source.id), [s], length(s.intervals)) for s in used]
        rows.sort(key=lambda r: (-r.billable_ms, -r.cost_usd, r.key))
    else:
        rows = []
        for label, start, end in _days(period, options.tz):
            day_sessions = []
            billable = 0
            for group in by_project.values():
                clipped = [
                    _Session(
                        source=s.source,
                        intervals=clip(s.intervals, start, end),
                        records=[r for r in s.records if start <= r.t < end],
                    )
                    for s in group
                ]
                clipped = [s for s in clipped if s.intervals or s.records]
                billable += _billable((s.intervals for s in clipped), options.overlap)
                day_sessions += clipped
            if not day_sessions:
                continue
            row = Row(key=(label,), billable_ms=billable)
            row.sessions = sum(1 for s in day_sessions if s.source.activity and s.intervals)
            row.active_ms = sum(length(s.intervals) for s in day_sessions)
            for session in day_sessions:
                for record in session.records:
                    row.tokens.add(record)
                    row.cost_usd += _cost(record, options.prices, set())
            row.amount = _amount(billable, options.hourly_rate)
            rows.append(row)

    total = Row(key=("total",))
    total.sessions = sum(1 for s in used if s.source.activity)
    total.active_ms = sum(length(s.intervals) for s in used)
    total.billable_ms = sum(project_billable.values())
    total.amount = round(sum(_amount(ms, options.hourly_rate) for ms in project_billable.values()), 2)
    for session in used:
        for record in session.records:
            total.tokens.add(record)
        total.cost_usd += costs[id(session)]
    return UsageReport(
        period=period,
        by=by,
        rows=rows,
        total=total,
        unpriced_models=sorted(unpriced),
        hourly_rate=options.hourly_rate,
        currency=options.currency,
    )


def top_project(report: UsageReport) -> tuple[str, int] | None:
    """(project, billable ms) with the most billable time, when the report is by project."""
    if report.by != "project" or not report.rows or report.rows[0].billable_ms <= 0:
        return None
    return report.rows[0].key[0], report.rows[0].billable_ms
