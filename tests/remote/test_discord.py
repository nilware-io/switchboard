"""Discord notifications (`remote/discord.py`): the webhook secret, what a post looks like (mentions
inert, untrusted text as code, the hub's words escaped, length cut), coalescing, quiet hours, 429
with retry_after, server errors, refusals, the hourly cap and the incidents they open. The webhook is
a MockTransport: no network."""

from __future__ import annotations

import asyncio
import json
import os
import zoneinfo
from typing import Any

import httpx2
import pytest

from tests.helpers import FakeClock, LogCapture
from voice_coding_mcp.cli import _discord
from voice_coding_mcp.config import ConfigError, parse_config
from voice_coding_mcp.remote.config import DiscordConfig, QuietHoursConfig
from voice_coding_mcp.remote.discord import (
    DISCORD_WEBHOOK_ENV,
    MAX_ATTEMPTS,
    MESSAGE_CHARS,
    SUPPRESS_NOTIFICATIONS,
    DiscordNotifier,
    EventView,
    Item,
    combine,
    escape,
    in_quiet_hours,
    literal,
    render,
    render_item,
    webhook_from_env,
)

URL = "https://discord.com/api/webhooks/123456789012345678/" + "A" * 68
T0 = 1_795_000_000_000


def test_the_webhook_comes_from_the_environment_and_is_removed() -> None:
    env = {DISCORD_WEBHOOK_ENV: f" {URL}\n", "PATH": "/bin"}
    assert webhook_from_env(env) == (URL, None)
    assert DISCORD_WEBHOOK_ENV not in env


@pytest.mark.parametrize(
    "value",
    [
        "http://discord.com/api/webhooks/123456789012345678/abc" + "d" * 40,
        "https://discord.com.evil.example/api/webhooks/123456789012345678/" + "a" * 60,
        "https://evil.example/?https://discord.com/api/webhooks/123456789012345678/" + "a" * 60,
        "https://discord.com/api/webhooks/123456789012345678/" + "a" * 60 + "?wait=true",
        "https://user:pw@discord.com/api/webhooks/123456789012345678/" + "a" * 60,
        "https://discord.com/api/webhooks/abc/" + "a" * 60,
    ],
)
def test_anything_but_a_discord_webhook_is_refused_without_echoing_it(value: str) -> None:
    url, problem = webhook_from_env({DISCORD_WEBHOOK_ENV: value})
    assert url is None
    assert problem is not None
    assert value not in problem
    assert "a" * 20 not in problem


@pytest.mark.parametrize(
    "value",
    [
        "https://discordapp.com/api/webhooks/123456789012345678/" + "b-_" * 22,
        "https://canary.discord.com/api/v10/webhooks/123456789012345678/" + "c" * 68,
    ],
)
def test_other_discord_hosts(value: str) -> None:
    assert webhook_from_env({DISCORD_WEBHOOK_ENV: value})[0] == value


def test_a_missing_webhook_is_a_problem() -> None:
    assert webhook_from_env({}) == (None, f"{DISCORD_WEBHOOK_ENV} is not set")


# --- what a post looks like ---------------------------------------------------------------------


def event(**keys: Any) -> Item:
    view = EventView(
        **{
            "kind": "approval_needed",
            "session_id": "0f8fad5b-d9cb-469f-a165-70867728950e",
            "session_name": "fix tests",
            "project": "fpgadc",
            "tmux_session": "fpga",
            "message": "Claude needs your permission to use Bash",
            **keys,
        }
    )
    return Item(kind=view.kind, machine="dev", event=view)


