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
from tests.remote.menu_fixtures import (
    DRAFT_EDGES,
    FOOTER,
    OPTIONS,
    PROSE_DIALOG,
    PROSE_YES_NO,
    QUESTION,
    SHELL_MENU,
    TALL_QUESTION,
    capture,
    described_menu_lines,
    menu_lines,
    numbered_draft,
    plain_menu_lines,
    tall_menu_lines,
)
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
from voice_coding_mcp.remote.send import KEYS, Confirmation
from voice_coding_mcp.remote.speech import format_confirm_outcome
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
"""The menu as read back for keys that only move, choose and confirm (no footer hints then)."""
HINTS = 'Its key hints also say: "left/right change size".'


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
    said: list[str] = []
    ctx.tools.echo.owner_said = said.append  # type: ignore[method-assign,assignment]
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
    assert said == [], "pressed keys are never the owner's words"
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
    assert readback.startswith("I dropped the earlier key presses for fpga on dev. Ready to press down and enter")
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
        'That should choose 2, "No, and tell Claude". '
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


DIALOG_3 = (
    "╭──────────────────────────────────────────────╮\n"
    "│ Bash command                                 │\n"
    "│   git push origin main                       │\n"
    "│ Do you want to proceed?                      │\n"
    "│ ❯ 1. Yes                                     │\n"
    "│   2. Yes, and don't ask again for git push   │\n"
    "│   3. No, and tell Claude what to do          │\n"
    "╰──────────────────────────────────────────────╯\n"
    "  Esc to cancel\n"
)


@pytest.mark.parametrize(
    ("keys", "says"),
    [
        (["Down", "Enter"], 'That should choose 2, "Yes, and don\'t ask again for git push".'),
        (["Enter"], 'That should choose 1, "Yes".'),
        (["3"], 'That should choose 3, "No, and tell Claude what to do".'),
        (["Down", "Down", "Enter"], 'That should choose 3, "No, and tell Claude what to do".'),
    ],
)
async def test_permission_answers_by_keys_say_which_option_they_choose(make: Make, keys: list[str], says: str) -> None:
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, DIALOG_3, waiting_for="permission prompt")
    readback, error = await prepare(ctx, keys)
    assert not error
    assert 'It asks for permission: "Bash command git push origin main". ' + says in readback
    assert "Careful," in readback


NOT_VISIBLE = (
    "fpga on dev is waiting for a permission decision, but the permission question is not visible on its "
    "screen, so I will not press keys there. Check it at the keyboard."
)


@pytest.mark.parametrize(
    "names", [["Up", "Enter"], ["Enter"], ["BTab", "BTab", "BTab"], ["Down", "2"], ["Tab"], ["1"], ["y"], ["Escape"]]
)
async def test_a_registry_only_permission_wait_takes_no_keys(make: Make, names: list[str]) -> None:
    """The registry says it waits for permission, the screen shows only the empty input box (a stale
    entry, or a dialog shows_dialog cannot see): nothing binds the keys to a dialog, so Up Enter would
    submit an earlier prompt, Enter a draft, Shift+Tab would cycle the permission mode toward bypass.
    No keys at all, whatever the opt-ins."""
    ctx = await make(dev={"allowPermissionAnswers": True, "sendToAnyPane": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers", "--send-to-any-pane"])
    show(ctx, EMPTY_BOX, waiting_for="permission prompt")
    assert await prepare(ctx, names) == (NOT_VISIBLE, True)
    assert len(ctx.tools.confirmations) == 0
    assert keys_calls(ctx) == []
    assert pressed(ctx) == []


async def test_a_registry_only_permission_wait_with_a_draft_takes_no_keys(make: Make) -> None:
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, EMPTY_BOX.replace(">                ", "> rm -rf build   "), waiting_for="permission prompt")
    assert await prepare(ctx, ["Enter"]) == (NOT_VISIBLE, True)
    assert keys_calls(ctx) == []


async def test_a_dialog_that_left_the_screen_before_confirm_is_not_pressed(make: Make) -> None:
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, DIALOG, waiting_for="permission prompt")
    _, error = await prepare(ctx, ["Down", "Enter"])
    assert not error
    # The dialog closed at the keyboard; the registry still says permission.
    show(ctx, EMPTY_BOX, waiting_for="permission prompt")
    ctx.later()
    text, error = await ctx.confirm("4172", risky_ok=True)
    assert error
    assert text.startswith("Not pressed")
    assert pressed(ctx) == []


