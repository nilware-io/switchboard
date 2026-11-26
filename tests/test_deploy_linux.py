"""The Linux and WSL deploy files agree with each other and with the security model: the example
config for the dedicated `switchboard` user, its systemd unit and the defaults of sb-linux."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from voice_coding_mcp.config import parse_config
from voice_coding_mcp.remote.config import remote_warnings

REPO = Path(__file__).resolve().parent.parent
LINUX = REPO / "deploy" / "linux"
EXAMPLE = LINUX / "config.switchboard.example.json"
UNIT = LINUX / "switchboard.service"
SB_LINUX = LINUX / "sb-linux"
HUB_HOME = "/home/switchboard"


def example() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return data


def unit() -> dict[str, list[str]]:
    """The unit's `Key=value` lines (comments and sections dropped), values per key in order."""
    out: dict[str, list[str]] = {}
    for line in UNIT.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        out.setdefault(key, []).append(value)
    return out


def test_the_example_is_a_valid_config_once_its_folders_exist(tmp_path: Path) -> None:
    data = example()
    projects = tmp_path / "projects"
    projects.mkdir()
    config = parse_config(
        {**data, "projectRoots": [str(projects)], "stateDir": str(tmp_path / "state")}, home_dir=str(tmp_path / "h")
    )
    dev = config.remote.machines[0]
    assert dev.name == "dev"
    assert (dev.transport, dev.host, dev.dispatcher) == ("ssh", "localhost", "forced")
    assert (dev.allow_send, dev.allow_permission_answers, dev.send_to_any_pane) == (False, False, False)
    assert "*" not in dev.sessions
    assert config.claude.access == "isolated"
    assert config.listen.host == "127.0.0.1"
    assert config.public_url.endswith(".ts.net")


def test_the_example_keeps_everything_of_the_hub_in_the_hub_users_home() -> None:
    data = example()
    assert data["stateDir"].startswith(HUB_HOME + "/")
    assert all(root.startswith(HUB_HOME + "/") for root in data["projectRoots"])
    for machine in data["remote"]["machines"]:
        assert machine["identityFile"].startswith(HUB_HOME + "/.ssh/")
        assert machine["knownHostsFile"].startswith(HUB_HOME + "/.ssh/")


def test_the_example_raises_no_isolation_warning_for_the_hub_user(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    projects.mkdir()
    config = parse_config(
        {**example(), "projectRoots": [str(projects)], "stateDir": str(tmp_path / "state")}, home_dir=str(tmp_path)
    )
    warnings = remote_warnings(config.remote, uid=0, home=HUB_HOME, other_homes=[HUB_HOME, "/home/owner"])
    # The keys do not exist here, so the identity file checks only say that; nothing else weakens isolation.
    assert warnings == [
        f"remote machine dev: identityFile {HUB_HOME}/.ssh/switchboard_dev does not exist yet",
        f"remote machine buildbox: identityFile {HUB_HOME}/.ssh/switchboard_buildbox does not exist yet",
    ]


def test_the_unit_runs_the_hub_as_switchboard_with_the_example_paths() -> None:
    u = unit()
    assert u["User"] == ["switchboard"]
    assert u["Group"] == ["switchboard"]
    [exec_start] = u["ExecStart"]
    argv = exec_start.split()
    assert argv[0] == f"{HUB_HOME}/switchboard/.venv/bin/voice-coding-mcp"
    assert argv[1:3] == ["serve", "--config"]
    assert argv[3] == f"{HUB_HOME}/.config/voice-coding-mcp/config.json"
    assert u["WorkingDirectory"] == [HUB_HOME]


def test_the_unit_hides_other_homes_and_keeps_the_hardening() -> None:
    u = unit()
    assert u["ProtectHome"] == ["tmpfs"]
    assert u["BindPaths"] == [HUB_HOME]
    assert u["ProtectSystem"] == ["strict"]
    assert u["NoNewPrivileges"] == ["yes"]
    assert u["UMask"] == ["0077"]
    assert u["KillMode"] == ["control-group"]
    assert u["EnvironmentFile"] == ["-/etc/switchboard/env"], "the token file is root-owned, outside the hub's home"
    assert "ANTHROPIC_API_KEY" in u["UnsetEnvironment"][0]
    # The unit itself carries no secret.
    environment = " ".join(u.get("Environment", []))
    assert not re.search(r"TOKEN|KEY|PASSWORD|SECRET", environment)


def test_the_unit_and_the_shared_hardening_agree() -> None:
    base = _unit_lines(REPO / "deploy" / "voice-coding-mcp.service")
    ours = unit()
    for key in (
        "ProtectSystem",
        "ProtectHome",
        "PrivateTmp",
        "PrivateDevices",
        "ProtectProc",
        "NoNewPrivileges",
        "RestrictSUIDSGID",
        "RestrictAddressFamilies",
        "SystemCallArchitectures",
        "LockPersonality",
    ):
        assert ours[key] == base[key], key


def _unit_lines(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        out.setdefault(key, []).append(value)
    return out


def test_sb_linux_defaults_match_the_unit_and_the_example() -> None:
    script = SB_LINUX.read_text(encoding="utf-8")
    assert 'HUB_USER="${SB_USER:-switchboard}"' in script
    assert 'SERVICE="${SB_SERVICE:-switchboard}"' in script
    assert 'HUB_HOME="/home/$HUB_USER"' in script
    assert 'CONFIG="${SB_CONFIG:-$HUB_HOME/.config/voice-coding-mcp/config.json}"' in script
    assert 'VCM_CMD=("$HUB_HOME/switchboard/.venv/bin/voice-coding-mcp")' in script
    assert UNIT.name == "switchboard.service"
    assert "printf '%s\\n' \"${SB_PORT:-8787}\"" in script
    assert example()["listen"]["port"] == 8787


def test_deploy_files_carry_no_personal_details() -> None:
    for path in (EXAMPLE, UNIT, SB_LINUX, REPO / "docs" / "SETUP-WSL.md"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"/home/(?!switchboard\b|USER\b|owner\b|bot\b|<SB_USER>|\$HUB_USER)[a-z]", text), path
        assert not re.search(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text.replace("127.0.0.1", "")), path
        assert not re.search("[\\u2013\\u2014]", text), f"{path}: no en or em dashes"
