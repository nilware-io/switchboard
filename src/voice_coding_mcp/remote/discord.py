"""Posts to the owner's Discord channel through a webhook (docs/SWITCHBOARD.md, "Discord
notifications").

One path for everything the owner is told: session events from the machines' hooks
(`remote/events.py`), the notification watcher's pushes and incident pushes. The webhook URL is a
secret that lets anyone post to the channel: it comes only from the hub's environment
(DISCORD_WEBHOOK_ENV, set in the root-owned unit environment file), is checked to be a Discord
webhook URL, is never written to the config, a log line or an error text, and is removed from the
process environment once read.

What is posted is plain text with `allowed_mentions: {"parse": []}`, so no `@everyone`, `@here`, role
or user mention in any text can ping anyone. Text that came from a machine (session names, the
messages of hooks and notify_owner, watcher pushes that may quote a screen) is cleaned, redacted and
shown as inline code, so it cannot format, link or mention; the hub's own words are escaped.
Items within `coalesceSeconds` go out as one message, repeats counted; a 429 waits `retry_after`,
other failures back off; a message that still fails, or anything beyond `maxPerHour`, is dropped
and counted in one incident of the hub. Never raises.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import random
import re
from collections.abc import Awaitable, Callable, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo

import httpx2

from ..clock import HOUR_MS, now_ms
from ..log import Logger
from .config import DiscordConfig
from .incidents import clean_text

DISCORD_WEBHOOK_ENV = "SWITCHBOARD_DISCORD_WEBHOOK_URL"
WEBHOOK_RE = re.compile(
    r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v[0-9]{1,2}/)?webhooks/[0-9]{15,22}/[A-Za-z0-9_-]{20,200}",
    re.ASCII,
)
CONTENT_LIMIT = 2000
"""Discord's limit for `content`; messages are cut a little below it."""
MESSAGE_CHARS = 1900
MAX_BUFFER = 100
"""Items waiting to be coalesced; beyond it the oldest are dropped (and counted)."""
MAX_ATTEMPTS = 5
MAX_RETRY_AFTER_SECONDS = 120.0
TIMEOUT_SECONDS = 10.0
SUPPRESS_NOTIFICATIONS = 1 << 12
"""Message flag: posted without a push or sound (quiet hours)."""
MAX_LINES_PER_ITEM = 4

TITLES = {
    "approval_needed": "Needs your approval",
    "question": "Waits for your answer",
    "finished": "Finished its turn",
    "failed": "Stopped on an error",
    "pr_opened": "Pull request to review",
    "owner_note": "Note from the session",
}
FAILURE_WORDS = {
    "rate_limit": "rate limit reached",
    "authentication_failed": "authentication failed",
    "oauth_org_not_allowed": "organization not allowed",
    "account_on_hold": "account on hold",
    "billing_error": "billing error",
    "invalid_request": "invalid request",
    "model_not_found": "model not found",
    "server_error": "API server error or overloaded",
    "max_output_tokens": "output token limit reached",
    "cloud_credential_error": "cloud credentials failed",
    "unknown": "unknown API error",
}


def webhook_from_env(env: MutableMapping[str, str]) -> tuple[str | None, str | None]:
    """(webhook URL, problem). Removes the variable from `env` (normally `os.environ`), so no child
    process of the hub inherits it. The problem text never contains the value."""
    value = env.pop(DISCORD_WEBHOOK_ENV, None)
    if value is None or not value.strip():
        return None, f"{DISCORD_WEBHOOK_ENV} is not set"
    value = value.strip()
    if not WEBHOOK_RE.fullmatch(value):
        return (
            None,
            f"{DISCORD_WEBHOOK_ENV} is not a Discord webhook URL (https://discord.com/api/webhooks/<id>/<token>)",
        )
    return value, None


_MARKDOWN_RE = re.compile(r"([\\*_~`|>\[\]()#<@:!-])")


def escape(text: str) -> str:
    """The hub's own words with every Discord markdown character escaped."""
    return _MARKDOWN_RE.sub(r"\\\1", text)


def literal(value: str | None, limit: int) -> str:
    """Untrusted text as inline code: cleaned, redacted, clipped, backticks replaced, so it can
    neither format, link nor mention. Empty for no text."""
    text = clean_text(value, limit)
    if not text:
        return ""
    text = text.replace("`", "ˋ").replace("@", "@\u200b")
    return f"`{text}`"


@dataclass(frozen=True)
class EventView:
    """What Discord shows of one session event (checked by `remote/events.py`)."""

    kind: str
    session_id: str | None = None
    session_name: str | None = None
    project: str | None = None
    tmux_session: str | None = None
    message: str | None = None
    url: str | None = None
    level: str | None = None


