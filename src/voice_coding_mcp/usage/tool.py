"""The `usage_report` tool (docs/SWITCHBOARD.md, "usage_report").

It answers at once when every needed cache is younger than ten minutes. Otherwise it refreshes the
caches (SSH calls of up to two minutes each) and waits for that until the tool deadline; a refresh
that takes longer continues as a `remote` task whose number is returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..clock import now_ms
from ..log import Logger
from ..projects import MatchOk
from ..remote.inventory import resolve_machine
from ..remote.redact import clean_name
from ..remote.speech import format_machine_problem, quote
from ..remote.tools import DEADLINE_MARGIN_SECONDS, MIN_DEADLINE_SECONDS, ToolAnswer
from ..tasks.manager import StartFailed, TaskManager, format_for_voice
from .compute import PERIOD_HELP, Period, PeriodError, parse_period
from .report import format_spoken
from .service import ProjectProblem, SourceOutcome, UsageService


def _query_digest(period: Period, project: str | None) -> str:
    """Part of a task's lock key: the same question in flight is one task (no spoken text stored)."""
    text = f"{period.start_ms}:{period.end_ms}:{project or ''}"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


@dataclass
class UsageToolOptions:
    service: UsageService
    manager: TaskManager
    logger: Logger
    sync_wait_seconds: int
    now: Callable[[], int] | None = None


class UsageTool:
    def __init__(self, options: UsageToolOptions) -> None:
        self._service = options.service
        self._manager = options.manager
        self._logger = options.logger.child({"component": "usage"})
        self._wait = options.sync_wait_seconds
        self._now = options.now or now_ms
        self._refreshes: dict[str, asyncio.Future[dict[str, SourceOutcome]]] = {}
        """One refresh per source set at a time; every call waiting for it builds its own answer."""
        self._background: set[asyncio.Future[ToolAnswer]] = set()

    def _forget(self, key: str, done: asyncio.Future[dict[str, SourceOutcome]]) -> None:
        if self._refreshes.get(key) is done:
            del self._refreshes[key]

    def _answer(
        self,
        names: list[str],
        period: Period,
        project: str | None,
        scope: str,
        outcomes: Mapping[str, SourceOutcome] | None = None,
    ) -> ToolAnswer:
        if project is not None:
            found = self._service.match_project(names, project)
            if isinstance(found, ProjectProblem):
                known = ", ".join(clean_name(c) for c in found.candidates[:8]) or "none"
                if found.ambiguous:
                    return ToolAnswer(
                        f"{quote(clean_name(project))} matches several projects: {known}. Which one?", True
                    )
                return ToolAnswer(
                    f"I found no project called {quote(clean_name(project))} in the usage records. Projects: {known}.",
                    True,
                )
            scope += f", project {quote(clean_name(found))}"
            project = found
        report, context = self._service.report(names, period, project=project, outcomes=outcomes)
        context.scope = scope
        return ToolAnswer(format_spoken(report, context))

    async def usage_report(self, period: str | None, machine: str | None, project: str | None) -> ToolAnswer:
        deadline = time.monotonic() + max(MIN_DEADLINE_SECONDS, self._wait - DEADLINE_MARGIN_SECONDS)
        try:
            chosen = parse_period(period, self._now(), self._service.tz)
        except PeriodError:
            return ToolAnswer(f"I did not understand the period. Say {PERIOD_HELP}.", True)
        scope = ""
        filter_name: str | None = None
        if machine is not None and machine.strip():
            found = resolve_machine(self._service.machines, machine)
            if not isinstance(found, MatchOk):
                names = [m.name for m in self._service.machines]
                return ToolAnswer(format_machine_problem(machine, names, ambiguous=found.reason == "ambiguous"), True)
            if not found.item.allow_usage:
                return ToolAnswer(f"Usage reading is off for {found.item.name}.", True)
            filter_name = found.item.name
            scope = f" on {filter_name}"
        names = self._service.sources(filter_name)
        self._logger.info("tool call", {"tool": "usage_report", "period": chosen.label, "sources": len(names)})
        if not names:
            return ToolAnswer("No machine has usage reading turned on.", True)
        fresh = await asyncio.to_thread(self._service.fresh, names)
        if fresh:
            return await asyncio.to_thread(self._answer, names, chosen, project, scope)

        refresh_key = f"usage:{filter_name or 'all'}"
        refresh = self._refreshes.get(refresh_key)
        if refresh is None or refresh.done():
            refresh = asyncio.ensure_future(self._service.refresh(names))
            self._refreshes[refresh_key] = refresh
            refresh.add_done_callback(lambda done: self._forget(refresh_key, done))

        async def work() -> ToolAnswer:
            assert refresh is not None
            outcomes = await asyncio.shield(refresh)
            return await asyncio.to_thread(self._answer, names, chosen, project, scope, outcomes)

        run = asyncio.ensure_future(work())
        self._background.add(run)
        run.add_done_callback(self._background.discard)
        budget = deadline - time.monotonic()
        if budget > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(run), budget)
        if run.done():
            if run.cancelled() or run.exception() is not None:
                return ToolAnswer("I could not read the usage records.", True)
            return run.result()

        async def finish(abort: asyncio.Event) -> str:
            return (await asyncio.shield(run)).text

        started = self._manager.start_job(
            label=f"usage report{scope}",
            machine=filter_name or "all machines",
            lock_key=f"{refresh_key}:{_query_digest(chosen, project)}",
            job=finish,
        )
        if isinstance(started, StartFailed):
            return ToolAnswer(f"I could not start reading the usage records ({format_for_voice(started)}).", True)
        return ToolAnswer(f"I am reading the usage records. That is task {started.task.id}; ask me for it in a moment.")
