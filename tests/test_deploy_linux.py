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


# --- the guide: what runs as the owner or root comes from the owner's own copy ---------------------

GUIDE = REPO / "docs" / "SETUP-WSL.md"
OWNER_COPY = "~/src/switchboard"
# Destinations whose files run as the owner (or a remote machine user) or as root.
PRIVILEGED_DESTINATIONS = ("~/.local/bin/", "/usr/local/bin/", "/etc/systemd/system/")


def guide() -> str:
    return GUIDE.read_text(encoding="utf-8")


def code_blocks(text: str) -> list[list[str]]:
    """The lines of every fenced code block, indentation stripped."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("```"):
            if current is None:
                current = []
            else:
                blocks.append(current)
                current = None
            continue
        if current is not None:
            current.append(line)
    return blocks


def section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end >= 0 else len(text)]


def test_nothing_the_owner_or_root_runs_is_copied_from_the_hub_users_checkout() -> None:
    text = guide()
    # Any copy, install, cat or scp that reads a deploy file from the hub's checkout is a way for a
    # compromised hub user to become the owner or root.
    assert not re.search(r"(?:install|cp|cat|scp|rsync|tee)\b[^\n`]*/home/switchboard/switchboard/deploy", text)
    assert "one short Python file" not in text
    for line in (line for block in code_blocks(text) for line in block):
        if re.match(r"(?:sudo )?install\b", line) and any(dest in line for dest in PRIVILEGED_DESTINATIONS):
            assert "/home/switchboard" not in line, line
            assert re.search(r"\binstall\b.* deploy/(?:remote|linux)/", line), line


def test_every_privileged_install_is_from_the_owners_copy_and_read_first() -> None:
    installs = 0
    for block in code_blocks(guide()):
        cwd = None
        read: set[str] = set()
        for line in block:
            if line.startswith("cd "):
                cwd = line.split()[1]
            if line.startswith("less "):
                read.update(line.split("#")[0].split()[1:])
            match = re.match(r"(?:sudo )?install\b.* (deploy/\S+) (\S+)", line)
            if match and any(match.group(2).startswith(dest) for dest in PRIVILEGED_DESTINATIONS):
                installs += 1
                assert cwd == OWNER_COPY, line
                assert match.group(1) in read, f"{match.group(1)} is installed before it is read"
    # The dispatcher (step 5.2), the unit and sb-linux (step 9).
    assert installs == 3


def test_the_owners_copy_is_pinned_and_verified_and_updates_repeat_it() -> None:
    text = guide()
    copy = section(text, "### Your own copy")
    assert f"git clone https://github.com/nilware-io/switchboard.git {OWNER_COPY}" in copy
    assert "git checkout --detach <commit>" in copy
    assert "git rev-parse HEAD" in copy
    assert "git status --porcelain" in copy
    assert "voice-coding-mcp remote-dispatcher" in copy
    assert "Updates repeat this" in copy
    # Other machines get the dispatcher from the same copy, as the owner.
    other = section(text, "## 6. Other machines")
    assert f"scp {OWNER_COPY}/deploy/remote/switchboard-remote" in other
    assert "sha256sum" in other


def test_the_guide_states_the_real_size_of_the_files_to_read() -> None:
    text = guide()
    for path, claim in (
        (REPO / "deploy" / "remote" / "switchboard-remote", r"one Python file of about (\d+) lines"),
        (SB_LINUX, r"a bash script of about (\d+) lines"),
    ):
        match = re.search(claim, text)
        assert match, claim
        stated = int(match.group(1))
        actual = len(path.read_text(encoding="utf-8").splitlines())
        assert abs(actual - stated) <= stated * 0.2, f"{path.name}: the guide says {stated} lines, it has {actual}"


def test_the_compromise_steps_wipe_the_hubs_checkout_before_reinstalling_anything() -> None:
    steps = section(guide(), "## If something is compromised")
    wipe = steps.index("Throw away the hub's code")
    assert "deluser --remove-home switchboard" in steps
    assert "rm -rf ~/switchboard" in steps
    assert ".venv" in steps
    # The fresh clone is checked against the owner's verified commit.
    assert "git checkout --detach <commit>" in steps[wipe:]
    # The installed owner and root files are compared with the owner's own copy.
    for installed in (
        "~/.local/bin/switchboard-remote",
        "/usr/local/bin/sb-linux",
        "/etc/systemd/system/switchboard.service",
    ):
        assert re.search(rf"cmp {re.escape(OWNER_COPY)}/deploy/\S+ {re.escape(installed)}", steps), installed
    # Keys and dispatchers are reinstalled only after the wipe, and only from the owner's copy.
    rotate = steps.index("Rotate the hub's SSH keys")
    assert wipe < rotate
    assert "never from the hub's new checkout" in steps[rotate:]
    assert "(steps 5 and 6)" not in steps


def test_the_design_docs_record_the_trusted_source() -> None:
    switchboard = (REPO / "docs" / "SWITCHBOARD.md").read_text(encoding="utf-8")
    design = (REPO / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    assert "### Trusted source for owner and root files" in switchboard
    assert "#trusted-source-for-owner-and-root-files" in design
    assert "never from the hub user's checkout" in switchboard
