"""A hub with its own Claude Code home, a timesheet config and fixed times, for the service, store,
tool and CLI tests."""

from __future__ import annotations

import json
import zoneinfo
from datetime import datetime
from pathlib import Path
from typing import Any

from tests.helpers import FIXTURES, claude_config
from tests.usage.helpers import assistant, user, write_transcript
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.timesheet.service import TimesheetService
from voice_coding_mcp.usage.service import UsageService

ZURICH = zoneinfo.ZoneInfo("Europe/Zurich")
MIN = 60_000
FAKE_GH = str(FIXTURES / "fake_gh.py")
OWNER_EMAIL = "owner@example.com"
S1 = "44444444-0000-4000-8000-000000000001"
S2 = "44444444-0000-4000-8000-000000000002"
S3 = "44444444-0000-4000-8000-000000000003"


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(2026, 11, day, hour, minute, tzinfo=ZURICH).timestamp() * 1000)


NOW = at(25, 12)
"""Wednesday 2026-11-25 noon: "last week" is 2026-11-16 to 2026-11-22."""
PERIOD_ID = "2026-11-16_2026-11-22"


def config_data(tmp: Path, **timesheet: Any) -> dict[str, Any]:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    hub = tmp / "hub-claude"
    hub.mkdir(exist_ok=True)
    claude = claude_config(str(tmp / "record.jsonl"))
    section: dict[str, Any] = {
        "timeZone": "Europe/Zurich",
        "clients": {"acme": {"name": "Acme AG"}, "nilware": {"name": "nilware"}},
        "projects": {
            "web": {"client": "acme", "aliases": ["web-*"], "repos": ["acme/web"]},
            "switchboard": {"client": "nilware"},
        },
        "authorEmails": [OWNER_EMAIL],
        "githubLogins": ["owner-login"],
        "ghBin": FAKE_GH,
        **timesheet,
    }
    return {
        "publicUrl": "https://vcm.example.com",
        "stateDir": str(tmp / "state"),
        "projectRoots": [str(projects)],
        "claude": {"bin": claude.bin, "env": dict(claude.env), "configDir": str(hub)},
        "tasks": {"syncWaitSeconds": 5},
        "timesheet": section,
    }


def make_config(tmp: Path, **timesheet: Any) -> Config:
    return parse_config(config_data(tmp, **timesheet), home_dir="/nonexistent-home-dir")


def write_config(tmp: Path, **timesheet: Any) -> str:
    path = tmp / "config.json"
    path.write_text(json.dumps(config_data(tmp, **timesheet)), encoding="utf-8")
    return str(path)


def make_service(config: Config, now: int = NOW) -> TimesheetService:
    usage = UsageService(config, {}, silent_logger, now=lambda: now, tz=ZURICH, env={"HOME": "/nonexistent"})
    return TimesheetService(config, usage, now=lambda: now, env={"PATH": "/usr/bin:/bin"})


def seed(tmp: Path) -> None:
    """Tuesday: 30 owner minutes in web (two folders), 20 in switchboard, a headless task of 10
    minutes in web, a subagent in switchboard, an unmapped folder, and secret text everywhere."""
    hub = tmp / "hub-claude"
    web, worktree, board, other = (
        "/srv/projects/web",
        "/srv/projects/web-feature",
        "/srv/projects/switchboard",
        "/srv/projects/scratch",
    )
    write_transcript(
        hub,
        S1,
        [
            user(at(17, 9), cwd=web, entrypoint="cli", origin={"kind": "human"}),
            assistant(at(17, 9, 10), "m1", cwd=web),
            assistant(at(17, 9, 20), "m2", cwd=worktree),
            assistant(at(17, 9, 30), "m3", cwd=web),
            user(at(17, 9, 31), cwd=web, origin={"kind": "task-notification"}),
        ],
        project_dir="-srv-projects-web",
    )
    write_transcript(
        hub,
        S2,
        [
            user(at(17, 14), cwd=board, entrypoint="cli", origin={"kind": "human"}),
            assistant(at(17, 14, 10), "b1", cwd=board),
            assistant(at(17, 14, 20), "b2", cwd=board),
            user(at(17, 20), cwd=other, origin={"kind": "human"}),
        ],
        project_dir="-srv-projects-switchboard",
    )
    agents = hub / "projects" / "-srv-projects-switchboard" / S2 / "subagents"
    agents.mkdir(parents=True)
    lines = [
        assistant(at(17, 15), "x1", cwd=board, isSidechain=True),
        assistant(at(17, 15, 12), "x2", cwd=board, isSidechain=True),
    ]
    (agents / "agent-a1.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    write_transcript(
        hub,
        S3,
        [user(at(17, 11), cwd=web, entrypoint="sdk-cli"), assistant(at(17, 11, 10), "t1", cwd=web)],
        project_dir="-srv-projects-web",
    )
