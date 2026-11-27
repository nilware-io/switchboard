"""The voice tools `timesheet_summary`, `timesheet_approve` and `timesheet_approve_confirm`
(docs/SWITCHBOARD.md, "Timesheet", "Voice").

`timesheet_summary` drafts the period (usage caches refreshed until the tool deadline) and reads back
totals. `timesheet_approve` only reads back the current draft and returns a confirmation number bound
to that exact draft (its hash) and to the OAuth client; `timesheet_approve_confirm` approves it in a
later turn (not sooner than `remote.send.minConfirmSeconds`, within `remote.send.confirmSeconds`,
once, and only while the draft and its adjustments are unchanged). Approval freezes the timesheet;
nothing is sent anywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..clock import SECOND_MS, now_ms
from ..log import Logger
from ..remote.tools import DEADLINE_MARGIN_SECONDS, MIN_DEADLINE_SECONDS, Caller, ToolAnswer
from ..usage.compute import Period, PeriodError
from .service import PERIOD_HELP, TimesheetService, spoken_summary
from .store import TimesheetError

ID_DIGITS = 4


@dataclass(frozen=True)
class PendingApproval:
    id: str
    client: str
    period_id: str
    view_sha256: str
    created_ms: int
    expires_ms: int


@dataclass
class TimesheetToolOptions:
    service: TimesheetService
    logger: Logger
    sync_wait_seconds: int
    confirm_seconds: int
    min_confirm_seconds: int
    now: Callable[[], int] | None = None


class TimesheetTool:
    def __init__(self, options: TimesheetToolOptions) -> None:
        self._service = options.service
        self._logger = options.logger.child({"component": "timesheet"})
        self._wait = options.sync_wait_seconds
        self._ttl_ms = options.confirm_seconds * SECOND_MS
        self._min_ms = options.min_confirm_seconds * SECOND_MS
        self._now = options.now or now_ms
        self._pending: dict[str, PendingApproval] = {}
        self._drafts: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def _period(self, text: str | None) -> Period | ToolAnswer:
        try:
            return self._service.period(text)
        except PeriodError:
            return ToolAnswer(f"I did not understand the period. Say {PERIOD_HELP}.", True)

    async def summary(self, period: str | None) -> ToolAnswer:
        deadline = time.monotonic() + max(MIN_DEADLINE_SECONDS, self._wait - DEADLINE_MARGIN_SECONDS)
        chosen = self._period(period)
        if isinstance(chosen, ToolAnswer):
            return chosen
        period_id = self._service.period_id(chosen)
        store = self._service.store
        try:
            if store.approved(period_id) is not None:
                return ToolAnswer(spoken_summary(store.view(period_id, self._service.client_of)))
        except TimesheetError as err:
            return ToolAnswer(f"I could not read that timesheet: {err}.", True)
        running = self._drafts.get(period_id)
        if running is None or running.done():
            running = asyncio.ensure_future(self._draft(chosen))
            self._drafts[period_id] = running
        budget = deadline - time.monotonic()
        if budget > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(running), budget)
        self._logger.info("tool call", {"tool": "timesheet_summary", "period": period_id, "done": running.done()})
        if not running.done():
            return ToolAnswer("I am still collecting the hours and commits. Ask me again in a minute.")
        if running.cancelled() or running.exception() is not None:
            error = None if running.cancelled() else running.exception()
            self._logger.warn("timesheet draft failed", {"period": period_id, "error": type(error).__name__})
            return ToolAnswer("I could not draft the timesheet. Run voice-coding-mcp timesheet draft to see why.", True)
        return ToolAnswer(spoken_summary(running.result()))

    async def _draft(self, period: Period) -> dict[str, Any]:
        names = self._service.sources()
        outcomes = await self._service.refresh(names)
        return await self._service.draft(period, names, outcomes)

    def _expire(self) -> None:
        now = self._now()
        for key in [k for k, item in self._pending.items() if item.expires_ms <= now]:
            del self._pending[key]

    async def approve(self, period: str | None, caller: Caller) -> ToolAnswer:
        if not self._service.settings.voice_approval:
            return ToolAnswer(
                "Approving by voice is off. Approve it with voice-coding-mcp timesheet approve on the computer.", True
            )
        chosen = self._period(period)
        if isinstance(chosen, ToolAnswer):
            return chosen
        period_id = self._service.period_id(chosen)
        store = self._service.store
        try:
            if store.approved(period_id) is not None:
                return ToolAnswer("That timesheet is already approved.")
            view = store.view(period_id, self._service.client_of)
        except TimesheetError:
            return ToolAnswer("There is no draft for that period yet. Ask me for the timesheet first.", True)
        self._expire()
        for key in [k for k, item in self._pending.items() if item.client == caller.fingerprint]:
            del self._pending[key]
        new_id = f"{secrets.randbelow(9 * 10 ** (ID_DIGITS - 1)) + 10 ** (ID_DIGITS - 1)}"
        while new_id in self._pending:
            new_id = f"{secrets.randbelow(9 * 10 ** (ID_DIGITS - 1)) + 10 ** (ID_DIGITS - 1)}"
        now = self._now()
        self._pending[new_id] = PendingApproval(
            id=new_id,
            client=caller.fingerprint,
            period_id=period_id,
            view_sha256=view["viewSha256"],
            created_ms=now,
            expires_ms=now + self._ttl_ms,
        )
        self._logger.info("tool call", {"tool": "timesheet_approve", "period": period_id})
        return ToolAnswer(
            f"{spoken_summary(view)} Shall I approve it? Approving freezes it; nothing is sent anywhere. "
            f"(confirmation {new_id})"
        )

    async def confirm(self, confirmation_id: str, caller: Caller) -> ToolAnswer:
        self._expire()
        key = confirmation_id.strip()
        item = self._pending.get(key)
        if item is None or not secrets.compare_digest(item.client, caller.fingerprint):
            return ToolAnswer("That approval expired or is unknown. Ask me to approve the timesheet again.", True)
        if self._now() - item.created_ms < self._min_ms:
            return ToolAnswer("Read the summary to the owner and wait for their yes before confirming.", True)
        del self._pending[key]
        try:
            document = self._service.store.approve(
                item.period_id, self._service.client_of, via="voice", expect=item.view_sha256
            )
        except (TimesheetError, OSError) as err:
            self._logger.info("tool call", {"tool": "timesheet_approve_confirm", "period": item.period_id, "ok": False})
            return ToolAnswer(f"Not approved: {err}.", True)
        self._logger.info("tool call", {"tool": "timesheet_approve_confirm", "period": item.period_id, "ok": True})
        period = document["period"]
        return ToolAnswer(
            f"Approved the timesheet from {period['start']} to {period['end']}. "
            "It is frozen; export it on the computer when you want to send it."
        )
