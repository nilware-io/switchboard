"""`remote_send_keys`, `remote_resize` and their `remote_send_confirm` end to end (real ssh transport on
the fake ssh, the real dispatcher on the fake tmux, an injected clock), plus `remote_status` and
`pending_input` on synthetic menu screens: the whole menu reaches the summariser and the heuristic,
which names the question and every option instead of the footer."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import read_records
from tests.remote.helpers import SESSION_A, RemoteWorld, pane
from tests.remote.menu_fixtures import FOOTER, OPTIONS, QUESTION, capture, menu_lines
from tests.remote.test_send_tools import (
    CLAUDE_PID,
    DIALOG,
    EMPTY_BOX,
    FORCED,
    OTHER,
    OWNER,
    Ctx,
    Make,
    build,
)
from tests.remote.test_tools import build as build_status
from voice_coding_mcp.remote import dispatcher as d
from voice_coding_mcp.remote.config import MachineConfig
from voice_coding_mcp.remote.protocol import VersionResponse
from voice_coding_mcp.remote.tools import EARLY, EXPIRED, NEEDS_RISKY_OK, RemoteTools, RemoteToolsOptions
from voice_coding_mcp.remote.transport import Transport, TransportResult

MENU = capture(menu_lines(), rows=30, alternate=False)


@pytest.fixture
async def make(tmp_path: Path) -> AsyncIterator[Make]:
    """`await make(**options)`: a send Ctx (tests/remote/test_send_tools.py `build`), shut down afterwards."""
    built: list[Ctx] = []

    async def factory(**options: Any) -> Ctx:
        built.append(await build(tmp_path, **options))
        return built[-1]

    yield factory
    for item in built:
        await item.manager.shutdown("test finished")


READBACK_MENU = (
    'A menu on the screen asks: "How big should the high score table be?". Options: 1 "Small (10 entries)", '
    'selected; 2 "Medium (50 entries)"; 3 "Large (100 entries)"; 4 "Type something else".'
)


def show(ctx: Ctx, text: str, *, status: str = "waiting", waiting_for: str = "user answer") -> None:
    ctx.world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", 9_200_002, window_index=1, window_name="shell", command="bash"),
        ],
        {"%1": text, "%2": "$ "},
    )
    ctx.world.register(
        "2001", os.getpid(), sessionId=SESSION_A, name="fpga", status=status, waitingFor=waiting_for, tmux="0:@0.%1"
    )


async def prepare(ctx: Ctx, keys: Sequence[str], target: str = "fpga", caller: Any = OWNER) -> tuple[str, bool]:
    answer = await ctx.tools.remote_send_keys("dev", target, list(keys), caller)
    return answer.text, answer.is_error


def pressed(ctx: Ctx) -> list[list[str]]:
    return [c["argv"] for c in ctx.world.tmux_calls() if c["argv"][:1] == ["send-keys"]]


def keys_calls(ctx: Ctx) -> list[str]:
    return [c["argv"][-1] for c in ctx.world.ssh_calls() if " keys " in c["argv"][-1]]


async def test_keys_read_back_the_menu_and_press_only_after_confirm(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    readback, error = await prepare(ctx, ["down", "Enter"])
    assert not error
    assert readback == (
        f"Ready to press down and enter in fpga on dev. {READBACK_MENU} "
        'That should choose 2, "Medium (50 entries)". Say yes to press them. '
        "(confirmation 4172, do not read this number aloud)"
    )
    assert pressed(ctx) == [], "remote_send_keys presses nothing"
    assert keys_calls(ctx) == []
    ctx.later()
    assert await ctx.confirm("4172") == ("Pressed down and enter in fpga on dev.", False)
    assert pressed(ctx) == [["send-keys", "-t", "%1", "Down", "Enter"]]
    assert keys_calls(ctx) == [
        f"switchboard-remote keys %1 {CLAUDE_PID} {SESSION_A} menu {d.menu_hash(MENU)} Down Enter"
    ]
    # Single use.
    assert await ctx.confirm("4172") == (EXPIRED, True)
    assert len(pressed(ctx)) == 1
    (line,) = ctx.audit()
    assert line["event"] == "remote_send_keys"
    assert line["keys"] == ["Down", "Enter"]
    assert line["menuBound"] is True
    assert (line["outcome"], line["machine"], line["paneId"], line["permissionAnswer"]) == ("sent", "dev", "%1", False)
    assert line["client"] == OWNER.fingerprint
    logged = ctx.log.text()
    assert "remote_send_keys" in logged
    assert QUESTION not in logged, "no screen text in the log"
    assert "Medium" not in logged


async def test_confirm_needs_a_new_turn_expires_and_is_bound_to_the_client(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    await prepare(ctx, ["2"])
    assert await ctx.confirm("4172") == (EARLY, True), "same turn: minConfirmSeconds"
    ctx.later()
    assert await ctx.confirm("4172", caller=OTHER) == (EXPIRED, True), "another OAuth client"
    assert pressed(ctx) == []
    assert await ctx.confirm("4172") == ("Pressed 2 in fpga on dev.", False)
    await prepare(ctx, ["Enter"])
    ctx.later(121)
    assert await ctx.confirm("5283") == (EXPIRED, True)
    assert len(pressed(ctx)) == 1


async def test_a_new_preparation_replaces_the_old_one(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    await prepare(ctx, ["Enter"])
    readback, _ = await prepare(ctx, ["Down", "Enter"])
    assert readback.startswith("I dropped the earlier message for fpga on dev. Ready to press down and enter")
    ctx.later()
    assert await ctx.confirm("4172") == (EXPIRED, True)


@pytest.mark.parametrize(
    ("keys", "says"),
    [
        (
            ["C-c"],
            "I can only press up, down, left, right, enter, escape, tab, shift tab, the numbers 1 to 9, y and n.",
        ),
        (["Down", "send-keys -t %2 C-c"], "I can only press"),
        (["Down"] * 11, "That is too many keys at once; I press at most 10."),
        ([], "Which keys should I press?"),
        (["M-x"], "I can only press"),
        (["Enter;"], "I can only press"),
    ],
)
async def test_unknown_keys_are_refused_before_anything_runs(make: Make, keys: list[str], says: str) -> None:
    ctx = await make()
    show(ctx, MENU)
    text, error = await prepare(ctx, keys)
    assert error
    assert text.startswith(says)
    assert ctx.world.ssh_calls() == []


async def test_keys_need_allow_send_on_the_hub(make: Make) -> None:
    ctx = await make()
    answer = await ctx.tools.remote_send_keys("buildbox", "api", ["Enter"], OWNER)
    assert (answer.text, answer.is_error) == ("Sending is not enabled for buildbox.", True)
    assert ctx.world.ssh_calls() == []


async def test_keys_need_allow_send_on_the_dispatcher(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    ctx.world.configure(forced=["--sessions", "fpga,notes,work-*"])
    await prepare(ctx, ["Enter"])
    ctx.later()
    assert await ctx.confirm("4172") == ("Not pressed: switchboard-remote on dev does not allow sending.", True)
    assert ctx.audit()[0]["outcome"] == "not_sent:not_allowed"


async def test_a_shell_pane_is_refused_without_send_to_any_pane(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    assert await prepare(ctx, ["Enter"], target="fpga:1") == (
        "fpga window 1 on dev is not running Claude Code, so I will not press keys in it.",
        True,
    )


async def test_a_permission_dialog_needs_the_permission_opt_in(make: Make) -> None:
    ctx = await make()
    show(ctx, DIALOG, waiting_for="permission prompt")
    text, error = await prepare(ctx, ["Down", "Enter"])
    assert error
    assert text.startswith(
        "fpga on dev is waiting for a permission decision, and answering those by voice is turned off."
    )
    assert ctx.world.ssh_calls()[-1]["argv"][-1].split()[1] != "keys"


async def test_permission_answers_by_keys_are_risky_and_bound_to_the_dialog(make: Make) -> None:
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, DIALOG, waiting_for="permission prompt")
    readback, error = await prepare(ctx, ["Down", "Enter"])
    assert not error
    assert readback.startswith(
        'Ready to press down and enter in fpga on dev. It asks for permission: "Bash command git push origin main". '
        "Careful, this answers a question about a push, and this answers a permission question."
    )
    ctx.later()
    assert await ctx.confirm("4172") == (NEEDS_RISKY_OK, True)
    assert await ctx.confirm("4172", risky_ok=True) == ("Pressed down and enter in fpga on dev.", False)
    assert keys_calls(ctx) == [
        f"switchboard-remote keys %1 {CLAUDE_PID} {SESSION_A} answer {d.dialog_hash(DIALOG)} Down Enter"
    ]
    (line,) = ctx.audit()
    assert (line["permissionAnswer"], line["riskyOk"], line["risky"]) == (True, True, ["question:push", "permission"])


async def test_a_menu_that_changed_before_confirm_is_not_pressed(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    await prepare(ctx, ["Enter"])
    show(ctx, capture(menu_lines(selected=3), rows=30, alternate=False))
    ctx.later()
    assert await ctx.confirm("4172") == (
        "Not pressed: the menu on that screen changed since I read it to you. Ask me to read it again.",
        True,
    )
    assert pressed(ctx) == []
    assert ctx.audit()[0]["outcome"] == "not_sent:menu_changed"


async def test_a_risky_option_needs_risky_ok(make: Make) -> None:
    ctx = await make()
    show(
        ctx,
        capture(menu_lines(question="What next?", options=("Run the tests", "Push to main")), rows=30, alternate=False),
    )
    readback, _ = await prepare(ctx, ["Down", "Enter"])
    assert (
        'That should choose 2, "Push to main". Careful, this asks for a push. To press them anyway, say yes, send the push.'
        in readback
    )
    ctx.later()
    assert await ctx.confirm("4172") == (NEEDS_RISKY_OK, True)
    assert pressed(ctx) == []
    # When the option is not known, every option counts as the question's.
    await prepare(ctx, ["Escape", "Enter"])
    assert ctx.tools.confirmations.check("5283", OWNER.fingerprint) in ("early",)
    ctx.later()
    item = ctx.tools.confirmations.check("5283", OWNER.fingerprint)
    assert not isinstance(item, str)
    assert item.risky == ("question:push",)


async def test_keys_without_a_menu_say_so(make: Make) -> None:
    ctx = await make()
    show(ctx, EMPTY_BOX, status="busy")
    readback, _ = await prepare(ctx, ["Escape"])
    assert readback.startswith(
        "Ready to press escape in fpga on dev. I see no menu on its screen. Say yes to press them."
    )
    ctx.later()
    assert await ctx.confirm("4172") == ("Pressed escape in fpga on dev.", False)
    assert keys_calls(ctx)[0].endswith(" menu none Escape")


# --- an older dispatcher ---------------------------------------------------------------------------


class OldDispatcher:
    """A dispatcher 0.4.0 in front of the real transport: no `keys` or `resize` verb."""

    def __init__(self, inner: Transport, *, version_ok: bool = True) -> None:
        self.inner = inner
        self.machine: MachineConfig = inner.machine
        self.version_ok = version_ok

    async def call(
        self, verb: str, args: Sequence[str] = (), *, timeout_seconds: float, stdin: bytes | None = None
    ) -> TransportResult:
        if verb == "version":
            if not self.version_ok:
                return TransportResult(ok=False, error="timeout")
            data = VersionResponse.model_validate(
                {"version": "0.4.0", "verbs": ["version", "list", "capture", "send", "usage"], "allowSend": True}
            )
            return TransportResult(ok=True, data=data)
        if verb in ("keys", "resize"):
            return TransportResult(ok=False, error="refused_by_policy", detail="bad_request")
        return await self.inner.call(verb, args, timeout_seconds=timeout_seconds, stdin=stdin)


def with_old_dispatcher(ctx: Ctx, *, version_ok: bool) -> RemoteTools:
    old = {name: OldDispatcher(t, version_ok=version_ok) for name, t in ctx.tools.transports.items()}
    return RemoteTools(
        RemoteToolsOptions(
            config=ctx.config,
            manager=ctx.manager,
            logger=ctx.log.logger,
            transports=old,
            claude_home=str(Path(ctx.config.state_dir) / "hub-claude"),
            now=ctx.clock,
            new_confirmation_id=lambda: "4172",
        )
    )


async def test_an_older_dispatcher_is_named_when_preparing(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    tools = with_old_dispatcher(ctx, version_ok=True)
    answer = await tools.remote_send_keys("dev", "fpga", ["Enter"], OWNER)
    assert (answer.text, answer.is_error) == (
        "switchboard-remote on dev is too old to press keys. Update it there.",
        True,
    )
    answer = await tools.remote_resize("dev", "fpga", 120, 40, OWNER)
    assert answer.text == "switchboard-remote on dev is too old to resize windows. Update it there."


async def test_an_older_dispatcher_is_named_at_confirm_when_version_did_not_answer(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    tools = with_old_dispatcher(ctx, version_ok=False)
    answer = await tools.remote_send_keys("dev", "fpga", ["Enter"], OWNER)
    assert not answer.is_error
    ctx.later()
    done = await tools.remote_send_confirm("4172", False, OWNER)
    assert (done.text, done.is_error) == (
        "Not pressed: switchboard-remote on dev is too old to press keys. Update it there.",
        True,
    )


# --- resize ------------------------------------------------------------------------------------


async def test_resize_reads_back_confirms_and_audits(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    readback = await ctx.tools.remote_resize("dev", "fpga", 120, 40, OWNER)
    assert readback.text == (
        "Ready to resize the window of fpga on dev to 120 columns by 40 rows. It is now 80 by 24. "
        "Say yes to resize it. (confirmation 4172, do not read this number aloud)"
    )
    ctx.later()
    assert await ctx.confirm("4172") == ("Resized the window of fpga on dev to 120 by 40.", False)
    resized = [c["argv"] for c in ctx.world.tmux_calls() if c["argv"][:1] == ["resize-window"]]
    assert resized == [["resize-window", "-t", "%1", "-x", "120", "-y", "40"]]
    (line,) = ctx.audit()
    assert (line["event"], line["size"], line["outcome"]) == ("remote_resize", {"columns": 120, "rows": 40}, "sent")


async def test_resize_bounds_and_opt_in(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    for columns, rows in ((79, 40), (301, 40), (120, 23), (120, 121)):
        answer = await ctx.tools.remote_resize("dev", "fpga", columns, rows, OWNER)
        assert (answer.text, answer.is_error) == ("I can set 80 to 300 columns and 24 to 120 rows.", True)


async def test_resize_needs_allow_send(make: Make) -> None:
    ctx = await make()
    answer = await ctx.tools.remote_resize("buildbox", "api", 120, 40, OWNER)
    assert answer.text == "Sending is not enabled for buildbox, and resizing goes with it."
    assert ctx.world.ssh_calls() == []


# --- remote_status and pending_input on a menu ------------------------------------------------


def menu_world(tmp_path: Path, text: str, *, height: int = 40, alternate: bool = False) -> RemoteWorld:
    world = RemoteWorld.create(tmp_path, forced=["--sessions", "fpga,notes,work-*", "--allow-send"])
    world.set_panes([pane("fpga", "%1", 9_999_801, command="claude", height=height, alternate=alternate)], {"%1": text})
    world.register(
        "2001",
        os.getpid(),
        sessionId=SESSION_A,
        name="fpga",
        status="waiting",
        waitingFor="user answer",
        tmux="0:@0.%1",
    )
    return world


HEURISTIC = f"dev, fpga: Claude Code is waiting for your answer. {READBACK_MENU}"


@pytest.mark.parametrize(
    ("rows", "alternate"), [(40, False), (40, True), (8, False)], ids=["normal", "alternate", "short-normal"]
)
async def test_status_heuristic_names_the_question_and_options_not_the_footer(
    tmp_path: Path, rows: int, alternate: bool
) -> None:
    text = capture(menu_lines(), rows=rows, alternate=alternate)
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=rows, alternate=alternate), summary=False)
    try:
        for lines in (None, 120):
            answer = await c.tools.remote_status("dev", "fpga", lines)
            assert answer.text == HEURISTIC
            assert "choose" not in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_a_short_alternate_screen_suggests_a_resize(tmp_path: Path) -> None:
    text = capture(menu_lines(), rows=8, alternate=True)
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=8, alternate=True), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text.startswith(HEURISTIC)
        assert answer.text.endswith(
            " The pane is only 8 rows high and shows a full-screen program, so part of a menu may be cut off."
        )
    finally:
        await c.manager.shutdown("test finished")


async def test_status_heuristic_with_no_free_slot_names_the_menu(tmp_path: Path) -> None:
    c = await build_status(tmp_path, menu_world(tmp_path, MENU), summary=True)
    try:
        lease = c.manager.claim_slot("other")
        assert lease is not None
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == HEURISTIC
        assert read_records(c.record) == []
        lease.release()
    finally:
        await c.manager.shutdown("test finished")


async def test_the_summariser_gets_the_question_options_and_footer(tmp_path: Path) -> None:
    text = capture(
        ["RESULT It asks how big the table should be; 1 is selected.", *menu_lines()], rows=30, alternate=True
    )
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=30, alternate=True), summary=True)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == (
            "dev, fpga: Claude Code is waiting for your answer. A summary of the screen: "
            '"It asks how big the table should be; 1 is selected."'
        )
        (run,) = read_records(c.record)
        stdin = run["stdin"]
        for needle in (QUESTION, *OPTIONS, FOOTER.strip(), "❯ 1."):
            assert needle in stdin, needle
        prompt = run["argv"][run["argv"].index("--system-prompt") + 1]
        assert "name the question, every choice with its number, and which one is selected" in prompt
    finally:
        await c.manager.shutdown("test finished")


async def test_a_failed_summary_falls_back_to_the_menu(tmp_path: Path) -> None:
    text = capture(["FAIL", *menu_lines()], rows=30, alternate=True)
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=30, alternate=True), summary=True)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == HEURISTIC
    finally:
        await c.manager.shutdown("test finished")


async def test_pending_input_quotes_the_question_of_a_tall_menu(tmp_path: Path) -> None:
    tall = menu_lines(options=tuple(f"Option number {n}" for n in range(1, 10)))
    text = capture(tall, rows=40, alternate=False)
    c = await build_status(tmp_path, menu_world(tmp_path, text), summary=False)
    try:
        answer = await c.tools.pending_input("dev")
        assert answer.text == f'1 session needs you.\nOn dev, fpga asks: "{QUESTION}".'
    finally:
        await c.manager.shutdown("test finished")
