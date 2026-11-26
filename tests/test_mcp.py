"""The tool layer alone, without OAuth.

The SDK client talks to the MCPServer in process (the HTTP path is covered by test_http.py and
test_e2e.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.types import TextContent

from tests.helpers import FIXTURES, claude_config, read_records
from tests.session_fixtures import (
    assistant_text,
    claude_home,
    prompt,
    stop_background_sessions,
    write_live_session,
    write_transcript,
)
from voice_coding_mcp.clock import ago
from voice_coding_mcp.config import parse_config
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.mcp import McpServerOptions, create_mcp_server, server_instructions
from voice_coding_mcp.remote_control import RcOpened, RcStarted, RemoteControl, RemoteControlOptions
from voice_coding_mcp.sessions import read_live_sessions
from voice_coding_mcp.tasks.access import task_access
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions


@dataclass
class Ctx:
    client: Client
    manager: TaskManager
    record: str
    home: str
    """Fake Claude home (claude.configDir) with transcripts and the live registry."""
    projects: str

    async def call(self, name: str, args: dict[str, Any] | None = None) -> tuple[str, bool]:
        result = await self.client.call_tool(name, args or {})
        assert len(result.content) == 1
        content = result.content[0]
        assert isinstance(content, TextContent)
        assert result.structured_content is None
        return content.text, result.is_error is True

    async def text(self, name: str, args: dict[str, Any] | None = None) -> str:
        return (await self.call(name, args))[0]


@pytest.fixture
async def ctx(real_tmp: Path, request: pytest.FixtureRequest) -> AsyncIterator[Ctx]:
    """Parametrize indirectly with `{"tasks": {...}, "sessions": {...}, "remoteControl": {...}}` to
    override config sections, and `"bg"` for the fake claude's FAKE_CLAUDE_BG."""
    overrides: dict[str, Any] = getattr(request, "param", {})
    projects = real_tmp / "projects"
    (projects / "alpha-app").mkdir(parents=True)
    (projects / "beta-site").mkdir(parents=True)
    record = str(real_tmp / "record.jsonl")
    home = claude_home(str(real_tmp))
    claude = claude_config(record)
    config = parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(real_tmp / "state"),
            "projectRoots": [str(projects)],
            "claude": {
                "bin": claude.bin,
                "permissionMode": claude.permission_mode,
                "settingSources": list(claude.setting_sources),
                "configDir": home,
                "env": {**claude.env, "FAKE_CLAUDE_BG": overrides.get("bg", "")},
            },
            "remoteControl": overrides.get("remoteControl", {}),
            "tasks": {"syncWaitSeconds": 1, **overrides.get("tasks", {})},
            "sessions": overrides.get("sessions", {"days": 7, "replyWaitMinutes": 1}),
        },
        home_dir="/nonexistent-home-dir",
    )
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=config.state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            sessions=config.sessions,
            logger=silent_logger,
            kill_grace_ms=500,
        )
    )
    server = create_mcp_server(McpServerOptions(manager=manager, config=config, logger=silent_logger))
    # The client's cancel scopes must be entered and left in one task; pytest-asyncio runs fixture
    # setup and teardown in different tasks, so the client lives in a task of its own.
    connected: asyncio.Future[Client] = asyncio.get_running_loop().create_future()
    stop = asyncio.Event()

    async def hold_client() -> None:
        async with Client(server) as client:
            connected.set_result(client)
            await stop.wait()

    holder = asyncio.create_task(hold_client())
    try:
        client = await asyncio.wait_for(asyncio.shield(connected), 10)
        yield Ctx(client=client, manager=manager, record=record, home=home, projects=str(projects))
    finally:
        stop.set()
        await holder
        await manager.shutdown("test finished")
        stop_background_sessions(home)


# --- tool list ------------------------------------------------------------------------------------


