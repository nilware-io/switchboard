"""`remote_clear_input` and its `remote_send_confirm` end to end (real ssh transport on the fake ssh,
the real dispatcher on the fake tmux, an injected clock): the read-back of the draft, the
confirmation bound to it, every refusal on the hub and at confirm time, the audit line without the
draft, and the refusals of `remote_send` and `remote_send_keys` that point to it."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from tests.remote.helpers import SESSION_A, SESSION_B, RemoteWorld, pane
from tests.remote.menu_fixtures import capture, menu_lines
from tests.remote.test_send_tools import CLAUDE_PID, DIALOG, OTHER, OWNER, Ctx, Make, build
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.protocol import VersionResponse
from voice_coding_mcp.remote.send import CLEAR, Confirmation
from voice_coding_mcp.remote.speech import draft_words, format_confirm_outcome
from voice_coding_mcp.remote.tools import EARLY, EXPIRED
from voice_coding_mcp.remote.transport import TransportResult

RULE = "─" * 60
STATUS = "  ⏵⏵ auto mode on (shift+tab to cycle)"


def box(*content: str, top: str = RULE, below: str = STATUS) -> str:
    return "⏺ Done. All 41 tests pass.\n\n" + "\n".join([top, *content, RULE, below]) + "\n"


DRAFT = box("❯ fix the parser and then run the tests")
EMPTY = box("❯ ")
READBACK = (
    "Ready to clear what is typed in the input of fpga on dev. Its screen shows this typed there: "
    '"fix the parser and then run the tests". Clearing throws that text away; I send nothing and press '
    "only Ctrl+C once. At the keyboard, the up arrow may bring it back. Say yes to clear it. "
    "(confirmation 4172, do not read this number aloud)"
)


@pytest.fixture
async def make(tmp_path: Path) -> AsyncIterator[Make]:
    built: list[Ctx] = []

    async def factory(**options: Any) -> Ctx:
        built.append(await build(tmp_path, **options))
        return built[-1]

    yield factory
    for item in built:
        await item.manager.shutdown("test finished")


def show(world: RemoteWorld, text: str, *, status: str = "idle", waiting_for: str | None = None, **extra: Any) -> None:
    world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", 9_200_002, window_index=1, window_name="shell", command="bash"),
        ],
        {"%1": text, "%2": "$ make"},
        **extra,
    )
    data: dict[str, Any] = {"sessionId": SESSION_A, "name": "fpga", "status": status, "tmux": "0:@0.%1"}
    if waiting_for is not None:
        data["waitingFor"] = waiting_for
    world.register("2001", os.getpid(), **data)


async def prepare(ctx: Ctx, target: str = "fpga", caller: Any = OWNER) -> tuple[str, bool]:
    answer = await ctx.tools.remote_clear_input("dev", target, caller)
    return answer.text, answer.is_error


def pressed(ctx: Ctx) -> list[list[str]]:
    return [c["argv"] for c in ctx.world.tmux_calls() if c["argv"][:1] == ["send-keys"]]


def clear_calls(ctx: Ctx) -> list[str]:
    return [c["argv"][-1] for c in ctx.world.ssh_calls() if " clear " in c["argv"][-1]]


async def test_reads_back_the_draft_and_clears_only_after_confirm(make: Make) -> None:
    ctx = await make()
    said: list[str] = []
    ctx.tools.echo.owner_said = said.append  # type: ignore[method-assign,assignment]
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    assert await prepare(ctx) == (READBACK, False)
    assert pressed(ctx) == [], "remote_clear_input presses nothing"
    ctx.later()
    assert await ctx.confirm("4172") == ("Cleared the input of fpga on dev.", False)
    assert said == [], "clearing is never the owner's words"
    assert pressed(ctx) == [["send-keys", "-t", "%1", "C-c"]]
    assert clear_calls(ctx) == [f"switchboard-remote clear %1 {CLAUDE_PID} {SESSION_A} {d.draft_hash(DRAFT)}"]
    assert await ctx.confirm("4172") == (EXPIRED, True), "single use"
    (line,) = ctx.audit()
    assert (line["event"], line["outcome"], line["machine"], line["paneId"]) == (
        "remote_clear_input",
        "sent",
        "dev",
        "%1",
    )
    assert "textHmac" not in line
    assert "textChars" not in line
    assert "text" not in line
    assert "fix the parser" not in str(line)
    logged = ctx.log.text()
    assert "remote_clear_input" in logged
    assert "fix the parser" not in logged, "no draft text in the log"


async def test_the_audit_keeps_no_draft_even_with_log_prompts(make: Make) -> None:
    ctx = await make(log_prompts=True)
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    await prepare(ctx)
    ctx.later()
    await ctx.confirm("4172")
    (line,) = ctx.audit()
    assert "text" not in line
    assert "fix the parser" not in str(line)


async def test_confirm_needs_a_new_turn_and_the_same_client(make: Make) -> None:
    ctx = await make()
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    await prepare(ctx)
    assert await ctx.confirm("4172") == (EARLY, True)
    ctx.later()
    assert await ctx.confirm("4172", caller=OTHER) == (EXPIRED, True)
    assert pressed(ctx) == []
    ctx.later(200)
    assert await ctx.confirm("4172") == (EXPIRED, True)
    assert pressed(ctx) == []


async def test_a_draft_that_changed_after_the_read_back_is_not_cleared(make: Make) -> None:
    ctx = await make()
    show(ctx.world, DRAFT)
    await prepare(ctx)
    show(ctx.world, box("❯ fix the parser and then push"))
    ctx.later()
    assert await ctx.confirm("4172") == (
        "Not cleared: what is typed there changed since I read it to you. Ask me to read it again.",
        True,
    )
    assert pressed(ctx) == []
    (line,) = ctx.audit()
    assert line["outcome"] == "not_sent:draft_changed"


@pytest.mark.parametrize(
    ("after", "said"),
    [
        (
            box("❯ t two"),
            "I pressed Ctrl+C once in fpga on dev, but its input still shows text (about 5 characters). I pressed "
            "nothing else. Ask me what its screen says, or clear it at the keyboard.",
        ),
    ],
)
async def test_text_left_after_the_key_is_reported_without_more_keys(make: Make, after: str, said: str) -> None:
    ctx = await make()
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": after}})
    await prepare(ctx)
    ctx.later()
    assert await ctx.confirm("4172") == (said, True)
    assert len(pressed(ctx)) == 1
    (line,) = ctx.audit()
    assert line["outcome"] == "not_cleared"


async def test_a_long_draft_is_clipped_and_redacted_in_the_read_back(make: Make) -> None:
    ctx = await make()
    words = " ".join(f"word{n}" for n in range(30))
    show(ctx.world, box(f"❯ use token ghp_{'a1' * 15} then {words}"))
    readback, error = await prepare(ctx)
    assert not error
    assert "ghp_" not in readback
    assert "[secret]" in readback
    assert "word20" not in readback
    assert ' ..."' in readback


async def test_a_tall_draft_names_the_lines_above(make: Make) -> None:
    ctx = await make()
    tall = box(*[f"  typed row {n}" for n in range(36, 44)], top="─── ↑ 35 more ───")
    show(ctx.world, tall)
    readback, error = await prepare(ctx)
    assert not error
    assert 'Its screen shows this typed there, and more lines above it: "typed row 36 typed row 37' in readback


@pytest.mark.parametrize(
    "case",
    [
        (
            DIALOG,
            "idle",
            None,
            "fpga on dev is waiting for a permission decision, and Ctrl+C would deny it. Answer it first.",
            "permission_prompt",
        ),
        (
            DRAFT,
            "waiting",
            "permission to run Bash",
            "fpga on dev is waiting for a permission decision, and Ctrl+C would deny it. Answer it first.",
            "permission_prompt",
        ),
        (
            DRAFT,
            "busy",
            None,
            "fpga on dev is working or waiting for an answer, and Ctrl+C would interrupt it. Ask me again when it is idle.",
            "not_idle",
        ),
        (
            box("❯ fix it", below="  -- NORMAL --"),
            "idle",
            None,
            "The input of fpga on dev is in vim normal mode. Clear it at the keyboard.",
            "vim_normal",
        ),
        (
            box("❯ fix it", below="  Press Ctrl-C again to exit"),
            "idle",
            None,
            "fpga on dev asks to press Ctrl-C again to exit, so I will not press it now. Try again in a few seconds.",
            "exit_armed",
        ),
        (
            box("❯ fix it", below="  ✻ Thinking… (esc to interrupt)"),
            "idle",
            None,
            "fpga on dev is working or waiting for an answer, and Ctrl+C would interrupt it. Ask me again when it is idle.",
            "not_idle",
        ),
        (
            "~/proj\n❯ make -j32\ncc -c a.c\ncc -c b.c\n[ 40%] Building\n",
            "idle",
            None,
            "I cannot see the input box of fpga on dev on its screen, so I will not press Ctrl+C there. Clear it at the keyboard.",
            "no_input_box",
        ),
        (EMPTY, "idle", None, "Nothing is typed in the input of fpga on dev.", "nothing_to_clear"),
        (
            box('❯ Try "refactor <filepath>"'),
            "idle",
            None,
            "Nothing is typed in the input of fpga on dev.",
            "nothing_to_clear",
        ),
    ],
)
async def test_refusals_when_preparing(make: Make, case: tuple[str, str, str | None, str, str]) -> None:
    screen, status, waiting_for, said, reason = case
    ctx = await make()
    show(ctx.world, screen, status=status, waiting_for=waiting_for)
    assert await prepare(ctx) == (said, True)
    assert ctx.tools.confirmations.check("4172", OWNER.fingerprint) == "unknown"
    assert f'"refused": "{reason}"' in ctx.log.text() or reason in ctx.log.text()


async def test_a_menu_is_refused_when_preparing(make: Make) -> None:
    ctx = await make()
    show(ctx.world, capture(menu_lines(), rows=30, alternate=False))
    assert await prepare(ctx) == ("fpga on dev shows a menu, and Ctrl+C would close it. Ask me what it says.", True)


async def test_a_shell_pane_is_never_cleared(make: Make) -> None:
    ctx = await make(dev={"sendToAnyPane": True})
    show(ctx.world, DRAFT)
    assert await prepare(ctx, target="fpga:1") == (
        "fpga window 1 on dev is not running Claude Code. I only clear the input of a Claude Code session.",
        True,
    )


async def test_an_older_dispatcher_is_named_when_preparing(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await make()
    show(ctx.world, DRAFT)
    # 0.9.0, the dispatcher with attachments and health but without clear.
    verbs = ["version", "list", "capture", "send", "usage", "keys", "resize", "attachments", "attachment", "health"]
    old = VersionResponse.model_validate({"protocol": 1, "ok": True, "version": "0.9.0", "verbs": verbs})

    async def version(name: str, *, budget_seconds: float | None = None) -> TransportResult:
        return TransportResult(ok=True, data=old, elapsed_ms=1)

    monkeypatch.setattr(ctx.tools.inventory, "version", version)
    assert await prepare(ctx) == ("switchboard-remote on dev is too old to clear an input. Update it there.", True)


@pytest.mark.parametrize(
    ("code", "said"),
    [
        ("nothing_to_clear", "Not cleared: nothing is typed in that input any more."),
        (
            "not_idle",
            "Not cleared: that session is working or waiting for an answer now, and Ctrl+C would interrupt it.",
        ),
        ("menu_present", "Not cleared: that screen now shows a menu, and Ctrl+C would close it. Ask me what it says."),
        (
            "exit_armed",
            "Not cleared: that screen asks to press Ctrl-C again to exit, so I pressed nothing. Try again in a few seconds.",
        ),
        ("vim_normal", "Not cleared: that input is in vim normal mode. Clear it at the keyboard."),
        ("not_claude", "Not cleared: that pane no longer runs Claude Code."),
        (
            "no_input_box",
            "Not cleared: I cannot see the input box on that screen any more, so I pressed nothing. Clear it at the keyboard.",
        ),
        ("locked", "Not cleared: another send to dev was still running. Try again in a moment."),
        (
            "permission_prompt",
            "Not cleared: that session is now waiting for a permission decision. Approve it at the keyboard or in the Claude app.",
        ),
        ("gone", "Not cleared: the pane is gone."),
        ("changed", "Not cleared: that pane now runs a different session."),
        ("not_allowed", "Not cleared: switchboard-remote on dev does not allow sending."),
        (
            "bad_request",
            "Not cleared: switchboard-remote on dev refused the request. If it is older than 0.11.0, update it there.",
        ),
    ],
)
def test_every_dispatcher_refusal_has_a_sentence(code: str, said: str) -> None:
    item = Confirmation(
        id="4172",
        client="c",
        client_name="Claude",
        machine="dev",
        session="fpga",
        place="fpga on dev",
        pane_id="%1",
        pane_pid=1,
        session_id=SESSION_A,
        text="",
        risky=(),
        permission_answer=False,
        dialog=None,
        created_ms=0,
        expires_ms=1,
        kind=CLEAR,
        draft="0" * 24,
    )
    assert format_confirm_outcome(item, f"not_sent:{code}", code) == said
    assert format_confirm_outcome(item, "not_verified", None).startswith("I pressed Ctrl+C once in fpga on dev, but")
    assert format_confirm_outcome(item, "unknown", None) == (
        "I do not know whether the input was cleared, dev stopped answering. Ask me what fpga is doing before clearing again."
    )


async def test_a_session_change_between_read_back_and_confirm_is_not_cleared(make: Make) -> None:
    ctx = await make()
    show(ctx.world, DRAFT)
    await prepare(ctx)
    ctx.world.register("2001", os.getpid(), sessionId=SESSION_B, name="fpga", status="idle", tmux="0:@0.%1")
    ctx.later()
    text, error = await ctx.confirm("4172")
    assert error
    assert text == "Not cleared: that pane now runs a different session."
    assert pressed(ctx) == []


async def test_send_and_keys_refusals_point_to_clearing(make: Make) -> None:
    ctx = await make()
    show(ctx.world, DRAFT)
    readback = (await ctx.tools.remote_send("dev", "fpga", "run the tests", OWNER)).text
    assert readback.startswith("Ready to send")
    ctx.later()
    assert await ctx.confirm("4172") == (
        "Not sent: something is already typed in the input of fpga on dev. If it can go, say clear the input of "
        "fpga on dev first.",
        True,
    )


def test_draft_words_clip() -> None:
    assert draft_words(["one two", "three"]) == "one two three"
    many = [" ".join(f"w{n}" for n in range(20))]
    assert draft_words(many).endswith(" ...")
    assert len(draft_words(["x" * 300])) <= 101


async def test_confirming_a_clear_drops_the_other_prepared_clears_of_that_pane(make: Make) -> None:
    """Two prepared clears of one draft (here by two clients), confirmed one after the other:
    only one Ctrl+C is ever pressed, a second one would exit Claude Code."""
    ctx = await make()
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    await prepare(ctx)
    second, error = await prepare(ctx, caller=OTHER)
    assert not error
    assert "(confirmation 5283," in second
    ctx.later()
    assert await ctx.confirm("4172") == ("Cleared the input of fpga on dev.", False)
    assert await ctx.confirm("5283", caller=OTHER) == (EXPIRED, True)
    assert len(pressed(ctx)) == 1


async def test_parallel_confirms_of_one_machine_run_one_at_a_time(make: Make, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = await make()
    show(ctx.world, DRAFT, afterKeys={"text": {"%1": EMPTY}})
    running: list[int] = []
    overlap: list[bool] = []
    real = ctx.tools._send

    async def tracked(item: Confirmation, deadline: Any) -> tuple[str, str | None]:
        running.append(1)
        overlap.append(len(running) > 1)
        await asyncio.sleep(0.05)
        try:
            return await real(item, deadline)
        finally:
            running.pop()

    monkeypatch.setattr(ctx.tools, "_send", tracked)
    await prepare(ctx)
    resize = await ctx.tools.remote_resize("dev", "fpga", 120, 40, OTHER)
    assert "(confirmation 5283," in resize.text
    ctx.later()
    await asyncio.gather(
        ctx.tools.remote_send_confirm("4172", False, OWNER), ctx.tools.remote_send_confirm("5283", False, OTHER)
    )
    assert overlap == [False, False]
