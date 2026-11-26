"""The full app on a real uvicorn server.

`start_app()` binds 127.0.0.1 on a free port, uses `publicUrl http://127.0.0.1:<port>`, the fake
claude and a temp project root. Requests go through `httpx2` (it can set any Host header, so no
separate raw request helper is needed).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from starlette.applications import Starlette

from tests.helpers import LogCapture, claude_config, config_data
from voice_coding_mcp.auth import OAuthStore
from voice_coding_mcp.cli import _bind, _Server
from voice_coding_mcp.config import Config, parse_config
from voice_coding_mcp.http import AuthRouterOverrides, CreateAppOptions, create_app
from voice_coding_mcp.tasks.manager import TaskManager, TaskManagerOptions


@dataclass
class AppCtx:
    base: str
    port: int
    config: Config
    root: str
    state_dir: str
    projects_dir: str
    record: str
    store: OAuthStore
    manager: TaskManager
    app: Starlette
    log: LogCapture
    http: httpx2.AsyncClient
    """Client for `base`, no redirects followed."""


@contextlib.asynccontextmanager
async def start_app(
    root: Path,
    *,
    tasks: dict[str, Any] | None = None,
    oauth: dict[str, Any] | None = None,
    http_client: httpx2.AsyncClient | None = None,
    projects: list[str] | None = None,
    remote: dict[str, Any] | None = None,
) -> AsyncIterator[AppCtx]:
    state_dir = str(root / "state")
    projects_dir = root / "projects"
    projects_dir.mkdir()
    for name in projects if projects is not None else ["alpha-app", "beta-site"]:
        (projects_dir / name).mkdir()

    sock = _bind("127.0.0.1", 0)
    port = int(sock.getsockname()[1])
    base = f"http://127.0.0.1:{port}"
    record = str(root / "claude-record.jsonl")
    claude = claude_config(record)
    config = parse_config(
        config_data(
            base,
            state_dir,
            str(projects_dir),
            claude={
                "bin": claude.bin,
                "permissionMode": claude.permission_mode,
                "settingSources": list(claude.setting_sources),
                "env": dict(claude.env),
            },
            tasks={"syncWaitSeconds": 2, **(tasks or {})},
            oauth=oauth or {},
            **({"remote": remote} if remote is not None else {}),
        ),
        home_dir="/nonexistent-home-dir",
    )
    log = LogCapture()
    store = OAuthStore(state_dir, access_token_hours=24, refresh_token_days=90)
    manager = await TaskManager.create(
        TaskManagerOptions(
            state_dir=state_dir,
            project_roots=config.project_roots,
            claude=config.claude,
            tasks=config.tasks,
            logger=log.logger,
            base_env=os.environ,
            kill_grace_ms=500,
        )
    )
    app = create_app(
        CreateAppOptions(
            config=config,
            logger=log.logger,
            store=store,
            manager=manager,
            auth=AuthRouterOverrides(rate_limit=None, http_client=http_client),
        )
    )
    server = _Server(
        uvicorn.Config(app, proxy_headers=False, access_log=False, log_config=None, server_header=False, ws="none")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:  # uvicorn has no startup event
        assert not task.done(), "server failed to start"
        await asyncio.sleep(0.01)
    http = httpx2.AsyncClient(base_url=base, follow_redirects=False, timeout=30)
    try:
        yield AppCtx(
            base=base,
            port=port,
            config=config,
            root=str(root),
            state_dir=state_dir,
            projects_dir=str(projects_dir),
            record=record,
            store=store,
            manager=manager,
            app=app,
            log=log,
            http=http,
        )
    finally:
        await http.aclose()
        await manager.shutdown("test finished")
        server.should_exit = True
        await task