async def test_has_the_thirteen_tools_with_the_design_annotations_and_server_instructions(ctx: Ctx) -> None:
    tools = (await ctx.client.list_tools()).tools
    by_name = {t.name: t for t in tools}
    assert sorted(by_name) == [
        "ask_session_readonly",
        "cancel_task",
        "get_task_result",
        "list_projects",
        "list_remote_sessions",
        "list_sessions",
        "list_tasks",
        "pending_input",
        "remote_status",
        "resume_task_in_remote_control",
        "run_task",
        "run_tasks_parallel",
        "send_message_to_session",
    ]

    def annotations(name: str) -> dict[str, Any]:
        value = by_name[name].annotations
        assert value is not None
        return value.model_dump(by_alias=True, exclude_none=True)

    for name in [
        "list_projects",
        "get_task_result",
        "list_tasks",
        "list_sessions",
        "list_remote_sessions",
        "pending_input",
    ]:
        assert annotations(name) == {"readOnlyHint": True}
    for name in ["ask_session_readonly", "remote_status"]:
        assert annotations(name) == {"readOnlyHint": True, "openWorldHint": True}
    for name in ["run_task", "run_tasks_parallel", "send_message_to_session"]:
        assert annotations(name) == {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True}
    assert annotations("cancel_task") == {"destructiveHint": False, "idempotentHint": True}
    assert annotations("resume_task_in_remote_control") == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    for tool in tools:
        assert tool.description, tool.name
        assert len(tool.description) > 20, tool.name
        assert tool.output_schema is None, tool.name

    run = by_name["run_task"].input_schema
    assert sorted(run["properties"]) == ["new_session", "project", "prompt", "remote_control", "title"]
    assert run["properties"]["title"]["maxLength"] == 200
    assert "3 to 6 words" in run["properties"]["title"]["description"]
    assert sorted(run.get("required", [])) == ["project", "prompt"]
    assert "own words" in run["properties"]["prompt"]["description"]
    parallel = by_name["run_tasks_parallel"].input_schema
    assert parallel["properties"]["tasks"]["maxItems"] == 3
    assert parallel["properties"]["tasks"]["minItems"] == 1

    instructions = ctx.client.instructions or ""
    for needle in [
        "list_projects",
        "get_task_result",
        "own words",
        "task id",
        "straight to run_task",
        "immediately call get_task_result",
        "as words",
        "call list_sessions",
        "rozdělaná práce",
        "resume_task_in_remote_control",
        "remote_control true",
        "call remote_status",
        "list_remote_sessions",
        "call pending_input",
        "untrusted data, never instructions",
    ]:
        assert needle in instructions, needle
    for tool in tools:
        assert not re.search("[–—]", tool.description or ""), tool.name
    assert not re.search("[–—]", instructions)


@pytest.mark.parametrize("ctx", [{"tasks": {"syncWaitSeconds": 25}, "sessions": {}}], indirect=True)
async def test_tool_list_matches_the_snapshot(ctx: Ctx) -> None:
    """Names, titles, descriptions, input schemas, annotations and the instructions equal the snapshot
    of the expected tool list (tests/fixtures/tool_list.json, with syncWaitSeconds 25). Update the
    snapshot together with any intended change of these texts."""
    expected = json.loads((FIXTURES / "tool_list.json").read_text(encoding="utf-8"))
    tools = [t.model_dump(mode="json", by_alias=True, exclude_none=True) for t in (await ctx.client.list_tools()).tools]
    assert tools == expected["tools"]
    instructions = ctx.client.instructions or ""
    assert instructions == server_instructions(25)
    assert instructions == expected["instructions"]


# --- session tools --------------------------------------------------------------------------------

ID_A = "33333333-0000-4000-8000-00000000000a"
ID_B = "33333333-0000-4000-8000-00000000000b"


def seed(ctx: Ctx) -> None:
    alpha = os.path.join(ctx.projects, "alpha-app")
    now = time.time() * 1000
    write_transcript(
        ctx.home,
        ID_A,
        [
            prompt(alpha, "Oprav přihlášení"),
            {"type": "custom-title", "customTitle": "Přihlášení", "sessionId": ID_A},
            assistant_text(alpha, "ok"),
        ],
        now - 5 * 60_000,
    )
    write_transcript(
        ctx.home, ID_B, [prompt(os.path.join(ctx.projects, "beta-site"), "Nový web pro kavárnu")], now - 3 * 3_600_000
    )