def test_an_event_line() -> None:
    assert render_item(event()) == (
        "**Needs your approval** · on dev · project `fpgadc` · session `fix tests` · tmux `fpga`\n"
        "> `Claude needs your permission to use Bash`"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "@everyone deploy now",
        "<@&123456789> <@123> @here",
        "`` ` **bold** [click](https://evil.example) ||spoiler||",
        "line\nbreak\x1b[31m red \u202e",
        "token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_session_text_cannot_mention_format_or_leak(hostile: str) -> None:
    text = render_item(event(message=hostile, session_name=hostile, project=hostile))
    # Inside code nothing renders; every @ is also broken by a zero-width space.
    assert "@everyone" not in text
    assert "@here" not in text
    assert text.count("@") == text.count("@\u200b")
    assert "](" not in text.replace("`", "") or text.count("`") % 2 == 0
    assert "ghp_" not in text
    assert "\x1b" not in text
    assert "\u202e" not in text
    for line in text.splitlines()[1:]:
        assert line.startswith("> `")
        assert line.endswith("`")
        assert line.count("`") == 2


def test_literal_and_escape() -> None:
    assert literal("a`b", 10) == "`aˋb`"
    assert literal("  ", 10) == ""
    assert literal("x" * 50, 10).endswith("…`")
    assert escape("low_disk *now* [x](y) @a <b>") == r"low\_disk \*now\* \[x\]\(y\) \@a \<b\>"


def test_kinds_render_their_details() -> None:
    assert "rate limit reached" in render_item(event(kind="failed", message="rate_limit"))
    pr = render_item(event(kind="pr_opened", message="gh pr create", url="https://github.com/o/r/pull/7"))
    assert pr.endswith("<https://github.com/o/r/pull/7>")
    assert "gh pr create" not in pr
    note = render_item(event(kind="owner_note", message="Ready for review", level="urgent"))
    assert note.startswith("**Urgent note from the session**")
    assert "> the session says: `Ready for review`" in note
    assert render_item(Item(kind="incident", machine=None, text="Incident on wsl: low_disk.")) == (
        r"Incident on wsl\: low\_disk."
    )
    watch = render_item(Item(kind="watch", machine=None, text='fpga on dev needs a decision: "rm -rf @here"'))
    assert watch.startswith("**Watcher**: `")
    assert "@\u200bhere" in watch


def test_repeats_are_counted_and_messages_cut() -> None:
    items = [event(kind="finished", message=None)] * 3 + [event(session_id=None, project="other")]
    [message] = render(items)
    assert "**Finished its turn** (x3)" in message
    assert message.count("\n") == 2
    many = [event(project=f"p{i}", session_id=None, message="m" * 190) for i in range(60)]
    messages = render(many)
    assert len(messages) > 1
    assert all(len(m) <= MESSAGE_CHARS for m in messages)
    assert sum(m.count("**Needs your approval**") for m in messages) == 60


def test_quiet_hours_wrap_midnight() -> None:
    config = DiscordConfig(quiet_hours=QuietHoursConfig(start="23:00", end="07:00"))
    tz = zoneinfo.ZoneInfo("UTC")
    day = 1_795_046_400_000  # a midnight UTC
    assert in_quiet_hours(config, day + 30 * 60_000, tz)
    assert in_quiet_hours(config, day + 23 * 3_600_000 + 60_000, tz)
    assert not in_quiet_hours(config, day + 12 * 3_600_000, tz)
    assert not in_quiet_hours(DiscordConfig(), day, tz)


# --- posting ------------------------------------------------------------------------------------


class Webhook:
    """The Discord side: answers from a script, records every request."""

    def __init__(self, answers: list[httpx2.Response] | None = None) -> None:
        self.answers = answers or []
        self.requests: list[dict[str, Any]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append({"url": str(request.url), "body": json.loads(request.content)})
        return self.answers.pop(0) if self.answers else httpx2.Response(204)


def notifier(
    webhook: Webhook, *, clock: FakeClock | None = None, **config: Any
) -> tuple[DiscordNotifier, list[float], list[str], LogCapture]:
    sleeps: list[float] = []
    problems: list[str] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    log = LogCapture()
    sender = DiscordNotifier(
        URL,
        DiscordConfig(**{"coalesce_seconds": 0, **config}),
        log.logger,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(webhook)),
        now=clock or FakeClock(T0),
        sleep=sleep,
    )
    sender.on_problem = problems.append
    return sender, sleeps, problems, log


async def drain(sender: DiscordNotifier) -> None:
    for _ in range(200):
        if not sender._buffer and (sender._worker is None or sender._worker.done()):
            return
        await asyncio.sleep(0.01)
    raise AssertionError("not drained")


async def test_a_post_has_inert_mentions_and_never_logs_the_url() -> None:
    webhook = Webhook()
    sender, _, problems, log = notifier(webhook)
    sender.add(event(message="@everyone look"))
    await drain(sender)
    [request] = webhook.requests
    assert request["url"] == URL
    assert request["body"]["allowed_mentions"] == {"parse": []}
    assert "flags" not in request["body"]
    assert "@everyone" not in request["body"]["content"]
    assert problems == []
    assert URL not in json.dumps(log.lines)
    assert "A" * 30 not in json.dumps(log.lines)
    await sender.aclose()


async def test_429_waits_retry_after_then_posts() -> None:
    webhook = Webhook([httpx2.Response(429, json={"retry_after": 1.5, "global": False}), httpx2.Response(204)])
    sender, sleeps, problems, _ = notifier(webhook)
    sender.add(event())
    await drain(sender)
    assert len(webhook.requests) == 2
    assert sleeps == [pytest.approx(1.55)]
    assert problems == []
    assert sender.posted == 1
    await sender.aclose()


async def test_429_header_and_clamp() -> None:
    webhook = Webhook(
        [httpx2.Response(429, headers={"retry-after": "3"}), httpx2.Response(429, json={"retry_after": 9999})]
    )
    sender, sleeps, _, _ = notifier(webhook)
    sender.add(event())
    await drain(sender)
    assert sleeps[:2] == [pytest.approx(3.05), pytest.approx(120.05)]
    await sender.aclose()


async def test_server_errors_retry_then_drop_with_an_incident() -> None:
    webhook = Webhook([httpx2.Response(502) for _ in range(MAX_ATTEMPTS)])
    sender, sleeps, problems, _ = notifier(webhook)
    sender.add(event())
    await drain(sender)
    assert len(webhook.requests) == MAX_ATTEMPTS
    assert len(sleeps) == MAX_ATTEMPTS - 1
    assert problems == ["discord_failed"]
    await sender.aclose()


async def test_a_deleted_webhook_is_not_retried() -> None:
    webhook = Webhook([httpx2.Response(404, json={"message": "Unknown Webhook"})])
    sender, sleeps, problems, _ = notifier(webhook)
    sender.add(event())
    await drain(sender)
    assert len(webhook.requests) == 1
    assert sleeps == []
    assert problems == ["discord_rejected"]
    await sender.aclose()


async def test_network_errors_retry() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx2.ConnectError("down", request=request)
        return httpx2.Response(204)

    sender = DiscordNotifier(
        URL,
        DiscordConfig(coalesce_seconds=0),
        LogCapture().logger,
        client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
        sleep=lambda _s: asyncio.sleep(0),
    )
    sender.add(event())
    await drain(sender)
    assert len(calls) == 2
    assert sender.posted == 1
    await sender.aclose()


async def test_the_hourly_cap_drops_and_counts() -> None:
    webhook = Webhook()
    clock = FakeClock(T0)
    sender, _, problems, _ = notifier(webhook, clock=clock, max_per_hour=2)
    for index in range(3):
        sender.add(event(project=f"p{index}", session_id=f"s{index}"))
        await drain(sender)
    assert len(webhook.requests) == 2
    assert problems == ["discord_dropped"]
    clock.t += 3_600_001
    sender.add(event())
    await drain(sender)
    assert len(webhook.requests) == 3
    await sender.aclose()


async def test_items_within_the_window_go_out_as_one_message() -> None:
    webhook = Webhook()
    sender, _, _, _ = notifier(webhook, coalesce_seconds=1)
    for _ in range(5):
        sender.add(event(kind="finished", message=None))
    sender.add(event())
    await asyncio.sleep(1.3)
    await drain(sender)
    [request] = webhook.requests
    assert "(x5)" in request["body"]["content"]
    assert "Needs your approval" in request["body"]["content"]
    await sender.aclose()


async def test_close_flushes_without_waiting_the_window() -> None:
    webhook = Webhook()
    sender, _, _, _ = notifier(webhook, coalesce_seconds=300)
    sender.add(event())
    await asyncio.sleep(0)
    await sender.aclose(seconds=5)
    assert len(webhook.requests) == 1


async def test_quiet_hours_post_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    webhook = Webhook()
    sender, _, _, _ = notifier(webhook, quiet_hours=QuietHoursConfig(start="00:00", end="23:59"))
    monkeypatch.setattr("voice_coding_mcp.remote.discord.in_quiet_hours", lambda *_a: True)
    sender.add(event())
    await drain(sender)
    assert webhook.requests[0]["body"]["flags"] == SUPPRESS_NOTIFICATIONS
    await sender.aclose()


async def test_disabled_kinds_and_the_buffer_bound() -> None:
    webhook = Webhook()
    sender, _, problems, _ = notifier(webhook, kinds=["approval_needed"])
    assert not sender.wants("finished")
    await sender.send_watch("ignored")
    await sender.send_incident("ignored")
    sender.add(event(kind="finished"))
    assert sender._buffer == []
    sender._closing = False
    sender._worker = asyncio.get_running_loop().create_task(asyncio.sleep(10))
    for index in range(105):
        sender.add(event(project=f"p{index}", session_id=None))
    assert len(sender._buffer) == 100
    assert problems.count("discord_dropped") == 5
    sender._worker.cancel()
    sender._buffer = []
    await sender.aclose()


async def test_combine_sends_through_each_and_survives_a_failure() -> None:
    got: list[str] = []

    async def broken(_text: str) -> None:
        raise RuntimeError("down")

    async def working(text: str) -> None:
        got.append(text)

    assert combine(None, None) is None
    assert combine(working) is working
    both = combine(broken, working)
    assert both is not None
    await both("hello")
    assert got == ["hello"]


# --- configuration and startup ------------------------------------------------------------------


def hub_config(tmp_path: Any, **sections: Any) -> Any:
    projects = tmp_path / "projects"
    projects.mkdir(exist_ok=True)
    return parse_config(
        {
            "publicUrl": "https://vcm.example.com",
            "stateDir": str(tmp_path / "state"),
            "projectRoots": [str(projects)],
            **sections,
        },
        home_dir="/nonexistent-home-dir",
    )


def test_config_sections(tmp_path: Any) -> None:
    config = hub_config(tmp_path, discord={"enabled": True, "quietHours": {"start": "23:00", "end": "07:00"}})
    assert config.discord.kinds[0] == "approval_needed"
    assert len(config.discord.kinds) == 8
    assert config.events.enabled is False
    assert config.events.ssh_poll_seconds == 0
    # The watcher may run with Discord alone.
    assert hub_config(tmp_path, discord={"enabled": True}, watch={"intervalSeconds": 60}).watch.interval_seconds == 60
    for bad in (
        {"discord": {"webhookUrl": URL}},
        {"discord": {"kinds": ["everything"]}},
        {"discord": {"quietHours": {"start": "25:00", "end": "07:00"}}},
        {"events": {"pollSeconds": 1}},
        {"events": {"sshPollSeconds": 5}},
    ):
        with pytest.raises(ConfigError):
            hub_config(tmp_path, **bad)


def test_startup_reads_and_removes_the_secret(tmp_path: Any) -> None:
    log = LogCapture()
    on = hub_config(tmp_path, discord={"enabled": True})
    env = {DISCORD_WEBHOOK_ENV: URL}
    sender, problem = _discord(on, log.logger, env)
    assert sender is not None
    assert problem is None
    sender, problem = _discord(on, log.logger, {})
    assert sender is None
    assert problem == f"{DISCORD_WEBHOOK_ENV} is not set"
    off = hub_config(tmp_path)
    assert _discord(off, log.logger, {DISCORD_WEBHOOK_ENV: URL}) == (None, None)
    assert "discord.enabled is false" in log.text()
    assert URL not in log.text()


def test_startup_pops_the_variable_from_the_process_environment(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, URL)
    sender, _ = _discord(hub_config(tmp_path, discord={"enabled": True}), LogCapture().logger, None)
    assert sender is not None
    assert DISCORD_WEBHOOK_ENV not in os.environ


def test_startup_from_the_cli_copy_of_the_environment_removes_both(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DISCORD_WEBHOOK_ENV, URL)
    copy = dict(os.environ)
    sender, _ = _discord(hub_config(tmp_path, discord={"enabled": True}), LogCapture().logger, copy)
    assert sender is not None
    assert DISCORD_WEBHOOK_ENV not in os.environ
    assert DISCORD_WEBHOOK_ENV not in copy
