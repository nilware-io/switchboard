"""`voice-coding-mcp timesheet draft|show|adjust|approve|export` (docs/REFERENCE.md, "CLI")."""

from __future__ import annotations

import argparse
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..json_file import dump_json
from ..log import silent_logger
from ..remote.transport import make_transport
from ..usage.compute import PeriodError
from ..usage.service import UsageService, spoken_source
from .service import PERIOD_HELP, TimesheetService
from .store import NOTE_MAX, TimesheetError, render_csv, render_markdown, write_text_atomic

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
ACTIONS = ("draft", "show", "adjust", "approve", "export")
_DURATION_RE = re.compile(r"([+-]?)(?:([0-9]{1,3}(?:\.[0-9]{1,2})?)h)?(?:([0-9]{1,4})m)?")
_PLAIN_RE = re.compile(r"[+-]?[0-9]{1,4}")
_DAY_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


@dataclass(frozen=True)
class TimesheetArgs:
    action: str
    period: str | None = None
    cached: bool = False
    project: str | None = None
    day: str | None = None
    owner: str | None = None
    agent: str | None = None
    move_to: str | None = None
    note: str | None = None
    expect: str | None = None
    fmt: str = "md"
    out: str | None = None


def add_parser(command: Callable[[str, str], argparse.ArgumentParser]) -> None:
    sheet = command("timesheet", "draft timesheets: owner and agent hours, commits and pull requests per project")
    actions = sheet.add_subparsers(dest="ts_action", metavar="<action>", required=True)
    period_help = f"{PERIOD_HELP}"

    def action(name: str, text: str) -> argparse.ArgumentParser:
        sub = actions.add_parser(name, help=text, description=text)
        sub.add_argument("period", nargs="?", help=period_help)
        sub.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="config file")
        return sub

    draft = action("draft", "compute a draft from the usage records, voice calls, commits and pull requests")
    draft.add_argument("--cached", action="store_true", help="use the cached usage records, do not read the machines")
    show = action("show", "print the draft (with adjustments) or the approved timesheet")
    show.add_argument("--format", dest="fmt", choices=("md", "csv", "json"), default="md")
    adjust = action("adjust", "record an adjustment on top of the draft (the computed numbers stay)")
    adjust.add_argument("--project", required=True, help="the project (as the draft names it)")
    adjust.add_argument("--day", help="the day, YYYY-MM-DD (required for hours)")
    adjust.add_argument("--owner", metavar="DELTA", help="owner time change: +1h30m, -0.5h, 45m or minutes")
    adjust.add_argument("--agent", metavar="DELTA", help="agent time change, same forms")
    adjust.add_argument("--move-to", metavar="PROJECT", help="move that much time to this configured project")
    adjust.add_argument("--note", help=f"a note for the client, at most {NOTE_MAX} characters")
    approve = action("approve", "approve the draft with its adjustments: freezes it (nothing is sent)")
    approve.add_argument("--expect", metavar="ID", help="the draft id `show` printed; refused if it changed since")
    export = action("export", "write an approved timesheet (only approved, unchanged ones)")
    export.add_argument("--format", dest="fmt", choices=("md", "csv", "json"), default="md")
    export.add_argument("--out", metavar="PATH", help="write to this file (mode 0600) instead of stdout")


def args_of(ns: argparse.Namespace) -> TimesheetArgs:
    return TimesheetArgs(
        action=ns.ts_action,
        period=getattr(ns, "period", None),
        cached=getattr(ns, "cached", False),
        project=getattr(ns, "project", None),
        day=getattr(ns, "day", None),
        owner=getattr(ns, "owner", None),
        agent=getattr(ns, "agent", None),
        move_to=getattr(ns, "move_to", None),
        note=getattr(ns, "note", None),
        expect=getattr(ns, "expect", None),
        fmt=getattr(ns, "fmt", "md"),
        out=getattr(ns, "out", None),
    )


def parse_delta(text: str) -> int:
    """Signed minutes of `+1h30m`, `-0.5h`, `45m` or plain minutes. Raises ValueError."""
    value = text.strip()
    if _PLAIN_RE.fullmatch(value):
        return int(value)
    found = _DURATION_RE.fullmatch(value)
    if found is None or not (found.group(2) or found.group(3)):
        raise ValueError(f"not a duration: {text!r}")
    minutes = round(float(found.group(2) or 0) * 60) + int(found.group(3) or 0)
    return -minutes if found.group(1) == "-" else minutes


def _clean_note(text: str) -> str:
    note = " ".join("".join(ch for ch in text if ch.isprintable()).split())
    if not note or len(note) > NOTE_MAX:
        raise ValueError(f"a note must be 1 to {NOTE_MAX} printable characters")
    return note