def test_the_dispatcher_refusal_of_an_unbound_answer_is_spoken() -> None:
    item = Confirmation(
        id="4172",
        client="c",
        client_name=None,
        machine="dev",
        session="fpga",
        place="fpga on dev",
        pane_id="%1",
        pane_pid=CLAUDE_PID,
        session_id=SESSION_A,
        text="1",
        risky=("permission",),
        permission_answer=True,
        dialog=None,
        created_ms=0,
        expires_ms=1,
        kind=KEYS,
        keys=("1",),
    )
    assert format_confirm_outcome(item, "not_sent:dialog_not_visible", "dialog_not_visible") == (
        "Not pressed: the permission question is not visible on that screen. Check it at the keyboard."
    )


@pytest.mark.parametrize("key", ["Tab", "shift tab", "BTab", "backtab"])
async def test_tab_and_shift_tab_without_a_menu_are_refused(make: Make, key: str) -> None:
    """Shift+Tab at an idle Claude Code input cycles its permission mode: no voice route around the
    permission-answer opt-in."""
    ctx = await make(dev={"allowPermissionAnswers": True, "sendToAnyPane": True})
    show(ctx, EMPTY_BOX, status="idle")
    text, error = await prepare(ctx, ["Down", key])
    assert (text, error) == (
        "There is no menu on the screen of fpga on dev; tab and shift tab there would change Claude Code's mode, "
        "so I will not press them.",
        True,
    )
    assert keys_calls(ctx) == []
    # Into a menu the owner hears they still go.
    show(ctx, MENU)
    readback, error = await prepare(ctx, [key])
    assert not error
    assert readback.startswith(f"Ready to press {'tab' if key == 'Tab' else 'shift tab'} in fpga on dev. A menu")


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
    await prepare(ctx, ["Left", "Enter"])
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


async def test_the_version_probe_runs_beside_the_list_and_is_cached(make: Make) -> None:
    """The `version` probe does not add a round trip before the capture (it runs alongside the list),
    and its answer is reused for the next preparations on the machine."""
    ctx = await make()
    show(ctx, MENU)
    for _ in range(3):
        _, error = await prepare(ctx, ["Enter"])
        assert not error
    assert await ctx.tools.remote_resize("dev", "fpga", 120, 40, OWNER)
    verbs = [c["argv"][-1].split()[1] for c in ctx.world.ssh_calls()]
    assert verbs.count("version") == 1
    assert verbs[:2] in (["list", "version"], ["version", "list"])


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
        "Not pressed: switchboard-remote on dev refused the request. If it is older than 0.5.0, update it there.",
        True,
    )


# --- resize ------------------------------------------------------------------------------------


async def test_resize_reads_back_confirms_and_audits(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    readback = await ctx.tools.remote_resize("dev", "fpga", 120, 40, OWNER)
    assert readback.text == (
        "Ready to resize the window of fpga on dev to 120 columns by 40 rows. It is now 80 by 24. "
        "The size stays fixed afterwards, and it also changes the other panes in that window and every "
        "terminal attached to it. To undo it, run tmux set-option -w -u window-size in that window at the "
        "keyboard, which goes back to automatic sizing. Say yes to resize it. "
        "(confirmation 4172, do not read this number aloud)"
    )
    ctx.later()
    assert await ctx.confirm("4172") == (
        "Resized the window of fpga on dev to 120 by 40. It stays that size until someone runs "
        "tmux set-option -w -u window-size in that window.",
        False,
    )
    assert [c["argv"][-1] for c in ctx.world.ssh_calls() if " resize " in c["argv"][-1]] == [
        f"switchboard-remote resize %1 {CLAUDE_PID} {SESSION_A} 120 40"
    ]
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


async def test_resize_a_pane_without_claude_code_needs_send_to_any_pane(make: Make) -> None:
    ctx = await make()
    show(ctx, MENU)
    answer = await ctx.tools.remote_resize("dev", "fpga:1", 120, 40, OWNER)
    assert (answer.text, answer.is_error) == (
        "fpga window 1 on dev is not running Claude Code, so I will not resize it.",
        True,
    )
    assert not any(" resize " in c["argv"][-1] for c in ctx.world.ssh_calls())


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


HEURISTIC = f"dev, fpga: Claude Code is waiting for your answer. {READBACK_MENU} {HINTS}"


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
            '"It asks how big the table should be; 1 is selected." '
            'The menu\'s options: 1 "Small (10 entries)", selected; 2 "Medium (50 entries)"; '
            '3 "Large (100 entries)"; 4 "Type something else".'
        )
        # The question and the key hints are not spoken a second time after the summary.
        assert answer.text.count(QUESTION) == 0
        assert "key hints" not in answer.text
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