async def test_lists_sessions_with_name_project_state_and_age(ctx: Ctx) -> None:
    assert await ctx.text("list_sessions") == "There are no sessions from the last 7 days."
    seed(ctx)
    assert await ctx.text("list_sessions") == (
        "Sessions:\nPřihlášení (alpha-app, not running, 5 minutes ago)\n"
        "beta-site: Nový web pro kavárnu (beta-site, not running, 3 hours ago)"
    )
    assert (
        await ctx.text("list_sessions", {"project": "beta"})
        == "Sessions:\nbeta-site: Nový web pro kavárnu (beta-site, not running, 3 hours ago)"
    )
    assert (
        await ctx.text("list_sessions", {"limit": 1})
        == "Sessions:\nPřihlášení (alpha-app, not running, 5 minutes ago)\nAnd 1 older."
    )


async def test_asks_a_session_by_a_loosely_spoken_name(ctx: Ctx) -> None:
    seed(ctx)
    text = await ctx.text("ask_session_readonly", {"session": "prihlaseni", "question": "Co zbývá?\nRESULT Jen testy."})
    assert text == "Task 1 for session Přihlášení is done. Jen testy."
    rec = read_records(ctx.record)[0]
    assert rec["argv"][rec["argv"].index("--resume") + 1] == ID_A
    # The fork the ask created is the server's own session: never listed.
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    fork_id = task.session_id
    assert fork_id != ID_A
    write_transcript(ctx.home, fork_id, [prompt(os.path.join(ctx.projects, "alpha-app"), "fork of the session")])
    listed = await ctx.text("list_sessions")
    assert "fork" not in listed
    assert "Přihlášení" in listed
    assert await ctx.text("list_tasks") == "Task 1 for session Přihlášení: done."


async def test_reports_unknown_sessions_and_refuses_to_message_a_session_that_is_not_running(ctx: Ctx) -> None:
    seed(ctx)
    text, is_error = await ctx.call("ask_session_readonly", {"session": "kubernetes", "question": "x"})
    assert is_error
    assert text == 'No session matches "kubernetes". Recent sessions: Přihlášení, beta-site: Nový web pro kavárnu.'
    text, is_error = await ctx.call("send_message_to_session", {"session": "Přihlášení", "message": "ahoj"})
    assert is_error
    assert "is not running, so it cannot receive a message" in text
    assert read_records(ctx.record) == []


# --- tools ----------------------------------------------------------------------------------------


async def test_lists_projects(ctx: Ctx) -> None:
    assert await ctx.text("list_projects") == "Projects: alpha-app, beta-site."


async def test_runs_a_short_task_synchronously_and_passes_the_prompt_unchanged_through_stdin(ctx: Ctx) -> None:
    text_prompt = "Co je nového?\nRESULT Všechno v pořádku."
    text, is_error = await ctx.call("run_task", {"project": "alpha app", "prompt": text_prompt})
    assert not is_error
    assert text == "Task 1 in alpha-app is done. Všechno v pořádku."
    rec = read_records(ctx.record)[0]
    assert rec["stdin"] == text_prompt
    assert text_prompt not in rec["argv"]


async def test_reports_an_unknown_project_as_an_error_with_the_available_names(ctx: Ctx) -> None:
    text, is_error = await ctx.call("run_task", {"project": "gamma", "prompt": "hi"})
    assert is_error
    assert 'No project matches "gamma". Available projects: alpha-app, beta-site.' in text


async def test_returns_a_task_id_for_a_long_task_then_get_task_result_waits_for_it(ctx: Ctx) -> None:
    started = await ctx.text("run_task", {"project": "alpha-app", "prompt": "SLEEP 1800\nPROGRESS 1\nRESULT finished"})
    assert started.startswith("Task 1 in alpha-app is still running")
    assert "Ask for task 1" in started
    text = await ctx.text("get_task_result")
    if "is done" not in text:
        text = await ctx.text("get_task_result", {"task_id": 1})
    assert text == "Task 1 in alpha-app is done. finished"


async def test_caps_wait_seconds_at_sync_wait_seconds(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "SLEEP 8000"})
    start = time.monotonic()
    text = await ctx.text("get_task_result", {"task_id": 1, "wait_seconds": 120})
    assert time.monotonic() - start < 3
    assert "still running" in text
    quick = time.monotonic()
    await ctx.call("get_task_result", {"task_id": 1, "wait_seconds": 0})
    assert time.monotonic() - quick < 0.5


