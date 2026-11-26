"""Optional push notification when a background task finishes, and plain-text pushes of the
Switchboard notification watcher (`send_text`).

POSTs `text/plain; charset=utf-8` (plus `notify.headers`) to `notify.url` with one long-lived
`httpx2.AsyncClient` (created on the first send, closed by `aclose()` at shutdown) or an injected
one, redirects are errors (`follow_redirects=False`, a 3xx counts as
failure), 5 s timeout. Logs only the URL origin, the status or the error class
(`timeout` for timeouts), and the task id (or `kind: "text"` for `send_text`, never the text).
Never raises.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx2

from .clock import SECOND_MS
from .config import NotifyConfig
from .log import LogFields, Logger
from .text import clip
from .urls import parse_url

RESULT_LIMIT = 2000
"""Result characters kept before the ellipsis."""


@dataclass(frozen=True)
class NotifyTask:
    """The task fields a notification needs."""

    id: int
    project: str
    status: str
    result: str | None
    label: str | None = None
    """Set for remote jobs: the job's title ("status of fpga on dev"), named instead of the project.
    Their result is never attached (it holds screen text)."""


Notifier = Callable[[NotifyTask], Awaitable[None]]
TextSender = Callable[[str], Awaitable[None]]
"""Sends one plain-text push (`HttpNotifier.send_text`). Never raises."""


def notification_text(task: NotifyTask, include_result: bool) -> str:
    """ "Task 7 in voice-coding-mcp finished." (or "timed out", "was cancelled", "failed"), plus the
    result after a blank line when `include_result` (cut at 2000 characters with "…"). A remote job
    (`label` set) reads "Task 7 (status of fpga on dev) finished." and never carries its result,
    whatever `include_result` says: that result is built from screen text and the push goes to a
    third party."""
    if task.status == "done":
        verb = "finished"
    elif task.status == "timeout":
        verb = "timed out"
    elif task.status == "cancelled":
        verb = "was cancelled"
    else:
        verb = "failed"
    if task.label is not None:
        return f"Task {task.id} ({task.label}) {verb}."
    text = f"Task {task.id} in {task.project} {verb}."
    if include_result and task.result:
        text += f"\n\n{clip(task.result, RESULT_LIMIT + 1)}"
    return text


class HttpNotifier:
    """A `Notifier` that POSTs to one URL. Call `aclose()` once at shutdown, after the task manager
    stopped; sends after that are dropped."""

    def __init__(
        self, config: NotifyConfig, logger: Logger, *, client: httpx2.AsyncClient | None, timeout_ms: int
    ) -> None:
        assert config.url
        self._config = config
        self._url = config.url
        parsed = parse_url(config.url)
        self._origin = parsed.origin if parsed is not None else "?"
        self._logger = logger
        self._timeout_s = timeout_ms / SECOND_MS
        self._client = client
        self._owns_client = client is None
        self._closed = False

    def _http(self) -> httpx2.AsyncClient:
        # Synchronous check and create: concurrent first sends share one client.
        if self._client is None:
            self._client = httpx2.AsyncClient()
        return self._client

    async def _post(self, text: str, fields: LogFields) -> None:
        headers = {"content-type": "text/plain; charset=utf-8", **self._config.headers}
        body = text.encode("utf-8")
        async with (
            asyncio.timeout(self._timeout_s),
            self._http().stream(
                "POST", self._url, headers=headers, content=body, follow_redirects=False, timeout=self._timeout_s
            ) as response,
        ):
            # The body is not needed: closing the stream discards it.
            status = response.status_code
        if not 200 <= status < 300:
            self._logger.warn("notify failed", {"origin": self._origin, "status": status, **fields})
        else:
            self._logger.info("notify sent", {"origin": self._origin, **fields})

    async def _send(self, text: str, fields: LogFields) -> None:
        if self._closed:
            self._logger.warn("notify failed", {"origin": self._origin, "error": "closed", **fields})
            return
        try:
            await self._post(text, fields)
        except (TimeoutError, httpx2.TimeoutException):
            self._logger.warn("notify failed", {"origin": self._origin, "error": "timeout", **fields})
        except Exception as err:
            self._logger.warn("notify failed", {"origin": self._origin, "error": type(err).__name__, **fields})

    async def __call__(self, task: NotifyTask) -> None:
        await self._send(notification_text(task, self._config.include_result), {"taskId": task.id})

    async def send_text(self, text: str) -> None:
        """POSTs `text` as is, through the same client, URL and headers. Never raises."""
        await self._send(text, {"kind": "text"})

    async def aclose(self) -> None:
        """Closes the client this notifier created (an injected one belongs to the caller). Idempotent."""
        self._closed = True
        client, self._client = self._client, None
        if client is not None and self._owns_client:
            await client.aclose()


def create_notifier(
    config: NotifyConfig,
    logger: Logger,
    *,
    client: httpx2.AsyncClient | None = None,
    timeout_ms: int = 5 * SECOND_MS,
) -> HttpNotifier | None:
    """A notifier that POSTs to `notify.url`, or None when notifications are off."""
    if not config.url:
        return None
    return HttpNotifier(config, logger, client=client, timeout_ms=timeout_ms)
