"""Remote jobs (`TaskManager.start_job`) and slot leases (`TaskManager.claim_slot`)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx2
import pytest

from tests.helpers import claude_config, tasks_config, until
from voice_coding_mcp.config import NotifyConfig
from voice_coding_mcp.log import silent_logger
from voice_coding_mcp.notify import Notifier, NotifyTask, create_notifier, notification_text
from voice_coding_mcp.tasks.manager import Started, StartFailed, TaskManager, TaskManagerOptions, format_for_voice


async def make(
    tmp: Path,
    *,
    max_concurrent: int = 1,
    notified: list[NotifyTask] | None = None,
    notifier_override: Notifier | None = None,
) -> TaskManager:
    root = tmp / "projects"
    (root / "alpha").mkdir(parents=True, exist_ok=True)

    async def notifier(note: NotifyTask) -> None:
        if notified is not None:
            notified.append(note)

    return await TaskManager.create(
        TaskManagerOptions(
            state_dir=str(tmp / "state"),
            project_roots=[str(root)],
            claude=claude_config(str(tmp / "record.jsonl")),
            tasks=tasks_config(max_concurrent=max_concurrent),
            logger=silent_logger,
            notifier=notifier_override or notifier,
            kill_grace_ms=200,
        )
    )


@pytest.fixture
async def manager(tmp_path: Path) -> AsyncIterator[TaskManager]:
    m = await make(tmp_path)
    yield m
    await m.shutdown("test finished")


async def test_a_job_runs_in_the_background_and_reports_its_result(manager: TaskManager, tmp_path: Path) -> None:
    gate = asyncio.Event()

    async def job(abort: asyncio.Event) -> str:
        await gate.wait()
        return "dev, fpga: Claude Code is idle."

    started = manager.start_job(label="status of fpga on dev", machine="dev", lock_key="remote:dev:%1", job=job)
    assert isinstance(started, Started)
    task = started.task
    assert (task.kind, task.project, task.status, task.prompt) == ("remote", "dev", "running", "status of fpga on dev")
    assert format_for_voice(task).startswith(f"Task {task.id} (status of fpga on dev) is still running")
    # Same lock key while it runs: the same task, not a second job.
    again = manager.start_job(label="x", machine="dev", lock_key="remote:dev:%1", job=job)
    assert isinstance(again, Started)
    assert again.task.id == task.id
    gate.set()
    done = await manager.wait(task.id, 2000)
    assert done is not None
    assert done.status == "done"
    assert format_for_voice(done) == f"Task {task.id} (status of fpga on dev) is done. dev, fpga: Claude Code is idle."
    saved = json.loads((tmp_path / "state" / "tasks" / f"{task.id}.json").read_text())
    assert saved["kind"] == "remote"
    assert saved["result"] == "dev, fpga: Claude Code is idle."


async def test_failed_and_cancelled_jobs(manager: TaskManager) -> None:
    async def broken(abort: asyncio.Event) -> str:
        raise RuntimeError("dispatcher went away")

    started = manager.start_job(label="status", machine="dev", lock_key="a", job=broken)
    assert isinstance(started, Started)
    done = await manager.wait(started.task.id, 2000)
    assert done is not None
    assert (done.status, done.error) == ("failed", "dispatcher went away")

    async def forever(abort: asyncio.Event) -> str:
        await asyncio.sleep(60)
        return "never"

    slow = manager.start_job(label="status", machine="dev", lock_key="b", job=forever)
    assert isinstance(slow, Started)
    await manager.cancel(slow.task.id)
    cancelled = manager.get(slow.task.id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"


async def test_shutdown_fails_running_jobs_and_a_restart_keeps_them_failed(tmp_path: Path) -> None:
    m = await make(tmp_path)

    async def forever(abort: asyncio.Event) -> str:
        await asyncio.sleep(60)
        return "never"

    started = m.start_job(label="status", machine="dev", lock_key="b", job=forever)
    assert isinstance(started, Started)
    await m.shutdown("server stopped")
    after = m.get(started.task.id)
    assert after is not None
    assert (after.status, after.error) == ("failed", "server stopped")
    assert isinstance(m.start_job(label="x", machine="dev", lock_key="c", job=forever), StartFailed)


async def test_a_job_nobody_waits_for_notifies(tmp_path: Path) -> None:
    notified: list[NotifyTask] = []
    m = await make(tmp_path, notified=notified)

    async def job(abort: asyncio.Event) -> str:
        return "done"

    started = m.start_job(label="status", machine="dev", lock_key="a", job=job)
    assert isinstance(started, Started)
    await until(lambda: bool(notified))
    assert (notified[0].id, notified[0].project, notified[0].status) == (started.task.id, "dev", "done")
    await m.shutdown("test finished")


SCREEN = 'dev, fpga: idle. A summary of the screen: "deploy key sk-live-123 printed"'


async def test_a_job_push_never_carries_the_screen_text_even_with_include_result(tmp_path: Path) -> None:
    bodies: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(request.content.decode("utf-8"))
        return httpx2.Response(200, text="ok")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http:
        push = create_notifier(
            NotifyConfig(url="https://ntfy.example.com/topic", headers={}, include_result=True),
            silent_logger,
            client=http,
        )
        assert push is not None
        m = await make(tmp_path, notifier_override=push)

        async def job(abort: asyncio.Event) -> str:
            return SCREEN

        started = m.start_job(label="status of fpga on dev", machine="dev", lock_key="a", job=job)
        assert isinstance(started, Started)
        await until(lambda: bool(bodies))
        await m.shutdown("test finished")
    done = m.get(started.task.id)
    assert done is not None
    assert done.result == SCREEN  # the result itself is kept for get_task_result
    assert bodies == [f"Task {started.task.id} (status of fpga on dev) finished."]
    for word in ("summary", "screen", "sk-live", "deploy", "idle"):
        assert word not in bodies[0]


async def test_a_failed_job_push_names_the_job_only(tmp_path: Path) -> None:
    notified: list[NotifyTask] = []
    m = await make(tmp_path, notified=notified)

    async def job(abort: asyncio.Event) -> str:
        raise RuntimeError(SCREEN)

    started = m.start_job(label="status of fpga on dev", machine="dev", lock_key="a", job=job)
    assert isinstance(started, Started)
    await until(lambda: bool(notified))
    await m.shutdown("test finished")
    assert notified[0].result is None
    assert notification_text(notified[0], True) == f"Task {started.task.id} (status of fpga on dev) failed."


async def test_claim_slot_counts_toward_max_concurrent_and_releases(tmp_path: Path) -> None:
    m = await make(tmp_path, max_concurrent=1)
    lease = m.claim_slot("remote:dev:%1")
    assert lease is not None
    assert m.claim_slot("remote:dev:%2") is None  # the only slot is taken
    started = await m.start("alpha", "SLEEP 50\nRESULT hi")
    assert isinstance(started, Started)
    assert m.get(started.task.id) is not None
    queued = m.get(started.task.id)
    assert queued is not None
    assert queued.status == "queued"
    lease.release()
    lease.release()  # idempotent
    done = await m.wait(started.task.id, 5000)
    assert done is not None
    assert done.status == "done"
    await m.shutdown("test finished")


async def test_claim_slot_lets_queued_tasks_go_first_and_honours_locks(tmp_path: Path) -> None:
    m = await make(tmp_path, max_concurrent=2)
    first = m.claim_slot("remote:dev:%1")
    assert first is not None
    assert m.claim_slot("remote:dev:%1") is None  # same key is locked
    second = m.claim_slot("remote:dev:%2")
    assert second is not None
    started = await m.start("alpha", "RESULT hi")
    assert isinstance(started, Started)
    first.release()
    # The queued task took the freed slot.
    assert m.claim_slot("remote:dev:%3") is None
    await m.wait(started.task.id, 5000)
    second.release()
    third = m.claim_slot("remote:dev:%3")
    assert third is not None
    await m.shutdown("test finished")
    assert third.abort.is_set()
