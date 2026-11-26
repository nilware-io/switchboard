"""The remote tools behind `list_remote_sessions` and `remote_status` (docs/SWITCHBOARD.md, "Tools").

Each call has a total deadline of `tasks.syncWaitSeconds - 3` seconds for its synchronous part; a
summary that is not ready by then continues as a `remote` task whose number is returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..config import Config
from ..log import Logger
from ..projects import MatchOk
from ..tasks.manager import StartFailed, TaskManager, format_for_voice
from .inventory import Inventory, resolve_machine
from .protocol import CaptureResponse, PaneInfo
from .redact import clean_name, clean_screen
from .speech import (
    format_list,
    format_machine_problem,
    format_pending_task,
    format_status,
    format_target_problem,
    pane_label,
    unreachable_reason,
)
from .summarize import Summarizer, SummarizerOptions, last_meaningful_line, screen_hash
from .targets import Resolved, resolve_target
from .transport import Transport, make_transport

DEADLINE_MARGIN_SECONDS = 3
SUMMARY_MARGIN_SECONDS = 2
MIN_DEADLINE_SECONDS = 2.0


@dataclass(frozen=True)
class ToolAnswer:
    text: str
    is_error: bool = False


@dataclass
class RemoteToolsOptions:
    config: Config
    manager: TaskManager
    logger: Logger
    base_env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    transports: Mapping[str, Transport] | None = None
    """Default: one per configured machine (`make_transport`)."""
    summarizer: Summarizer | None = None
    now: Callable[[], int] | None = None


class _Deadline:
    def __init__(self, seconds: float) -> None:
        self._end = time.monotonic() + seconds

    def left(self) -> float:
        return self._end - time.monotonic()


class RemoteTools:
    def __init__(self, options: RemoteToolsOptions) -> None:
        config = options.config
        self._config = config
        self._remote = config.remote
        self._manager = options.manager
        self._logger = options.logger.child({"component": "remote"})
        transports = options.transports or {
            m.name: make_transport(m, config.remote, config.state_dir, options.base_env) for m in config.remote.machines
        }
        self.inventory = Inventory(config.remote, transports, self._logger, now=options.now)
        self._summarizer = options.summarizer or Summarizer(
            SummarizerOptions(
                claude=config.claude,
                summary=config.remote.summary,
                state_dir=config.state_dir,
                logger=self._logger,
                base_env=options.base_env,
                now=options.now,
            )
        )
        self._background: set[asyncio.Task[str | None]] = set()

    def _deadline(self) -> _Deadline:
        wait = self._config.tasks.sync_wait_seconds
        return _Deadline(max(MIN_DEADLINE_SECONDS, wait - DEADLINE_MARGIN_SECONDS))

    async def list_remote_sessions(self, machine: str | None) -> ToolAnswer:
        machines = self.inventory.machines
        if machine is not None and machine.strip():
            found = resolve_machine(machines, machine)
            if not isinstance(found, MatchOk):
                return ToolAnswer(
                    format_machine_problem(machine, found.candidates, ambiguous=found.reason == "ambiguous"), True
                )
            names = [found.item.name]
        else:
            names = [m.name for m in machines]
        deadline = self._deadline()
        views = await self.inventory.views(names, budget_seconds=deadline.left())
        self._logger.info(
            "tool call", {"tool": "list_remote_sessions", "machines": len(views), "reachable": sum(v.ok for v in views)}
        )
        return ToolAnswer(format_list(views, total_machines=len(machines)))

    async def remote_status(self, machine: str | None, target: str | None, lines: int | None) -> ToolAnswer:
        deadline = self._deadline()
        found = resolve_machine(self.inventory.machines, machine)
        if not isinstance(found, MatchOk):
            return ToolAnswer(
                format_machine_problem(
                    machine, [m.name for m in self.inventory.machines], ambiguous=found.reason == "ambiguous"
                ),
                True,
            )
        name = found.item.name
        view = await self.inventory.view(name, max_age_ms=15_000, budget_seconds=deadline.left())
        if not view.ok:
            return ToolAnswer(f"{name} is not reachable: {unreachable_reason(view.error, view.detail)}.", True)
        resolved = resolve_target(view, target)
        if not isinstance(resolved, Resolved):
            return ToolAnswer(format_target_problem(name, resolved), True)
        pane = resolved.pane
        label = pane_label(name, pane, resolved.name_window)
        count = min(lines if lines is not None else self._remote.capture_lines, 500)
        captured = await self.inventory.capture(name, pane.pane_id, count, budget_seconds=deadline.left())
        if not isinstance(captured, CaptureResponse):
            if captured.detail == "gone":
                return ToolAnswer(f"{label}: that pane is gone. Ask me to list the sessions again.", True)
            return ToolAnswer(f"{name} is not reachable: {unreachable_reason(captured.error, captured.detail)}.", True)
        screen = clean_screen(captured.text)
        self._logger.info(
            "tool call", {"tool": "remote_status", "machine": name, "paneId": pane.pane_id, "screenChars": len(screen)}
        )
        return await self._describe(name, pane, label, screen, deadline)

    async def _describe(self, machine: str, pane: PaneInfo, label: str, screen: str, deadline: _Deadline) -> ToolAnswer:
        line = last_meaningful_line(screen)

        def heuristic() -> str:
            return format_status(label, pane, summary=None, line=line)

        if not self._summarizer.enabled or not screen.strip():
            return ToolAnswer(heuristic())
        key = f"{machine}:{pane.pane_id}"
        cached = self._summarizer.cached(key, screen_hash(screen))
        if cached is not None:
            return ToolAnswer(format_status(label, pane, summary=cached, line=line))
        lock_key = f"remote:{key}"
        lease = self._manager.claim_slot(lock_key)
        if lease is None:
            # No queueing for a status read: the heuristic answers at once.
            return ToolAnswer(heuristic())
        run = asyncio.ensure_future(self._summarizer.summarize(key, screen, lease))
        self._background.add(run)
        run.add_done_callback(self._background.discard)
        budget = min(float(self._remote.summary.timeout_seconds), deadline.left() - SUMMARY_MARGIN_SECONDS)
        if budget > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(run), budget)
        if run.done():
            summary = run.result()
            return ToolAnswer(format_status(label, pane, summary=summary, line=line))

        async def finish(abort: asyncio.Event) -> str:
            try:
                summary = await asyncio.shield(run)
            except asyncio.CancelledError:
                lease.abort.set()
                raise
            return format_status(label, pane, summary=summary, line=line)

        started = self._manager.start_job(
            label=f"status of {clean_name(pane.session)} on {machine}", machine=machine, lock_key=lock_key, job=finish
        )
        if isinstance(started, StartFailed):
            lease.abort.set()
            return ToolAnswer(f"{heuristic()} ({format_for_voice(started)})")
        return ToolAnswer(format_pending_task(label, started.task.id))
