"""The hub's view of each machine: its allowed tmux panes joined with Claude Code's registry, as the
dispatcher's `list` reports them, filtered again by the hub's own allow-list and cached briefly
(docs/SWITCHBOARD.md, "Target resolution")."""

from __future__ import annotations

import asyncio
import fnmatch
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..clock import SECOND_MS, now_ms
from ..log import Logger
from ..projects import MatchFailed, MatchOk, MatchResult, match_by_name
from .config import MachineConfig, RemoteConfig
from .protocol import CaptureResponse, ListResponse, PaneInfo
from .transport import Transport, TransportError, TransportResult

LIST_CACHE_MS = 15 * SECOND_MS


@dataclass(frozen=True)
class MachineView:
    machine: MachineConfig
    panes: tuple[PaneInfo, ...] = ()
    error: TransportError | None = None
    detail: str | None = None
    fetched_ms: int = 0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None

    def sessions(self) -> dict[str, list[PaneInfo]]:
        """Panes by tmux session name, in tmux order (sessions in order of first appearance)."""
        out: dict[str, list[PaneInfo]] = {}
        for pane in self.panes:
            out.setdefault(pane.session, []).append(pane)
        return out


def allowed(machine: MachineConfig, session: str) -> bool:
    return any(fnmatch.fnmatchcase(session, glob) for glob in machine.sessions)


class Inventory:
    """Lists and captures through each machine's transport. Never raises."""

    def __init__(
        self,
        remote: RemoteConfig,
        transports: Mapping[str, Transport],
        logger: Logger,
        now: Callable[[], int] | None = None,
        observe: Callable[[str], None] | None = None,
    ) -> None:
        self._remote = remote
        self._transports = dict(transports)
        self._logger = logger
        self._now = now or now_ms
        self._cache: dict[str, MachineView] = {}
        self._observe = observe
        """Gets every screen text, `waitingFor` and name a machine reports (`send.ScreenEcho`)."""

    def transport(self, name: str) -> Transport:
        return self._transports[name]

    @property
    def machines(self) -> list[MachineConfig]:
        return list(self._remote.machines)

    def _timeout(self, budget_seconds: float | None) -> float:
        limit = float(self._remote.ssh.command_timeout_seconds)
        return max(0.5, min(limit, budget_seconds)) if budget_seconds is not None else limit

    def _log(self, machine: str, verb: str, result: TransportResult) -> None:
        self._logger.info(
            "remote call",
            {
                "machine": machine,
                "verb": verb,
                "ok": result.ok,
                "error": result.error,
                "detail": result.detail,
                "ms": result.elapsed_ms,
            },
        )

    async def view(self, name: str, *, max_age_ms: int = 0, budget_seconds: float | None = None) -> MachineView:
        """The machine's panes; a cached view at most `max_age_ms` old is reused."""
        cached = self._cache.get(name)
        if cached is not None and max_age_ms > 0 and self._now() - cached.fetched_ms <= max_age_ms:
            return cached
        transport = self._transports[name]
        machine = transport.machine
        result = await transport.call("list", timeout_seconds=self._timeout(budget_seconds))
        self._log(name, "list", result)
        if not result.ok or not isinstance(result.data, ListResponse):
            return MachineView(machine=machine, error=result.error or "bad_response", detail=result.detail)
        # The dispatcher filtered with its own globs; the hub filters with the config's too.
        panes = tuple(pane for pane in result.data.panes if allowed(machine, pane.session))
        if self._observe is not None:
            for pane in panes:
                for text in (pane.window_name, pane.claude.name if pane.claude else None):
                    if text:
                        self._observe(text)
                if pane.claude and pane.claude.waiting_for:
                    self._observe(pane.claude.waiting_for)
        view = MachineView(machine=machine, panes=panes, fetched_ms=self._now(), truncated=result.data.truncated)
        self._cache[name] = view
        return view

    async def views(self, names: Sequence[str], *, budget_seconds: float | None = None) -> list[MachineView]:
        return list(await asyncio.gather(*(self.view(name, budget_seconds=budget_seconds) for name in names)))

    async def capture(
        self, name: str, pane_id: str, lines: int, *, budget_seconds: float | None = None
    ) -> CaptureResponse | TransportResult:
        """The captured pane, or the failed TransportResult. A pane whose session (as the capture
        reports it) fails the hub's allow-list counts as gone."""
        result = await self._transports[name].call(
            "capture", [pane_id, str(lines)], timeout_seconds=self._timeout(budget_seconds)
        )
        self._log(name, "capture", result)
        if result.ok and isinstance(result.data, CaptureResponse):
            if allowed(self._transports[name].machine, result.data.session):
                if self._observe is not None:
                    self._observe(result.data.text)
                return result.data
            # The pane moved (join-pane, rename) to a session only the dispatcher allows: for the
            # hub it is gone, and its text is dropped unread.
            result = TransportResult(ok=False, error="refused_by_policy", detail="gone", elapsed_ms=result.elapsed_ms)
        if result.ok:
            return TransportResult(ok=False, error="bad_response", elapsed_ms=result.elapsed_ms)
        if result.detail == "gone":
            self._cache.pop(name, None)
        return result

    async def version(self, name: str, *, budget_seconds: float | None = None) -> TransportResult:
        result = await self._transports[name].call("version", timeout_seconds=self._timeout(budget_seconds))
        self._log(name, "version", result)
        return result


def resolve_machine(machines: Sequence[MachineConfig], spoken: str | None) -> MatchResult[MachineConfig]:
    """The machine a spoken name means. Without a name only when exactly one is configured."""
    if spoken is None or not spoken.strip():
        if len(machines) == 1:
            return MatchOk(item=machines[0])
        return MatchFailed(reason="ambiguous", candidates=[m.name for m in machines])
    return match_by_name(machines, lambda m: m.name, spoken)