async def test_answers_for_unknown_tasks_and_when_there_are_none(ctx: Ctx) -> None:
    assert await ctx.text("get_task_result") == "There are no tasks yet."
    assert await ctx.text("list_tasks") == "There are no tasks yet."
    text, is_error = await ctx.call("get_task_result", {"task_id": 42})
    assert is_error
    assert text == "There is no task 42."


async def test_runs_tasks_in_parallel_and_reports_each(ctx: Ctx) -> None:
    text, is_error = await ctx.call(
        "run_tasks_parallel",
        {
            "tasks": [
                {"project": "alpha-app", "prompt": "RESULT one"},
                {"project": "beta-site", "prompt": "RESULT two"},
                {"project": "nope", "prompt": "RESULT three"},
            ]
        },
    )
    assert not is_error
    lines = text.split("\n")
    assert lines[0] == "Task 1 in alpha-app is done. one"
    assert lines[1] == "Task 2 in beta-site is done. two"
    assert 'No project matches "nope"' in lines[2]


async def test_rejects_more_than_three_parallel_tasks(ctx: Ctx) -> None:
    tasks = [{"project": "alpha-app", "prompt": "hi"} for _ in range(4)]
    _text, is_error = await ctx.call("run_tasks_parallel", {"tasks": tasks})
    assert is_error
    assert read_records(ctx.record) == []


async def test_lists_tasks_newest_first_optionally_per_project(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT a"})
    await ctx.call("run_task", {"project": "beta-site", "prompt": "FAIL"})
    assert await ctx.text("list_tasks") == "Task 2 in beta-site: failed.\nTask 1 in alpha-app: done."
    assert await ctx.text("list_tasks", {"project": "alpha"}) == "Task 1 in alpha-app: done."
    assert await ctx.text("list_tasks", {"limit": 1}) == "Task 2 in beta-site: failed."


async def test_cancels_a_running_task(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "SLEEP 60000"})
    assert await ctx.text("cancel_task", {"task_id": 1}) == "Task 1 was cancelled."
    task = ctx.manager.get(1)
    assert task is not None
    assert task.status == "cancelled"
    assert "already finished" in await ctx.text("cancel_task", {"task_id": 1})
    _text, is_error = await ctx.call("cancel_task", {"task_id": 9})
    assert is_error


def test_ago_rounds_to_the_nearest_unit() -> None:
    assert ago(0) == "just now"
    assert ago(29_999) == "just now"
    assert ago(31_000) == "1 minute ago"
    assert ago(5 * 60_000) == "5 minutes ago"
    assert ago(90 * 60_000) == "2 hours ago"
    assert ago(36 * 3_600_000) == "2 days ago"
    assert ago(-5000) == "just now"


# --- resume_task_in_remote_control ----------------------------------------------------------------


async def test_reopens_a_finished_task_for_the_app_and_lists_it_as_a_session(ctx: Ctx) -> None:
    alpha = os.path.join(ctx.projects, "alpha-app")
    assert await ctx.text("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo"}) == (
        "Task 1 in alpha-app is done. hotovo"
    )
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    # The transcript of a `claude -p` run: hidden from list_sessions until it is reopened.
    headless = {**prompt(alpha, "RESULT hotovo"), "entrypoint": "sdk-cli", "turnOrigin": "sdk"}
    write_transcript(ctx.home, task.session_id, [headless])
    assert "vcm-" not in await ctx.text("list_sessions")

    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert text.startswith("Task 1 is now open as session alpha-app (voice 1) in alpha-app.")
    assert f"https://claude.ai/code/session_01FAKE{task.session_id[:8]}" in text
    bg = read_records(ctx.record)[-1]
    assert bg["argv"][:3] == ["--bg", "--resume", task.session_id]
    assert bg["cwd"] == alpha

    again, _ = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert again.startswith("Task 1 is already open as session alpha-app (voice 1)")
    assert len(read_records(ctx.record)) == 2
    assert "alpha-app (voice 1) (alpha-app, running, just now)" in await ctx.text("list_sessions")
    persisted = ctx.manager.get(1)
    assert persisted is not None
    assert persisted.remote_session_id == task.session_id


async def test_resume_refuses_unknown_and_running_tasks(ctx: Ctx) -> None:
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 42})
    assert is_error
    assert text == "There is no task 42."
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "SLEEP 60000"})
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert is_error
    assert "is still running. Wait for its result" in text
    assert all("--bg" not in r["argv"] for r in read_records(ctx.record))
    await ctx.call("cancel_task", {"task_id": 1})


