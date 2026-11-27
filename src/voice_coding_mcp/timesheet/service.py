"""Builds timesheet drafts from the usage caches, the voice activity log and the work products
(docs/SWITCHBOARD.md, "Timesheet")."""

from __future__ import annotations

import re
import zoneinfo
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta, tzinfo
from typing import Any

from ..clock import HOUR_MS, now_ms, to_iso
from ..config import Config
from ..usage.cache import HUB
from ..usage.compute import Period, PeriodError, _day_start, local_datetime, parse_period
from ..usage.service import SourceOutcome, UsageService, failure_reason, spoken_source
from .activity import read_voice
from .compute import Source, VoiceEvent, compute_time, days_of, round_minutes
from .config import TimesheetConfig
from .store import TimesheetStore
from .work import WorkCollector, WorkProducts

PERIOD_HELP = (
    "last week (the default), this week, last month, this month, a month like 2026-10, "
    "a week like 2026-W47, or days like 2026-11-16..2026-11-22"
)
_WEEK_RE = re.compile(r"([0-9]{4})-w([0-9]{2})")
_ID_RE = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})_([0-9]{4}-[0-9]{2}-[0-9]{2})")


def owner_tz(config: TimesheetConfig, fallback: tzinfo | None = None) -> tzinfo | None:
    return zoneinfo.ZoneInfo(config.time_zone) if config.time_zone else fallback


def parse_timesheet_period(text: str | None, now: int, tz: tzinfo | None) -> Period:
    """A period in the owner's time zone; default last week (Monday to Sunday). Also accepts an
    ISO week (`2026-W47`) and a period id (`2026-11-16_2026-11-22`). Raises PeriodError."""
    spoken = " ".join((text or "last week").lower().split())
    found = _WEEK_RE.fullmatch(spoken)
    if found is not None:
        try:
            monday = date.fromisocalendar(int(found.group(1)), int(found.group(2)), 1)
        except ValueError:
            raise PeriodError(spoken) from None
        return Period(_day_start(monday, tz), _day_start(monday + timedelta(days=7), tz), f"Week {spoken.upper()}")
    found = _ID_RE.fullmatch(spoken)
    if found is not None:
        spoken = f"{found.group(1)}..{found.group(2)}"
    return parse_period(spoken, now, tz)


def period_bounds(period: Period, tz: tzinfo | None) -> tuple[str, str, str]:
    """(id, first day, last day) of a period."""
    first = local_datetime(period.start_ms, tz).date().isoformat()
    last = local_datetime(period.end_ms - 1, tz).date().isoformat()
    return f"{first}_{last}", first, last


def tz_name(tz: tzinfo | None) -> str:
    if tz is None:
        return "local"
    key = getattr(tz, "key", None)
    return key if isinstance(key, str) else str(tz)


