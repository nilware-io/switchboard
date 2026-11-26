"""Refreshes the usage caches (machines through the dispatcher's `usage` verb, the hub's own Claude
Code home locally) and builds reports from them (docs/SWITCHBOARD.md, "Usage computation")."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import tzinfo

from ..clock import MINUTE_MS, ago, now_ms
from ..config import Config
from ..log import Logger
from ..projects import MatchOk, match_by_name
from ..remote.config import MachineConfig
from ..remote.protocol import USAGE_TIMEOUT_SECONDS, UsageResponse
from ..remote.speech import unreachable_reason
from ..remote.transport import Transport, TransportError
from ..sessions import claude_home_dir
from .cache import HUB, MachineCache, apply_batch, known_list, known_map, load_cache, retention_start, save_cache
from .compute import ComputeOptions, Grouping, Period, SessionUsage, UsageReport, compute
from .prices import PriceTable
from .records import SUMMARY_PROJECT, read_summaries, scan_local
from .report import ReportContext, SourceNote

FRESH_MS = 10 * MINUTE_MS
"""Caches younger than this answer `usage_report` at once, without a refresh."""
SUMMARIES_ID = "summaries"


@dataclass(frozen=True)
class SourceOutcome:
    name: str
    """Machine name, or `local` for the hub's own Claude Code home."""
    ok: bool
    error: TransportError | None = None
    detail: str | None = None


@dataclass(frozen=True)
class ProjectProblem:
    spoken: str
    candidates: list[str]
    ambiguous: bool


def spoken_source(name: str) -> str:
    return "this machine" if name == HUB else name


def failure_reason(outcome: SourceOutcome) -> str:
    if outcome.error == "refused_by_policy" and outcome.detail == "not_allowed":
        return "its switchboard-remote does not allow usage, add --allow-usage"
    if outcome.error == "refused_by_policy" and outcome.detail == "bad_request":
        return "its switchboard-remote is too old for usage"
    if outcome.name == HUB:
        return "its Claude Code home could not be read"
    return unreachable_reason(outcome.error, outcome.detail)