def make_disconnected(home: str, session_id: str) -> None:
    """Turns the fake's live registry entry of `session_id` into one that lost Remote Control: no
    `bridgeSessionId`, started two hours ago (its process stays alive)."""
    directory = os.path.join(home, "sessions")
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("sessionId") == session_id:
            data.pop("bridgeSessionId", None)
            data["startedAt"] = int(time.time() * 1000) - 7_200_000
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            return
    raise AssertionError("no registry entry")


async def test_cancel_closes_the_session_of_a_reopened_finished_task(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo"})
    await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    assert task.bg_id == task.session_id[:8]
    assert "It is open as session alpha-app (voice 1) in the Claude app." in await ctx.text(
        "get_task_result", {"task_id": 1}
    )
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: done; open in the Claude app as session alpha-app (voice 1)."
    )
    text, is_error = await ctx.call("cancel_task", {"task_id": 1})
    assert not is_error
    assert text == (
        "Session alpha-app (voice 1) closed. Its conversation is kept; ask to resume task 1 in the Claude app to"
        " continue it. Task 1 itself had already finished."
    )
    assert read_records(ctx.record)[-1]["argv"] == ["stop", task.session_id[:8]]
    assert await ctx.text("list_tasks") == "Task 1 in alpha-app: done."
    again = await ctx.text("cancel_task", {"task_id": 1})
    assert again.startswith("Task 1 had already finished. Task 1 in alpha-app is done.")


async def test_a_reopened_task_whose_session_lost_the_app_is_reported_and_reopened(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo"})
    await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    make_disconnected(ctx.home, task.session_id)
    result = await ctx.text("get_task_result", {"task_id": 1})
    assert result.endswith(
        "Its session alpha-app (voice 1) lost its connection to the Claude app and no longer shows there. Ask to"
        " resume task 1 in the Claude app to reconnect it, or cancel task 1 to close it."
    )
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: done; session alpha-app (voice 1) lost its connection to the Claude app."
    )
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert text.startswith("The earlier session of task 1 had lost its connection to the Claude app, so it was closed.")
    argvs = [r["argv"] for r in read_records(ctx.record)]
    assert argvs[-2] == ["stop", task.session_id[:8]]
    assert argvs[-1][:3] == ["--bg", "--resume", task.session_id]
    assert "It is open as session alpha-app (voice 1)" in await ctx.text("get_task_result", {"task_id": 1})


async def test_a_session_task_that_lost_the_app_is_reported_reopened_and_cancelled(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    make_disconnected(ctx.home, task.session_id)
    assert (await ctx.text("get_task_result", {"task_id": 1})).startswith(
        "Task 1 in alpha-app: session alpha-app (voice 1) lost its connection to the Claude app and no longer shows"
        " there. Its latest answer: OK Ask to resume task 1 in the Claude app to reconnect it, or cancel task 1"
    )
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: session alpha-app (voice 1) lost its connection to the Claude app."
    )
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert "Task 1 is now open again as session alpha-app (voice 1)" in text
    after = ctx.manager.get(1)
    assert after is not None
    assert (after.status, after.remote_session_id) == ("running", task.session_id)
    assert (await ctx.text("get_task_result", {"task_id": 1})).startswith(
        "Task 1 in alpha-app finished its turn in session alpha-app (voice 1)."
    )
    stopped = await ctx.text("cancel_task", {"task_id": 1})
    assert stopped.startswith("Task 1 was stopped.")


# --- run_task with remote_control -----------------------------------------------------------------


