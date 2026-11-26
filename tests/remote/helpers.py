"""A fake remote world for the Switchboard tests: fake ssh (tests/fixtures/fake_ssh.py) that runs the
real dispatcher against a fake tmux (tests/fixtures/fake_tmux.py) and a fake Claude Code registry.

`RemoteWorld.create(tmp)` lays out:
- `ssh`: a wrapper script used as `remote.ssh.bin` (sets FAKE_SSH_CONFIG, then runs the fake);
- `home`: the remote user's HOME, with `.claude/sessions/` for registry entries;
- the scenario, ssh log and tmux log files.
Nothing here touches the real home, tmux, ssh or claude.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.helpers import FIXTURES

FAKE_SSH = str(FIXTURES / "fake_ssh.py")
FAKE_TMUX = str(FIXTURES / "fake_tmux.py")
DISPATCHER = str(Path(__file__).resolve().parents[2] / "src" / "voice_coding_mcp" / "remote" / "dispatcher.py")


def pane(
    session: str,
    pane_id: str,
    pid: int,
    *,
    window_index: int = 0,
    window_name: str = "bash",
    pane_index: int = 0,
    command: str = "node",
    active: bool = True,
    attached: bool = False,
) -> dict[str, Any]:
    """One fake tmux pane (tmux format names)."""
    return {
        "session_name": session,
        "session_id": f"${len(session)}",
        "window_index": window_index,
        "window_id": f"@{window_index}",
        "window_name": window_name,
        "pane_index": pane_index,
        "pane_id": pane_id,
        "pane_pid": pid,
        "pane_current_command": command,
        "pane_active": 1 if active else 0,
        "session_attached": 1 if attached else 0,
        "window_activity": 1764160000,
    }


@dataclass
class RemoteWorld:
    root: Path
    ssh: str
    home: str
    scenario: str
    ssh_log: str
    tmux_log: str
    settings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(cls, root: Path, **settings: Any) -> RemoteWorld:
        world_dir = root / "world"
        home = world_dir / "remote-home"
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "projects").mkdir(parents=True)
        world = cls(
            root=world_dir,
            ssh=str(world_dir / "ssh"),
            home=str(home),
            scenario=str(world_dir / "scenario.json"),
            ssh_log=str(world_dir / "ssh.jsonl"),
            tmux_log=str(world_dir / "tmux.jsonl"),
            settings={"forced": ["--sessions", "fpga,notes,work-*"], **settings},
        )
        world.write_settings()
        world.set_panes([])
        wrapper = Path(world.ssh)
        wrapper.write_text(
            "#!/bin/sh\n"
            f"FAKE_SSH_CONFIG={shlex.quote(str(world_dir / 'fake-ssh.json'))}\n"
            "export FAKE_SSH_CONFIG\n"
            f'exec {shlex.quote(sys.executable)} {shlex.quote(FAKE_SSH)} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        return world

    def write_settings(self) -> None:
        data = {
            "log": self.ssh_log,
            "home": self.home,
            "scenario": self.scenario,
            "tmuxLog": self.tmux_log,
            **self.settings,
        }
        (self.root / "fake-ssh.json").write_text(json.dumps(data), encoding="utf-8")

    def configure(self, **settings: Any) -> None:
        self.settings.update(settings)
        self.write_settings()

    def set_panes(self, panes: list[dict[str, Any]], text: dict[str, str] | None = None, **extra: Any) -> None:
        Path(self.scenario).write_text(json.dumps({"panes": panes, "text": text or {}, **extra}), encoding="utf-8")

    def register(self, file_id: str, pid: int, **data: Any) -> str:
        """A live registry entry `<home>/.claude/sessions/<file_id>.json` (digits) for a live `pid`."""
        path = os.path.join(self.home, ".claude", "sessions", f"{file_id}.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"pid": pid, **data}, handle)
        return path

    def ssh_calls(self) -> list[dict[str, Any]]:
        return _lines(self.ssh_log)

    def tmux_calls(self) -> list[dict[str, Any]]:
        return _lines(self.tmux_log)

    def tmux_env(self) -> dict[str, str]:
        """Environment for running the dispatcher in process against the fake tmux."""
        return {
            "HOME": self.home,
            "SWITCHBOARD_REMOTE_TEST": "1",
            "SWITCHBOARD_REMOTE_TEST_TMUX": FAKE_TMUX,
            "FAKE_TMUX_SCENARIO": self.scenario,
            "FAKE_TMUX_LOG": self.tmux_log,
        }


def _lines(path: str) -> list[dict[str, Any]]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return [json.loads(line) for line in text.split("\n") if line]


def machine(
    name: str, *, world: RemoteWorld | None = None, identity: str = "/home/USER/.ssh/switchboard_dev", **keys: Any
) -> dict[str, Any]:
    """A machine entry (camelCase keys) for an ssh machine on localhost."""
    data: dict[str, Any] = {
        "name": name,
        "transport": "ssh",
        "host": "localhost",
        "user": "owner",
        "identityFile": identity,
        "sessions": ["fpga", "notes", "work-*"],
    }
    if world is not None:
        data["knownHostsFile"] = str(world.root / "known_hosts")
    data.update(keys)
    return data


SESSION_A = "11111111-0000-4000-8000-00000000000a"
SESSION_B = "11111111-0000-4000-8000-00000000000b"
SESSION_C = "11111111-0000-4000-8000-00000000000c"