@dataclass(frozen=True)
class Item:
    kind: str
    """A `DiscordKind`: an event kind, `watch` or `incident`."""
    machine: str | None
    text: str = ""
    """`watch` and `incident`: the push text (a watcher push may quote a screen: shown as code)."""
    event: EventView | None = None

    def key(self) -> tuple[str, str | None, str]:
        if self.event is not None:
            return (self.kind, self.machine, self.event.session_id or self.event.project or "")
        return (self.kind, self.machine, self.text)


def render_item(item: Item, count: int = 1) -> str:
    """One item as up to MAX_LINES_PER_ITEM lines."""
    repeat = f" (x{count})" if count > 1 else ""
    if item.event is None:
        if item.kind == "incident":
            return escape(item.text) + repeat
        return "**Watcher**: " + literal(item.text, 400) + repeat
    event = item.event
    title = TITLES.get(event.kind, "Event")
    if event.kind == "owner_note" and event.level in ("warning", "urgent"):
        title = f"{event.level.capitalize()} note from the session"
    parts = [f"**{escape(title)}**{repeat}"]
    if item.machine:
        parts.append(f"on {escape(item.machine)}")
    for label, value in (("project", event.project), ("session", event.session_name), ("tmux", event.tmux_session)):
        shown = literal(value, 64)
        if shown:
            parts.append(f"{label} {shown}")
    lines = [" · ".join(parts)]
    if event.kind == "failed":
        lines.append("> " + escape(FAILURE_WORDS.get(event.message or "", FAILURE_WORDS["unknown"])))
    elif event.kind == "owner_note":
        said = literal(event.message, 500)
        if said:
            lines.append("> the session says: " + said)
    elif event.kind != "pr_opened":
        said = literal(event.message, 200)
        if said:
            lines.append("> " + said)
    if event.url:
        # Validated URLs only (`remote/events.py`); angle brackets keep Discord from embedding it.
        lines.append(f"<{event.url}>")
    return "\n".join(lines[:MAX_LINES_PER_ITEM])


def render(items: Sequence[Item]) -> list[str]:
    """Messages of at most MESSAGE_CHARS characters: repeats of one key folded with a count, in the
    order the first of each came."""
    counts: dict[tuple[str, str | None, str], int] = {}
    first: dict[tuple[str, str | None, str], Item] = {}
    for item in items:
        key = item.key()
        counts[key] = counts.get(key, 0) + 1
        first.setdefault(key, item)
    messages: list[str] = []
    current = ""
    for key, item in first.items():
        block = render_item(item, counts[key])
        if len(block) > MESSAGE_CHARS:
            block = block[: MESSAGE_CHARS - 1] + "…"
        candidate = f"{current}\n{block}" if current else block
        if len(candidate) > MESSAGE_CHARS:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(config: DiscordConfig, ms: int, tz: tzinfo | None) -> bool:
    quiet = config.quiet_hours
    if quiet is None:
        return False
    moment = datetime.fromtimestamp(ms / 1000, tz) if tz is not None else datetime.fromtimestamp(ms / 1000)
    now = moment.hour * 60 + moment.minute
    start, end = _minutes(quiet.start), _minutes(quiet.end)
    if start == end:
        return False
    return start <= now < end if start < end else now >= start or now < end


Problem = Callable[[str], None]
"""Called with `discord_failed`, `discord_rejected` or `discord_dropped` (the incident log)."""


