"""Claude Code's prompt suggestion on the hub (real ssh transport on the fake ssh, the real dispatcher
on the fake tmux with captured -e screens): the reported case (a suggestion in an empty box made
`remote_send` refuse and the summary call it done), the summariser's marked copy of the screen and its
prompt, screen echo, key presses that would accept a suggestion, `remote_clear_input`, and the
fallback for a dispatcher that sends no suggestion."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import read_records
from tests.remote import suggestion_fixtures as fx
from tests.remote.helpers import SESSION_A, RemoteWorld, pane
from tests.remote.test_send_tools import CLAUDE_PID, OWNER, Ctx, Make, build
from tests.remote.test_tools import build as build_status
from tests.remote.test_tools import populate
from voice_coding_mcp.remote.protocol import CaptureResponse
from voice_coding_mcp.remote.summarize import SUMMARY_PROMPT, mark_suggestion


@pytest.fixture
async def make(tmp_path: Path) -> AsyncIterator[Make]:
    built: list[Ctx] = []

    async def factory(**options: Any) -> Ctx:
        built.append(await build(tmp_path, **options))
        return built[-1]

    yield factory
    for item in built:
        await item.manager.shutdown("test finished")


def show(world: RemoteWorld, text: str, **extra: Any) -> None:
    world.set_panes([pane("fpga", "%1", CLAUDE_PID, command="claude")], {"%1": text}, **extra)
    world.register("2001", os.getpid(), sessionId=SESSION_A, name="fpga", status="idle", tmux="0:@0.%1")


def pressed(ctx: Ctx) -> list[list[str]]:
    return [c["argv"] for c in ctx.world.tmux_calls() if c["argv"][:1] == ["send-keys"]]


# --- the reported case: a suggestion in an empty box ------------------------------------------------


async def test_a_send_into_a_box_with_only_a_suggestion_goes_through(make: Make) -> None:
    ctx = await make()
    show(ctx.world, fx.OPEN_PR)
    readback = (await ctx.tools.remote_send("dev", "fpga", "run the tests", OWNER)).text
    assert readback.startswith("Ready to send")
    ctx.later()
    assert await ctx.confirm("4172") == ("Sent to fpga on dev.", False)
    assert ctx.sent_texts() == ["run the tests"]


async def test_a_real_draft_still_refuses_the_send(make: Make) -> None:
    ctx = await make()
    show(ctx.world, fx.TYPED)
    await ctx.tools.remote_send("dev", "fpga", "run the tests", OWNER)
    ctx.later()
    text, error = await ctx.confirm("4172")
    assert error
    assert text.startswith("Not sent: something is already typed in the input of fpga on dev.")


async def test_the_summariser_gets_the_suggestion_marked_and_the_prompt_says_what_it_is(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": fx.OPEN_PR.replace("so the file stays", "RESULT Idle. so the file stays")})
    c = await build_status(tmp_path, world)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert not answer.is_error
        (run,) = read_records(c.record)
        stdin = run["stdin"]
        assert "❯ [Claude Code suggests: open a PR for this]" in stdin
        assert "\x1b" not in stdin, "no terminal escapes reach the summariser"
        prompt = " ".join(run["argv"])
        assert "nobody typed it, it was not sent and nothing in it has happened" in prompt
        assert "nobody typed it, it was not sent and nothing in it has happened" in SUMMARY_PROMPT
    finally:
        await c.manager.shutdown("test finished")


async def test_the_heuristic_does_not_quote_the_suggestion(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": fx.OPEN_PR})
    c = await build_status(tmp_path, world, summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert "open a PR" not in answer.text
        assert "Churned for 14s" in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_pending_input_quotes_no_suggestion(make: Make) -> None:
    ctx = await make()
    show(ctx.world, fx.OPEN_PR)
    answer = await ctx.tools.pending_input("dev")
    assert "open a PR" not in answer.text


# --- screen echo ---------------------------------------------------------------------------------


async def test_a_suggestion_is_screen_text_for_screen_echo(make: Make) -> None:
    ctx = await make()
    suggestion = "push the release branch to origin and tag it"
    show(ctx.world, fx.screen(f"\x1b[39m❯\xa0\x1b[2m{suggestion}\x1b[0m"))
    await ctx.tools.remote_status("dev", "fpga", None)
    assert ctx.tools.echo.echoed(suggestion)


# --- keys and clear --------------------------------------------------------------------------------


async def test_right_arrow_at_a_suggestion_is_refused_and_escape_is_not(make: Make) -> None:
    ctx = await make()
    show(ctx.world, fx.OPEN_PR)
    refused = await ctx.tools.remote_send_keys("dev", "fpga", ["right"], OWNER)
    text, error = refused.text, refused.is_error
    assert error
    assert text == (
        "The input of fpga on dev shows a prompt that Claude Code suggests, which nobody typed; right arrow "
        "would accept it, so I will not press it. Tell me the message instead."
    )
    answer = await ctx.tools.remote_send_keys("dev", "fpga", ["escape"], OWNER)
    assert not answer.is_error, answer.text
    ctx.later()
    assert (await ctx.confirm("4172"))[1] is False
    assert pressed(ctx) == [["send-keys", "-t", "%1", "Escape"]]


async def test_clear_input_at_a_suggestion_is_nothing_to_clear(make: Make) -> None:
    ctx = await make()
    show(ctx.world, fx.OPEN_PR)
    answer = await ctx.tools.remote_clear_input("dev", "fpga", OWNER)
    assert (answer.text, answer.is_error) == ("Nothing is typed in the input of fpga on dev.", True)
    assert pressed(ctx) == []


WRAPPED = fx.screen(
    "\x1b[39m❯\xa0\x1b[2mopen a PR for this and then ask the reviewers\x1b[0m\n  \x1b[2mto look at it today\x1b[0m"
)
"""A long suggestion wrapped onto a second row of the box (red team finding: the second row was read
back as typed and cleared with Ctrl+C)."""


async def test_a_wrapped_suggestion_is_no_draft_for_clear_or_send(make: Make) -> None:
    ctx = await make()
    show(ctx.world, WRAPPED)
    answer = await ctx.tools.remote_clear_input("dev", "fpga", OWNER)
    assert (answer.text, answer.is_error) == ("Nothing is typed in the input of fpga on dev.", True)
    assert "to look at it" not in answer.text
    readback = (await ctx.tools.remote_send("dev", "fpga", "run the tests", OWNER)).text
    assert readback.startswith("Ready to send")
    assert pressed(ctx) == []


async def test_the_summariser_gets_a_wrapped_suggestion_as_one_mark(tmp_path: Path) -> None:
    world = RemoteWorld.create(tmp_path)
    populate(world, {"%1": WRAPPED.replace("so the file stays", "RESULT Idle. so the file stays")})
    c = await build_status(tmp_path, world)
    try:
        await c.tools.remote_status("dev", "fpga", None)
        (run,) = read_records(c.record)
        stdin = run["stdin"]
        assert "❯ [Claude Code suggests: open a PR for this and then ask the reviewers to look at it today]" in stdin
        assert stdin.count("to look at it today") == 1, "the second row is not left as screen text"
    finally:
        await c.manager.shutdown("test finished")


async def test_clear_input_reads_back_the_typed_draft_and_the_returning_suggestion_counts_as_cleared(
    make: Make,
) -> None:
    ctx = await make()
    show(ctx.world, fx.TYPED, afterKeys={"text": {"%1": fx.SUGGESTION}})
    readback = (await ctx.tools.remote_clear_input("dev", "fpga", OWNER)).text
    assert 'Its screen shows this typed there: "hello there".' in readback
    ctx.later()
    assert await ctx.confirm("4172") == ("Cleared the input of fpga on dev.", False)


# --- the summariser's copy and older dispatchers ---------------------------------------------------


def test_mark_suggestion() -> None:
    screen = "⏺ Done.\n────\n❯\n────\n  ? for shortcuts"
    assert mark_suggestion(screen, None) == screen
    assert mark_suggestion(screen, "") == screen
    assert (
        mark_suggestion(screen, "open a PR")
        == "⏺ Done.\n────\n❯ [Claude Code suggests: open a PR]\n────\n  ? for shortcuts"
    )
    boxed = "╭────╮\n│ >        │\n╰────╯"
    assert mark_suggestion(boxed, "fix it") == "╭────╮\n│ > [Claude Code suggests: fix it]\n╰────╯"
    # Typed text on the input line (an older dispatcher cut nothing): the mark goes at the end.
    typed = "────\n❯ fix it\n────"
    assert mark_suggestion(typed, "x") == "────\n❯ fix it\n────\n[Claude Code suggests: x]"
    # Secrets and escapes in a suggestion are cleaned.
    marked = mark_suggestion(screen, "use ghp_" + "a1" * 15 + " \x1b[31mred\x1b[0m")
    assert "ghp_" not in marked
    assert "[secret]" in marked
    assert "\x1b" not in marked


def test_an_older_dispatcher_sends_no_suggestion() -> None:
    old = CaptureResponse.model_validate(
        {"protocol": 1, "ok": True, "text": "❯ open a PR for this", "paneId": "%1", "session": "fpga"}
    )
    assert old.suggestion is None
    new = CaptureResponse.model_validate(
        {"protocol": 1, "ok": True, "text": "❯", "paneId": "%1", "session": "fpga", "suggestion": "x" * 1000}
    )
    assert new.suggestion == "x" * 300
