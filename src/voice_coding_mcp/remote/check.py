"""`remote-check` for one machine: the dispatcher's `version` and `list`, as printed lines. Used by
the CLI for ssh and local machines, and by the hub's control socket for agent machines (only the
hub holds their connections)."""

from __future__ import annotations

from collections.abc import Sequence

from .inventory import Inventory
from .protocol import VersionResponse
from .speech import machine_line, unreachable_reason


async def check_machine(inventory: Inventory, name: str, warnings: Sequence[str] = ()) -> tuple[list[str], bool]:
    """The lines under the machine's heading in `remote-check`, and whether it answered."""
    lines = [f"  warning: {w}" for w in warnings if w.startswith(f"remote machine {name}:")]
    version = await inventory.version(name)
    if not version.ok:
        lines.append(f"  not reachable: {unreachable_reason(version.error, version.detail)}")
        return lines, False
    data = version.data
    if isinstance(data, VersionResponse):
        lines.append(
            f"  switchboard-remote {data.version}, verbs {', '.join(data.verbs)}, "
            f"send {'allowed' if data.allow_send else 'off'}, usage {'allowed' if data.allow_usage else 'off'}"
        )
    view = await inventory.view(name)
    lines.append("  " + machine_line(view))
    return lines, view.ok