async def test_starts_a_task_as_a_session_with_the_prompt_on_stdin(ctx: Ctx) -> None:
    start = time.monotonic()
    text, is_error = await ctx.call(
        "run_task", {"project": "alpha-app", "prompt": "Oprav testy\nRESULT Opraveno.", "remote_control": True}
    )
    assert time.monotonic() - start < 5  # no wait for the result
    assert not is_error
    assert text.startswith("Task 1 in alpha-app started as session alpha-app (voice 1).")
    assert "open Code and pick alpha-app (voice 1)" in text
    task = ctx.manager.get(1)
    assert task is not None
    assert task.remote_control
    assert task.status == "running"
    assert task.session_id is not None
    assert task.remote_session_id == task.session_id
    assert task.bg_id == task.session_id[:8]
    assert text.endswith(f"Link (do not read it aloud): https://claude.ai/code/session_01FAKE{task.bg_id}")

    rec = read_records(ctx.record)[-1]
    argv = rec["argv"]
    assert rec["stdin"] == "Oprav testy\nRESULT Opraveno."
    assert "Oprav testy" not in " ".join(argv)
    assert "--resume" not in argv
    assert "-p" not in argv
    assert argv[:5] == ["--bg", "--remote-control", "alpha-app (voice 1)", "--name", "alpha-app (voice 1)"]
    assert argv[argv.index("--permission-mode") + 1] == "auto"
    assert "--strict-mcp-config" in argv  # isolated access by default
    assert argv.count("--settings") == 1
    hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["Stop"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert ctx.manager.session_answer_path(1) in hook["command"]
    assert rec["cwd"] == os.path.join(ctx.projects, "alpha-app")
    assert "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC" not in rec["env"]

    # The fake ran the Stop hook and its session is idle: the answer is reported at once.
    start = time.monotonic()
    result = await ctx.text("get_task_result", {"task_id": 1})
    assert time.monotonic() - start < 2
    assert result.startswith(
        "Task 1 in alpha-app finished its turn in session alpha-app (voice 1). Its latest answer: Opraveno."
    )
    assert await ctx.text("list_tasks") == "Task 1 in alpha-app: finished its turn in session alpha-app (voice 1)."

    # resume_task_in_remote_control names the session instead of starting another.
    again, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert again.startswith("Task 1 is already open as session alpha-app (voice 1) in alpha-app.")
    assert sum(1 for r in read_records(ctx.record) if "--bg" in r["argv"]) == 1


def write_json(path: str, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


async def test_desktop_access_args_go_into_the_session(ctx: Ctx, real_tmp: Path) -> None:
    alpha = os.path.join(ctx.projects, "alpha-app")
    trust = {"projects": {alpha: {"hasTrustDialogAccepted": True}}}
    write_json(os.path.join(ctx.home, ".claude.json"), trust)
    config = claude_config(ctx.record, access="desktop", config_dir=ctx.home)
    rc = RemoteControl(
        RemoteControlOptions(
            claude=config,
            claude_home=ctx.home,
            mcp_config_path=ctx.manager.mcp_config_path,
            max_sessions=3,
            logger=silent_logger,
            public_url="https://vcm.example.com",
            answer_path=ctx.manager.session_answer_path,
        )
    )
    result = await rc.start_session(lambda: ctx.manager.start_remote("alpha-app", "Ahoj"), opened={})
    assert isinstance(result, RcStarted)
    assert isinstance(result.outcome, RcOpened)
    argv = read_records(ctx.record)[-1]["argv"]
    expected = task_access(
        config, project_path=alpha, public_url="https://vcm.example.com", mcp_config_path=ctx.manager.mcp_config_path
    )
    assert "--strict-mcp-config" not in argv
    assert argv[argv.index("--setting-sources") + 1] == "user,project,local"
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["deniedMcpServers"] == expected.settings["deniedMcpServers"]
    assert "Stop" in settings["hooks"]
    assert argv.count("--settings") == 1


@pytest.mark.parametrize("ctx", [{"remoteControl": {"maxSessions": 1}}], indirect=True)
async def test_falls_back_to_a_headless_run_when_the_sessions_are_full(ctx: Ctx) -> None:
    first, _ = await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    assert first.startswith("Task 1 in alpha-app started as session alpha-app (voice 1).")
    text, is_error = await ctx.call(
        "run_task", {"project": "beta-site", "prompt": "RESULT hotovo", "remote_control": True}
    )
    assert not is_error
    assert text == (
        "There is already 1 session open in the Claude app: alpha-app (voice 1) (task 1). To close one, ask to cancel"
        " its task, for example cancel task 1. So this task runs without the Claude app. Task 2 in beta-site is"
        " done. hotovo"
    )
    second = ctx.manager.get(2)
    assert second is not None
    assert not second.remote_control
    assert "-p" in read_records(ctx.record)[-1]["argv"]


@pytest.mark.parametrize("ctx", [{"remoteControl": {"maxSessions": 0}}], indirect=True)
async def test_runs_headless_when_sessions_are_turned_off(ctx: Ctx) -> None:
    text = await ctx.text("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo", "remote_control": True})
    assert text.startswith("Sessions for the Claude app are turned off on this server. So this task runs without")
    assert all("--bg" not in r["argv"] for r in read_records(ctx.record))


@pytest.mark.parametrize("ctx", [{"bg": "untrusted"}], indirect=True)
async def test_an_untrusted_folder_fails_with_a_spoken_hint(ctx: Ctx) -> None:
    text, is_error = await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    assert is_error
    assert "Run claude once in that folder on the Mac and confirm the trust question" in text
    task = ctx.manager.get(1)
    assert task is not None
    assert task.status == "failed"
    assert "does not trust the folder" in (task.error or "")


async def test_cancel_stops_the_session(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    task = ctx.manager.get(1)
    assert task is not None
    assert task.bg_id is not None
    text, is_error = await ctx.call("cancel_task", {"task_id": 1})
    assert not is_error
    assert text.startswith("Task 1 was stopped. Its conversation is kept")
    assert read_records(ctx.record)[-1]["argv"] == ["stop", task.bg_id]
    after = ctx.manager.get(1)
    assert after is not None
    assert after.status == "cancelled"
    assert after.result == "OK"
    assert "was stopped; session alpha-app (voice 1) has ended" in await ctx.text("get_task_result", {"task_id": 1})
    again = await ctx.text("cancel_task", {"task_id": 1})
    assert again.startswith("Task 1 had already finished.")


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        ({"bg": "busy,nohook"}, "Task 1 in alpha-app is working in session alpha-app (voice 1). In the Claude app"),
        ({"bg": "waiting,nohook"}, "Task 1 in alpha-app is waiting for your approval in session alpha-app (voice 1)."),
        ({"bg": "input,nohook"}, "Task 1 in alpha-app is waiting for your answer in session alpha-app (voice 1)."),
        (
            {"bg": "nohook"},
            "Task 1 in alpha-app finished its turn in session alpha-app (voice 1). It wrote no text answer.",
        ),
    ],
    indirect=["ctx"],
)
async def test_get_task_result_maps_the_session_state(ctx: Ctx, expected: str) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    assert (await ctx.text("get_task_result", {"task_id": 1})).startswith(expected)