class TimesheetService:
    def __init__(
        self,
        config: Config,
        usage: UsageService,
        *,
        collector: WorkCollector | None = None,
        now: Callable[[], int] | None = None,
        tz: tzinfo | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self.settings = config.timesheet
        self.usage = usage
        self._now = now or now_ms
        self.tz = tz if tz is not None else owner_tz(self.settings, usage.tz)
        self.collector = collector or WorkCollector(self.settings, env=env)
        self.store = TimesheetStore(config.state_dir, now=self._now)

    @property
    def client_of(self) -> dict[str, str]:
        """Configured project -> client id."""
        return {name: p.client for name, p in self.settings.projects.items()}

    def period(self, text: str | None) -> Period:
        return parse_timesheet_period(text, self._now(), self.tz)

    def period_id(self, period: Period) -> str:
        return period_bounds(period, self.tz)[0]

    def sources(self) -> list[str]:
        return self.usage.sources()

    async def refresh(self, names: Sequence[str]) -> dict[str, SourceOutcome]:
        return await self.usage.refresh(names)

    def _transcripts(self, names: Sequence[str], period: Period) -> tuple[list[Source], list[str]]:
        sources: list[Source] = []
        notes: list[str] = []
        skipped = 0
        for name, cache in self.usage.caches(names).items():
            sessions_without_entrypoint = 0
            sessions = 0
            for transcript in cache.files.values():
                if transcript.skipped:
                    if transcript.mtime >= period.start_ms:
                        skipped += 1
                    continue
                if not any(period.start_ms - HOUR_MS <= r.t < period.end_ms + HOUR_MS for r in transcript.records):
                    continue
                sources.append(Source(name, transcript))
                if not transcript.is_agent_file and transcript.records:
                    sessions += 1
                    sessions_without_entrypoint += transcript.entrypoint is None
            if name != HUB and sessions and sessions_without_entrypoint == sessions:
                notes.append(
                    f"{spoken_source(name)}: no session names its entrypoint; its switchboard-remote is probably older "
                    "than 0.10.0, so its headless runs count as owner time, task notifications as owner prompts, and "
                    "its subagents are missing; update it there"
                )
        if skipped:
            notes.append(f"{skipped} transcript(s) larger than usage.maxTranscriptMegabytes were not read")
        return sources, notes

    def _rename(self) -> tuple[Callable[[str], str], dict[str, list[str]]]:
        """Folder name -> configured project (or the folder itself), and the folders that matched several."""
        ambiguous: dict[str, list[str]] = {}
        cache: dict[str, str] = {}

        def rename(folder: str) -> str:
            found = cache.get(folder)
            if found is not None:
                return found
            project, candidates = self.settings.project_of(folder)
            if project is None and len(candidates) > 1:
                ambiguous[folder] = candidates
            cache[folder] = project or folder
            return cache[folder]

        return rename, ambiguous

    async def build(
        self,
        period: Period,
        names: Sequence[str],
        outcomes: Mapping[str, SourceOutcome] | None = None,
        work: WorkProducts | None = None,
    ) -> dict[str, Any]:
        """The draft document of a period (not yet saved)."""
        settings = self.settings
        sources, notes = self._transcripts(names, period)
        calls, bad = read_voice(self._config.state_dir, period.start_ms, period.end_ms)
        if bad:
            notes.append(f"{bad} unreadable line(s) in the voice activity log")
        rename, ambiguous = self._rename()
        result = compute_time(
            sources,
            [VoiceEvent(c.start_ms, c.end_ms, c.project) for c in calls],
            period,
            idle_gap_ms=self._config.usage.idle_gap_minutes * 60_000,
            owner_overlap=settings.owner_overlap,
            tz=self.tz,
            rename=rename,
        )
        if work is None:
            work = await self.collector.collect(period.start_ms, period.end_ms)
        notes += work.notes
        for name, outcome in (outcomes or {}).items():
            if not outcome.ok:
                notes.append(
                    f"{spoken_source(name)} could not be read ({failure_reason(outcome)}); its cached records are used"
                )
        for folder, candidates in sorted(ambiguous.items()):
            notes.append(f"the folder {folder} matches several projects ({', '.join(candidates)}); it is unassigned")
        period_id, first, last = period_bounds(period, self.tz)
        day_labels = [label for label, _, _ in days_of(period, self.tz)]

        def day_of(ms: int) -> str:
            return local_datetime(ms, self.tz).date().isoformat()

        projects: dict[str, dict[str, dict[str, Any]]] = {}

        def entry(project: str, day: str) -> dict[str, Any]:
            return projects.setdefault(project, {}).setdefault(
                day,
                {
                    "date": day,
                    "ownerMs": 0,
                    "agentMs": 0,
                    "ownerOverlapMs": 0,
                    "ownerMinutes": 0,
                    "agentMinutes": 0,
                    "commits": [],
                    "pullRequests": [],
                },
            )

        step, mode = settings.rounding_minutes, settings.rounding_mode
        for project, days in result.days.items():
            for day, times in days.items():
                item = entry(project, day)
                item["ownerMs"] = round(times.owner_ms)
                item["agentMs"] = round(times.agent_ms)
                item["ownerOverlapMs"] = round(times.owner_overlap_ms)
                item["ownerMinutes"] = round_minutes(times.owner_ms, step, mode) if times.owner_ms else 0
                item["agentMinutes"] = round_minutes(times.agent_ms, step, mode) if times.agent_ms else 0
        for project, commits in work.commits.items():
            for commit in commits:
                day = day_of(commit.time_ms)
                if day in day_labels:
                    entry(project, day)["commits"].append(
                        {
                            "sha": commit.sha,
                            "time": to_iso(commit.time_ms),
                            "subject": commit.subject,
                            "repo": commit.repo,
                            **({"url": commit.url} if commit.url else {}),
                        }
                    )
        for project, pulls in work.pulls.items():
            for pull in pulls:
                events: dict[str, list[str]] = {}
                if period.start_ms <= pull.created_ms < period.end_ms:
                    events.setdefault(day_of(pull.created_ms), []).append("opened")
                if pull.merged_ms is not None and period.start_ms <= pull.merged_ms < period.end_ms:
                    events.setdefault(day_of(pull.merged_ms), []).append("merged")
                for day, kinds in events.items():
                    entry(project, day)["pullRequests"].append(
                        {
                            "repo": pull.repo,
                            "number": pull.number,
                            "title": pull.title,
                            "url": pull.url,
                            "events": kinds,
                        }
                    )

        def project_doc(name: str) -> dict[str, Any]:
            days = [projects[name][d] for d in sorted(projects[name])]
            days = [d for d in days if d["ownerMinutes"] or d["agentMinutes"] or d["commits"] or d["pullRequests"]]
            return {"project": name, "days": days, "notes": []}

        clients = []
        for client_id, client in sorted(settings.clients.items()):
            mine = [name for name, p in sorted(settings.projects.items()) if p.client == client_id]
            docs = [project_doc(name) for name in mine if name in projects]
            clients.append({"id": client_id, "name": client.name, "projects": [d for d in docs if d["days"]]})
        unassigned = [project_doc(name) for name in sorted(projects) if name not in settings.projects]
        unassigned = [d for d in unassigned if d["days"]]
        if unassigned:
            notes.append(
                "unassigned folders (add them to timesheet.projects or as aliases): "
                + ", ".join(d["project"] for d in unassigned)
            )
        if result.wall_owner_ms:
            notes.append(f"wall-clock owner time over all projects: {result.wall_owner_ms / HOUR_MS:.2f} h")
        return {
            "format": "switchboard-timesheet",
            "version": 1,
            "status": "draft",
            "period": {
                "id": period_id,
                "start": first,
                "end": last,
                "label": period.label,
                "timeZone": tz_name(self.tz),
            },
            "generatedAt": to_iso(self._now()),
            "rules": {
                "idleGapMinutes": self._config.usage.idle_gap_minutes,
                "ownerOverlap": settings.owner_overlap,
                "roundingMinutes": step,
                "roundingMode": mode,
            },
            "clients": clients,
            "unassigned": unassigned,
            "notes": notes,
        }

    async def draft(
        self, period: Period, names: Sequence[str], outcomes: Mapping[str, SourceOutcome] | None = None
    ) -> dict[str, Any]:
        """Builds and saves the draft; returns the view with the adjustments applied."""
        document = await self.build(period, names, outcomes)
        return self.store.save_draft(document, self.client_of)


# --- speech ----------------------------------------------------------------------------------------


def spoken_hours(minutes: int, kind: str = "") -> str:
    """`14.5 owner hours`, `45 owner minutes`, `1 owner hour`; `kind` may be empty."""
    word = f"{kind} " if kind else ""
    if minutes <= 0:
        return f"no {word}time"
    if minutes < 60:
        return f"{minutes} {word}minute{'' if minutes == 1 else 's'}"
    text = f"{minutes / 60:.1f}".rstrip("0").rstrip(".")
    return f"{text} {word}hour{'' if text == '1' else 's'}"


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def spoken_summary(view: dict[str, Any]) -> str:
    """`Last week: nilware 14.5 owner hours and 20 agent hours across 2 projects, 9 commits, 2 pull
    requests; 3 hours unassigned. This is a draft.`"""
    label = view["period"].get("label") or f"{view['period']['start']} to {view['period']['end']}"
    parts = []
    for client in view["clients"]:
        total = client["total"]
        if not (total["ownerMinutes"] or total["agentMinutes"] or total["commits"] or total["pullRequests"]):
            continue
        parts.append(
            f"{client['name']} {spoken_hours(total['ownerMinutes'], 'owner')}"
            f" and {spoken_hours(total['agentMinutes'], 'agent')}"
            f" across {_count(len(client['projects']), 'project')}, {_count(total['commits'], 'commit')},"
            f" {_count(total['pullRequests'], 'pull request')}"
        )
    text = f"{label}: " + ("; ".join(parts) if parts else "no billable work for any client")
    unassigned = view.get("unassignedTotal", {})
    loose = unassigned.get("ownerMinutes", 0) + unassigned.get("agentMinutes", 0)
    if loose:
        text += f"; {spoken_hours(loose)} unassigned"
    status = view.get("status")
    text += ". It is approved." if status == "approved" else ". This is a draft, not approved."
    return text
