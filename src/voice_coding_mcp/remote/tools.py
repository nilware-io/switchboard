"""The remote tools behind `list_remote_sessions`, `remote_status`, `pending_input`, `remote_send`,
`remote_send_keys`, `remote_resize` and `remote_send_confirm` (docs/SWITCHBOARD.md, "Tools").

Each call has a total deadline of `tasks.syncWaitSeconds - 3` seconds for its synchronous part; a
summary that is not ready by then continues as a `remote` task whose number is returned.

`remote_send` only prepares: it checks everything, classifies the risk and keeps a confirmation in
memory; `remote_send_confirm` rechecks the pane and sends through the dispatcher's `send` verb, which
checks everything again on the machine. The text goes to the dispatcher on stdin, never in a command.
`remote_send_keys` and `remote_resize` prepare the same way, and `remote_send_confirm` presses the keys
(dispatcher `keys`, key names from a fixed list) or resizes the window (dispatcher `resize`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from ..clock import now_ms
from ..config import Config
from ..log import Logger
from ..projects import MatchOk
from ..sessions import claude_home_dir
from ..tasks.manager import StartFailed, TaskManager, format_for_voice
from .agents import AgentHub
from .audit import AuditLog
from .check import check_machine
from .config import MachineConfig, command_timeout_seconds, known_names, known_terms
from .dispatcher import (
    KEY_NAMES,
    KEYS_MENU,
    MAX_KEYS,
    NO_DIALOG,
    NO_MENU,
    RESIZE_COLUMNS,
    RESIZE_ROWS,
    SEND_ANSWER,
    SEND_CHECK_LINES,
    SEND_MESSAGE,
    bound_menu_hash,
    dialog_bound,
    keys_problem,
    shows_draft,
    shows_picker,
)
from .inventory import Inventory, resolve_machine
from .menu import footer_block, screen_menu
from .pending import (
    PendingChecker,
    PendingCheckerOptions,
    PendingReport,
    screen_ask,
    waiting_for_text,
    waits_for_permission,
)
from .protocol import CaptureResponse, KeysResponse, PaneInfo, ResizeResponse, SendResponse, VersionResponse
from .redact import clean_name, clean_screen
from .send import (
    KEYS,
    PERMISSION_ANSWER_RE,
    RESIZE,
    TEXT,
    Confirmation,
    ConfirmationStore,
    RiskClassifier,
    ScreenEcho,
    chosen_option,
    dialog_hash,
    key_words,
    normalize_text,
    parse_keys,
    shows_dialog,
    text_problem,
)
from .speech import (
    cut_note,
    format_confirm_outcome,
    format_keys_readback,
    format_list,
    format_machine_problem,
    format_pending,
    format_pending_task,
    format_readback,
    format_resize_readback,
    format_status,
    format_target_problem,
    pane_label,
    send_place,
    text_refusal,
    unreachable_sentence,
)
from .summarize import Summarizer, SummarizerOptions, last_meaningful_line, screen_hash
from .targets import Resolved, resolve_target
from .transport import Transport, make_transport

DEADLINE_MARGIN_SECONDS = 3
SUMMARY_MARGIN_SECONDS = 2
MIN_DEADLINE_SECONDS = 2.0
SEND_CAPTURE_LINES = SEND_CHECK_LINES
"""Screen lines `remote_send` reads for the permission and question checks: as many as the
dispatcher's `send` reads, so both run `shows_dialog` and `dialog_hash` on the same lines
(`pending.screen_ask` looks at the last 15 of them, as `pending_input`)."""
MIN_SEND_SECONDS = 2.0
"""A send is not started with less time left: it would likely end as an unknown outcome."""
EXPIRED = "That confirmation has expired. Ask me to prepare the message again."
EARLY = "I have not heard your answer yet. Read the message to the owner first. Nothing was sent."
NEEDS_RISKY_OK = "This one needs an explicit yes to the risky part. Nothing was sent."
VERSION_CACHE_MS = 5 * 60 * 1000
"""How long a machine's verb list (dispatcher `version`) is trusted before a key press or resize is
prepared again."""
_SPOKEN_KEYS = key_words(
    [name for name in KEY_NAMES if not name.isdigit() and name not in ("y", "n")] + ["the numbers 1 to 9", "y", "n"]
)
KEYS_ORDER_REFUSAL = (
    "I press a number, enter, escape, y or n only as the last key: anything after it would land on "
    "the next screen, which I have not read. Ask for the rest once I have read that screen."
)
KEYS_ANSWER_ORDER_REFUSAL = (
    "In a permission question, tab and shift tab can choose an answer themselves, so I press them only "
    "as the last key: anything after them would land on the next screen, which I have not read."
)
KEYS_REFUSAL = {
    "empty": f"Which keys should I press? I can press {_SPOKEN_KEYS}.",
    "too_many": f"That is too many keys at once; I press at most {MAX_KEYS}.",
    "unknown": f"I can only press {_SPOKEN_KEYS}.",
}
UNREACHED_ERRORS = ("refused", "host_key", "auth", "not_connected")
"""ssh failed before it connected, or an agent machine had no connection: nothing was typed. Every other failure after the request was
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
    agents: AgentHub | None = None
    """The relay socket server for `agent` machines. Default: one when an agent machine is
    configured (started by `start`, stopped by `stop`)."""


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
        self.agents = options.agents
        if self.agents is None and any(m.transport == "agent" for m in config.remote.machines):
            self.agents = AgentHub(config.remote, config.state_dir, options.logger)
        transports = options.transports or {
            m.name: make_transport(m, config.remote, config.state_dir, options.base_env, self.agents)
            for m in config.remote.machines
        }
        self.transports: dict[str, Transport] = dict(transports)
        """One per machine; `usage_report` calls the same ones."""
        self._now = options.now or now_ms
        self.echo = ScreenEcho(self._now, known_terms(config.remote), known_names(config.remote))
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
        self._verbs: dict[str, tuple[int, frozenset[str]]] = {}
        self._pending = PendingChecker(
            PendingCheckerOptions(
                inventory=self.inventory,
                logger=self._logger,
                project_roots=config.project_roots,
                claude_home=options.claude_home or claude_home_dir(config.claude.config_dir),
            )
        )

    async def start(self) -> None:
        """Starts serving agent machines (the app's lifespan)."""
        if self.agents is not None:
            self.agents.check = self._agent_check
            await self.agents.start()

    async def stop(self) -> None:
        if self.agents is not None:
            await self.agents.stop()

    async def _agent_check(self, name: str) -> tuple[list[str], bool]:
        """`remote-check <machine>` for an agent machine, asked through the control socket."""
        return await check_machine(self.inventory, name)

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
        # The answer quotes these questions; machine screens were observed by the inventory already,
        # but the hub's own registry (`waitingFor` of local sessions) was not.
        for entry in report.entries:
            self.echo.observe(entry.question)
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
            return ToolAnswer(unreachable_sentence(name, view.error, view.detail), True)
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
            return ToolAnswer(unreachable_sentence(name, captured.error, captured.detail), True)
        screen = clean_screen(captured.text).rstrip("\n ")
        self._logger.info(
            "tool call",
            {
                "tool": "remote_status",
                "machine": name,
                "paneId": pane.pane_id,
                "screenChars": len(screen),
                "paneHeight": captured.pane_height,
                "alternateOn": captured.alternate_on,
            },
        )
        note = ""
        rows = captured.pane_height
        if captured.alternate_on and rows is not None:
            # The alternate screen has no scrollback: a menu taller than the pane is cut at its top.
            # Not said when the whole menu was found below a free top row. A picker footer whose menu
            # cannot be read is a cut menu at any height (a 32-line menu in a 24-row pane).
            unread = screen_menu(screen) is None
            cut = unread and shows_picker(screen)
            if cut or (rows < RESIZE_ROWS[0] and (unread or len(screen.lstrip("\n").split("\n")) >= rows)):
                note = cut_note(rows, found.item.allow_send and rows < RESIZE_ROWS[1], menu_cut=cut)
        answer = await self._describe(name, pane, label, screen, deadline)
        return ToolAnswer(answer.text + note, answer.is_error) if note else answer

    def _log_answer(self, machine: str, pane: PaneInfo, path: str, reason: str | None = None) -> None:
        """Which path answered `remote_status` (no screen text)."""
        fields: dict[str, object] = {"tool": "remote_status", "machine": machine, "paneId": pane.pane_id, "path": path}
        if reason is not None:
            fields["reason"] = reason
        self._logger.info("remote_status answer", fields)

    async def _describe(self, machine: str, pane: PaneInfo, label: str, screen: str, deadline: _Deadline) -> ToolAnswer:
        menu = screen_menu(screen)
        # A menu that cannot be read still has its question and options above the key hints.
        line = (footer_block(screen) if menu is None else None) or last_meaningful_line(screen)

        def heuristic(reason: str) -> str:
            self._log_answer(machine, pane, "heuristic", reason)
            return format_status(label, pane, summary=None, line=line, menu=menu)

        if not screen.strip():
            return ToolAnswer(heuristic("empty"))
        if not self._summarizer.enabled:
            return ToolAnswer(heuristic("disabled"))
        key = f"{machine}:{pane.pane_id}"
        cached = self._summarizer.cached(key, screen_hash(screen))
        if cached is not None:
            self.echo.observe(cached)
            self._log_answer(machine, pane, "cached")
            return ToolAnswer(format_status(label, pane, summary=cached, line=line, menu=menu))
        lock_key = f"remote:{key}"
        lease = self._manager.claim_slot(lock_key)
        if lease is None:
            # No queueing for a status read: the heuristic answers at once.
            return ToolAnswer(heuristic("locked" if self._manager.is_locked(lock_key) else "no_slot"))
        run = asyncio.ensure_future(self._summarizer.summarize(key, screen, lease))
        self._background.add(run)
        run.add_done_callback(self._background.discard)
        budget = min(float(self._remote.summary.timeout_seconds), deadline.left() - SUMMARY_MARGIN_SECONDS)
        if budget > 0:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(run), budget)
        if run.done():
            summary = run.result()
            if summary is None:
                return ToolAnswer(heuristic("failed"))
            self.echo.observe(summary)
            self._log_answer(machine, pane, "summary")
            return ToolAnswer(format_status(label, pane, summary=summary, line=line, menu=menu))

        async def finish(abort: asyncio.Event) -> str:
            try:
                summary = await asyncio.shield(run)
            except asyncio.CancelledError:
                lease.abort.set()
                raise
            if summary is None:
                return heuristic("failed")
            self.echo.observe(summary)
            self._log_answer(machine, pane, "summary", "task")
            return format_status(label, pane, summary=summary, line=line, menu=menu)

        started = self._manager.start_job(
            label=f"status of {clean_name(pane.session)} on {machine}", machine=machine, lock_key=lock_key, job=finish
        )
        if isinstance(started, StartFailed):
            lease.abort.set()
            return ToolAnswer(f"{heuristic('no_task')} ({format_for_voice(started)})")
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
            return refuse(unreachable_sentence(name, view.error, view.detail), "unreachable")
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
            return refuse(unreachable_sentence(name, captured.error, captured.detail), "unreachable")
        screen = clean_screen(captured.text)
        ask = screen_ask(screen) if claude is not None else None
        # At least as broad as the dispatcher's own check (the same `shows_dialog` on the same lines),
        # so a send is prepared as a permission answer exactly when the dispatcher will see one.
        permission = (
            (claude is not None and claude.status == "waiting" and waits_for_permission(claude.waiting_for))
            or (ask is not None and ask.kind == "permission")
            or shows_dialog(captured.text)
        )
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
                menu = screen_menu(screen)
                question = (
                    (ask.text if ask is not None and ask.kind == "question" else None)
                    or (menu.question if menu is not None else None)
                    or waiting_for_text(claude.waiting_for)
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
            dialog=dialog_hash(captured.text) if permission else None,
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

    async def _lacks_verb(self, name: str, verb: str, deadline: _Deadline) -> bool:
        """The machine's dispatcher is known to lack `verb` (an older version). A failed `version`
        call says nothing: the confirm step then reports what the dispatcher answers."""
        cached = self._verbs.get(name)
        if cached is not None and self._now() - cached[0] <= VERSION_CACHE_MS:
            return verb not in cached[1]
        result = await self.inventory.version(name, budget_seconds=deadline.left())
        if not result.ok or not isinstance(result.data, VersionResponse):
            return False
        verbs = frozenset(result.data.verbs)
        self._verbs[name] = (self._now(), verbs)
        return verb not in verbs

    async def remote_send_keys(
        self, machine: str | None, target: str | None, keys: Sequence[str], caller: Caller
    ) -> ToolAnswer:
        """Prepares pressing keys (from a fixed list) in a pane, usually to answer a menu: checks,
        reads back the keys and the menu or question on the screen, stores a confirmation. Sends
        nothing."""
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
                    {"tool": "remote_send_keys", "machine": name, "refused": reason, "count": len(keys)},
                    target=target,
                ),
            )
            return ToolAnswer(message, True)

        if not config.allow_send:
            return refuse(f"Sending is not enabled for {name}.", "not_enabled")
        parsed = parse_keys(keys)
        if isinstance(parsed, str):
            return refuse(KEYS_REFUSAL[parsed], f"keys_{parsed}")
        if keys_problem(parsed, menu_bound=True) == "key_order":
            return refuse(KEYS_ORDER_REFUSAL, "key_order")
        # The version probe runs alongside the list, not as a third round trip before the capture.
        view, lacks_keys = await asyncio.gather(
            self.inventory.view(name, max_age_ms=15_000, budget_seconds=deadline.left()),
            self._lacks_verb(name, "keys", deadline),
        )
        if not view.ok:
            return refuse(unreachable_sentence(name, view.error, view.detail), "unreachable")
        resolved = resolve_target(view, target)
        if not isinstance(resolved, Resolved):
            return refuse(format_target_problem(name, resolved), "target")
        pane = resolved.pane
        place = send_place(name, pane, resolved.name_window)
        claude = pane.claude
        if claude is None and not config.send_to_any_pane:
            return refuse(f"{place} is not running Claude Code, so I will not press keys in it.", "not_claude")
        if lacks_keys:
            return refuse(f"switchboard-remote on {name} is too old to press keys. Update it there.", "old_dispatcher")
        captured = await self.inventory.capture(name, pane.pane_id, SEND_CAPTURE_LINES, budget_seconds=deadline.left())
        if not isinstance(captured, CaptureResponse):
            if captured.detail == "gone":
                return refuse(f"{place}: that pane is gone. Ask me to list the sessions again.", "gone")
            return refuse(unreachable_sentence(name, captured.error, captured.detail), "unreachable")
        screen = clean_screen(captured.text)
        ask = screen_ask(screen) if claude is not None else None
        menu = screen_menu(screen)
        # The same checks as remote_send: the dispatcher's `shows_dialog` on the same lines.
        permission = (
            (claude is not None and claude.status == "waiting" and waits_for_permission(claude.waiting_for))
            or (ask is not None and ask.kind == "permission")
            or shows_dialog(captured.text)
        )
        question: str | None
        bound: str | None = None
        # Keys land at a menu or permission dialog the owner heard; otherwise (`unbound`) at an
        # input, where the stricter rules of `keys_problem` hold.
        unbound = False
        if permission:
            if not config.allow_permission_answers:
                return refuse(
                    f"{place} is waiting for a permission decision, and answering those by voice is turned off. "
                    "Approve it at the keyboard or in the Claude app.",
                    "permission_prompt",
                )
            question = (ask.text if ask is not None and ask.kind == "permission" else None) or waiting_for_text(
                claude.waiting_for if claude else None
            )
            if dialog_hash(captured.text) is None:
                # The registry says it waits for permission, but no dialog is on the screen to bind
                # the keys to (a stale entry, or a dialog shows_dialog cannot see): Enter would submit
                # a draft, Up and Enter an earlier prompt, Shift+Tab would change the permission
                # mode. No keys at all; the dispatcher refuses the same (`dialog_not_visible`).
                return refuse(
                    f"{place} is waiting for a permission decision, but the permission question is not "
                    "visible on its screen, so I will not press keys there. Check it at the keyboard.",
                    "dialog_not_visible",
                )
            # Prose that looks like a dialog ("Do you want to ...?" above an idle input box) has no
            # answer options at the bottom, and shell output may print any dialog: the dispatcher
            # applies the unbound rules there too.
            unbound = not dialog_bound(captured.text, claude_pane=claude is not None)
            if not unbound and keys_problem(parsed, menu_bound=True, answer=True) == "key_order":
                # Shift+Tab is itself a choice in Claude Code's edit dialog (allow all edits this
                # session) and closes it; a key after it would answer a queued dialog nobody heard.
                # The dispatcher refuses the same (`key_order`). Unbound, `mode_keys` refuses them.
                return refuse(KEYS_ANSWER_ORDER_REFUSAL, "key_order")
        else:
            # In a shell pane only a menu under a picker footer binds: shell output can print a fake
            # menu above its prompt, where Up Enter reruns the previous command.
            bound = bound_menu_hash(captured.text, claude_pane=claude is not None)
            unbound = bound is None
            if bound is not None and shows_draft(captured.text) and not shows_picker(captured.text):
                # A "menu" over a draft without a picker footer may be the draft's own lines. The
                # dispatcher refuses the same (`draft_present`).
                return refuse(
                    f"Something is already typed in the input of {place}, so I will not press keys there. "
                    "Send or clear it at the keyboard first.",
                    "draft_present",
                )
            question = (menu.question if menu is not None else None) or (
                ask.text if ask is not None and ask.kind == "question" else None
            )
            if question is None and claude is not None and claude.status == "waiting":
                question = waiting_for_text(claude.waiting_for)
        if unbound:
            if shows_picker(captured.text):
                # A menu is open but cannot be read (a short full-screen pane may cut its top): its
                # selection mark is no draft, and Enter would choose an option nobody heard. The
                # dispatcher refuses the same (`menu_unreadable`).
                taller = (
                    " If it is cut off, I can make the pane taller."
                    if captured.alternate_on
                    and captured.pane_height is not None
                    and captured.pane_height < RESIZE_ROWS[1]
                    else ""
                )
                return refuse(
                    f"{place} shows a menu I cannot read, so I will not press keys in it.{taller} "
                    "Ask me what its screen says, or answer it at the keyboard.",
                    "menu_unreadable",
                )
            problem = keys_problem(parsed, menu_bound=False)
            if problem == "mode_keys":
                # Tab and Shift+Tab at a Claude Code input switch modes (Shift+Tab cycles the
                # permission mode up to accept edits or bypass), a broader grant than answering one
                # dialog: only into a menu the owner heard or a permission dialog.
                return refuse(
                    f"There is no menu on the screen of {place}; tab and shift tab there would change "
                    "Claude Code's mode, so I will not press them.",
                    "mode_keys",
                )
            if problem == "history_submit":
                # Up or Down at an input recalls an earlier prompt or shell command; Enter would
                # submit text that was never read back or risk-checked.
                return refuse(
                    f"There is no menu on the screen of {place}; up or down and then enter would send "
                    "an earlier prompt or command again without me reading it to you, so I will not "
                    "press them. Tell me the message instead.",
                    "history_submit",
                )
        if unbound:
            # The keys land at an input, not at the menu find_menu may still read there (shell
            # output, prose above the input): no option is read back as chosen.
            menu = None
        # Also for a permission dialog: find_menu reads its options, so the owner hears which one.
        chosen = chosen_option(menu, parsed)
        context = question
        if chosen is None and menu is not None:
            # Which option the keys choose is not known: every option counts as the question's.
            context = " ".join([question or "", *(text for _, text in menu.options)]).strip() or None
        risky = self._risk.classify(
            chosen[1] if chosen is not None else "",
            question=context,
            echoed=False,
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
            text=" ".join(parsed),
            risky=risky,
            permission_answer=permission,
            dialog=dialog_hash(captured.text) if permission else None,
            kind=KEYS,
            keys=parsed,
            menu=bound,
        )
        self._logger.info(
            "tool call",
            self._log_fields(
                {
                    "tool": "remote_send_keys",
                    "machine": name,
                    "paneId": pane.pane_id,
                    "keys": parsed,
                    "confirmation": item.id,
                    "risky": risky,
                    "permissionAnswer": permission,
                    "menu": menu is not None,
                    "dropped": [d.id for d in dropped],
                },
                target=target,
            ),
        )
        return ToolAnswer(
            format_keys_readback(
                item, menu=menu, question=question, chosen=chosen, dropped=dropped, dialog_shown=not unbound
            )
        )

    async def remote_resize(
        self, machine: str | None, target: str | None, columns: int, rows: int, caller: Caller
    ) -> ToolAnswer:
        """Prepares resizing the window of a pane (for a menu cut off by a short pane). Sends nothing."""
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
                self._log_fields({"tool": "remote_resize", "machine": name, "refused": reason}, target=target),
            )
            return ToolAnswer(message, True)

        if not config.allow_send:
            return refuse(f"Sending is not enabled for {name}, and resizing goes with it.", "not_enabled")
        if not (RESIZE_COLUMNS[0] <= columns <= RESIZE_COLUMNS[1] and RESIZE_ROWS[0] <= rows <= RESIZE_ROWS[1]):
            return refuse(
                f"I can set {RESIZE_COLUMNS[0]} to {RESIZE_COLUMNS[1]} columns and {RESIZE_ROWS[0]} to "
                f"{RESIZE_ROWS[1]} rows.",
                "bad_size",
            )
        view, lacks_resize = await asyncio.gather(
            self.inventory.view(name, max_age_ms=15_000, budget_seconds=deadline.left()),
            self._lacks_verb(name, "resize", deadline),
        )
        if not view.ok:
            return refuse(unreachable_sentence(name, view.error, view.detail), "unreachable")
        resolved = resolve_target(view, target)
        if not isinstance(resolved, Resolved):
            return refuse(format_target_problem(name, resolved), "target")
        pane = resolved.pane
        if pane.claude is None and not config.send_to_any_pane:
            return refuse(
                f"{send_place(name, pane, resolved.name_window)} is not running Claude Code, so I will not resize it.",
                "not_claude",
            )
        if lacks_resize:
            return refuse(
                f"switchboard-remote on {name} is too old to resize windows. Update it there.", "old_dispatcher"
            )
        item, dropped = self.confirmations.add(
            client=caller.fingerprint,
            client_name=caller.name,
            machine=name,
            session=clean_name(pane.session, 64),
            place=send_place(name, pane, resolved.name_window),
            pane_id=pane.pane_id,
            pane_pid=pane.pane_pid,
            session_id=pane.claude.session_id if pane.claude is not None else None,
            text=f"{columns}x{rows}",
            risky=[],
            permission_answer=False,
            kind=RESIZE,
            size=(columns, rows),
        )
        self._logger.info(
            "tool call",
            self._log_fields(
                {
                    "tool": "remote_resize",
                    "machine": name,
                    "paneId": pane.pane_id,
                    "columns": columns,
                    "rows": rows,
                    "confirmation": item.id,
                    "dropped": [d.id for d in dropped],
                },
                target=target,
            ),
        )
        current = (pane.width, pane.height) if pane.width is not None and pane.height is not None else None
        return ToolAnswer(format_resize_readback(item, current=current, dropped=dropped))

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
        if outcome == "sent" and item.kind == TEXT:
            # The owner heard this text read back and said yes: its new words are the owner's.
            self.echo.owner_said(item.text)
        self.audit.record(item, outcome=outcome, risky_ok=risky_ok)
        self._logger.info(
            "tool call",
            {
                "tool": "remote_send_confirm",
                "confirmation": key,
                "kind": item.kind,
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
        machine_config = self.inventory.transport(item.machine).machine
        left = min(float(command_timeout_seconds(self._remote, machine_config)), deadline.left())
        if left < MIN_SEND_SECONDS:
            return "not_sent:slow", None
        verb = "send"
        stdin: bytes | None = None
        session = item.session_id or "none"
        if item.kind == KEYS:
            verb = "keys"
            bound = (
                [SEND_ANSWER, item.dialog or NO_DIALOG] if item.permission_answer else [KEYS_MENU, item.menu or NO_MENU]
            )
            args = [item.pane_id, str(item.pane_pid), session, *bound, *item.keys]
        elif item.kind == RESIZE and item.size is not None:
            verb = "resize"
            args = [item.pane_id, str(item.pane_pid), session, str(item.size[0]), str(item.size[1])]
        else:
            mode = [SEND_ANSWER, item.dialog or NO_DIALOG] if item.permission_answer else [SEND_MESSAGE]
            args = [item.pane_id, str(item.pane_pid), session, *mode]
            stdin = item.text.encode("utf-8")
        result = await self.inventory.transport(item.machine).call(verb, args, timeout_seconds=left, stdin=stdin)
        self._logger.info(
            "remote call",
            {
                "machine": item.machine,
                "verb": verb,
                "ok": result.ok,
                "error": result.error,
                "detail": result.detail,
                "ms": result.elapsed_ms,
            },
        )
        data = result.data
        if result.ok and (
            (isinstance(data, SendResponse | KeysResponse) and data.sent)
            or (isinstance(data, ResizeResponse) and data.resized)
        ):
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