async def test_a_session_that_ended_is_done_with_its_last_answer(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT Hotovo.", "remote_control": True})
    stop_background_sessions(ctx.home)
    task = ctx.manager.get(1)
    assert task is not None
    for _ in range(100):
        if not os.listdir(os.path.join(ctx.home, "sessions")):
            break
        await asyncio.sleep(0.02)
    text = await ctx.text("get_task_result", {"task_id": 1})
    assert text.startswith(
        "Task 1 in alpha-app is done; session alpha-app (voice 1) has ended. Its latest answer: Hotovo."
    )
    assert await ctx.text("list_tasks") == "Task 1 in alpha-app: done, session alpha-app (voice 1) has ended."


async def test_a_handed_over_session_is_reported_and_never_opened_twice(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo"})
    await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not (await ctx.call("cancel_task", {"task_id": 1}))[1]
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    sid = task.session_id
    ctx.manager.handed_over(1, sid)  # what the handoff job records after `open claude://resume`
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: done; session alpha-app (voice 1) is in the Claude desktop app's history,"
        " resume task 1 in the Claude app to continue it on the phone."
    )
    assert (await ctx.text("get_task_result", {"task_id": 1})).endswith(
        " Its session alpha-app (voice 1) is kept in the Claude desktop app on the Mac, in its session history."
        " To continue it on the phone, ask to resume task 1 in the Claude app."
    )

    # The owner continues it in the desktop app, without Remote Control.
    entry = write_live_session(ctx.home, os.getpid(), {"sessionId": sid, "kind": "interactive", "status": "idle"})
    launches = len(read_records(ctx.record))
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: done; session alpha-app (voice 1) is open in the Claude desktop app on the Mac."
    )
    assert (await ctx.text("get_task_result", {"task_id": 1})).endswith(
        "To continue it on the phone, switch on Remote Control for it there."
    )
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert is_error
    assert text.startswith("Task 1's session alpha-app (voice 1) is open in the Claude desktop app on the Mac")
    # With Remote Control switched on there: named like an open session, still nothing launched.
    write_live_session(
        ctx.home, os.getpid(), {"sessionId": sid, "kind": "interactive", "bridgeSessionId": "session_01DESK"}
    )
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert text.startswith("Task 1 is already open as session alpha-app (voice 1) in alpha-app.")
    assert len(read_records(ctx.record)) == launches
    assert (ctx.manager.get(1) or task).handed_over_at is not None

    # Closed in the desktop app: resumed by the server again, which clears the handoff marker.
    os.remove(entry)
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    assert not is_error
    assert text.startswith("Task 1 is now open as session alpha-app (voice 1)")
    assert read_records(ctx.record)[-1]["argv"][:3] == ["--bg", "--resume", sid]
    after = ctx.manager.get(1)
    assert after is not None
    assert (after.handed_over_at, after.desktop_session_id) == (None, None)
    await ctx.call("cancel_task", {"task_id": 1})


