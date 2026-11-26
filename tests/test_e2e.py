"""End to end: the way Claude connects, against a real uvicorn server.

Discovery from the 401 challenge, DCR or CIMD, pairing, PKCE, the consent POST, the token
exchange, MCP tools through the SDK client (2025-11-25 handshake, as Claude speaks it), refresh
rotation, and no secrets in the logs.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlencode, urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from tests.app_helpers import AppCtx, start_app
from tests.auth.helpers import location_params, pkce
from tests.helpers import create_grant
from voice_coding_mcp.auth import open_pairing
from voice_coding_mcp.auth.store import NewClient, NewGrant

CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CLAUDE_CIMD = "https://claude.ai/oauth/mcp-oauth-client-metadata"


@contextlib.asynccontextmanager
async def mcp_client(base: str, access_token: str, mode: str = "legacy") -> AsyncIterator[Client]:
    async with (
        httpx2.AsyncClient(headers={"authorization": f"Bearer {access_token}"}, timeout=30) as http,
        Client(streamable_http_client(f"{base}/mcp", http_client=http), mode=mode) as client,
    ):
        yield client


async def tool_text(client: Client, name: str, args: dict[str, Any] | None = None) -> str:
    result = await client.call_tool(name, args or {})
    return "\n".join(c.text for c in result.content if isinstance(c, TextContent))


async def discover(ctx: AppCtx) -> dict[str, Any]:
    """Discovery as Claude does it: 401 challenge, PRM from the header, then AS metadata."""
    base = ctx.base
    challenge = await ctx.http.post(
        "/mcp",
        headers={"content-type": "application/json", "accept": "application/json, text/event-stream"},
        content=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "c", "version": "1"},
                },
            }
        ),
    )
    assert challenge.status_code == 401
    header = challenge.headers.get("www-authenticate", "")
    match = re.search(r'resource_metadata="([^"]+)"', header)
    assert match is not None
    prm_url = match.group(1)
    assert prm_url == f"{base}/.well-known/oauth-protected-resource/mcp"
    assert 'scope="mcp"' in header

    prm = (await ctx.http.get(prm_url)).json()
    assert prm["resource"] == f"{base}/mcp"
    assert prm["authorization_servers"] == [base]

    as_meta = cast(
        dict[str, Any],
        (await ctx.http.get(f"{prm['authorization_servers'][0]}/.well-known/oauth-authorization-server")).json(),
    )
    assert as_meta["issuer"] == base
    assert as_meta["client_id_metadata_document_supported"] is True
    return as_meta


async def authorize(ctx: AppCtx, as_meta: dict[str, Any], client_id: str, challenge: str, pairing_code: str) -> str:
    """GET and POST /authorize with the pairing code; returns the authorization code."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": CLAUDE_CALLBACK,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "state-xyz",
        "resource": f"{ctx.base}/mcp",
        "scope": "mcp",
    }
    page = await ctx.http.get(f"{as_meta['authorization_endpoint']}?{urlencode(params)}")
    assert page.status_code == 200
    assert 'name="pairing_code"' in page.text
    assert "claude.ai" in page.text

    approved = await ctx.http.post(
        str(as_meta["authorization_endpoint"]), data={**params, "pairing_code": pairing_code.lower()}
    )
    assert approved.status_code == 302
    location = urlsplit(approved.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == CLAUDE_CALLBACK
    query = location_params(approved)
    assert query["state"] == "state-xyz"
    assert query["iss"] == ctx.base
    code = query.get("code")
    assert code
    return code


async def exchange(ctx: AppCtx, as_meta: dict[str, Any], client_id: str, code: str, verifier: str) -> dict[str, Any]:
    res = await ctx.http.post(
        str(as_meta["token_endpoint"]),
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": CLAUDE_CALLBACK,
            "resource": f"{ctx.base}/mcp",
        },
    )
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    tokens = cast(dict[str, Any], res.json())
    assert tokens["token_type"] == "Bearer"
    return tokens