@pytest.mark.parametrize(
    "names", [["1", "BTab", "BTab", "BTab"], ["Enter", "Tab"], ["Escape", "Up", "Enter"], ["y", "Enter"]]
)
async def test_nothing_follows_a_key_that_ends_the_selection(make: Make, names: list[str]) -> None:
    """With only allowSend and an ordinary menu, one confirmation must not pick an option and then
    cycle Claude Code's permission mode (or type into the screen that follows)."""
    ctx = await make()
    show(ctx, MENU)
    text, error = await prepare(ctx, names)
    assert (text, error) == (
        "I press a number, enter, escape, y or n only as the last key: anything after it would land on the next "
        "screen, which I have not read. Ask for the rest once I have read that screen.",
        True,
    )
    assert len(ctx.tools.confirmations) == 0
    assert keys_calls(ctx) == []


@pytest.mark.parametrize("names", [["Up", "Enter"], ["down", "down", "enter"]])
async def test_no_history_recall_and_submit_without_a_menu(make: Make, names: list[str]) -> None:
    ctx = await make(dev={"sendToAnyPane": True})
    show(ctx, EMPTY_BOX, status="idle")
    text, error = await prepare(ctx, names)
    assert error
    assert text.startswith(
        "There is no menu on the screen of fpga on dev; up or down and then enter would send an earlier prompt"
    )
    assert len(ctx.tools.confirmations) == 0
    text, error = await prepare(ctx, names, target="fpga:1")
    assert error, "a shell pane reruns its last command"
    assert "earlier prompt or command" in text


async def test_a_menu_without_numbers_is_read_back_and_pressed(make: Make) -> None:
    ctx = await make()
    screen = capture(plain_menu_lines(), rows=30, alternate=True)
    show(ctx, screen)
    readback, error = await prepare(ctx, ["Down", "Enter"])
    assert not error
    assert readback == (
        'Ready to press down and enter in fpga on dev. A menu on the screen asks: "How big should the high score '
        'table be?". Options: "Small", selected; "Medium"; "Large". That should choose "Medium". '
        "Say yes to press them. (confirmation 4172, do not read this number aloud)"
    )
    ctx.later()
    assert await ctx.confirm("4172") == ("Pressed down and enter in fpga on dev.", False)
    assert keys_calls(ctx) == [
        f"switchboard-remote keys %1 {CLAUDE_PID} {SESSION_A} menu {d.menu_hash(screen)} Down Enter"
    ]
    # A digit does not choose an option without a number.
    readback, _ = await prepare(ctx, ["2"])
    assert "That should choose" not in readback


async def test_an_unreadable_menu_is_refused_up_front(make: Make) -> None:
    ctx = await make()
    lines = plain_menu_lines()
    lines[6] = lines[6].replace("  Medium", "❯ Medium")
    show(ctx, capture(lines, rows=30, alternate=True))
    text, error = await prepare(ctx, ["Escape"])
    assert (text, error) == (
        "fpga on dev shows a menu I cannot read, so I will not press keys in it. "
        "Ask me what its screen says, or answer it at the keyboard.",
        True,
    )
    assert len(ctx.tools.confirmations) == 0


async def test_status_of_a_menu_without_numbers_names_question_and_options(tmp_path: Path) -> None:
    for rows, alternate in ((40, False), (40, True), (12, True)):
        text = capture(plain_menu_lines(selected=3), rows=rows, alternate=alternate)
        world = menu_world(tmp_path / f"{rows}{alternate}", text, height=rows, alternate=alternate)
        c = await build_status(tmp_path / f"{rows}{alternate}", world, summary=False)
        try:
            answer = await c.tools.remote_status("dev", "fpga", 120)
            assert answer.text == (
                'dev, fpga: Claude Code is waiting for your answer. A menu on the screen asks: "How big should the '
                'high score table be?". Options: "Small"; "Medium"; "Large", selected. '
                'Its key hints also say: "left/right change size".'
            ), (rows, alternate)
        finally:
            await c.manager.shutdown("test finished")


