"""The voice tools (summary, approval in a later turn) and `voice-coding-mcp timesheet`."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

import pytest

from tests.test_cli import run_cli
from tests.timesheet.helpers import NOW, PERIOD_ID, make_config, make_service, seed, write_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.remote.tools import Caller
from voice_coding_mcp.timesheet.cli import parse_delta
from voice_coding_mcp.timesheet.tool import TimesheetTool, TimesheetToolOptions

OWNER = Caller(fingerprint="grant-a")
OTHER = Caller(fingerprint="grant-b")


def make_tool(tmp: Path, clock: list[int], **timesheet: object) -> TimesheetTool:
    config = make_config(tmp, **timesheet)
    service = make_service(config)
    service._now = lambda: clock[0]
    service.store._now = lambda: clock[0]
    seed(tmp)
    return TimesheetTool(
        TimesheetToolOptions(
            service=service,
            logger=silent_logger,
            sync_wait_seconds=10,
            confirm_seconds=120,
            min_confirm_seconds=4,
            now=lambda: clock[0],
        )
    )


def confirmation(text: str) -> str:
    found = re.search(r"\(confirmation ([0-9]{4})\)$", text)
    assert found is not None, text
    return found.group(1)


async def test_summary_drafts_and_reads_back(tmp_path: Path) -> None:
    tool = make_tool(tmp_path, [NOW])
    answer = await tool.summary(None)
    assert not answer.is_error
    assert answer.text.startswith("Last week: Acme AG 31 owner minutes and 10 agent minutes across 1 project")
    assert answer.text.endswith("This is a draft, not approved.")
    assert (tmp_path / "state" / "timesheets" / f"{PERIOD_ID}.draft.json").exists()
    bad = await tool.summary("someday")
    assert bad.is_error


async def test_approval_needs_a_later_confirmation_by_the_same_client(tmp_path: Path) -> None:
    clock = [NOW]
    tool = make_tool(tmp_path, clock)
    answer = await tool.approve(None, OWNER)
    assert answer.is_error
    assert "no draft" in answer.text
    await tool.summary(None)
    answer = await tool.approve(None, OWNER)
    assert "Shall I approve it?" in answer.text
    assert "nothing is sent anywhere" in answer.text
    number = confirmation(answer.text)
    early = await tool.confirm(number, OWNER)
    assert early.is_error
    assert "wait for their yes" in early.text
    clock[0] += 5_000
    stranger = await tool.confirm(number, OTHER)
    assert stranger.is_error
    assert not (tmp_path / "state" / "timesheets" / f"{PERIOD_ID}.approved.json").exists()
    done = await tool.confirm(number, OWNER)
    assert not done.is_error
    assert done.text.startswith("Approved the timesheet from 2026-11-16 to 2026-11-22.")
    again = await tool.confirm(number, OWNER)
    assert again.is_error
    approved = json.loads(
        (tmp_path / "state" / "timesheets" / f"{PERIOD_ID}.approved.json").read_text(encoding="utf-8")
    )
    assert approved["approval"]["via"] == "voice"
    summary = await tool.summary(None)
    assert summary.text.endswith("It is approved.")


async def test_a_changed_draft_is_not_approved(tmp_path: Path) -> None:
    clock = [NOW]
    tool = make_tool(tmp_path, clock)
    await tool.summary(None)
    number = confirmation((await tool.approve(None, OWNER)).text)
    store = tool._service.store
    store.add_adjustment(
        PERIOD_ID,
        {"type": "hours", "project": "web", "day": "2026-11-17", "kind": "owner", "minutes": 5},
        tool._service.client_of,
    )
    clock[0] += 5_000
    answer = await tool.confirm(number, OWNER)
    assert answer.is_error
    assert "changed since it was shown" in answer.text


async def test_expired_approvals_and_voice_approval_off(tmp_path: Path) -> None:
    clock = [NOW]
    tool = make_tool(tmp_path, clock)
    await tool.summary(None)
    number = confirmation((await tool.approve(None, OWNER)).text)
    clock[0] += 121_000
    assert (await tool.confirm(number, OWNER)).is_error
    (tmp_path / "off").mkdir()
    off = make_tool(tmp_path / "off", [NOW], voiceApproval=False)
    answer = await off.approve(None, OWNER)
    assert answer.is_error
    assert "Approving by voice is off" in answer.text


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("+1h30m", 90), ("-0.5h", -30), ("45m", 45), ("90", 90), ("-15", -15), ("2h", 120)],
)
def test_parse_delta(text: str, minutes: int) -> None:
    assert parse_delta(text) == minutes


@pytest.mark.parametrize("text", ["", "h", "1x", "1h30", "++1h"])
def test_parse_delta_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="not a duration"):
        parse_delta(text)


async def test_cli_draft_show_adjust_approve_export(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    seed(tmp_path)
    period = "2026-11-16_2026-11-22"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    code, io = await run_cli("--config", path, "timesheet", "draft", period, env=env)
    assert code == 0, io.stderr
    assert "# Timesheet 2026-11-16 to 2026-11-22 (DRAFT)" in io.stdout
    assert "## Acme AG: owner 0.52 h, agent 0.17 h, 1 commits, 1 PRs" in io.stdout
    draft_id = re.search(r"Draft ([0-9a-f]{12}) written", io.stdout)
    assert draft_id is not None
    code, io = await run_cli(
        "--config", path, "timesheet", "adjust", period, "--project", "scratch", "--day", "2026-11-17",
        "--owner", "1m", "--move-to", "switchboard", "--note", "scratch belongs to switchboard", env=env,
    )  # fmt: skip
    assert code == 0, io.stderr
    code, io = await run_cli("--config", path, "timesheet", "approve", period, "--expect", draft_id.group(1), env=env)
    assert code == 1
    assert "changed since it was shown" in io.stderr
    code, io = await run_cli("--config", path, "timesheet", "show", period, "--format", "json", env=env)
    view = json.loads(io.stdout)
    code, io = await run_cli("--config", path, "timesheet", "export", period, env=env)
    assert code == 1
    assert "not approved" in io.stderr
    code, io = await run_cli(
        "--config", path, "timesheet", "approve", period, "--expect", view["viewSha256"][:12], env=env
    )
    assert code == 0, io.stderr
    out = tmp_path / "exported.csv"
    code, io = await run_cli(
        "--config", path, "timesheet", "export", period, "--format", "csv", "--out", str(out), env=env
    )
    assert code == 0, io.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600
    assert "nilware,switchboard,2026-11-17,0.35" in out.read_text(encoding="utf-8")
    code, io = await run_cli(
        "--config", path, "timesheet", "adjust", period, "--project", "web", "--note", "late", env=env
    )  # fmt: skip
    assert code == 1
    assert "cannot change" in io.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ["adjust", "--project", "web", "--owner", "1h"],
        ["adjust", "--project", "web", "--day", "2026-11-17"],
        ["adjust", "--project", "web", "--day", "2026-11-17", "--owner", "1h", "--agent", "1h"],
        ["adjust", "--project", "nope", "--day", "2026-11-17", "--owner", "1h"],
        ["adjust", "--project", "web", "--day", "2026-11-17", "--owner", "-1h", "--move-to", "switchboard"],
    ],
)
async def test_cli_adjust_argument_errors(tmp_path: Path, argv: list[str]) -> None:
    path = write_config(tmp_path)
    seed(tmp_path)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    code, io = await run_cli("--config", path, "timesheet", "draft", "2026-11-16_2026-11-22", env=env)
    assert code == 0, io.stderr
    code, io = await run_cli("--config", path, "timesheet", argv[0], "2026-11-16_2026-11-22", *argv[1:], env=env)
    assert code == 2, io.stdout


async def test_cli_bad_period(tmp_path: Path) -> None:
    code, io = await run_cli("--config", write_config(tmp_path), "timesheet", "show", "someday")
    assert code == 2
    assert "Unknown period" in io.stderr
