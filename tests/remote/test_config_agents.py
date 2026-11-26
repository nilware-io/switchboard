"""Config of agent machines (`transport: "agent"`) and the `remote.agents` section."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.helpers import config_data
from tests.remote.agent_helpers import agent_machine
from tests.remote.helpers import machine
from voice_coding_mcp.config import ConfigError, parse_config
from voice_coding_mcp.remote.config import RemoteConfig, check_remote, command_timeout_seconds, remote_warnings


def parse(tmp: Path, remote: dict[str, Any]) -> Any:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    data = config_data("https://vcm.example.com", str(tmp / "state"), str(projects), remote=remote)
    return parse_config(data, home_dir="/nonexistent-home-dir")


def error(tmp: Path, remote: dict[str, Any]) -> str:
    with pytest.raises(ConfigError) as caught:
        parse(tmp, remote)
    return str(caught.value)


def test_an_agent_machine_and_the_defaults(tmp_path: Path) -> None:
    config = parse(tmp_path, {"machines": [agent_machine("laptop", allowSend=True, allowPermissionAnswers=True)]})
    laptop = config.remote.machines[0]
    assert (laptop.transport, laptop.allow_send, laptop.allow_permission_answers) == ("agent", True, True)
    agents = config.remote.agents
    assert agents.socket_path == "/run/switchboard-relay/hub.sock"
    assert agents.relay_user == "sbrelay"
    assert (agents.heartbeat_seconds, agents.command_timeout_seconds) == (20, 10)
    assert agents.authorized_keys_file == "/etc/ssh/switchboard-relay/authorized_keys"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("host", "hub.example.org"),
        ("user", "owner"),
        ("port", 22),
        ("port", 2222),
        ("identityFile", "/home/USER/.ssh/key"),
        ("knownHostsFile", "/home/USER/.ssh/known"),
        ("dispatcher", "forced"),
        ("dispatcher", "/usr/local/bin/switchboard-remote"),
        ("claudeHome", "/home/USER/.claude"),
    ],
)
def test_keys_an_agent_machine_may_not_have(tmp_path: Path, key: str, value: Any) -> None:
    message = error(tmp_path, {"machines": [agent_machine("laptop", **{key: value})]})
    assert f"remote.machines.0.{key}: not for an agent machine" in message


def test_rules_shared_with_other_transports(tmp_path: Path) -> None:
    message = error(
        tmp_path,
        {
            "machines": [
                agent_machine("laptop", allowPermissionAnswers=True),
                agent_machine("laptop"),
                agent_machine("local"),
            ]
        },
    )
    assert "remote.machines.0.allowPermissionAnswers: needs allowSend" in message
    assert "duplicate machine name laptop" in message
    assert "local is reserved" in message


def test_agent_machines_mix_with_ssh_and_local(tmp_path: Path) -> None:
    config = parse(
        tmp_path,
        {
            "machines": [
                machine("dev", transport="local", host=None, user=None, identityFile=None),
                machine("buildbox", identity="/home/USER/.ssh/k", host="example-host"),
                agent_machine("laptop"),
            ]
        },
    )
    assert [m.transport for m in config.remote.machines] == ["local", "ssh", "agent"]


def test_agent_machines_need_linux() -> None:
    remote = RemoteConfig.model_validate({"machines": [agent_machine("laptop")]})
    assert check_remote(remote, allow_non_loopback=False, state_dir="/x", platform="linux") == []
    problems = check_remote(remote, allow_non_loopback=False, state_dir="/x", platform="darwin")
    assert problems == ["remote.machines.0.transport: agent machines need a hub on Linux (SO_PEERCRED)"]


@pytest.mark.parametrize(
    ("agents", "where"),
    [
        ({"socketPath": "relative.sock"}, "socketPath"),
        ({"socketPath": "/run/" + "a" * 100}, "socketPath"),
        ({"socketPath": "/run/../tmp/x.sock"}, "socketPath"),
        ({"relayUser": "Root"}, "relayUser"),
        ({"heartbeatSeconds": 4}, "heartbeatSeconds"),
        ({"heartbeatSeconds": 61}, "heartbeatSeconds"),
        ({"commandTimeoutSeconds": 0}, "commandTimeoutSeconds"),
        ({"commandTimeoutSeconds": 61}, "commandTimeoutSeconds"),
        ({"authorizedKeysFile": "keys"}, "authorizedKeysFile"),
        ({"bogus": 1}, "bogus"),
    ],
)
def test_agents_field_rules(tmp_path: Path, agents: dict[str, Any], where: str) -> None:
    assert where in error(tmp_path, {"machines": [agent_machine("laptop")], "agents": agents})


def test_agents_without_an_agent_machine_is_a_warning(tmp_path: Path) -> None:
    config = parse(tmp_path, {"machines": [], "agents": {"heartbeatSeconds": 30}})
    assert "remote.agents is set but no machine uses transport agent; it is ignored" in remote_warnings(
        config.remote, other_homes=[]
    )
    config = parse(tmp_path, {"machines": [agent_machine("laptop")], "agents": {"heartbeatSeconds": 30}})
    assert remote_warnings(config.remote, other_homes=[]) == []


def test_the_command_timeout_follows_the_transport(tmp_path: Path) -> None:
    config = parse(
        tmp_path,
        {
            "machines": [machine("dev", identity="/home/USER/.ssh/k"), agent_machine("laptop")],
            "ssh": {"commandTimeoutSeconds": 7},
            "agents": {"commandTimeoutSeconds": 12},
        },
    )
    dev, laptop = config.remote.machines
    assert command_timeout_seconds(config.remote, dev) == 7
    assert command_timeout_seconds(config.remote, laptop) == 12