async def test_status_of_an_unreadable_menu_speaks_the_block_above_the_footer(tmp_path: Path) -> None:
    lines = plain_menu_lines()
    lines[6] = lines[6].replace("  Medium", "❯ Medium")
    c = await build_status(tmp_path, menu_world(tmp_path, capture(lines, rows=30, alternate=True)), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == (
            "dev, fpga: Claude Code is waiting for your answer. The screen says: "
            '"⏺ I can set the high score table up in a few ways. / How big should the high score table be? / '
            '❯ Small / ❯ Medium / Large"'
        )
    finally:
        await c.manager.shutdown("test finished")


async def test_status_of_a_menu_with_long_descriptions(tmp_path: Path) -> None:
    c = await build_status(
        tmp_path, menu_world(tmp_path, capture(described_menu_lines(6), rows=30, alternate=False)), summary=False
    )
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert (
            'Options: 1 "A" (details: "detail 0 of option A detail 1 of option A detail 2 of optio…"), selected; 2 "B".'
        ) in answer.text
        assert QUESTION in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_no_cut_note_when_the_whole_menu_fits_a_short_alternate_pane(tmp_path: Path) -> None:
    text = capture(menu_lines(), rows=12, alternate=True)
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=12, alternate=True), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text == HEURISTIC
    finally:
        await c.manager.shutdown("test finished")