class UsageService:
    def __init__(
        self,
        config: Config,
        transports: Mapping[str, Transport],
        logger: Logger,
        *,
        now: Callable[[], int] | None = None,
        hub_claude_home: str | None = None,
        tz: tzinfo | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._usage = config.usage
        self._transports = dict(transports)
        self._logger = logger.child({"component": "usage"})
        self._now = now or now_ms
        self._hub_home = hub_claude_home or claude_home_dir(config.claude.config_dir)
        self.tz = tz
        self._env = env if env is not None else os.environ
        self._prices = PriceTable(config.usage.prices)

    # --- sources ---------------------------------------------------------------------------------

    @property
    def machines(self) -> list[MachineConfig]:
        return list(self._config.remote.machines)

    def includes_hub(self) -> bool:
        """The hub's own Claude Code home counts, unless a `local` machine already reads the same one."""
        own = os.path.realpath(self._hub_home)
        for machine in self.machines:
            if machine.transport != "local":
                continue
            configured = self._env.get("CLAUDE_CONFIG_DIR")
            home = machine.claude_home or (
                configured
                if configured and os.path.isabs(configured)
                else os.path.join(self._env.get("HOME") or os.path.expanduser("~"), ".claude")
            )
            if os.path.realpath(home) == own:
                return False
        return True

    def sources(self, machine: str | None = None) -> list[str]:
        """Machines with `allowUsage` (or just `machine`), plus `local` for the hub's own home."""
        names = [m.name for m in self.machines if m.allow_usage and (machine is None or m.name == machine)]
        if machine in (None, HUB) and self.includes_hub():
            names.append(HUB)
        return names

    def caches(self, names: Sequence[str]) -> dict[str, MachineCache]:
        return {name: load_cache(self._config.state_dir, name) for name in names}

    def fresh(self, names: Sequence[str]) -> bool:
        now = self._now()
        for cache in self.caches(names).values():
            if cache.refreshed_ms is None or now - cache.refreshed_ms > FRESH_MS:
                return False
        return True

    # --- refresh ---------------------------------------------------------------------------------

    async def refresh(self, names: Sequence[str]) -> dict[str, SourceOutcome]:
        results = await asyncio.gather(*(self._refresh_one(name) for name in names))
        return {outcome.name: outcome for outcome in results}

    async def _refresh_one(self, name: str) -> SourceOutcome:
        if name == HUB:
            return await asyncio.to_thread(self._refresh_hub)
        transport = self._transports.get(name)
        if transport is None:
            return SourceOutcome(name=name, ok=False, error="other")
        state_dir = self._config.state_dir
        cache = await asyncio.to_thread(load_cache, state_dir, name)
        since = retention_start(self._now()) // 1000
        result = await transport.call(
            "usage",
            [str(max(0, since)), str(self._usage.max_transcript_megabytes)],
            timeout_seconds=USAGE_TIMEOUT_SECONDS,
            stdin=known_list(cache),
        )
        self._logger.info(
            "usage refresh",
            {"machine": name, "ok": result.ok, "error": result.error, "detail": result.detail, "ms": result.elapsed_ms},
        )
        if not result.ok or not isinstance(result.data, UsageResponse):
            return SourceOutcome(name=name, ok=False, error=result.error or "bad_response", detail=result.detail)
        batch = result.data.batch

        def store() -> None:
            apply_batch(cache, batch, self._now())
            save_cache(state_dir, cache)

        try:
            await asyncio.to_thread(store)
        except OSError as err:
            self._logger.warn("usage cache not saved", {"machine": name, "error": type(err).__name__})
            return SourceOutcome(name=name, ok=False, error="other")
        return SourceOutcome(name=name, ok=True)

    def _refresh_hub(self) -> SourceOutcome:
        state_dir = self._config.state_dir
        cache = load_cache(state_dir, HUB)
        now = self._now()
        try:
            batch = scan_local(
                self._hub_home,
                max(0, retention_start(now)),
                self._usage.max_transcript_megabytes * 1024 * 1024,
                known_map(cache),
            )
            apply_batch(cache, batch, now)
            save_cache(state_dir, cache)
        except (OSError, ValueError) as err:
            self._logger.warn("usage refresh", {"machine": HUB, "ok": False, "error": type(err).__name__})
            return SourceOutcome(name=HUB, ok=False, error="other")
        self._logger.info("usage refresh", {"machine": HUB, "ok": True, "files": len(batch.files)})
        return SourceOutcome(name=HUB, ok=True)

    # --- reports ---------------------------------------------------------------------------------

    def sessions(self, names: Sequence[str], period: Period | None = None) -> tuple[list[SessionUsage], int]:
        """Every cached transcript of `names` as a session (plus the summariser's runs with the hub),
        and how many transcripts were skipped as too large (modified inside the period, if given)."""
        out: list[SessionUsage] = []
        skipped = 0
        for name, cache in self.caches(names).items():
            for transcript in cache.files.values():
                if transcript.skipped:
                    if period is None or transcript.mtime >= period.start_ms:
                        skipped += 1
                    continue
                out.append(SessionUsage(name, transcript.id, transcript.project, transcript.records))
        if HUB in names:
            summaries = read_summaries(self._config.state_dir, retention_start(self._now()))
            if summaries:
                out.append(SessionUsage(HUB, SUMMARIES_ID, SUMMARY_PROJECT, summaries, activity=False))
        return out, skipped

    def projects(self, names: Sequence[str]) -> list[str]:
        sessions, _ = self.sessions(names)
        return sorted({s.project for s in sessions if s.project})

    def match_project(self, names: Sequence[str], spoken: str) -> str | ProjectProblem:
        projects = self.projects(names)
        found = match_by_name(projects, lambda p: p, spoken)
        if isinstance(found, MatchOk):
            return found.item
        return ProjectProblem(spoken=spoken, candidates=found.candidates, ambiguous=found.reason == "ambiguous")

    def options(self) -> ComputeOptions:
        return ComputeOptions(
            idle_gap_ms=self._usage.idle_gap_minutes * MINUTE_MS,
            overlap=self._usage.overlap,
            hourly_rate=self._usage.hourly_rate,
            currency=self._usage.currency,
            prices=self._prices,
            tz=self.tz,
        )

    def report(
        self,
        names: Sequence[str],
        period: Period,
        *,
        project: str | None = None,
        by: Grouping = "project",
        outcomes: Mapping[str, SourceOutcome] | None = None,
    ) -> tuple[UsageReport, ReportContext]:
        sessions, skipped = self.sessions(names, period)
        if project is not None:
            sessions = [s for s in sessions if s.project == project]
        report = compute(sessions, period, self.options(), by)
        context = ReportContext(skipped=skipped)
        now = self._now()
        caches = self.caches([name for name, o in (outcomes or {}).items() if not o.ok])
        for name, outcome in (outcomes or {}).items():
            if outcome.ok:
                continue
            cached = caches[name].refreshed_ms
            context.notes.append(
                SourceNote(
                    name=spoken_source(name),
                    reason=failure_reason(outcome),
                    cached_ago=ago(now - cached) if cached is not None else None,
                )
            )
        return report, context
