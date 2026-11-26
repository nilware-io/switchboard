"""Deploy files of outbound agents: the machine's user unit, the relay's sshd drop-in, tmpfiles line
and key file example, the hub units' socket path, setup.sh's opt-in relay part, and the CLI
commands that need no config (`remote-connector`, `agent-relay`)."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest

from tests.remote.agent_helpers import agent_machine
from tests.test_cli import run_cli
from voice_coding_mcp.remote import connector as c
from voice_coding_mcp.remote.agents import lint_authorized_keys

REPO = Path(__file__).resolve().parents[2]
USER_UNIT = REPO / "deploy" / "remote" / "switchboard-connect.service"
SSHD = REPO / "deploy" / "relay" / "sshd-switchboard-relay.conf"
TMPFILES = REPO / "deploy" / "relay" / "tmpfiles-switchboard-relay.conf"
KEYS = REPO / "deploy" / "relay" / "authorized_keys.example"
SETUP = REPO / "deploy" / "setup.sh"
NEW_FILES = (USER_UNIT, SSHD, TMPFILES, KEYS, REPO / "deploy" / "remote" / "switchboard-connect")


def unit_values(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith(("#", "[")):
            continue
        key, _, value = line.partition("=")
        out.setdefault(key, []).append(value)
    return out


def test_the_user_unit_runs_the_connector_with_a_serve_mode_dispatcher(tmp_path: Path) -> None:
    unit = unit_values(USER_UNIT)
    (exec_start,) = unit["ExecStart"]
    argv = shlex.split(exec_start.replace("%h", str(tmp_path)))
    assert argv[:3] == ["/usr/bin/python3", "-I", f"{tmp_path}/.local/bin/switchboard-connect"]
    # The command line parses with the connector's own rules once the key files exist.
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "switchboard_relay").write_text("k", encoding="utf-8")
    (tmp_path / ".ssh" / "switchboard_relay").chmod(0o600)
    (tmp_path / ".config" / "switchboard-connect").mkdir(parents=True)
    (tmp_path / ".config" / "switchboard-connect" / "known_hosts").write_text("x", encoding="utf-8")
    options = c.parse_args(argv[3:], os.getuid())
    assert options.command[:4] == ["/usr/bin/python3", "-I", f"{tmp_path}/.local/bin/switchboard-remote", "--serve"]
    assert "--allow-send" not in options.command
    assert unit["Restart"] == ["always"]
    assert unit["NoNewPrivileges"] == ["yes"]
    assert unit["WantedBy"] == ["default.target"]
    for absent in ("PrivateTmp", "ProtectHome", "ProtectSystem", "After", "Wants"):
        assert absent not in unit, absent


def test_the_sshd_drop_in_locks_the_relay_account_down() -> None:
    lines = [
        line.strip() for line in SSHD.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")
    ]
    assert lines[0] == "Match User sbrelay"
    for needed in (
        "AuthorizedKeysFile /etc/ssh/switchboard-relay/authorized_keys",
        "AuthenticationMethods publickey",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "DisableForwarding yes",
        "PermitTTY no",
        "PermitTunnel no",
        "PermitUserRC no",
        "MaxSessions 1",
    ):
        assert needed in lines, needed
    assert not any(line.startswith("ForceCommand") for line in lines)


def test_the_tmpfiles_line() -> None:
    lines = [line for line in TMPFILES.read_text(encoding="utf-8").splitlines() if line and not line.startswith("#")]
    assert lines == ["d /run/switchboard-relay 2750 vcm sbrelay -"]


def test_the_key_file_example_passes_the_lint() -> None:
    text = KEYS.read_text(encoding="utf-8")
    assert lint_authorized_keys(text, ["laptop"]) == []


@pytest.mark.parametrize("unit", ["deploy/voice-coding-mcp.service", "deploy/linux/switchboard.service"])
def test_the_hub_units_may_write_the_relay_socket(unit: str) -> None:
    assert unit_values(REPO / unit)["ReadWritePaths"] == ["-/run/switchboard-relay"]


def test_setup_sets_the_relay_up_only_when_asked() -> None:
    script = SETUP.read_text(encoding="utf-8")
    block = script[
        script.index("if [[ ${VCM_AGENT_RELAY:-0} == 1 ]]; then") : script.index(
            "# ---------------------------------------------------------------- caddy"
        )
    ]
    assert "--shell /bin/sh" in block
    assert "deploy/relay/tmpfiles-switchboard-relay.conf" in block
    assert "deploy/relay/sshd-switchboard-relay.conf" in block
    assert "sshd -t" in block
    assert "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub" in block
    # It never writes a key line.
    assert ">>" not in block
    assert "ssh-ed25519 AAAA" not in block


def test_new_deploy_files_carry_no_personal_details() -> None:
    for path in NEW_FILES:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"/home/(?!USER\b)[a-z]", text), path
        addresses = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
        assert all(a.startswith(("192.0.2.", "198.51.100.", "203.0.113.")) for a in addresses), path
        assert not re.search("[\\u2013\\u2014]", text), f"{path}: no en or em dashes"


async def test_remote_connector_prints_the_script_without_a_config() -> None:
    code, io = await run_cli("remote-connector", "--config", "/nonexistent/config.json")
    assert code == 0
    assert io.stdout == Path(c.__file__).read_text(encoding="utf-8")  # noqa: ASYNC240


async def test_agent_relay_is_the_relay(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, _ = await run_cli("agent-relay", "--machine", "Bad Name", "--config", "/nonexistent/config.json")
    assert code == 2
    code, _ = await run_cli("agent-relay", "--machine", "laptop", "--socket", str(tmp_path / "none.sock"))
    assert code == 1
    assert capsys.readouterr().err.splitlines() == [
        "switchboard relay: bad arguments",
        "switchboard relay: hub not running",
    ]


async def test_remote_check_lints_the_relay_keys_without_a_hub(tmp_path: Path) -> None:
    keys = tmp_path / "authorized_keys"
    keys.write_text('command="/bin/sh" ssh-ed25519 AAAA x\n', encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    data = {
        "publicUrl": "https://vcm.example.com",
        "stateDir": str(tmp_path / "state"),
        "projectRoots": [str(projects)],
        "remote": {"machines": [agent_machine("laptop")], "agents": {"authorizedKeysFile": str(keys)}},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    code, io = await run_cli("remote-check", "--config", str(path))
    assert code == 1
    assert io.stdout.splitlines() == [
        "laptop (agent: connects out to the hub)",
        "  not checked: the hub is not running (only the hub holds agent connections)",
        f"relay keys ({keys}):",
        "  problem: line 1: no restrict option",
        "  problem: line 1: command= is not the switchboard relay",
        "  problem: no key line for agent machine laptop",
    ]


# --- the owner guide ---------------------------------------------------------------------------------

GUIDE = REPO / "docs" / "SETUP-AGENTS.md"
OWNER_COPY = "~/src/switchboard"


def guide_blocks() -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in GUIDE.read_text(encoding="utf-8").splitlines():
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


def test_the_guide_installs_what_runs_as_the_owner_only_from_the_owners_copy_after_reading_it() -> None:
    installs = []
    for block in guide_blocks():
        cwd = None
        read: set[str] = set()
        for line in block:
            if line.startswith("cd "):
                cwd = line.split()[1]
            if line.startswith("less "):
                read.update(line.split("#")[0].split()[1:])
            match = re.match(r"install\b.* (deploy/\S+) (~/\S+)", line)
            if match:
                installs.append(match.group(1))
                assert cwd == OWNER_COPY, line
                assert match.group(1) in read, f"{match.group(1)} is installed before it is read"
    assert sorted(installs) == [
        "deploy/remote/switchboard-connect",
        "deploy/remote/switchboard-connect.service",
        "deploy/remote/switchboard-remote",
    ]
    text = GUIDE.read_text(encoding="utf-8")
    assert "/home/switchboard/switchboard/deploy" not in text
    assert "git checkout --detach <commit>" in text


def test_the_guide_states_the_real_size_of_the_files_to_read() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    for path, claim in (
        (REPO / "deploy" / "remote" / "switchboard-remote", r"The dispatcher is one Python file of about (\d+) lines"),
        (REPO / "deploy" / "remote" / "switchboard-connect", r"one Python file of about (\d+) lines with only"),
    ):
        match = re.search(claim, text)
        assert match, claim
        stated = int(match.group(1))
        actual = len(path.read_text(encoding="utf-8").splitlines())
        assert abs(actual - stated) <= stated * 0.2, f"{path.name}: the guide says {stated} lines, it has {actual}"


def test_the_guide_pins_the_hub_under_the_connectors_alias_and_carries_no_personal_details() -> None:
    text = GUIDE.read_text(encoding="utf-8")
    assert "printf 'switchboard-hub %s\\n'" in text
    assert c.HOST_KEY_ALIAS == "switchboard-hub"
    assert "sudo loginctl enable-linger USER" in text
    assert not re.search(r"/home/(?!switchboard\b|USER\b)[a-z]", text)
    addresses = re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", text)
    assert all(a.startswith(("192.0.2.", "198.51.100.", "203.0.113.")) for a in addresses), addresses
    assert not re.search("[\\u2013\\u2014]", text)
