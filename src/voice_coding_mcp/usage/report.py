"""Usage as spoken text (`usage_report`), as a CLI table and as CSV (docs/SWITCHBOARD.md, "usage_report")."""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass, field

from ..remote.redact import clean_name
from ..remote.speech import quote
from ..text import plural
from .compute import Row, UsageReport, top_project

HOUR_MS = 3_600_000


@dataclass(frozen=True)
class SourceNote:
    """A machine whose records could not be refreshed."""

    name: str
    """Spoken name: the machine, or "this machine" for the hub's own records."""
    reason: str | None = None
    cached_ago: str | None = None
    """How old the records used instead are ("3 hours ago"), or None when there are none."""


@dataclass
class ReportContext:
    scope: str = ""
    """Spoken after the period: " on buildbox, project api"."""
    skipped: int = 0
    """Transcripts too large to read that may hold activity in the period."""
    notes: list[SourceNote] = field(default_factory=list)


def hours_words(ms: int) -> str:
    minutes = round(ms / 60_000)
    if minutes < 60:
        return plural(minutes, "minute")
    hours = ms / HOUR_MS
    if hours < 10:
        value = f"{hours:.1f}".rstrip("0").rstrip(".")
        return f"{value} {'hour' if value == '1' else 'hours'}"
    return plural(round(hours), "hour")


def count_words(count: int) -> str:
    for size, word in ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand")):
        if count >= size:
            value = count / size
            text = f"{value:.1f}".rstrip("0").rstrip(".") if value < 10 else str(round(value))
            return f"{text} {word}"
    return str(count)


def money_words(amount: float, currency: str) -> str:
    text = str(round(amount)) if abs(amount - round(amount)) < 0.005 else f"{amount:.2f}"
    return f"{text} {currency}"


def rate_words(rate: float) -> str:
    return str(round(rate)) if rate == round(rate) else f"{rate:.2f}"


def dollars_words(amount: float) -> str:
    if amount < 1:
        return "less than 1 dollar"
    if amount < 10:
        return f"about {amount:.1f}".rstrip("0").rstrip(".") + " dollars"
    return f"about {round(amount)} dollars"


def _tokens_words(row: Row) -> str:
    tokens = row.tokens
    parts = [f"{count_words(tokens.input)} input", f"{count_words(tokens.output)} output"]
    if tokens.cache_read:
        parts.append(f"{count_words(tokens.cache_read)} cache reads")
    writes = tokens.cache_write_5m + tokens.cache_write_1h
    if writes:
        parts.append(f"{count_words(writes)} cache writes")
    return ", ".join(parts)


def _note_words(note: SourceNote) -> str:
    reason = f" ({note.reason})" if note.reason else ""
    if note.cached_ago is not None:
        return f"{note.name} could not be read{reason}; I used its records from {note.cached_ago}."
    return f"{note.name} could not be read{reason}; the numbers are without it."


def format_spoken(report: UsageReport, context: ReportContext) -> str:
    total = report.total
    head = f"{report.period.label}{context.scope}"
    sentences: list[str] = []
    if total.billable_ms <= 0 and total.tokens.total <= 0:
        sentences.append(f"{head}: no Claude Code activity.")
    else:
        sessions = plural(total.sessions, "session")
        hours = hours_words(total.billable_ms)
        active = hours.replace(" ", " active ", 1)
        if total.billable_ms > 0:
            sentences.append(
                f"{head}: {active} in {sessions}, {money_words(total.amount, report.currency)} "
                f"at {rate_words(report.hourly_rate)} per hour."
            )
        else:
            sentences.append(f"{head}: no active time.")
        if total.tokens.total > 0:
            unpriced = len(report.unpriced_models)
            if total.cost_usd > 0:
                extra = f", plus tokens of {plural(unpriced, 'model')} without a price" if unpriced else ""
                sentences.append(
                    f"API-equivalent token cost {dollars_words(total.cost_usd)}: {_tokens_words(total)}{extra}."
                )
            else:
                sentences.append(f"Tokens: {_tokens_words(total)}, from {plural(unpriced, 'model')} without a price.")
        top = top_project(report)
        if top is not None and len(report.rows) > 1:
            sentences.append(f"Most time: {quote(clean_name(top[0]))}, {hours_words(top[1])}.")
    if context.skipped:
        were = "was" if context.skipped == 1 else "were"
        sentences.append(f"{plural(context.skipped, 'transcript')} {were} too large and skipped.")
    sentences += [_note_words(note) for note in context.notes]
    return " ".join(sentences)


# --- CLI table and CSV ---------------------------------------------------------------------------

_KEY_COLUMNS = {"project": ("project",), "session": ("machine", "project", "session"), "day": ("day",)}
_VALUE_COLUMNS = (
    "sessions",
    "active_hours",
    "billable_hours",
    "amount",
    "currency",
    "input",
    "output",
    "cache_write_5m",
    "cache_write_1h",
    "cache_read",
    "cost_usd",
)


def _hours(ms: int) -> str:
    return f"{ms / HOUR_MS:.2f}"


def _cell(text: str) -> str:
    """A name from a machine, fit for a table cell or a spreadsheet: no control characters, and no
    leading character a spreadsheet would read as a formula."""
    name = clean_name(text, 64)
    return "'" + name if name[:1] in ("=", "+", "-", "@") else name


def _row_cells(report: UsageReport, row: Row, *, total: bool) -> list[str]:
    width = len(_KEY_COLUMNS[report.by])
    keys = ["total", *[""] * (width - 1)] if total else [_cell(part) for part in row.key]
    t = row.tokens
    return [
        *keys,
        str(row.sessions),
        _hours(row.active_ms),
        _hours(row.billable_ms),
        f"{row.amount:.2f}",
        report.currency,
        str(t.input),
        str(t.output),
        str(t.cache_write_5m),
        str(t.cache_write_1h),
        str(t.cache_read),
        f"{row.cost_usd:.2f}",
    ]


def table_rows(report: UsageReport) -> list[list[str]]:
    """Header, one row per group, then the total."""
    header = [*_KEY_COLUMNS[report.by], *_VALUE_COLUMNS]
    return [
        header,
        *(_row_cells(report, row, total=False) for row in report.rows),
        _row_cells(report, report.total, total=True),
    ]


def format_csv(report: UsageReport) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(table_rows(report))
    return out.getvalue()


def format_table(report: UsageReport, notes: Sequence[str] = ()) -> str:
    rows = table_rows(report)
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    keys = len(_KEY_COLUMNS[report.by])
    lines = [f"{report.period.label}, {report.currency} {rate_words(report.hourly_rate)} per billable hour"]
    for row in rows:
        cells = [
            cell.ljust(widths[i]) if i < keys or i == keys + 4 else cell.rjust(widths[i]) for i, cell in enumerate(row)
        ]
        lines.append("  ".join(cells).rstrip())
    if report.unpriced_models:
        names = ", ".join(_cell(name) for name in report.unpriced_models)
        lines.append(f"Models without a price (tokens counted, cost not): {names}")
    lines += list(notes)
    return "\n".join(lines) + "\n"
