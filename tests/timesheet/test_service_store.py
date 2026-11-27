"""Drafts end to end (hub home transcripts, voice log, fake gh), the files, adjustments on top of the
computed numbers, approval freezing the timesheet, export of approved files only, privacy."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from tests.timesheet.helpers import NOW, PERIOD_ID, at, make_config, make_service, seed
from tests.usage.helpers import SECRET_TEXT
from voice_coding_mcp.config import ConfigError
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.timesheet.activity import VoiceCall, VoiceLog
from voice_coding_mcp.timesheet.service import TimesheetService, spoken_summary
from voice_coding_mcp.timesheet.store import TimesheetError, content_hash, render_csv


async def drafted(tmp: Path, **timesheet: Any) -> tuple[TimesheetService, dict[str, Any]]:
    config = make_config(tmp, **timesheet)
    service = make_service(config)
    seed(tmp)
    names = service.sources()
    await service.refresh(names)
    view = await service.draft(service.period(None), names)
    return service, view


def read(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write(path: str, text: str) -> None:
    Path(path).write_text(text, encoding="utf-8")


def mode(path: str | Path) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)


def project(view: dict[str, Any], name: str) -> dict[str, Any]:
    for client in view["clients"]:
        for entry in client["projects"]:
            if entry["project"] == name:
                found: dict[str, Any] = entry
                return found
    for entry in view["unassigned"]:
        if entry["project"] == name:
            loose: dict[str, Any] = entry
            return loose
    raise AssertionError(name)


def day(entry: dict[str, Any], label: str) -> dict[str, Any]:
    found: dict[str, Any] = next(d for d in entry["days"] if d["date"] == label)
    return found


async def test_draft_splits_owner_and_agent_time_per_client_and_project(tmp_path: Path) -> None:
    _, view = await drafted(tmp_path)
    assert view["period"] == {
        "id": PERIOD_ID,
        "start": "2026-11-16",
        "end": "2026-11-22",
        "label": "Last week",
        "timeZone": "Europe/Zurich",
    }
    assert [c["id"] for c in view["clients"]] == ["acme", "nilware"]
    web = day(project(view, "web"), "2026-11-17")
    # 09:00 to 09:31 (the worktree folder is an alias), plus the headless task as agent time.
    assert (web["ownerMinutes"], web["agentMinutes"]) == (31, 10)
    assert [c["subject"] for c in web["commits"]] == ["Add the timesheet"]
    board = day(project(view, "switchboard"), "2026-11-17")
    assert (board["ownerMinutes"], board["agentMinutes"]) == (20, 12)
    scratch = day(project(view, "scratch"), "2026-11-17")
    assert scratch["ownerMinutes"] == 1
    pulls = day(project(view, "web"), "2026-11-18")["pullRequests"]
    assert pulls == [
        {
            "repo": "acme/web",
            "number": 9,
            "title": "Draft timesheets",
            "url": "https://github.com/acme/web/pull/9",
            "events": ["opened"],
        }
    ]
    assert view["clients"][0]["total"] == {"ownerMinutes": 31, "agentMinutes": 10, "commits": 1, "pullRequests": 1}
    assert view["unassignedTotal"]["ownerMinutes"] == 1
    assert any("unassigned folders" in n and "scratch" in n for n in view["notes"])
    assert view["status"] == "draft"
    assert spoken_summary(view) == (
        "Last week: Acme AG 31 owner minutes and 10 agent minutes across 1 project, 1 commit, 1 pull request; "
        "nilware 20 owner minutes and 12 agent minutes across 1 project, 0 commits, 0 pull requests; "
        "1 minute unassigned. This is a draft, not approved."
    )


async def test_files_are_private_and_carry_no_text(tmp_path: Path) -> None:
    service, _ = await drafted(tmp_path)
    assert mode(os.path.dirname(service.store.path(PERIOD_ID, "draft", "json"))) == 0o700
    for ext in ("json", "md", "csv"):
        path = service.store.path(PERIOD_ID, "draft", ext)
        assert mode(path) == 0o600
        text = read(path)
        assert SECRET_TEXT not in text
        assert "/srv/projects" not in text
        assert "notreal" not in text


async def test_rounding_per_day_and_project(tmp_path: Path) -> None:
    _, view = await drafted(tmp_path, roundingMinutes=15, roundingMode="up")
    web = day(project(view, "web"), "2026-11-17")
    assert (web["ownerMinutes"], web["agentMinutes"]) == (45, 15)
    assert web["ownerMs"] == 31 * 60_000


async def test_voice_calls_count_as_owner_time(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    log = VoiceLog(config.state_dir, silent_logger)
    log.append(VoiceCall(at(18, 8), at(18, 8) + 20_000, "run_task", "web-feature"))
    log.append(VoiceCall(at(18, 8, 5), at(18, 8, 6), "remote_send", "web"))
    log.append(VoiceCall(at(18, 9), at(18, 9) + 1_000, "pending_input", None))
    _, view = await drafted(tmp_path)
    assert day(project(view, "web"), "2026-11-18")["ownerMinutes"] == 6
    assert day(project(view, "(voice)"), "2026-11-18")["ownerMinutes"] == 1


async def test_adjustments_sit_on_top_of_the_computed_numbers(tmp_path: Path) -> None:
    service, _ = await drafted(tmp_path)
    projects = service.client_of
    store = service.store
    store.add_adjustment(
        PERIOD_ID, {"type": "hours", "project": "web", "day": "2026-11-17", "kind": "owner", "minutes": 29}, projects
    )
    store.add_adjustment(
        PERIOD_ID,
        {
            "type": "move",
            "project": "scratch",
            "day": "2026-11-17",
            "kind": "owner",
            "minutes": 1,
            "to": "switchboard",
            "note": "scratch was switchboard",
        },
        projects,
    )
    store.add_adjustment(PERIOD_ID, {"type": "note", "project": "web", "day": None, "note": "Review meeting"}, projects)
    computed = json.loads(read(store.path(PERIOD_ID, "draft", "json")))
    assert day(project(computed, "web"), "2026-11-17")["ownerMinutes"] == 31
    current = store.view(PERIOD_ID, projects)
    web = day(project(current, "web"), "2026-11-17")
    assert (web["ownerMinutes"], web["ownerAdjustMinutes"], web["ownerFinalMinutes"]) == (31, 29, 60)
    assert day(project(current, "switchboard"), "2026-11-17")["ownerFinalMinutes"] == 21
    assert day(project(current, "scratch"), "2026-11-17")["ownerFinalMinutes"] == 0
    assert project(current, "web")["notes"] == [{"day": None, "text": "Review meeting"}]
    assert current["clients"][0]["total"]["ownerMinutes"] == 60
    md = read(store.path(PERIOD_ID, "draft", "md"))
    assert "1.00 (0.52 + 0.48)" in md
    # A new draft keeps the adjustments.
    names = service.sources()
    again = await service.draft(service.period(None), names)
    assert day(project(again, "web"), "2026-11-17")["ownerFinalMinutes"] == 60


@pytest.mark.parametrize(
    ("adjustment", "problem"),
    [
        (
            {"type": "hours", "project": "web", "day": "2026-11-30", "kind": "owner", "minutes": 5},
            "not inside the period",
        ),
        ({"type": "hours", "project": "web", "day": "2026-11-17", "kind": "agent", "minutes": -11}, "below zero"),
        (
            {"type": "move", "project": "web", "day": "2026-11-17", "kind": "owner", "minutes": 5, "to": "scratch"},
            "configured project",
        ),
    ],
)
async def test_bad_adjustments_are_refused(tmp_path: Path, adjustment: dict[str, Any], problem: str) -> None:
    service, _ = await drafted(tmp_path)
    with pytest.raises(TimesheetError, match=problem):
        service.store.add_adjustment(PERIOD_ID, adjustment, service.client_of)


async def test_approval_freezes_and_export_checks_the_hash(tmp_path: Path) -> None:
    service, view = await drafted(tmp_path)
    store = service.store
    with pytest.raises(TimesheetError, match="not approved"):
        store.export(PERIOD_ID, "md")
    with pytest.raises(TimesheetError, match="changed since"):
        store.approve(PERIOD_ID, service.client_of, via="cli", expect="0000000000")
    document = store.approve(PERIOD_ID, service.client_of, via="cli", expect=view["viewSha256"][:12])
    assert document["status"] == "approved"
    assert document["approval"]["via"] == "cli"
    assert document["approval"]["sha256"] == content_hash(document)
    approved = store.path(PERIOD_ID, "approved", "json")
    assert mode(approved) == 0o600
    with pytest.raises(TimesheetError, match="approved"):
        store.approve(PERIOD_ID, service.client_of, via="cli")
    with pytest.raises(TimesheetError, match="cannot change"):
        store.add_adjustment(PERIOD_ID, {"type": "note", "project": "web", "day": None, "note": "x"}, service.client_of)
    with pytest.raises(TimesheetError, match="cannot change"):
        await service.draft(service.period(None), service.sources())
    assert "APPROVED" in store.export(PERIOD_ID, "md")
    assert store.export(PERIOD_ID, "csv").startswith("client_id,client,project,date,owner_hours")
    assert json.loads(store.export(PERIOD_ID, "json"))["status"] == "approved"
    tampered = json.loads(read(approved))
    tampered["clients"][0]["projects"][0]["days"][0]["ownerMinutes"] = 600
    write(approved, json.dumps(tampered))
    with pytest.raises(TimesheetError, match="changed after its approval"):
        store.export(PERIOD_ID, "md")


async def test_unmapped_ambiguous_folders_are_unassigned_with_a_note(tmp_path: Path) -> None:
    _, view = await drafted(
        tmp_path,
        projects={
            "web": {"client": "acme", "aliases": ["web-*"], "repos": ["acme/web"]},
            "switchboard": {"client": "nilware", "aliases": ["scr*"]},
            "other": {"client": "nilware", "aliases": ["*atch"]},
        },
    )
    assert project(view, "scratch") in view["unassigned"]
    assert any("scratch matches several projects (other, switchboard)" in n for n in view["notes"])


def test_csv_guards_formulas() -> None:
    document = {
        "clients": [
            {
                "id": "acme",
                "name": "=cmd",
                "projects": [
                    {
                        "project": "web",
                        "days": [
                            {
                                "date": "2026-11-17",
                                "ownerMinutes": 60,
                                "agentMinutes": 0,
                                "commits": [{"sha": "a" * 40, "subject": "@evil"}],
                                "pullRequests": [],
                            }
                        ],
                    }
                ],
            }
        ],
        "unassigned": [],
        "total": {"ownerMinutes": 60, "agentMinutes": 0, "commits": 1, "pullRequests": 0},
    }
    text = render_csv(document)
    assert "'=cmd" in text
    assert "'@evil" in text


@pytest.mark.parametrize(
    ("timesheet", "problem"),
    [
        ({"projects": {"web": {"client": "nobody"}}}, "not in timesheet.clients"),
        ({"timeZone": "Mars/Olympus"}, "unknown time zone"),
        (
            {"projects": {"a": {"client": "acme", "aliases": ["x"]}, "b": {"client": "acme", "aliases": ["x"]}}},
            "used by projects",
        ),
        ({"projects": {"web": {"client": "acme", "aliases": ["*"]}, "other": {"client": "acme"}}}, "also matches"),
        ({"authorEmails": ["not-an-email"]}, "email"),
        ({"ghBin": "-x"}, "command name"),
        ({"projects": {"web": {"client": "acme", "paths": ["/etc"]}}}, "not inside projectRoots"),
    ],
)
def test_config_validation(tmp_path: Path, timesheet: dict[str, Any], problem: str) -> None:
    with pytest.raises(ConfigError, match=problem):
        make_config(tmp_path, **timesheet)


def test_now_is_wednesday() -> None:
    assert at(25, 12) == NOW