def make_stale(ctx: Ctx, session_id: str) -> None:
    """Rewrites the fake bg session's registry entry as one that lost Remote Control long ago."""
    directory = os.path.join(ctx.home, "sessions")
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        if data.get("sessionId") == session_id:
            data.pop("bridgeSessionId", None)
            data["startedAt"] = int(time.time() * 1000) - 7_200_000
            write_json(path, data)
            return
    raise AssertionError("no registry entry")


async def test_a_session_task_stays_running_while_its_stale_session_is_replaced(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "Ahoj", "remote_control": True})
    task = ctx.manager.get(1)
    assert task is not None
    assert task.session_id is not None
    sid = task.session_id
    make_stale(ctx, sid)
    # The idle close or get_task_result refreshing while the old session is gone and the new one
    # not yet started must not end the task.
    done = asyncio.Event()
    ended: list[str] = []

    async def refresh() -> None:
        while not done.is_set():
            ctx.manager.refresh_remote(read_live_sessions(ctx.home))
            current = ctx.manager.get(1)
            if current is not None and current.status != "running":
                ended.append(current.status)
            await asyncio.sleep(0.01)

    refresher = asyncio.create_task(refresh())
    text, is_error = await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    done.set()
    await refresher
    assert not is_error
    assert text.startswith("The earlier session of task 1 had lost its connection to the Claude app, so it was closed.")
    assert ended == []
    after = ctx.manager.get(1)
    assert after is not None
    assert (after.status, after.session_id, after.remote_session_id, after.bg_id) == ("running", sid, sid, sid[:8])
    assert [r["argv"][0] for r in read_records(ctx.record)] == ["--bg", "stop", "--bg"]
    ctx.manager.refresh_remote(read_live_sessions(ctx.home))
    assert (ctx.manager.get(1) or after).status == "running"


@pytest.mark.parametrize("ctx", [{"bg": "turn"}], indirect=True)
async def test_reports_the_latest_answer_of_a_task_continued_in_the_app(ctx: Ctx) -> None:
    await ctx.call("run_task", {"project": "alpha-app", "prompt": "RESULT hotovo"})
    await ctx.call("resume_task_in_remote_control", {"task_id": 1})
    argv = read_records(ctx.record)[-1]["argv"]
    assert argv[:2] == ["--bg", "--resume"]
    assert argv.count("--settings") == 1
    hook = json.loads(argv[argv.index("--settings") + 1])["hooks"]["Stop"][0]["hooks"][0]
    assert ctx.manager.session_answer_path(1) in hook["command"]
    assert await ctx.text("get_task_result", {"task_id": 1}) == (
        "Task 1 in alpha-app was continued in session alpha-app (voice 1) in the Claude app. Its latest answer"
        " there: OK It is open as session alpha-app (voice 1) in the Claude app."
    )
    assert await ctx.text("list_tasks") == (
        "Task 1 in alpha-app: done; latest answer in the Claude app: OK; open in the Claude app as session"
        " alpha-app (voice 1)."
    )
    task = ctx.manager.get(1)
    assert task is not None
    assert (task.result, task.session_answer) == ("hotovo", "OK")
    # The answer of another session is ignored.
    write_json(ctx.manager.session_answer_path(1), {"session_id": "x", "last_assistant_message": "cizí"})
    assert "cizí" not in await ctx.text("get_task_result", {"task_id": 1})
