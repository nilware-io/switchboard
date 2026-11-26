"""The remote tools behind `list_remote_sessions`, `remote_status`, `pending_input`, `remote_send` and
`remote_send_confirm` (docs/SWITCHBOARD.md, "Tools").

Each call has a total deadline of `tasks.syncWaitSeconds - 3` seconds for its synchronous part; a
summary that is not ready by then continues as a `remote` task whose number is returned.

`remote_send` only prepares: it checks everything, classifies the risk and keeps a confirmation in
memory; `remote_send_confirm` rechecks the pane and sends through the dispatcher's `send` verb, which
checks everything again on the machine. The text goes to the dispatcher on stdin, never in a command.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from ..clock import now_ms
from ..config import Config
from ..log import Logger
from ..projects import MatchOk
from ..sessions import claude_home_dir
from ..tasks.manager import StartFailed, TaskManager, format_for_voice
from .audit import AuditLog
from .config import MachineConfig
from .inventory import Inventory, resolve_machine
from .pending import (
    PendingChecker,
    PendingCheckerOptions,
    PendingReport,
    screen_ask,
    waiting_for_text,
    waits_for_permission,
)
from .protocol import CaptureResponse, PaneInfo, SendResponse
from .redact import clean_name, clean_screen
from .send import (
    PERMISSION_ANSWER_RE,
    Confirmation,
    ConfirmationStore,
    RiskClassifier,
    ScreenEcho,
    normalize_text,
    text_problem,
)
from .speech import (
    format_confirm_outcome,
    format_list,
    format_machine_problem,
    format_pending,
    format_pending_task,
    format_readback,
    format_status,
    format_target_problem,
    pane_label,
    send_place,
    text_refusal,
    unreachable_reason,
)
from .summarize import Summarizer, SummarizerOptions, last_meaningful_line, screen_hash
from .targets import Resolved, resolve_target
from .transport import Transport, make_transport

DEADLINE_MARGIN_SECONDS = 3
SUMMARY_MARGIN_SECONDS = 2
MIN_DEADLINE_SECONDS = 2.0
SEND_CAPTURE_LINES = 15
"""Screen lines `remote_send` reads for the permission and question checks (as `pending_input`)."""
MIN_SEND_SECONDS = 2.0
"""A send is not started with less time left: it would likely end as an unknown outcome."""
EXPIRED = "That confirmation has expired. Ask me to prepare the message again."
EARLY = "I have not heard your answer yet. Read the message to the owner first. Nothing was sent."
NEEDS_RISKY_OK = "This one needs an explicit yes to the risky part. Nothing was sent."
UNREACHED_ERRORS = ("refused", "host_key", "auth")
"""ssh failed before it connected: nothing was typed. Every other failure after the request was
handed over (also `dispatcher_missing`, which an unexpected banner can cause) is an unknown outcome."""


@dataclass(frozen=True)
class Caller:
    """The OAuth client of a tool call: a confirmation is bound to it."""

    fingerprint: str
    """`log.fingerprint` of the grant id (`none` for an in-process call without OAuth)."""
    name: str | None = None


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
    claude_home: str | None = None
    """The hub's own Claude Code home for `pending_input`'s local sessions. Default:
    `claude.configDir`, else `~/.claude` (as the upstream session tools)."""
    new_confirmation_id: Callable[[], str] | None = None
    """Test hook: how confirmation ids are made (default: random 4-digit numbers)."""


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
        self._now = options.now or now_ms
        self.echo = ScreenEcho(self._now)
        self.inventory = Inventory(config.remote, transports, self._logger, now=options.now, observe=self.echo.observe)
        send = config.remote.send
        self.confirmations = ConfirmationStore(
            max_pending=send.max_pending,
            confirm_seconds=send.confirm_seconds,
            min_confirm_seconds=send.min_confirm_seconds,
            now=self._now,
            new_id=options.new_confirmation_id,
        )
        self._risk = RiskClassifier(send.risky_patterns)
        self.audit = AuditLog(config.state_dir, log_prompts=config.log_prompts, now=self._now)
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
        self._pending = PendingChecker(
            PendingCheckerOptions(
                inventory=self.inventory,
                logger=self._logger,
                project_roots=config.project_roots,
                claude_home=options.claude_home or claude_home_dir(config.claude.config_dir),
            )
        )

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

    async def pending_input(self, machine: str | None) -> ToolAnswer:
        """Which sessions wait on the owner: every machine plus the hub's own registry, or one machine."""
        if machine is not None and machine.strip():
            found = resolve_machine(self.inventory.machines, machine)
            if not isinstance(found, MatchOk):
                return ToolAnswer(
                    format_machine_problem(
                        machine, [m.name for m in self.inventory.machines], ambiguous=found.reason == "ambiguous"
                    ),
                    True,
                )
            report = await self._pending.check([found.item.name], include_local=False, left=self._deadline().left)
        else:
            report = await self.collect_pending()
        self._logger.info(
            "tool call",
            {
                "tool": "pending_input",
                "machines": len(report.views),
                "reachable": sum(v.ok for v in report.views),
                "waiting": len(report.entries),
            },
        )
        return ToolAnswer(format_pending(report))

    async def collect_pending(self) -> PendingReport:
        """The `pending_input` fast path over every machine and the hub's own registry (also used by
        the notification watcher)."""
        names = [m.name for m in self.inventory.machines]
        return await self._pending.check(names, include_local=True, left=self._deadline().left)

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
            self.echo.observe(cached)
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
            self.echo.observe(summary)
            return ToolAnswer(format_status(label, pane, summary=summary, line=line))

        async def finish(abort: asyncio.Event) -> str:
            try:
                summary = await asyncio.shield(run)
            except asyncio.CancelledError:
                lease.abort.set()
                raise
            self.echo.observe(summary)
            return format_status(label, pane, summary=summary, line=line)

        started = self._manager.start_job(
            label=f"status of {clean_name(pane.session)} on {machine}", machine=machine, lock_key=lock_key, job=finish
        )
        if isinstance(started, StartFailed):
            lease.abort.set()
            return ToolAnswer(f"{heuristic()} ({format_for_voice(started)})")
        return ToolAnswer(format_pending_task(label, started.task.id))

    # --- remote_send and remote_send_confirm ------------------------------------------------------

    def _log_fields(self, fields: dict[str, object], **prompt: str | None) -> dict[str, object]:
        """`fields` plus the send's text and target only with `logPrompts`."""
        if self._config.log_prompts:
            fields.update({key: value for key, value in prompt.items() if value is not None})
        return fields

    async def remote_send(self, machine: str | None, target: str | None, text: str, caller: Caller) -> ToolAnswer:
        """Prepares a send: checks, classifies the risk and stores a confirmation. Sends nothing."""
        deadline = self._deadline()
        found = resolve_machine(self.inventory.machines, machine)
        if not isinstance(found, MatchOk):
            return ToolAnswer(
                format_machine_problem(
                    machine, [m.name for m in self.inventory.machines], ambiguous=found.reason == "ambiguous"
                ),
                True,
            )
        config = found.item
        name = config.name

        def refuse(message: str, reason: str) -> ToolAnswer:
            self._logger.info(
                "tool call",
                self._log_fields(
                    {"tool": "remote_send", "machine": name, "refused": reason, "chars": len(text)},
                    target=target,
                    text=text,
                ),
            )
            return ToolAnswer(message, True)

        if not config.allow_send:
            return refuse(f"Sending is not enabled for {name}.", "not_enabled")
        text = normalize_text(text)
        # Before this call's own capture: what counts is screen text a tool already returned.
        echoed = self.echo.echoed(text)
        limit = self._remote.send.max_chars
        if len(text) > limit:
            return refuse(f"That message is too long: {len(text)} characters, at most {limit}.", "too_long")
        view = await self.inventory.view(name, max_age_ms=15_000, budget_seconds=deadline.left())
        if not view.ok:
            return refuse(f"{name} is not reachable: {unreachable_reason(view.error, view.detail)}.", "unreachable")
        resolved = resolve_target(view, target)
        if not isinstance(resolved, Resolved):
            return refuse(format_target_problem(name, resolved), "target")
        pane = resolved.pane
        place = send_place(name, pane, resolved.name_window)
        claude = pane.claude
        if claude is None and not config.send_to_any_pane:
            return refuse(f"{place} is not running Claude Code, so I will not type into it.", "not_claude")
        problem = text_problem(
            text, claude_pane=claude is not None, slash_commands=list(self._remote.send.slash_commands)
        )
        if problem is not None:
            return refuse(text_refusal(problem, self._remote.send.slash_commands), problem)
        captured = await self.inventory.capture(name, pane.pane_id, SEND_CAPTURE_LINES, budget_seconds=deadline.left())
        if not isinstance(captured, CaptureResponse):
            if captured.detail == "gone":
                return refuse(f"{place}: that pane is gone. Ask me to list the sessions again.", "gone")
            return refuse(
                f"{name} is not reachable: {unreachable_reason(captured.error, captured.detail)}.", "unreachable"
            )
        ask = screen_ask(clean_screen(captured.text)) if claude is not None else None
        permission = (
            claude is not None and claude.status == "waiting" and waits_for_permission(claude.waiting_for)
        ) or (ask is not None and ask.kind == "permission")
        question: str | None = None
        if permission:
            if not config.allow_permission_answers:
                return refuse(
                    f"{place} is waiting for a permission decision, and answering those by voice is turned off. "
                    "Approve it at the keyboard or in the Claude app.",
                    "permission_prompt",
                )
            text = text.strip()
            if not PERMISSION_ANSWER_RE.fullmatch(text):
                return refuse(
                    f"{place} shows a permission question. I only send the number of one of its choices, such as 1.",
                    "permission_text",
                )
            question = (ask.text if ask is not None and ask.kind == "permission" else None) or waiting_for_text(
                claude.waiting_for if claude else None
            )
        elif claude is not None:
            if claude.status == "waiting":
                question = (ask.text if ask is not None and ask.kind == "question" else None) or waiting_for_text(
                    claude.waiting_for
                )
            elif ask is not None and ask.kind == "question":
                question = ask.text
        risky = self._risk.classify(
            text,
            question=question,
            echoed=echoed,
            permission_answer=permission,
            shell=claude is None,
        )
        item, dropped = self.confirmations.add(
            client=caller.fingerprint,
            client_name=caller.name,
            machine=name,
            session=clean_name(pane.session, 64),
            place=place,
            pane_id=pane.pane_id,
            pane_pid=pane.pane_pid,
            session_id=claude.session_id if claude is not None else None,
            text=text,
            risky=risky,
            permission_answer=permission,
        )
        self._logger.info(
            "tool call",
            self._log_fields(
                {
                    "tool": "remote_send",
                    "machine": name,
                    "paneId": pane.pane_id,
                    "chars": len(text),
                    "confirmation": item.id,
                    "risky": risky,
                    "permissionAnswer": permission,
                    "dropped": [d.id for d in dropped],
                },
                target=target,
                text=text,
            ),
        )
        return ToolAnswer(format_readback(item, question=question, dropped=dropped))

    async def remote_send_confirm(self, confirmation_id: str, risky_ok: bool, caller: Caller) -> ToolAnswer:
        """Sends a prepared message after the owner said yes to its read-back."""
        deadline = self._deadline()
        key = confirmation_id.strip()
        found = self.confirmations.check(key, caller.fingerprint)
        if not isinstance(found, Confirmation):
            self._logger.info("tool call", {"tool": "remote_send_confirm", "confirmation": key, "refused": found})
            return ToolAnswer(EARLY if found == "early" else EXPIRED, True)
        item = found
        if item.risky and not risky_ok:
            self._logger.info(
                "tool call", {"tool": "remote_send_confirm", "confirmation": key, "refused": "risky_not_confirmed"}
            )
            return ToolAnswer(NEEDS_RISKY_OK, True)
        self.confirmations.consume(key)
        outcome, detail = await self._send(item, deadline)
        self.audit.record(item, outcome=outcome, risky_ok=risky_ok)
        self._logger.info(
            "tool call",
            {
                "tool": "remote_send_confirm",
                "confirmation": key,
                "machine": item.machine,
                "paneId": item.pane_id,
                "outcome": outcome,
            },
        )
        return ToolAnswer(format_confirm_outcome(item, outcome, detail), outcome != "sent")

    def _machine(self, name: str) -> MachineConfig | None:
        return next((m for m in self.inventory.machines if m.name == name), None)

    async def _send(self, item: Confirmation, deadline: _Deadline) -> tuple[str, str | None]:
        """(audit outcome, detail): `sent`, `not_sent:<code>`, `pasted_no_enter` or `unknown`."""
        machine = self._machine(item.machine)
        if machine is None or not machine.allow_send:
            return "not_sent:not_enabled", None
        # The hub's own allow-list again, on a fresh list: a pane that moved to a session only the
        # dispatcher allows, or now runs another process or session, is not typed into.
        view = await self.inventory.view(item.machine, budget_seconds=deadline.left())
        if not view.ok:
            return f"not_sent:{view.error}", view.detail
        pane = next((p for p in view.panes if p.pane_id == item.pane_id), None)
        if pane is None or pane.pane_pid != item.pane_pid:
            return "not_sent:gone", None
        session_id = pane.claude.session_id if pane.claude else None
        if session_id != item.session_id:
            return "not_sent:changed", None
        left = min(float(self._remote.ssh.command_timeout_seconds), deadline.left())
        if left < MIN_SEND_SECONDS:
            return "not_sent:slow", None
        result = await self.inventory.transport(item.machine).call(
            "send",
            [item.pane_id, str(item.pane_pid), item.session_id or "none"],
            timeout_seconds=left,
            stdin=item.text.encode("utf-8"),
        )
        self._logger.info(
            "remote call",
            {
                "machine": item.machine,
                "verb": "send",
                "ok": result.ok,
                "error": result.error,
                "detail": result.detail,
                "ms": result.elapsed_ms,
            },
        )
        if result.ok and isinstance(result.data, SendResponse) and result.data.sent:
            return "sent", None
        if result.ok:
            return "unknown", None
        if result.error == "refused_by_policy":
            if result.detail == "pasted_no_enter":
                return "pasted_no_enter", None
            return f"not_sent:{result.detail or 'other'}", result.detail
        if result.error in UNREACHED_ERRORS:
            return f"not_sent:{result.error}", None
        return "unknown", None
