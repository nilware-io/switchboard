"""Push notifications when a background task finishes."""

from __future__ import annotations

import asyncio
import dataclasses
import re
from typing import Any

import httpx2
import pytest

from voice_coding_mcp.config import NotifyConfig
from voice_coding_mcp.log import create_logger, silent_logger
from voice_coding_mcp.notify import NotifyTask, create_notifier, notification_text

TASK = NotifyTask(id=7, project="voice-coding-mcp", status="done", result="All tests pass.")


def config(**overrides: Any) -> NotifyConfig:
    base: dict[str, Any] = {
        "url": "https://ntfy.example.com/secret-topic?auth=abc",
        "headers": {"Authorization": "Bearer tok"},
        "include_result": False,
    }
    return NotifyConfig(**{**base, **overrides})


def client(handler: Any) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


class TestNotificationText:
    def test_excludes_the_result_by_default(self) -> None:
        assert notification_text(TASK, False) == "Task 7 in voice-coding-mcp finished."
        assert notification_text(TASK, True) == "Task 7 in voice-coding-mcp finished.\n\nAll tests pass."
        assert (
            notification_text(dataclasses.replace(TASK, status="failed"), False) == "Task 7 in voice-coding-mcp failed."
        )
        assert (
            notification_text(dataclasses.replace(TASK, status="timeout"), False)
            == "Task 7 in voice-coding-mcp timed out."
        )

    def test_a_remote_job_names_its_label_and_never_carries_the_result(self) -> None:
        job = NotifyTask(
            id=9, project="dev", status="done", result='A summary of the screen: "x"', label="status of fpga on dev"
        )
        assert notification_text(job, True) == "Task 9 (status of fpga on dev) finished."
        assert notification_text(job, False) == "Task 9 (status of fpga on dev) finished."
        assert notification_text(dataclasses.replace(job, status="cancelled"), True) == (
            "Task 9 (status of fpga on dev) was cancelled."
        )


class TestCreateNotifier:
    def test_is_off_without_a_url(self) -> None:
        assert create_notifier(config(url=None), silent_logger) is None

    async def test_posts_the_short_text_with_the_configured_headers(self) -> None:
        calls: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return httpx2.Response(200, text="ok")

        async with client(handler) as http:
            notify = create_notifier(config(), silent_logger, client=http)
            assert notify is not None
            await notify(TASK)
        assert len(calls) == 1
        assert str(calls[0].url) == "https://ntfy.example.com/secret-topic?auth=abc"
        assert calls[0].method == "POST"
        assert calls[0].content == b"Task 7 in voice-coding-mcp finished."
        assert calls[0].headers["Authorization"] == "Bearer tok"
        assert calls[0].headers["content-type"] == "text/plain; charset=utf-8"

    async def test_send_text_posts_the_text_as_is_and_logs_no_text(self) -> None:
        calls: list[httpx2.Request] = []
        lines: list[str] = []
        logger = create_logger(level="debug", write=lines.append)

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return httpx2.Response(200 if len(calls) == 1 else 500, text="ok")

        async with client(handler) as http:
            notify = create_notifier(config(), logger, client=http)
            assert notify is not None
            await notify.send_text("fpga on dev needs a decision.")
            await notify.send_text("fpga on dev asks you something.")
            await notify.aclose()
            await notify.send_text("after close")
        assert [c.content for c in calls] == [b"fpga on dev needs a decision.", b"fpga on dev asks you something."]
        assert calls[0].headers["Authorization"] == "Bearer tok"
        assert calls[0].headers["content-type"] == "text/plain; charset=utf-8"
        joined = "\n".join(lines)
        assert "fpga" not in joined
        assert '"kind":"text"' in joined
        assert '"status":500' in joined
        assert '"error":"closed"' in joined

    async def test_only_logs_failures_without_the_url_path_query_or_headers(self) -> None:
        lines: list[str] = []
        logger = create_logger(level="debug", write=lines.append)

        def failing(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("fetch failed https://ntfy.example.com/secret-topic?auth=abc", request=request)

        async with client(failing) as http:
            notify = create_notifier(config(), logger, client=http)
            assert notify is not None
            await notify(TASK)
        async with client(lambda _request: httpx2.Response(500, text="no")) as http:
            notify = create_notifier(config(), logger, client=http)
            assert notify is not None
            await notify(TASK)
        assert len(lines) == 2
        for line in lines:
            assert "notify failed" in line
            assert re.search(r"https://ntfy\.example\.com", line)
            assert not re.search(r"secret-topic|auth=abc|Bearer|tok", line)

    async def test_treats_a_redirect_as_failure(self) -> None:
        lines: list[str] = []
        logger = create_logger(level="debug", write=lines.append)
        redirect = httpx2.Response(302, headers={"location": "https://elsewhere.example.com/"})
        async with client(lambda _request: redirect) as http:
            notify = create_notifier(config(), logger, client=http)
            assert notify is not None
            await notify(TASK)
        assert len(lines) == 1
        assert '"msg":"notify failed"' in lines[0]
        assert '"status":302' in lines[0]

    async def test_times_out(self) -> None:
        lines: list[str] = []
        logger = create_logger(write=lines.append)

        async def hanging(_request: httpx2.Request) -> httpx2.Response:
            await asyncio.sleep(60)
            return httpx2.Response(200)

        async with client(hanging) as http:
            notify = create_notifier(config(), logger, client=http, timeout_ms=50)
            assert notify is not None
            await notify(TASK)
        assert '"error":"timeout"' in (lines[0] if lines else "")

    async def test_reuses_one_client_created_on_the_first_send_and_closes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        created: list[httpx2.AsyncClient] = []
        calls: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            calls.append(request)
            return httpx2.Response(200)

        real = httpx2.AsyncClient

        def factory() -> httpx2.AsyncClient:
            created.append(real(transport=httpx2.MockTransport(handler)))
            return created[-1]

        monkeypatch.setattr(httpx2, "AsyncClient", factory)
        lines: list[str] = []
        notify = create_notifier(config(), create_logger(write=lines.append))
        assert notify is not None
        assert created == []
        await asyncio.gather(notify(TASK), notify(dataclasses.replace(TASK, id=8)))
        await notify(TASK)
        assert len(created) == 1
        assert len(calls) == 3
        await notify.aclose()
        assert created[0].is_closed
        await notify.aclose()
        await notify(TASK)
        assert len(created) == 1
        assert len(calls) == 3
        assert '"error":"closed"' in lines[-1]

    async def test_leaves_an_injected_client_open(self) -> None:
        async with client(lambda _request: httpx2.Response(200)) as http:
            notify = create_notifier(config(), silent_logger, client=http)
            assert notify is not None
            await notify(TASK)
            await notify.aclose()
            assert not http.is_closed
