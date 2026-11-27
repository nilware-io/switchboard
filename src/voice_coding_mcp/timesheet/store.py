"""Timesheet files under `stateDir/timesheets/` (docs/SWITCHBOARD.md, "Timesheet", "Files and approval").

- `<period>.draft.{json,md,csv}`: the computed draft (status `draft`), replaced by every new draft of
  the period until it is approved.
- `<period>.adjustments.json`: the owner's adjustments (hours, moves, notes), kept apart from the
  computed numbers and applied on top of them.
- `<period>.approved.{json,md,csv}`: written once by an approval: the draft with its adjustments
  applied, status `approved`, and `approval` with the SHA-256 of the frozen content. After that the
  period accepts no new draft and no adjustment, and only an approved, unchanged file is exported.

Every file is mode 0600 in a 0700 directory. Nothing here sends anything anywhere.
"""

from __future__ import annotations

import contextlib
import copy
import csv
import hashlib
import io
import json
import os
import re
import secrets
from collections.abc import Callable
from typing import Any

from ..clock import now_ms, to_iso
from ..json_file import dump_json, ensure_dir
from .activity import timesheet_dir

PERIOD_ID_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{4}-[0-9]{2}-[0-9]{2}")
FORMAT = "switchboard-timesheet"
VERSION = 1
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_ADJUSTMENTS = 500
NOTE_MAX = 300
_FORMULA_START = ("=", "+", "-", "@")


class TimesheetError(ValueError):
    """A request the files do not allow (no draft, already approved, changed file, bad adjustment)."""


# --- hashing ---------------------------------------------------------------------------------------


def content_hash(document: dict[str, Any]) -> str:
    """SHA-256 of the canonical JSON of a document (sorted keys, compact). For an approved document
    the hash is taken with `approval.sha256` empty, so it covers the approval record too."""
    body = dict(document)
    approval = body.get("approval")
    if isinstance(approval, dict):
        body["approval"] = {**approval, "sha256": ""}
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --- files -----------------------------------------------------------------------------------------


def _path(state_dir: str, period_id: str, kind: str, ext: str) -> str:
    if not PERIOD_ID_RE.fullmatch(period_id):
        raise TimesheetError(f"bad period id {period_id!r}")
    return os.path.join(timesheet_dir(state_dir), f"{period_id}.{kind}.{ext}")


def write_text_atomic(path: str, text: str, *, make_dir: bool = True) -> None:
    """Mode 0600, atomic. `make_dir`: create the directory (0700) first; an export to a path the
    owner chose leaves its directory alone."""
    directory = os.path.dirname(path)
    if make_dir:
        ensure_dir(directory)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        raise TimesheetError(f"{os.path.basename(path)} is too large")
    try:
        data = json.loads(raw)
    except ValueError:
        raise TimesheetError(f"{os.path.basename(path)} is not valid JSON") from None
    if not isinstance(data, dict):
        raise TimesheetError(f"{os.path.basename(path)} is not a JSON object")
    out: dict[str, Any] = data
    return out


# --- adjustments -----------------------------------------------------------------------------------