async def test_dcr_pairing_pkce_mcp_tools_refresh_rotation_and_a_closed_window(real_tmp: Path) -> None:
    async with start_app(real_tmp, tasks={"syncWaitSeconds": 1}) as ctx:
        base = ctx.base
        as_meta = await discover(ctx)

        closed = await ctx.http.post(
            str(as_meta["registration_endpoint"]),
            json={"client_name": "Claude", "redirect_uris": [CLAUDE_CALLBACK], "token_endpoint_auth_method": "none"},
        )
        assert closed.status_code == 403

        pairing_code = (await open_pairing(ctx.state_dir, 10)).code
        registered = await ctx.http.post(
            str(as_meta["registration_endpoint"]),
            json={
                "client_name": "Claude",
                "redirect_uris": [CLAUDE_CALLBACK],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert registered.status_code == 201
        client_id = registered.json()["client_id"]

        verifier, challenge = pkce()
        code = await authorize(ctx, as_meta, client_id, challenge, pairing_code)
        tokens = await exchange(ctx, as_meta, client_id, code, verifier)

        # The window closed with the successful authorization.
        after_pairing = await ctx.http.post(
            str(as_meta["registration_endpoint"]), json={"redirect_uris": [CLAUDE_CALLBACK]}
        )
        assert after_pairing.status_code == 403

        # MCP over the legacy (2025-11-25) stateless path, as Claude speaks it.
        async with mcp_client(base, tokens["access_token"]) as mcp:
            assert "get_task_result" in (mcp.instructions or "")
            tools = (await mcp.list_tools()).tools
            assert len(tools) == 12
            assert await tool_text(mcp, "list_projects") == "Projects: alpha-app, beta-site."
            assert (
                await tool_text(mcp, "run_task", {"project": "Alpha App", "prompt": "RESULT Hotovo."})
                == "Task 1 in alpha-app is done. Hotovo."
            )
            slow = await tool_text(mcp, "run_task", {"project": "beta site", "prompt": "SLEEP 1500\nRESULT Later."})
            assert slow.startswith("Task 2 in beta-site is still running")
            result = await tool_text(mcp, "get_task_result", {"task_id": 2})
            if "is done" not in result:
                result = await tool_text(mcp, "get_task_result", {"task_id": 2})
            assert result == "Task 2 in beta-site is done. Later."

        # Refresh rotation: the new tokens work, the old access token does not.
        refreshed = await ctx.http.post(
            str(as_meta["token_endpoint"]),
            data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]},
        )
        assert refreshed.status_code == 200
        rotated = refreshed.json()
        assert rotated["refresh_token"] != tokens["refresh_token"]
        assert rotated["access_token"] != tokens["access_token"]
        old = await ctx.http.post(
            "/mcp",
            headers={"authorization": f"Bearer {tokens['access_token']}", "content-type": "application/json"},
            content="{}",
        )
        assert old.status_code == 401
        async with mcp_client(base, rotated["access_token"]) as mcp2:
            assert re.fullmatch(
                r"Task 2 in beta-site: done\.\nTask 1 in alpha-app: done\.", await tool_text(mcp2, "list_tasks")
            )

        # A repeat within 60 s (parallel refreshes) gets the same tokens; later reuse revokes (router tests).
        repeat = await ctx.http.post(
            str(as_meta["token_endpoint"]),
            data={"grant_type": "refresh_token", "client_id": client_id, "refresh_token": tokens["refresh_token"]},
        )
        assert repeat.status_code == 200
        assert repeat.json() == rotated

        # No secrets in the logs.
        logs = ctx.log.text()
        for secret in [
            tokens["access_token"],
            tokens["refresh_token"],
            rotated["access_token"],
            rotated["refresh_token"],
            code,
            pairing_code,
        ]:
            assert secret not in logs, "log leaked a secret"


async def test_cimd_with_claudes_published_client_metadata_document(real_tmp: Path) -> None:
    requested: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requested.append(str(request.url))
        return httpx2.Response(
            200,
            headers={"content-type": "application/json", "cache-control": "max-age=600"},
            content=json.dumps(
                {
                    "client_id": CLAUDE_CIMD,
                    "client_name": "Claude",
                    "redirect_uris": [CLAUDE_CALLBACK],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                }
            ),
        )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as fetch_stub,
        start_app(real_tmp, http_client=fetch_stub) as ctx,
    ):
        as_meta = await discover(ctx)
        pairing_code = (await open_pairing(ctx.state_dir, 10)).code
        verifier, challenge = pkce()
        code = await authorize(ctx, as_meta, CLAUDE_CIMD, challenge, pairing_code)
        tokens = await exchange(ctx, as_meta, CLAUDE_CIMD, code, verifier)
        assert len(requested) >= 1
        assert all(url == CLAUDE_CIMD for url in requested)

        async with mcp_client(ctx.base, tokens["access_token"]) as mcp:
            assert await tool_text(mcp, "list_projects") == "Projects: alpha-app, beta-site."
        status = await ctx.store.status()
        assert len(status.clients) == 1
        assert status.clients[0].kind == "cimd"
        assert status.clients[0].client_name == "Claude"


async def test_the_2026_protocol_also_works(real_tmp: Path) -> None:
    """The SDK client's default mode (server/discover, 2026-07-28) against the same endpoint."""
    async with start_app(real_tmp) as ctx:
        client = await ctx.store.register_client(
            NewClient(client_name="Claude", redirect_uris=[CLAUDE_CALLBACK], token_endpoint_auth_method="none")
        )
        issued = await create_grant(
            ctx.store,
            NewGrant(
                client_id=client.client.client_id, client_name="Claude", scope="mcp", resource=ctx.config.resource_url
            ),
        )
        async with mcp_client(ctx.base, issued.access_token, mode="auto") as mcp:
            assert mcp.protocol_version == "2026-07-28"
            assert await tool_text(mcp, "list_projects") == "Projects: alpha-app, beta-site."