def _adjustment(args: TimesheetArgs, view: dict[str, Any], configured: set[str]) -> dict[str, Any]:
    known = {p["project"] for c in view["clients"] for p in c["projects"]} | {p["project"] for p in view["unassigned"]}
    project = args.project or ""
    if args.owner is not None and args.agent is not None:
        raise ValueError("give --owner or --agent, not both")
    delta_text = args.owner if args.owner is not None else args.agent
    kind = "owner" if args.owner is not None else "agent"
    note = _clean_note(args.note) if args.note is not None else None
    if delta_text is None:
        if args.move_to is not None:
            raise ValueError("--move-to needs --owner or --agent with the time to move")
        if note is None:
            raise ValueError("give --owner, --agent or --note")
        if args.day is not None and not _DAY_RE.fullmatch(args.day):
            raise ValueError("--day must be YYYY-MM-DD")
        if project not in known | configured:
            raise ValueError(f"no project {project!r} in the draft or in timesheet.projects")
        return {"type": "note", "project": project, "day": args.day, "note": note}
    if args.day is None or not _DAY_RE.fullmatch(args.day):
        raise ValueError("hours need --day YYYY-MM-DD")
    minutes = parse_delta(delta_text)
    if minutes == 0 or abs(minutes) > 24 * 60:
        raise ValueError("the change must be between 1 minute and 24 hours")
    if project not in known:
        raise ValueError(f"the draft has no project {project!r}; projects: {', '.join(sorted(known)) or 'none'}")
    item: dict[str, Any] = {"type": "hours", "project": project, "day": args.day, "kind": kind, "minutes": minutes}
    if args.move_to is not None:
        if minutes < 0:
            raise ValueError("the time to move must be positive")
        if args.move_to == project:
            raise ValueError("--move-to must name another project")
        item = {**item, "type": "move", "to": args.move_to}
    if note is not None:
        item["note"] = note
    return item


def _write_out(path: str, text: str) -> str:
    target = os.path.abspath(path)
    write_text_atomic(target, text, make_dir=False)
    return target


async def run(
    config: Config, args: TimesheetArgs, out: Callable[[str], None], err: Callable[[str], None], env: Mapping[str, str]
) -> int:
    transports = {m.name: make_transport(m, config.remote, config.state_dir, env) for m in config.remote.machines}
    usage = UsageService(config, transports, silent_logger, env=env)
    service = TimesheetService(config, usage, env=env)
    try:
        period = service.period(args.period)
    except PeriodError:
        err(f"Unknown period {args.period}. Use {PERIOD_HELP}.\n")
        return EXIT_USAGE
    period_id = service.period_id(period)
    store = service.store
    try:
        if args.action == "draft":
            names = service.sources()
            agent_names = {m.name for m in config.remote.machines if m.transport == "agent"}
            readable = [n for n in names if n not in agent_names]
            outcomes = None
            if not args.cached and readable:
                err(f"Reading usage records from {', '.join(spoken_source(n) for n in readable)} ...\n")
                outcomes = await service.refresh(readable)
            view = await service.draft(period, names, outcomes)
            out(render_markdown(view))
            out(
                f"\nDraft {view['viewSha256'][:12]} written to {store.path(period_id, 'draft', 'md')} (and .csv, .json).\n"
            )
            return EXIT_ERROR if outcomes and any(not o.ok for o in outcomes.values()) else EXIT_OK
        if args.action == "show":
            view = store.view(period_id, service.client_of)
            out(
                dump_json(view)
                if args.fmt == "json"
                else render_csv(view)
                if args.fmt == "csv"
                else render_markdown(view)
            )
            return EXIT_OK
        if args.action == "adjust":
            view = store.view(period_id, service.client_of)
            try:
                adjustment = _adjustment(args, view, set(service.client_of))
            except ValueError as problem:
                err(f"{problem}\n")
                return EXIT_USAGE
            item = store.add_adjustment(period_id, adjustment, service.client_of)
            out(f"Adjustment {item['id']} recorded for {period_id}. Show the draft again before approving.\n")
            return EXIT_OK
        if args.action == "approve":
            document = store.approve(period_id, service.client_of, via="cli", expect=args.expect)
            out(render_markdown(document))
            out(
                f"\nApproved {period_id}: {store.path(period_id, 'approved', 'json')}, SHA-256 {document['approval']['sha256']}.\n"
            )
            return EXIT_OK
        text = store.export(period_id, args.fmt)
        if args.out is None:
            out(text)
        else:
            out(f"Wrote {_write_out(args.out, text)}\n")
        return EXIT_OK
    except TimesheetError as problem:
        err(f"{problem}\n")
        return EXIT_ERROR
