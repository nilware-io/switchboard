"""The command string, the ssh argv and environment, and strict response parsing."""

from __future__ import annotations

import json
import shlex
from typing import Any

import pytest

from voice_coding_mcp.remote.config import MachineConfig, SshConfig
from voice_coding_mcp.remote.protocol import (
    ARG_RE,
    BadRequestError,
    CaptureResponse,
    ListResponse,
    ResponseError,
    VersionResponse,
    build_command,
    parse_response,
)
from voice_coding_mcp.remote.ssh import build_ssh_argv, ssh_env

HOSTILE = [
    "",
    "-o",
    "--sessions",
    "a b",
    "a;id",
    "$(id)",
    "`id`",
    "a|b",
    "a&b",
    "a>b",
    "'",
    '"',
    "\\",
    "a\nb",
    "~root",
    "*",
    "?",
    "[a]",
    "{a,b}",
    "a#b",
    "é",
    "x" * 65,
    "%1\x00",
]


def test_command_string_for_forced_and_explicit_dispatchers() -> None:
    assert build_command("forced", "list", []) == "switchboard-remote list"
    assert build_command("forced", "capture", ["%3", "60"]) == "switchboard-remote capture %3 60"
    assert (
        build_command("/home/USER/bin/switchboard-remote", "version", []) == "/home/USER/bin/switchboard-remote version"
    )


@pytest.mark.parametrize("arg", HOSTILE)
def test_hostile_arguments_never_reach_a_command_string(arg: str) -> None:
    with pytest.raises(BadRequestError):
        build_command("forced", "capture", [arg, "10"])


@pytest.mark.parametrize("verb", ["send", "usage", "LIST", "list;id", "", "list "])
def test_only_known_verbs(verb: str) -> None:
    with pytest.raises(BadRequestError):
        build_command("forced", verb, [])


def test_every_accepted_command_splits_back_to_its_tokens_in_a_shell_neutral_charset() -> None:
    samples = ["%1", "%999999", "60", "abc", "a.b-c_d", "..", "%", "0"]
    for arg in samples:
        assert ARG_RE.fullmatch(arg)
        command = build_command("forced", "capture", [arg, "10"])
        assert shlex.split(command) == ["switchboard-remote", "capture", arg, "10"]
        assert all(ch.isalnum() or ch in "%_.- " for ch in command)


def ssh_machine(**keys: Any) -> MachineConfig:
    base: dict[str, Any] = {
        "name": "dev",
        "host": "localhost",
        "user": "owner",
        "identity_file": "/home/switchboard/.ssh/switchboard_dev",
        "sessions": ["fpga"],
    }
    return MachineConfig(**{**base, **keys})


def test_ssh_argv_is_fully_hardened() -> None:
    argv = build_ssh_argv(
        SshConfig(bin="/usr/bin/ssh", connect_timeout_seconds=7),
        ssh_machine(port=2222),
        "/state/known_hosts",
        "switchboard-remote list",
    )
    assert argv == [
        "/usr/bin/ssh",
        "-F",
        "/dev/null",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ForwardX11=no",
        "-o",
        "ClearAllForwardings=yes",
        "-o",
        "PermitLocalCommand=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UserKnownHostsFile=/state/known_hosts",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "HashKnownHosts=no",
        "-o",
        "CheckHostIP=no",
        "-o",
        "ConnectTimeout=7",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        "-o",
        "LogLevel=ERROR",
        "-i",
        "/home/switchboard/.ssh/switchboard_dev",
        "-p",
        "2222",
        "-l",
        "owner",
        "--",
        "localhost",
        "switchboard-remote list",
    ]


def test_ssh_argv_needs_an_ssh_machine() -> None:
    with pytest.raises(ValueError, match="not an ssh machine"):
        build_ssh_argv(SshConfig(), MachineConfig(name="x", transport="local", sessions=["a"]), "/k", "c")


def test_ssh_env_is_built_from_scratch() -> None:
    env = ssh_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/switchboard",
            "LANG": "C.UTF-8",
            "SSH_AUTH_SOCK": "/tmp/agent",
            "SSH_ASKPASS": "/x",
            "LD_PRELOAD": "x.so",
        }
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/home/switchboard", "LANG": "C.UTF-8"}


def response(**data: Any) -> bytes:
    return json.dumps({"protocol": 1, "ok": True, **data}).encode()


def test_parse_valid_responses() -> None:
    listed = parse_response(
        "list",
        response(
            panes=[{"session": "fpga", "paneId": "%1", "panePid": 5, "claude": {"status": "busy", "future": 1}}],
            newField=True,
        ),
    )
    assert isinstance(listed, ListResponse)
    assert listed.panes[0].claude is not None
    assert listed.panes[0].claude.status == "busy"
    captured = parse_response("capture", response(text="x" * 70000, paneId="%1", session="fpga"))
    assert isinstance(captured, CaptureResponse)
    assert len(captured.text) == 64 * 1024
    version = parse_response("version", response(version="0.1.0", verbs=["list"], allowSend=True))
    assert isinstance(version, VersionResponse)
    assert version.allow_send is True


def test_names_from_the_machine_are_capped() -> None:
    listed = parse_response(
        "list", response(panes=[{"session": "s" * 500, "paneId": "%1", "panePid": 1, "windowName": "w" * 500}])
    )
    assert isinstance(listed, ListResponse)
    assert len(listed.panes[0].session) == 64
    assert len(listed.panes[0].window_name) == 64


def test_refusals_return_the_dispatcher_code() -> None:
    assert parse_response("list", b'{"protocol":1,"ok":false,"error":"gone"}') == "gone"
    assert parse_response("list", b'{"protocol":1,"ok":false,"error":"evil text here"}') == "other"


@pytest.mark.parametrize(
    ("stdout", "code"),
    [
        (b"", "dispatcher_missing"),
        (b"bash: switchboard-remote: command not found", "dispatcher_missing"),
        (b"{not json", "bad_response"),
        (b"[]", "bad_response"),
        (b'{"protocol":2,"ok":true}', "bad_response"),
        (b'{"protocol":1}', "bad_response"),
        (b'{"protocol":1,"ok":true,"panes":"x"}', "bad_response"),
        (b'{"protocol":1,"ok":true,"panes":[{"session":"a","paneId":"1","panePid":1}]}', "bad_response"),
        (b'{"protocol":1,"ok":true,"panes":[{"session":"a","paneId":"%1","panePid":-1}]}', "bad_response"),
        (b"{" + b" " * (4 * 1024 * 1024) + b"}", "bad_response"),
    ],
)
def test_malformed_responses(stdout: bytes, code: str) -> None:
    with pytest.raises(ResponseError) as caught:
        parse_response("list", stdout)
    assert caught.value.code == code


def test_too_many_panes_is_a_bad_response() -> None:
    panes = [{"session": "a", "paneId": f"%{i}", "panePid": i} for i in range(513)]
    with pytest.raises(ResponseError):
        parse_response("list", response(panes=panes))