async def test_status_heuristic_names_option_descriptions_in_short(tmp_path: Path) -> None:
    lines = menu_lines()
    lines.insert(6, "│      Fits on one screen" + " " * 32 + " │")
    c = await build_status(tmp_path, menu_world(tmp_path, capture(lines, rows=30, alternate=False)), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert 'Options: 1 "Small (10 entries)" (details: "Fits on one screen"), selected; 2 "Medium' in answer.text
        assert '4 "Type something else".' in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_status_drops_descriptions_before_options(tmp_path: Path) -> None:
    """Descriptions too long for every option to fit: the options without them."""
    options = tuple(f"Option number {i} with a long name" for i in range(1, 9))
    lines = menu_lines(options=options)
    for index in range(len(options), 0, -1):
        lines.insert(5 + index, "│      " + "a fairly long description of this option".ljust(54) + " │")
    c = await build_status(tmp_path, menu_world(tmp_path, capture(lines, rows=40, alternate=False)), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert "details" not in answer.text
        assert '8 "Option number 8 with a long name"' in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_status_of_a_menu_with_twelve_options(tmp_path: Path) -> None:
    options = tuple(f"Choice {i}" for i in range(1, 13))
    text = capture(menu_lines(selected=11, options=options), rows=40, alternate=False)
    c = await build_status(tmp_path, menu_world(tmp_path, text), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert '9 "Choice 9"; 10 "Choice 10"; 11 "Choice 11", selected; 12 "Choice 12".' in answer.text
    finally:
        await c.manager.shutdown("test finished")


# --- prose that looks like a dialog, numbered drafts, menus cut by a short pane ---------------


@pytest.mark.parametrize("screen", [PROSE_DIALOG, PROSE_YES_NO], ids=["do-you-want-to", "yes-no-list"])
async def test_prose_that_looks_like_a_dialog_gets_the_unbound_rules(make: Make, screen: str) -> None:
    """Assistant prose "Do you want to ...?" (or a prose Yes/No list) above an idle input box: the
    dispatcher's broad `shows_dialog` calls it a dialog, but no answer options are at the bottom, so
    the keys land at the input: no history submit, no mode keys, and the read-back says so."""
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, screen, status="idle")
    text, error = await prepare(ctx, ["Up", "Enter"])
    assert error
    assert text.startswith("There is no menu on the screen of fpga on dev; up or down and then enter")
    text, error = await prepare(ctx, ["BTab", "BTab"])
    assert (text, error) == (
        "There is no menu on the screen of fpga on dev; tab and shift tab there would change Claude Code's mode, "
        "so I will not press them.",
        True,
    )
    assert len(ctx.tools.confirmations) == 0
    readback, error = await prepare(ctx, ["Enter"])
    assert not error
    assert "It asks for permission" not in readback
    assert "but I see no answer options at its bottom, so the keys go to its input." in readback
    # A draft typed meanwhile: the dispatcher refuses the Enter at confirm.
    show(ctx, screen.replace("│ >                │", "│ > rm -rf build   │"), status="idle")
    ctx.later()
    text, error = await ctx.confirm("4172", risky_ok=True)
    assert error
    assert text.startswith("Not pressed")
    assert pressed(ctx) == []


@pytest.mark.parametrize("lines", [2, 3])
@pytest.mark.parametrize("boxed", [True, False], ids=["rounded-box", "rules"])
async def test_a_numbered_draft_is_no_menu_to_the_hub(make: Make, lines: int, boxed: bool) -> None:
    ctx = await make()
    screen = numbered_draft(lines, boxed=boxed)
    show(ctx, screen, status="idle")
    text, error = await prepare(ctx, ["BTab", "BTab"])
    assert error
    assert text.startswith("There is no menu on the screen of fpga on dev; tab and shift tab")
    text, error = await prepare(ctx, ["Up", "Enter"])
    assert error
    assert "up or down and then enter" in text
    readback, error = await prepare(ctx, ["Enter"])
    assert not error
    assert "I see no menu on its screen." in readback
    assert "fix the parser" not in readback
    ctx.later()
    assert await ctx.confirm("4172") == ("Not pressed: something is already typed in that session's input.", True)
    assert pressed(ctx) == []


TALL_CUT_NOTE = (
    " The pane is 24 rows high and shows a full-screen program, and the menu on it is cut off: "
    "I cannot see its question or first options."
)


async def test_a_menu_taller_than_a_24_row_alternate_pane_is_said_to_be_cut(tmp_path: Path) -> None:
    text = capture(tall_menu_lines(), rows=24, alternate=True)
    c = await build_status(tmp_path, menu_world(tmp_path, text, height=24, alternate=True), summary=False)
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert answer.text.endswith(TALL_CUT_NOTE)
        assert "Theme 12" in answer.text
    finally:
        await c.manager.shutdown("test finished")
    # The same menu read whole at 40 rows: no note.
    whole = capture(tall_menu_lines(), rows=40, alternate=True)
    c = await build_status(
        tmp_path / "40", menu_world(tmp_path / "40", whole, height=40, alternate=True), summary=False
    )
    try:
        answer = await c.tools.remote_status("dev", "fpga", None)
        assert TALL_QUESTION in answer.text
        assert "cut off" not in answer.text
    finally:
        await c.manager.shutdown("test finished")


async def test_a_cut_menu_offers_a_resize_and_its_keys_refusal_names_it(make: Make) -> None:
    ctx = await make()
    for height, offer in ((24, True), (120, False)):
        text = capture(tall_menu_lines(), rows=24, alternate=True)
        ctx.world.set_panes(
            [pane("fpga", "%1", CLAUDE_PID, command="claude", height=height, alternate=True)], {"%1": text}
        )
        ctx.world.register(
            "2001",
            os.getpid(),
            sessionId=SESSION_A,
            name="fpga",
            status="waiting",
            waitingFor="user answer",
            tmux="0:@0.%1",
        )
        status = await ctx.tools.remote_status("dev", "fpga", None)
        assert ("I can make it bigger if you want." in status.text) is offer, height
        assert "the menu on it is cut off" in status.text
        refused, error = await prepare(ctx, ["Enter"])
        assert error
        taller = " If it is cut off, I can make the pane taller." if offer else ""
        assert refused == (
            f"fpga on dev shows a menu I cannot read, so I will not press keys in it.{taller} "
            "Ask me what its screen says, or answer it at the keyboard."
        )
    assert len(ctx.tools.confirmations) == 0


@pytest.mark.parametrize("case", list(DRAFT_EDGES))
async def test_numbered_drafts_with_an_empty_last_line_or_a_quote_line_are_no_menu_to_the_hub(
    make: Make, case: str
) -> None:
    ctx = await make()
    show(ctx, DRAFT_EDGES[case], status="idle")
    text, error = await prepare(ctx, ["BTab", "BTab", "BTab"])
    assert error
    assert text.startswith("There is no menu on the screen of fpga on dev; tab and shift tab")
    text, error = await prepare(ctx, ["Up", "Enter"])
    assert error
    assert "up or down and then enter" in text
    readback, error = await prepare(ctx, ["Enter"])
    assert not error
    assert "I see no menu on its screen." in readback
    assert "fix the parser" not in readback
    assert "That should choose" not in readback
    ctx.later()
    assert await ctx.confirm("4172") == ("Not pressed: something is already typed in that session's input.", True)
    assert pressed(ctx) == []


async def test_a_menu_without_a_picker_footer_over_a_draft_is_refused_by_the_hub(make: Make) -> None:
    lines = menu_lines()
    assert lines[-1] == FOOTER
    show(ctx := await make(), capture(lines[:-1], rows=30, alternate=True))
    assert await prepare(ctx, ["Enter"]) == (
        "Something is already typed in the input of fpga on dev, so I will not press keys there. "
        "Send or clear it at the keyboard first.",
        True,
    )
    assert len(ctx.tools.confirmations) == 0
    assert keys_calls(ctx) == []


EDIT_DIALOG = (
    "╭──────────────────────────────────────────────────────────╮\n"
    "│ Edit file                                                │\n"
    "│   src/parser.py                                          │\n"
    "│ Do you want to make this edit to parser.py?              │\n"
    "│ ❯ 1. Yes                                                 │\n"
    "│   2. Yes, allow all edits during this session (shift+tab) │\n"
    "│   3. No, and tell Claude what to do differently (esc)    │\n"
    "╰──────────────────────────────────────────────────────────╯\n"
)


@pytest.mark.parametrize("names", [["BTab", "Enter"], ["BTab", "1"], ["Tab", "Enter"], ["Down", "BTab", "y"]])
async def test_tab_and_shift_tab_end_a_permission_answer_on_the_hub(make: Make, names: list[str]) -> None:
    """Shift+Tab chooses "allow all edits during this session" in Claude Code's edit dialog and
    closes it: the Enter or 1 after it would answer a queued dialog nobody heard."""
    ctx = await make(dev={"allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--allow-permission-answers"])
    show(ctx, EDIT_DIALOG, waiting_for="permission prompt")
    text, error = await prepare(ctx, names)
    assert error
    assert text.startswith("In a permission question, tab and shift tab can choose an answer themselves")
    assert len(ctx.tools.confirmations) == 0
    assert keys_calls(ctx) == []
    # As the last key it is read back.
    _, error = await prepare(ctx, ["Down", "BTab"])
    assert not error


def show_shell(ctx: Ctx, text: str) -> None:
    ctx.world.set_panes(
        [
            pane("fpga", "%1", CLAUDE_PID, command="claude"),
            pane("fpga", "%2", 9_200_002, window_index=1, window_name="shell", command="bash"),
        ],
        {"%1": EMPTY_BOX, "%2": text},
    )


async def test_shell_output_that_looks_like_a_menu_does_not_bind_on_the_hub(make: Make) -> None:
    """Shell output ending in a numbered list above the prompt: no bound menu (the prompt would be
    read as option 2's description), so Up Enter is refused and no option is read back as chosen."""
    ctx = await make(dev={"sendToAnyPane": True})
    ctx.world.configure(forced=[*FORCED, "--send-to-any-pane"])
    show_shell(ctx, SHELL_MENU)
    text, error = await prepare(ctx, ["Up", "Enter"], target="fpga:1")
    assert error
    assert "up or down and then enter" in text
    text, error = await prepare(ctx, ["BTab"], target="fpga:1")
    assert error
    assert "tab and shift tab" in text
    readback, error = await prepare(ctx, ["1"], target="fpga:1")
    assert not error
    assert "That should choose" not in readback
    assert "Staging" not in readback
    ctx.later()
    assert await ctx.confirm("4172", risky_ok=True) == ("Pressed 1 in fpga window 1 on dev.", False)
    assert keys_calls(ctx) == ["switchboard-remote keys %2 9200002 none menu none 1"]


async def test_shell_output_that_looks_like_a_dialog_does_not_bind_on_the_hub(make: Make) -> None:
    ctx = await make(dev={"sendToAnyPane": True, "allowPermissionAnswers": True})
    ctx.world.configure(forced=[*FORCED, "--send-to-any-pane", "--allow-permission-answers"])
    show_shell(ctx, "$ ./deploy.sh\nShould I continue?\n  1. Yes\n❯ 2. No\nuser@box:~/proj$ ")
    text, error = await prepare(ctx, ["Up", "Enter"], target="fpga:1")
    assert error
    assert "up or down and then enter" in text
    readback, error = await prepare(ctx, ["Enter"], target="fpga:1")
    assert not error
    assert "That should choose" not in readback