class DiscordNotifier:
    """Buffers items, coalesces them, and posts them one message at a time from one worker task."""

    def __init__(
        self,
        url: str,
        config: DiscordConfig,
        logger: Logger,
        *,
        client: httpx2.AsyncClient | None = None,
        now: Callable[[], int] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        tz: tzinfo | None = None,
    ) -> None:
        assert WEBHOOK_RE.fullmatch(url)
        self._url = url
        self.config = config
        self._kinds = frozenset(config.kinds)
        self._logger = logger.child({"component": "discord"})
        self._client = client
        self._owns_client = client is None
        self._now = now or now_ms
        self._sleep = sleep or asyncio.sleep
        self.tz = tz
        self.on_problem: Problem | None = None
        self._buffer: list[Item] = []
        self._wake = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self._closing = False
        self._sent: list[int] = []
        self.dropped = 0
        self.posted = 0

    # --- what the rest of the hub calls ---------------------------------------------------------

    def wants(self, kind: str) -> bool:
        return kind in self._kinds

    def add(self, item: Item) -> None:
        """Queues one item (never blocks, never raises)."""
        if self._closing or not self.wants(item.kind):
            return
        if len(self._buffer) >= MAX_BUFFER:
            self._buffer.pop(0)
            self._drop("discord_dropped")
        self._buffer.append(item)
        self._wake.set()
        if self._worker is None or self._worker.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._worker = loop.create_task(self._run())

    async def send_watch(self, text: str) -> None:
        """A `TextSender` for the notification watcher."""
        self.add(Item(kind="watch", machine=None, text=text))

    async def send_incident(self, text: str) -> None:
        """A `TextSender` for incident pushes."""
        self.add(Item(kind="incident", machine=None, text=text))

    async def aclose(self, seconds: float = 5.0) -> None:
        """Sends what is buffered (skipping the coalescing wait) for up to `seconds`, then stops."""
        self._closing = True
        self._wake.set()
        worker, self._worker = self._worker, None
        if worker is not None:
            try:
                await asyncio.wait_for(asyncio.shield(worker), seconds)
            except (TimeoutError, asyncio.CancelledError, Exception):
                worker.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await worker
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()

    # --- the worker -----------------------------------------------------------------------------

    def _drop(self, code: str, count: int = 1) -> None:
        self.dropped += count
        if self.on_problem is not None:
            try:
                self.on_problem(code)
            except Exception as err:
                self._logger.error("incident observation failed", {"error": type(err).__name__})

    async def _run(self) -> None:
        while self._buffer:
            if not self._closing and self.config.coalesce_seconds:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wait_closing(), self.config.coalesce_seconds)
            items, self._buffer = self._buffer, []
            for content in render(items):
                await self._deliver(content)

    async def _wait_closing(self) -> None:
        while not self._closing:
            self._wake.clear()
            await self._wake.wait()

    def _within_rate(self) -> bool:
        now = self._now()
        self._sent = [t for t in self._sent if now - t < HOUR_MS]
        if len(self._sent) >= self.config.max_per_hour:
            return False
        self._sent.append(now)
        return True

    def _http(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = httpx2.AsyncClient()
        return self._client

    async def _deliver(self, content: str) -> None:
        if not self._within_rate():
            self._logger.warn("discord message dropped: maxPerHour reached")
            self._drop("discord_dropped")
            return
        payload: dict[str, object] = {"content": content[:CONTENT_LIMIT], "allowed_mentions": {"parse": []}}
        if in_quiet_hours(self.config, self._now(), self.tz):
            payload["flags"] = SUPPRESS_NOTIFICATIONS
        for attempt in range(1, MAX_ATTEMPTS + 1):
            wait = await self._post(payload)
            if wait is None:
                self.posted += 1
                return
            if wait < 0:
                self._drop("discord_rejected")
                return
            if attempt < MAX_ATTEMPTS:
                await self._sleep(wait)
        self._logger.warn("discord message dropped after retries", {"attempts": MAX_ATTEMPTS})
        self._drop("discord_failed")

    async def _post(self, payload: dict[str, object]) -> float | None:
        """None when posted, a negative number for a refusal that a retry cannot fix, else the
        seconds to wait before the next attempt. Logs the status or the error type, never the URL."""
        try:
            response = await self._http().post(self._url, json=payload, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        except Exception as err:
            self._logger.warn("discord post failed", {"error": type(err).__name__})
            return 2.0 + random.uniform(0, 1)
        status = response.status_code
        if 200 <= status < 300:
            self._logger.info("discord message posted", {"status": status})
            return None
        if status == 429:
            wait = _retry_after(response)
            self._logger.warn("discord rate limited", {"retryAfter": wait})
            return wait
        if 500 <= status < 600:
            self._logger.warn("discord post failed", {"status": status})
            return 4.0 + random.uniform(0, 2)
        # 400 (a bad message), 401, 403, 404 (the webhook was deleted): retrying cannot help.
        self._logger.warn("discord post refused", {"status": status})
        return -1.0


def _retry_after(response: httpx2.Response) -> float:
    """`retry_after` (seconds) of a 429 body, else the Retry-After header, clamped."""
    seconds: object = None
    with contextlib.suppress(Exception):
        body = json.loads(response.content[:4096].decode("utf-8"))
        if isinstance(body, dict):
            seconds = body.get("retry_after")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        header = response.headers.get("retry-after")
        with contextlib.suppress(TypeError, ValueError):
            seconds = float(header) if header is not None else None
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or math.isnan(seconds):
        return 5.0
    return min(max(float(seconds), 0.0), MAX_RETRY_AFTER_SECONDS) + 0.05


async def fan_out(senders: Sequence[Callable[[str], Awaitable[None]]], text: str) -> None:
    for send in senders:
        with contextlib.suppress(Exception):
            await send(text)


def combine(*senders: Callable[[str], Awaitable[None]] | None) -> Callable[[str], Awaitable[None]] | None:
    """One `TextSender` that sends through each given one (None when there is none)."""
    chosen = [s for s in senders if s is not None]
    if not chosen:
        return None
    if len(chosen) == 1:
        return chosen[0]

    async def send(text: str) -> None:
        await fan_out(chosen, text)

    return send