def _projects(document: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for client in document.get("clients", []):
        out += client["projects"]
    out += document.get("unassigned", [])
    return out


def _day_entry(project: dict[str, Any], day: str) -> dict[str, Any]:
    for found in project["days"]:
        if found["date"] == day:
            existing: dict[str, Any] = found
            return existing
    item: dict[str, Any] = {"date": day, "ownerMinutes": 0, "agentMinutes": 0, "commits": [], "pullRequests": []}
    project["days"].append(item)
    project["days"].sort(key=lambda d: d["date"])
    return item


def apply_adjustments(
    draft: dict[str, Any], adjustments: list[dict[str, Any]], projects: dict[str, str]
) -> dict[str, Any]:
    """The draft with the adjustments on top: computed minutes stay as they are, each day gets
    `ownerAdjustMinutes` / `agentAdjustMinutes` and the final `ownerFinalMinutes` /
    `agentFinalMinutes`; notes are listed. `projects` maps configured projects to their client ids
    (a move may target a configured project without computed time)."""
    document = copy.deepcopy(draft)
    by_name = {p["project"]: p for p in _projects(document)}

    def project_entry(name: str) -> dict[str, Any]:
        found = by_name.get(name)
        if found is not None:
            return found
        client_id = projects.get(name)
        client = next((c for c in document["clients"] if c["id"] == client_id), None)
        found = {"project": name, "days": [], "notes": []}
        # A project an adjustment names that a new draft no longer has: under its client, or
        # unassigned (moves only ever target configured projects).
        group = client["projects"] if client is not None else document["unassigned"]
        group.append(found)
        group.sort(key=lambda p: p["project"])
        by_name[name] = found
        return found

    for project in by_name.values():
        project.setdefault("notes", [])
        for item in project["days"]:
            item["ownerAdjustMinutes"] = 0
            item["agentAdjustMinutes"] = 0
    for adjustment in adjustments:
        kind = adjustment.get("kind", "owner")
        if adjustment["type"] == "note":
            project_entry(adjustment["project"])["notes"].append(
                {"day": adjustment.get("day"), "text": adjustment["note"]}
            )
            continue
        minutes = int(adjustment["minutes"])
        source = _day_entry(project_entry(adjustment["project"]), adjustment["day"])
        source.setdefault(f"{kind}AdjustMinutes", 0)
        if adjustment["type"] == "move":
            target = _day_entry(project_entry(adjustment["to"]), adjustment["day"])
            target.setdefault(f"{kind}AdjustMinutes", 0)
            source[f"{kind}AdjustMinutes"] -= minutes
            target[f"{kind}AdjustMinutes"] += minutes
        else:
            source[f"{kind}AdjustMinutes"] += minutes
        if adjustment.get("note"):
            project_entry(adjustment["project"])["notes"].append({"day": adjustment["day"], "text": adjustment["note"]})
    for project in by_name.values():
        for item in project["days"]:
            item.setdefault("ownerAdjustMinutes", 0)
            item.setdefault("agentAdjustMinutes", 0)
            item["ownerFinalMinutes"] = item["ownerMinutes"] + item["ownerAdjustMinutes"]
            item["agentFinalMinutes"] = item["agentMinutes"] + item["agentAdjustMinutes"]
    document["adjustments"] = adjustments
    _totals(document)
    return document


def _totals(document: dict[str, Any]) -> None:
    """Totals per project, client and document (sums of the days' minutes)."""

    def project_totals(project: dict[str, Any]) -> dict[str, int]:
        total = {"ownerMinutes": 0, "agentMinutes": 0, "commits": 0, "pullRequests": 0}
        pulls: set[str] = set()
        for item in project["days"]:
            total["ownerMinutes"] += item.get("ownerFinalMinutes", item["ownerMinutes"])
            total["agentMinutes"] += item.get("agentFinalMinutes", item["agentMinutes"])
            total["commits"] += len(item["commits"])
            pulls.update(p["url"] for p in item["pullRequests"])
        total["pullRequests"] = len(pulls)
        project["total"] = total
        return total

    grand = {"ownerMinutes": 0, "agentMinutes": 0, "commits": 0, "pullRequests": 0}
    for client in document["clients"]:
        client_total = {"ownerMinutes": 0, "agentMinutes": 0, "commits": 0, "pullRequests": 0}
        for project in client["projects"]:
            for key, value in project_totals(project).items():
                client_total[key] += value
        client["total"] = client_total
        for key, value in client_total.items():
            grand[key] += value
    unassigned = {"ownerMinutes": 0, "agentMinutes": 0, "commits": 0, "pullRequests": 0}
    for project in document["unassigned"]:
        for key, value in project_totals(project).items():
            unassigned[key] += value
    document["total"] = grand
    document["unassignedTotal"] = unassigned


class TimesheetStore:
    def __init__(self, state_dir: str, *, now: Callable[[], int] | None = None) -> None:
        self._state_dir = state_dir
        self._now = now or now_ms

    def path(self, period_id: str, kind: str, ext: str) -> str:
        return _path(self._state_dir, period_id, kind, ext)

    def approved(self, period_id: str) -> dict[str, Any] | None:
        return _read_json(self.path(period_id, "approved", "json"))

    def draft(self, period_id: str) -> dict[str, Any] | None:
        return _read_json(self.path(period_id, "draft", "json"))

    def adjustments(self, period_id: str) -> list[dict[str, Any]]:
        data = _read_json(self.path(period_id, "adjustments", "json"))
        items = data.get("items", []) if data else []
        if not isinstance(items, list):
            raise TimesheetError("the adjustments file is broken")
        return [item for item in items if isinstance(item, dict)]

    def _refuse_if_approved(self, period_id: str) -> None:
        if os.path.exists(self.path(period_id, "approved", "json")):
            raise TimesheetError(f"the timesheet {period_id} is approved; it cannot change any more")

    def save_draft(self, draft: dict[str, Any], projects: dict[str, str]) -> dict[str, Any]:
        """Writes the draft files (with the adjustments applied in md and csv) and returns the view."""
        period_id = draft["period"]["id"]
        self._refuse_if_approved(period_id)
        draft = {k: v for k, v in draft.items() if k != "sha256"}
        draft["status"] = "draft"
        _totals(draft)
        draft["sha256"] = content_hash(draft)
        write_text_atomic(self.path(period_id, "draft", "json"), dump_json(draft))
        view = self.view(period_id, projects, draft=draft)
        self._write_renderings(period_id, "draft", view)
        return view

    def _write_renderings(self, period_id: str, kind: str, view: dict[str, Any]) -> None:
        write_text_atomic(self.path(period_id, kind, "md"), render_markdown(view))
        write_text_atomic(self.path(period_id, kind, "csv"), render_csv(view))

    def view(self, period_id: str, projects: dict[str, str], *, draft: dict[str, Any] | None = None) -> dict[str, Any]:
        """The approved document, or the draft with the current adjustments applied."""
        approved = self.approved(period_id)
        if approved is not None:
            return approved
        base = draft if draft is not None else self.draft(period_id)
        if base is None:
            raise TimesheetError(f"there is no draft for {period_id}; run timesheet draft first")
        view = apply_adjustments(base, self.adjustments(period_id), projects)
        view["viewSha256"] = content_hash({k: v for k, v in view.items() if k != "viewSha256"})
        return view

    def add_adjustment(self, period_id: str, adjustment: dict[str, Any], projects: dict[str, str]) -> dict[str, Any]:
        self._refuse_if_approved(period_id)
        draft = self.draft(period_id)
        if draft is None:
            raise TimesheetError(f"there is no draft for {period_id}; run timesheet draft first")
        items = self.adjustments(period_id)
        if len(items) >= MAX_ADJUSTMENTS:
            raise TimesheetError("too many adjustments")
        start, end = draft["period"]["start"], draft["period"]["end"]
        day = adjustment.get("day")
        if day is not None and not (start <= day <= end):
            raise TimesheetError(f"{day} is not inside the period {start} to {end}")
        if adjustment.get("to") is not None and adjustment["to"] not in projects:
            raise TimesheetError(f"time can only move to a configured project, not {adjustment['to']!r}")
        item = {"id": max((int(i.get("id", 0)) for i in items), default=0) + 1, "at": to_iso(self._now()), **adjustment}
        candidate = apply_adjustments(draft, [*items, item], projects)
        if adjustment["type"] != "note":
            kind = adjustment.get("kind", "owner")
            for name in {adjustment["project"], adjustment.get("to") or adjustment["project"]}:
                for project in _projects(candidate):
                    if project["project"] != name:
                        continue
                    for entry in project["days"]:
                        if entry["date"] == day and entry[f"{kind}FinalMinutes"] < 0:
                            raise TimesheetError(f"that would leave {name} on {day} below zero {kind} hours")
        write_text_atomic(
            self.path(period_id, "adjustments", "json"), dump_json({"period": period_id, "items": [*items, item]})
        )
        view = self.view(period_id, projects)
        self._write_renderings(period_id, "draft", view)
        return item

    def approve(
        self, period_id: str, projects: dict[str, str], *, via: str, expect: str | None = None
    ) -> dict[str, Any]:
        """Freezes the draft with its adjustments. `expect`: a prefix of the view's hash that the owner
        saw (refused when the draft or the adjustments changed since)."""
        self._refuse_if_approved(period_id)
        view = self.view(period_id, projects)
        if expect is not None and (len(expect) < 8 or not view["viewSha256"].startswith(expect.lower())):
            raise TimesheetError("the draft changed since it was shown; show it again before approving")
        document = {k: v for k, v in view.items() if k != "viewSha256"}
        document["status"] = "approved"
        document["approval"] = {
            "at": to_iso(self._now()),
            "via": via,
            "draftSha256": document.get("sha256"),
            "sha256": "",
        }
        document["approval"]["sha256"] = content_hash(document)
        path = self.path(period_id, "approved", "json")
        ensure_dir(os.path.dirname(path))
        # O_EXCL: two approvals at once cannot both win.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(dump_json(document))
            handle.flush()
            os.fsync(handle.fileno())
        self._write_renderings(period_id, "approved", document)
        return document

    def verified(self, period_id: str) -> dict[str, Any]:
        """The approved document, after checking that it is unchanged since its approval."""
        document = self.approved(period_id)
        if document is None:
            raise TimesheetError(f"the timesheet {period_id} is not approved; only approved timesheets are exported")
        approval = document.get("approval")
        if not isinstance(approval, dict) or document.get("status") != "approved":
            raise TimesheetError("the approved file has no approval record")
        recorded = approval.get("sha256")
        if not isinstance(recorded, str) or content_hash(document) != recorded:
            raise TimesheetError("the approved file was changed after its approval; it is not exported")
        return document

    def export(self, period_id: str, fmt: str) -> str:
        document = self.verified(period_id)
        if fmt == "json":
            return dump_json(document)
        if fmt == "csv":
            return render_csv(document)
        return render_markdown(document)


# --- rendering -------------------------------------------------------------------------------------


def hours(minutes: int) -> str:
    return f"{minutes / 60:.2f}"


def _cell(value: object) -> str:
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return "'" + text if text.startswith(_FORMULA_START) else text


def _md(value: object) -> str:
    text = "".join(ch for ch in str(value) if ch.isprintable())
    return re.sub(r"([\\`*_\[\]|<>#])", r"\\\1", text)


def _final(item: dict[str, Any], kind: str) -> int:
    return int(item.get(f"{kind}FinalMinutes", item[f"{kind}Minutes"]))


def render_markdown(document: dict[str, Any]) -> str:
    period = document["period"]
    status = document.get("status", "draft").upper()
    lines = [
        f"# Timesheet {period['start']} to {period['end']} ({status})",
        "",
        f"Time zone {period['timeZone']}. Owner time: interactive Claude Code sessions and Switchboard voice calls; "
        "agent time: autonomous sessions. Hours are decimal.",
    ]
    rules = document.get("rules", {})
    lines.append(
        f"Rules: idle gap {rules.get('idleGapMinutes')} min, owner overlap {rules.get('ownerOverlap')}, "
        f"rounding {rules.get('roundingMinutes') or 'none'}"
        + (f" ({rules.get('roundingMode')})" if rules.get("roundingMinutes") else "")
        + "."
    )
    approval = document.get("approval")
    if isinstance(approval, dict):
        lines.append(f"Approved {approval.get('at')} via {approval.get('via')}, SHA-256 {approval.get('sha256')}.")
    elif document.get("viewSha256"):
        lines.append(f"Draft {str(document['viewSha256'])[:12]} (approve with this id to be sure it is this version).")

    def project_block(project: dict[str, Any]) -> list[str]:
        total = project["total"]
        out = [
            "",
            f"### {_md(project['project'])}: owner {hours(total['ownerMinutes'])} h, agent {hours(total['agentMinutes'])} h, "
            f"{total['commits']} commits, {total['pullRequests']} PRs",
            "",
            "| Day | Owner h | Agent h | Commits | Pull requests |",
            "|---|---|---|---|---|",
        ]
        for item in project["days"]:
            owner, agent = _final(item, "owner"), _final(item, "agent")
            owner_text = hours(owner)
            agent_text = hours(agent)
            if item.get("ownerAdjustMinutes"):
                owner_text += f" ({hours(item['ownerMinutes'])} {'+' if item['ownerAdjustMinutes'] > 0 else '-'} {hours(abs(item['ownerAdjustMinutes']))})"
            if item.get("agentAdjustMinutes"):
                agent_text += f" ({hours(item['agentMinutes'])} {'+' if item['agentAdjustMinutes'] > 0 else '-'} {hours(abs(item['agentAdjustMinutes']))})"
            commits = "<br>".join(f"`{c['sha'][:8]}` {_md(c['subject'])}" for c in item["commits"]) or ""
            pulls = "<br>".join(
                f"[{_md(p['repo'])}#{p['number']}]({p['url']}) {_md(p['title'])} ({', '.join(p['events'])})"
                for p in item["pullRequests"]
            )
            out.append(f"| {item['date']} | {owner_text} | {agent_text} | {commits} | {pulls} |")
        for note in project.get("notes", []):
            out.append(f"- Note{(' ' + note['day']) if note.get('day') else ''}: {_md(note['text'])}")
        return out

    for client in document["clients"]:
        total = client["total"]
        lines += [
            "",
            f"## {_md(client['name'])}: owner {hours(total['ownerMinutes'])} h, agent {hours(total['agentMinutes'])} h, "
            f"{total['commits']} commits, {total['pullRequests']} PRs",
        ]
        for project in client["projects"]:
            lines += project_block(project)
    if document["unassigned"]:
        total = document["unassignedTotal"]
        lines += [
            "",
            f"## Unassigned (map these in timesheet.projects): owner {hours(total['ownerMinutes'])} h, agent {hours(total['agentMinutes'])} h",
        ]
        for project in document["unassigned"]:
            lines += project_block(project)
    grand = document["total"]
    lines += [
        "",
        f"## Total (assigned): owner {hours(grand['ownerMinutes'])} h, agent {hours(grand['agentMinutes'])} h, "
        f"{grand['commits']} commits, {grand['pullRequests']} PRs",
    ]
    if document.get("notes"):
        lines += ["", "Notes:"] + [f"- {_md(n)}" for n in document["notes"]]
    return "\n".join(lines) + "\n"


CSV_HEADER = [
    "client_id",
    "client",
    "project",
    "date",
    "owner_hours",
    "agent_hours",
    "owner_hours_computed",
    "agent_hours_computed",
    "commits",
    "commit_subjects",
    "pull_requests",
    "pull_request_urls",
]


def render_csv(document: dict[str, Any]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)

    def rows(client_id: str, client_name: str, project: dict[str, Any]) -> None:
        for item in project["days"]:
            writer.writerow(
                [
                    _cell(client_id),
                    _cell(client_name),
                    _cell(project["project"]),
                    item["date"],
                    hours(_final(item, "owner")),
                    hours(_final(item, "agent")),
                    hours(item["ownerMinutes"]),
                    hours(item["agentMinutes"]),
                    len(item["commits"]),
                    _cell(" | ".join(c["subject"] for c in item["commits"])),
                    len(item["pullRequests"]),
                    _cell(" ".join(p["url"] for p in item["pullRequests"])),
                ]
            )

    for client in document["clients"]:
        for project in client["projects"]:
            rows(client["id"], client["name"], project)
    for project in document["unassigned"]:
        rows("", "unassigned", project)
    grand = document["total"]
    writer.writerow(
        [
            "",
            "total",
            "",
            "",
            hours(grand["ownerMinutes"]),
            hours(grand["agentMinutes"]),
            "",
            "",
            grand["commits"],
            "",
            grand["pullRequests"],
            "",
        ]
    )
    return buffer.getvalue()
