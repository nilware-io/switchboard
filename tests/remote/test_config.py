"""The `remote` config section: field rules, cross-field rules and startup warnings."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from tests.helpers import config_data
from tests.remote.helpers import machine
from voice_coding_mcp.config import ConfigError, config_warnings, parse_config
from voice_coding_mcp.remote.config import known_hosts_path, known_terms, remote_warnings

REPO = Path(__file__).resolve().parents[2]


def parse(tmp: Path, remote: dict[str, Any], **sections: Any) -> Any:
    projects = tmp / "projects"
    projects.mkdir(exist_ok=True)
    data = config_data("https://vcm.example.com", str(tmp / "state"), str(projects), remote=remote, **sections)
    return parse_config(data, home_dir="/nonexistent-home-dir")


def error(tmp: Path, remote: dict[str, Any], **sections: Any) -> str:
    with pytest.raises(ConfigError) as caught:
        parse(tmp, remote, **sections)
    return str(caught.value)


def test_defaults(tmp_path: Path) -> None:
    config = parse(tmp_path, {})
    remote = config.remote
    assert remote.machines == []
    assert (remote.ssh.bin, remote.ssh.connect_timeout_seconds, remote.ssh.command_timeout_seconds) == ("ssh", 5, 10)
    assert remote.capture_lines == 60
    assert (remote.summary.enabled, remote.summary.model, remote.summary.timeout_seconds) == (True, "haiku", 15)
    dev = parse(tmp_path, {"machines": [machine("dev")]}).remote.machines[0]
    assert (dev.transport, dev.port, dev.dispatcher, dev.claude_home) == ("ssh", 22, "forced", None)
    assert (dev.allow_usage, dev.allow_send, dev.allow_permission_answers, dev.send_to_any_pane) == (
        True,
        False,
        False,
        False,
    )
    assert known_hosts_path(dev, str(tmp_path / "state")) == str(tmp_path / "state" / "known_hosts")


def test_the_example_config_is_valid_and_lists_dev_first(tmp_path: Path) -> None:
    data = json.loads((REPO / "config.example.json").read_text(encoding="utf-8"))
    projects = tmp_path / "projects"
    projects.mkdir()
    data["projectRoots"] = [str(projects)]
    data["stateDir"] = str(tmp_path / "state")
    config = parse_config(data, home_dir="/nonexistent-home-dir")
    names = [m.name for m in config.remote.machines]
    assert names[0] == "dev"
    assert config.remote.machines[0].host == "localhost"
    assert len(names) >= 2


@pytest.mark.parametrize(
    ("keys", "message"),
    [
        ({"name": "Dev"}, "remote.machines.0.name"),
        ({"name": "-dev"}, "remote.machines.0.name"),
        ({"name": "d" * 33}, "remote.machines.0.name"),
        ({"transport": "telnet"}, "remote.machines.0.transport"),
        ({"host": "-oProxyCommand=x"}, "remote.machines.0.host"),
        ({"host": "user@host"}, "remote.machines.0.host"),
        ({"host": "host name"}, "remote.machines.0.host"),
        ({"host": "fe80::1%eth0"}, "remote.machines.0.host"),
        ({"host": "[::1]"}, "remote.machines.0.host"),
        ({"host": "a..b"}, "remote.machines.0.host"),
        ({"host": "::zz"}, "remote.machines.0.host"),
        ({"user": "Root"}, "remote.machines.0.user"),
        ({"user": "-l"}, "remote.machines.0.user"),
        ({"port": 0}, "remote.machines.0.port"),
        ({"port": 65536}, "remote.machines.0.port"),
        ({"port": True}, "remote.machines.0.port"),
        ({"identityFile": "relative/key"}, "remote.machines.0.identityFile"),
        ({"identityFile": "/home/USER/.ssh/key with space"}, "remote.machines.0.identityFile"),
        ({"identityFile": "/home/%u/key"}, "remote.machines.0.identityFile"),
        ({"knownHostsFile": "/a/../b"}, "remote.machines.0.knownHostsFile"),
        ({"dispatcher": "switchboard-remote"}, "remote.machines.0.dispatcher"),
        ({"dispatcher": "/usr/bin/x;id"}, "remote.machines.0.dispatcher"),
        ({"sessions": []}, "remote.machines.0.sessions"),
        ({"sessions": ["a:b"]}, "remote.machines.0.sessions.0"),
        ({"sessions": ["[ab]"]}, "remote.machines.0.sessions.0"),
        ({"sessions": ["a", "a"]}, "remote.machines.0.sessions"),
        ({"sessions": [f"s{i}" for i in range(65)]}, "remote.machines.0.sessions"),
        ({"claudeHome": "/x"}, "claudeHome: only for a local machine"),
        ({"allowPermissionAnswers": True}, "allowPermissionAnswers: needs allowSend"),
        ({"sendToAnyPane": True}, "sendToAnyPane: needs allowSend"),
        ({"unknownKey": 1}, "remote.machines.0.unknownKey"),
    ],
)
def test_machine_field_rules(tmp_path: Path, keys: dict[str, Any], message: str) -> None:
    assert message in error(tmp_path, {"machines": [{**machine("dev"), **keys}]})


@pytest.mark.parametrize("missing", ["host", "user", "identityFile"])
def test_ssh_machines_need_host_user_and_key(tmp_path: Path, missing: str) -> None:
    data = machine("dev")
    del data[missing]
    assert f"remote.machines.0.{missing}: required for an ssh machine" in error(tmp_path, {"machines": [data]})


def test_valid_hosts(tmp_path: Path) -> None:
    for host in ["localhost", "example-host", "10.0.0.5", "::1", "fd00::5", "host.tail1234.ts.net"]:
        assert parse(tmp_path, {"machines": [machine("dev", host=host)]}).remote.machines[0].host == host


def test_cross_machine_rules(tmp_path: Path) -> None:
    assert "duplicate machine name dev" in error(tmp_path, {"machines": [machine("dev"), machine("dev")]})
    # The hub's own registry sessions are keyed `local|...`: a machine of that name would mix with them.
    assert "remote.machines.0.name: local is reserved" in error(tmp_path, {"machines": [machine("local")]})
    local = {"name": "dev", "transport": "local", "sessions": ["fpga"]}
    two_local = [local, {**local, "name": "other"}]
    assert "at most one machine may use transport local" in error(tmp_path, {"machines": two_local})
    assert "not allowed with listen.allowNonLoopback" in error(
        tmp_path, {"machines": [local]}, listen={"host": "0.0.0.0", "allowNonLoopback": True}
    )
    for key, value in (("host", "localhost"), ("user", "owner"), ("identityFile", "/k"), ("knownHostsFile", "/k")):
        assert f"remote.machines.0.{key}: only for an ssh machine" in error(
            tmp_path, {"machines": [{**local, key: value}]}
        )
    assert "dispatcher: only for an ssh machine" in error(tmp_path, {"machines": [{**local, "dispatcher": "/x"}]})
    parsed = parse(tmp_path, {"machines": [{**local, "claudeHome": "/home/USER/.claude"}]}).remote.machines[0]
    assert parsed.claude_home == "/home/USER/.claude"


def test_a_state_dir_ssh_cannot_quote_needs_an_explicit_known_hosts_file(tmp_path: Path) -> None:
    odd = tmp_path / "state dir"
    projects = tmp_path / "projects"
    projects.mkdir()
    data = config_data("https://vcm.example.com", str(odd), str(projects), remote={"machines": [machine("dev")]})
    with pytest.raises(ConfigError, match="knownHostsFile: required"):
        parse_config(data, home_dir="/nonexistent-home-dir")
    data["remote"]["machines"][0]["knownHostsFile"] = "/home/USER/.ssh/known_hosts_switchboard"
    parse_config(data, home_dir="/nonexistent-home-dir")


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"ssh": {"connectTimeoutSeconds": 0}}, "remote.ssh.connectTimeoutSeconds"),
        ({"ssh": {"connectTimeoutSeconds": 31}}, "remote.ssh.connectTimeoutSeconds"),
        ({"ssh": {"commandTimeoutSeconds": 61}}, "remote.ssh.commandTimeoutSeconds"),
        ({"ssh": {"bin": ""}}, "remote.ssh.bin"),
        ({"captureLines": 9}, "remote.captureLines"),
        ({"captureLines": 501}, "remote.captureLines"),
        ({"summary": {"model": "haiku; rm"}}, "remote.summary.model"),
        ({"summary": {"timeoutSeconds": 4}}, "remote.summary.timeoutSeconds"),
        ({"summary": {"timeoutSeconds": 121}}, "remote.summary.timeoutSeconds"),
        ({"send": {"confirmSeconds": 29}}, "remote.send.confirmSeconds"),
        ({"send": {"confirmSeconds": 601}}, "remote.send.confirmSeconds"),
        ({"send": {"maxChars": 0}}, "remote.send.maxChars"),
        ({"send": {"maxChars": 2001}}, "remote.send.maxChars"),
        ({"send": {"maxPending": 6}}, "remote.send.maxPending"),
        ({"send": {"minConfirmSeconds": 31}}, "remote.send.minConfirmSeconds"),
        ({"send": {"slashCommands": ["/compact"]}}, "remote.send.slashCommands"),
        ({"send": {"slashCommands": ["compact", "compact"]}}, "remote.send.slashCommands"),
        ({"send": {"riskyPatterns": ["(unclosed"]}}, "remote.send.riskyPatterns"),
        ({"send": {"riskyPatterns": [""]}}, "remote.send.riskyPatterns"),
        ({"send": {"knownTerms": ["ab"]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": ["x" * 65]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": ["-dash"]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": ["two  spaces"]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": ["bad\u202eterm"]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": ["fpga", "fpga"]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": [f"term{i}" for i in range(201)]}}, "remote.send.knownTerms"),
        ({"send": {"knownTerms": "GreenArrays"}}, "remote.send.knownTerms"),
        ({"send": {"clientApproval": "yes"}}, "remote.send.clientApproval"),
        ({"send": {"unknown": 1}}, "remote.send.unknown"),
    ],
)
def test_other_remote_keys(tmp_path: Path, section: dict[str, Any], message: str) -> None:
    assert message in error(tmp_path, section)


def test_known_terms_come_from_the_hub_config_only(tmp_path: Path) -> None:
    terms = ["GreenArrays", "polly2-rtl", "voice coding", "O'Brien", "sh4"]
    config = parse(tmp_path, {"machines": [machine("dev")], "send": {"knownTerms": terms}})
    assert config.remote.send.known_terms == terms
    assert known_terms(config.remote) == [*terms, "dev", "fpga", "notes"], "globs such as work-* are left out"
    assert parse(tmp_path, {}).remote.send.known_terms == []


def test_warnings(tmp_path: Path) -> None:
    key = tmp_path / "hub-key"
    key.write_text("k", encoding="utf-8")
    os.chmod(key, 0o644)
    machines = [
        machine(
            "dev", identity=str(key), sessions=["*"], allowSend=True, sendToAnyPane=True, allowPermissionAnswers=True
        ),
        machine("explicit", identity=str(tmp_path / "missing"), dispatcher="/home/USER/bin/switchboard-remote"),
        {"name": "same-user", "transport": "local", "sessions": ["fpga"]},
    ]
    config = parse(tmp_path, {"machines": machines})
    warnings = config_warnings(config)
    joined = "\n".join(warnings)
    assert "remote machine dev: sessions allows every tmux session (*)" in joined
    assert "remote machine dev: sendToAnyPane allows typing into plain shells" in joined
    assert "remote machine dev: allowPermissionAnswers lets a voice send answer" in joined
    assert f"remote machine dev: identityFile {key} is readable by others; use mode 600" in joined
    assert "remote machine explicit: dispatcher is an explicit path" in joined
    assert f"identityFile {tmp_path / 'missing'} does not exist yet" in joined
    assert "remote machine same-user: transport local runs the dispatcher as the server's own user" in joined


def test_identity_file_checks(tmp_path: Path) -> None:
    homes = tmp_path / "home"
    own = homes / "switchboard"
    other = homes / "owner" / ".ssh"
    own.mkdir(parents=True)
    other.mkdir(parents=True)
    good = own / "key"
    foreign = other / "id_ed25519"
    for path in (good, foreign):
        path.write_text("k", encoding="utf-8")
        os.chmod(path, 0o600)
    config = parse(tmp_path, {"machines": [machine("dev", identity=str(good)), machine("b", identity=str(foreign))]})
    others = [str(homes / "owner"), "/", str(homes)]
    warnings = remote_warnings(config.remote, uid=os.getuid(), home=str(own), other_homes=others)
    assert warnings == [
        f"remote machine b: identityFile {foreign} lies in another user's home; use a dedicated key of the server's user"
    ]
    assert any(
        "not owned" in w for w in remote_warnings(config.remote, uid=os.getuid() + 1, home=str(own), other_homes=[])
    )
    (own / "dir-key").mkdir()
    config = parse(tmp_path, {"machines": [machine("dev", identity=str(own / "dir-key"))]})
    assert any(
        "not a regular file" in w
        for w in remote_warnings(config.remote, uid=os.getuid(), home=str(own), other_homes=[])
    )


# --- watch ----------------------------------------------------------------------------------------


def test_watch_defaults_off(tmp_path: Path) -> None:
    watch = parse(tmp_path, {}).watch
    assert (watch.interval_seconds, watch.long_turn_minutes, watch.notify_unreachable, watch.include_question) == (
        0,
        10,
        False,
        False,
    )


def test_watch_needs_notify_url(tmp_path: Path) -> None:
    assert "watch.intervalSeconds: the watcher needs notify.url" in error(tmp_path, {}, watch={"intervalSeconds": 60})
    on = parse(tmp_path, {}, watch={"intervalSeconds": 60}, notify={"url": "https://ntfy.example.com/topic"})
    assert on.watch.interval_seconds == 60


@pytest.mark.parametrize(
    ("watch", "where"),
    [
        ({"intervalSeconds": 10}, "watch.intervalSeconds"),
        ({"intervalSeconds": 3601}, "watch.intervalSeconds"),
        ({"longTurnMinutes": -1}, "watch.longTurnMinutes"),
        ({"longTurnMinutes": 1441}, "watch.longTurnMinutes"),
        ({"includeQuestion": "yes"}, "watch.includeQuestion"),
        ({"includeResult": True}, "watch.includeResult"),
    ],
)
def test_watch_field_rules(tmp_path: Path, watch: dict[str, Any], where: str) -> None:
    assert where in error(tmp_path, {}, watch=watch, notify={"url": "https://ntfy.example.com/topic"})
