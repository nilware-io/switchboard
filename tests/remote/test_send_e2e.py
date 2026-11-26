"""`remote_send` and `remote_send_confirm` through the real server: OAuth bearer tokens, the MCP
SDK client over HTTP, the fake ssh and the real dispatcher. A confirmation is bound to the OAuth grant
that prepared it, and the audit line names that grant by its fingerprint."""

from __future__ import annotations

import os
import re
from pathlib import Path

from tests.app_helpers import start_app
from tests.helpers import create_grant
from tests.remote.helpers import SESSION_A, RemoteWorld, machine, pane
from tests.test_e2e import CLAUDE_CALLBACK, mcp_client, tool_text
from voice_coding_mcp.auth.store import NewClient, NewGrant
from voice_coding_mcp.log import fingerprint
from voice_coding_mcp.remote.audit import read_audit

EMPTY_BOX = "⏺ Done.\n╭──────────────╮\n│ >            │\n╰──────────────╯\n"


async def test_a_confirmation_works_only_for_the_grant_that_prepared_it(real_tmp: Path) -> None:
    world = RemoteWorld.create(real_tmp, forced=["--sessions", "fpga", "--allow-send"])
    world.set_panes([pane("fpga", "%1", 9_300_001, command="claude")], {"%1": EMPTY_BOX})
    world.register("2001", os.getpid(), sessionId=SESSION_A, status="idle", tmux="0:@0.%1")
    remote = {
        "machines": [machine("dev", world=world, sessions=["fpga"], allowSend=True)],
        "ssh": {"bin": world.ssh},
        "summary": {"enabled": False},
        "send": {"minConfirmSeconds": 0},
    }
    async with start_app(real_tmp, tasks={"syncWaitSeconds": 20}, remote=remote) as ctx:
        grants = []
        for name in ("Claude", "Claude"):
            client = await ctx.store.register_client(
                NewClient(client_name=name, redirect_uris=[CLAUDE_CALLBACK], token_endpoint_auth_method="none")
            )
            grants.append(
                await create_grant(
                    ctx.store,
                    NewGrant(
                        client_id=client.client.client_id,
                        client_name=name,
                        scope="mcp",
                        resource=ctx.config.resource_url,
                    ),
                )
            )
        owner, other = grants
        text = "run the tests; then $(say done)"
        async with mcp_client(ctx.base, owner.access_token) as mcp:
            readback = await tool_text(mcp, "remote_send", {"machine": "dev", "target": "fpga", "text": text})
        match = re.search(r"\(confirmation (\d{4}), do not read this number aloud\)$", readback)
        assert match is not None, readback
        assert readback.startswith(f'Ready to send to fpga on dev: "{text}".')
        cid = match.group(1)
        async with mcp_client(ctx.base, other.access_token) as mcp:
            assert await tool_text(mcp, "remote_send_confirm", {"confirmation_id": cid}) == (
                "That confirmation has expired. Ask me to prepare the message again."
            )
        async with mcp_client(ctx.base, owner.access_token) as mcp:
            assert await tool_text(mcp, "remote_send_confirm", {"confirmation_id": cid}) == "Sent to fpga on dev."
            assert await tool_text(mcp, "remote_send_confirm", {"confirmation_id": cid}) == (
                "That confirmation has expired. Ask me to prepare the message again."
            )
        (line,) = read_audit(ctx.state_dir)[0]
        assert line["client"] == fingerprint(owner.grant_id)
        assert line["clientName"] == "Claude"
        assert line["outcome"] == "sent"
        loaded = [c["stdin"] for c in world.tmux_calls() if c["argv"][:1] == ["load-buffer"]]
        assert loaded == [text]
        logs = ctx.log.text()
        assert "say done" not in logs
        assert owner.access_token not in logs
        assert other.access_token not in logs
