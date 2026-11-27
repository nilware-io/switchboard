"""The `remote` config section: machines reached through the dispatcher, SSH options, capture and
summariser settings (docs/SWITCHBOARD.md, "Config").

Section models only check single fields; rules that need several fields or other sections
(`listen.allowNonLoopback`, `stateDir`) are in `check_remote` and `remote_warnings`, called by
`config.parse_config` and `config.config_warnings`.
"""

from __future__ import annotations

import ipaddress
import os
import pwd
import re
import stat
import sys
from collections.abc import Callable, Iterable
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field
from pydantic.alias_generators import to_camel

MACHINE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
RESERVED_MACHINE_NAME = "local"
"""Keys of the hub's own registry sessions start with it (`pending.LOCAL`)."""
HOST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
HOST_IPV6_RE = re.compile(r"[0-9A-Fa-f:]{2,39}")
USER_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}")
REMOTE_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,255}")
SESSION_GLOB_RE = re.compile(r"[A-Za-z0-9_.*?-]{1,64}")
MODEL_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")
SLASH_COMMAND_RE = re.compile(r"[a-z][a-z0-9:-]{0,31}")
AGENT_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]{1,100}")
"""Paths of the agent sockets and key file (a Unix socket path has at most 108 bytes)."""
MAX_RISKY_PATTERNS = 64
MAX_RISKY_PATTERN_CHARS = 200
MAX_KNOWN_TERMS = 200
KNOWN_TERM_RE = re.compile(r"[^\W_](?:[\w .'-]{0,62}[^\W_])?")
"""A known term: letters, digits and `_ .'-` inside, a letter or digit at both ends, 3 to 64
characters (checked in `_known_term`)."""

Transport = Literal["ssh", "local", "agent"]
AGENT_ONLY_KEYS = (
    "name",
    "transport",
    "sessions",
    "allow_usage",
    "allow_send",
    "allow_permission_answers",
    "send_to_any_pane",
)
"""The keys an `agent` machine may set: there is no connection for the hub to open and no
explicit-path mode."""


def _integral(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Input should be a valid integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _matching(pattern: re.Pattern[str], what: str) -> Callable[[str], str]:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"must be {what}")
        return value

    return check


def _host(value: str) -> str:
    """A DNS name or an IP literal, never starting with `-`, without `@`, `%`, spaces or brackets."""
    if HOST_NAME_RE.fullmatch(value) and ".." not in value:
        return value
    if HOST_IPV6_RE.fullmatch(value):
        try:
            ipaddress.IPv6Address(value)
        except ValueError:
            pass
        else:
            return value
    raise ValueError("must be a host name or an IP address (no user@, port, brackets or zone)")


def _remote_path(value: str) -> str:
    if not REMOTE_PATH_RE.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("must be an absolute path of letters, digits and ._/- only")
    return value


def _agent_path(value: str) -> str:
    if not AGENT_PATH_RE.fullmatch(value) or ".." in value.split("/"):
        raise ValueError("must be an absolute path of at most 100 letters, digits and ._/-")
    return value


def _dispatcher(value: str) -> str:
    return value if value == "forced" else _remote_path(value)


def _globs(values: list[str]) -> list[str]:
    if len(set(values)) != len(values):
        raise ValueError("must not contain duplicates")
    return values


Int = Annotated[int, BeforeValidator(_integral)]
MachineName = Annotated[
    str, AfterValidator(_matching(MACHINE_NAME_RE, "lowercase letters, digits and -, starting with a letter"))
]
Host = Annotated[str, AfterValidator(_host)]
UserName = Annotated[str, AfterValidator(_matching(USER_RE, "a user name (lowercase letters, digits, _ and -)"))]
RemotePath = Annotated[str, AfterValidator(_remote_path)]
SessionGlob = Annotated[
    str, AfterValidator(_matching(SESSION_GLOB_RE, "a tmux session name or glob of letters, digits and _.*?-"))
]


class _Section(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )


class MachineConfig(_Section):
    name: MachineName
    transport: Transport = "ssh"
    host: Host | None = None
    user: UserName | None = None
    port: Annotated[Int, Field(ge=1, le=65535)] = 22
    identity_file: RemotePath | None = None
    """Plain characters only: ssh expands `%` tokens and splits `-o` values at spaces."""
    known_hosts_file: RemotePath | None = None
    """None: `<stateDir>/known_hosts`."""
    dispatcher: Annotated[str, AfterValidator(_dispatcher)] = "forced"
    """`forced` (authorized_keys `command=`) or the absolute path of the dispatcher there."""
    sessions: Annotated[list[SessionGlob], Field(min_length=1, max_length=64), AfterValidator(_globs)]
    claude_home: RemotePath | None = None
    """Only for `transport: "local"`."""
    allow_usage: bool = True
    allow_send: bool = False
    allow_permission_answers: bool = False
    send_to_any_pane: bool = False


class SshConfig(_Section):
    bin: Annotated[str, Field(min_length=1)] = "ssh"
    connect_timeout_seconds: Annotated[Int, Field(ge=1, le=30)] = 5
    command_timeout_seconds: Annotated[Int, Field(ge=1, le=60)] = 10


class SummaryConfig(_Section):
    enabled: bool = True
    model: Annotated[str, AfterValidator(_matching(MODEL_RE, "a model name of letters, digits and ._-"))] = "haiku"
    timeout_seconds: Annotated[Int, Field(ge=5, le=120)] = 15


def _risky_pattern(value: str) -> str:
    if not 1 <= len(value) <= MAX_RISKY_PATTERN_CHARS:
        raise ValueError(f"must be 1 to {MAX_RISKY_PATTERN_CHARS} characters")
    try:
        re.compile(value, re.I)
    except re.error as err:
        raise ValueError(f"is not a valid regular expression: {err}") from None
    return value


def _known_term(value: str) -> str:
    if not 3 <= len(value) <= 64 or not KNOWN_TERM_RE.fullmatch(value) or "  " in value:
        raise ValueError("must be 3 to 64 letters, digits and single spaces or _.'- inside")
    return value


SlashCommand = Annotated[
    str, AfterValidator(_matching(SLASH_COMMAND_RE, "a slash command name without the / (a-z, 0-9, : and -)"))
]


class SendConfig(_Section):
    """`remote_send` and `remote_send_confirm` (docs/SWITCHBOARD.md, "remote_send and remote_send_confirm")."""

    confirm_seconds: Annotated[Int, Field(ge=30, le=600)] = 120
    max_chars: Annotated[Int, Field(ge=1, le=2000)] = 2000
    max_pending: Annotated[Int, Field(ge=1, le=5)] = 1
    min_confirm_seconds: Annotated[Int, Field(ge=0, le=30)] = 4
    slash_commands: Annotated[list[SlashCommand], Field(max_length=32), AfterValidator(_globs)] = Field(
        default_factory=list
    )
    risky_patterns: Annotated[
        list[Annotated[str, AfterValidator(_risky_pattern)]], Field(max_length=MAX_RISKY_PATTERNS)
    ] = Field(default_factory=list)
    """Case-insensitive regexes added to the built-in risky list (`send.RISKY`), never replacing it."""
    known_terms: Annotated[
        list[Annotated[str, AfterValidator(_known_term)]], Field(max_length=MAX_KNOWN_TERMS), AfterValidator(_globs)
    ] = Field(default_factory=list)
    """Words of the owner's projects that do not count as echoed screen text (`send.ScreenEcho`)."""
    client_approval: bool = False
    """`destructiveHint: true` on `remote_send_confirm`, so the Claude app asks on screen each time."""


AgentPath = Annotated[str, AfterValidator(_agent_path)]


class AgentsConfig(_Section):
    """Machines that connect out to the hub (docs/SWITCHBOARD.md, "Outbound agents")."""

    socket_path: AgentPath = "/run/switchboard-relay/hub.sock"
    """Where the hub listens for relay connections."""
    relay_user: UserName = "sbrelay"
    """The relay account: only its uid may connect to the socket."""
    heartbeat_seconds: Annotated[Int, Field(ge=5, le=60)] = 20
    command_timeout_seconds: Annotated[Int, Field(ge=1, le=60)] = 10
    authorized_keys_file: AgentPath = "/etc/ssh/switchboard-relay/authorized_keys"
    """Read only by `remote-check`, for its lint."""


class RemoteConfig(_Section):
    machines: Annotated[list[MachineConfig], Field(max_length=32)] = Field(default_factory=list)
    ssh: SshConfig = Field(default_factory=SshConfig)
    capture_lines: Annotated[Int, Field(ge=10, le=500)] = 60
    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    send: SendConfig = Field(default_factory=SendConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)


def _interval(value: int) -> int:
    if value != 0 and not 30 <= value <= 3600:
        raise ValueError("must be 0 (off) or 30 to 3600")
    return value


class WatchConfig(_Section):
    """The notification watcher (docs/SWITCHBOARD.md, "Notification watcher")."""

    interval_seconds: Annotated[Int, AfterValidator(_interval)] = 0
    """0 is off. Needs `notify.url` (checked in `config.parse_config`)."""
    long_turn_minutes: Annotated[Int, Field(ge=0, le=1440)] = 10
    notify_unreachable: bool = False
    include_question: bool = False


def check_remote(
    remote: RemoteConfig, *, allow_non_loopback: bool, state_dir: str, platform: str | None = None
) -> list[str]:
    """Rules across fields and sections, as `path: message` lines (empty when valid)."""
    problems: list[str] = []
    on = platform or sys.platform
    seen: set[str] = set()
    local = 0
    for index, machine in enumerate(remote.machines):
        path = f"remote.machines.{index}"
        if machine.name in seen:
            problems.append(f"{path}.name: duplicate machine name {machine.name}")
        if machine.name == RESERVED_MACHINE_NAME:
            problems.append(f"{path}.name: {RESERVED_MACHINE_NAME} is reserved for the hub's own sessions")
        seen.add(machine.name)
        if machine.transport == "ssh":
            for key, value in (("host", machine.host), ("user", machine.user), ("identityFile", machine.identity_file)):
                if value is None:
                    problems.append(f"{path}.{key}: required for an ssh machine")
            if machine.known_hosts_file is None and not REMOTE_PATH_RE.fullmatch(known_hosts_path(machine, state_dir)):
                problems.append(
                    f"{path}.knownHostsFile: required when stateDir has characters other than letters, digits and ._/-"
                )
            if machine.claude_home is not None:
                problems.append(
                    f"{path}.claudeHome: only for a local machine; on an ssh machine the dispatcher's --claude-home decides"
                )
        elif machine.transport == "agent":
            for key in sorted(machine.model_fields_set - set(AGENT_ONLY_KEYS)):
                problems.append(
                    f"{path}.{to_camel(key)}: not for an agent machine (it connects to the hub; its "
                    "dispatcher's own command line is its policy)"
                )
            if not on.startswith("linux"):
                problems.append(f"{path}.transport: agent machines need a hub on Linux (SO_PEERCRED)")
        else:
            local += 1
            for key, value in (
                ("host", machine.host),
                ("user", machine.user),
                ("identityFile", machine.identity_file),
                ("knownHostsFile", machine.known_hosts_file),
            ):
                if value is not None:
                    problems.append(f"{path}.{key}: only for an ssh machine")
            if machine.dispatcher != "forced":
                problems.append(f"{path}.dispatcher: only for an ssh machine")
            if allow_non_loopback:
                problems.append(f"{path}.transport: a local machine is not allowed with listen.allowNonLoopback")
        if machine.allow_permission_answers and not machine.allow_send:
            problems.append(f"{path}.allowPermissionAnswers: needs allowSend")
        if machine.send_to_any_pane and not machine.allow_send:
            problems.append(f"{path}.sendToAnyPane: needs allowSend")
    if local > 1:
        problems.append("remote.machines: at most one machine may use transport local")
    return problems


def command_timeout_seconds(remote: RemoteConfig, machine: MachineConfig) -> int:
    """Wall clock per dispatcher call (except usage) on this machine's transport."""
    if machine.transport == "agent":
        return remote.agents.command_timeout_seconds
    return remote.ssh.command_timeout_seconds


def known_terms(remote: RemoteConfig) -> list[str]:
    """Terms that do not count as echoed screen text: `send.knownTerms`, the machine names and the
    plain (non-glob) session names, all from the owner's hub config, never from what a machine
    reports."""
    names = [machine.name for machine in remote.machines]
    sessions = [s for machine in remote.machines for s in machine.sessions if "*" not in s and "?" not in s]
    return [*remote.send.known_terms, *names, *sessions]


def known_hosts_path(machine: MachineConfig, state_dir: str) -> str:
    return machine.known_hosts_file or os.path.join(state_dir, "known_hosts")


def _passwd_homes() -> list[str]:
    return [entry.pw_dir for entry in pwd.getpwall()]


def _identity_warnings(machine: MachineConfig, *, uid: int, home: str, other_homes: Iterable[str]) -> list[str]:
    path = machine.identity_file
    if path is None:
        return []
    where = f"remote machine {machine.name}: identityFile {path}"
    try:
        info = os.lstat(path)
    except OSError:
        return [f"{where} does not exist yet"]
    warnings: list[str] = []
    if not stat.S_ISREG(info.st_mode):
        warnings.append(f"{where} is not a regular file")
    if info.st_uid != uid:
        warnings.append(f"{where} is not owned by the server's user")
    if info.st_mode & 0o077:
        warnings.append(f"{where} is readable by others; use mode 600")
    real = os.path.realpath(path)
    own = os.path.realpath(home)
    for other in other_homes:
        other_real = os.path.realpath(other)
        if other_real in ("/", own) or own.startswith(other_real + os.sep):
            continue
        if real.startswith(other_real + os.sep):
            warnings.append(f"{where} lies in another user's home; use a dedicated key of the server's user")
            break
    return warnings


def remote_warnings(
    remote: RemoteConfig,
    *,
    uid: int | None = None,
    home: str | None = None,
    other_homes: Iterable[str] | None = None,
) -> list[str]:
    """Settings that are valid but weaken isolation, as startup warnings. `other_homes` (default: the
    home directories of the passwd database) are where a key of another user would live."""
    warnings: list[str] = []
    if "agents" in remote.model_fields_set and not any(m.transport == "agent" for m in remote.machines):
        warnings.append("remote.agents is set but no machine uses transport agent; it is ignored")
    for machine in remote.machines:
        if "*" in machine.sessions:
            warnings.append(
                f"remote machine {machine.name}: sessions allows every tmux session (*); list the session names instead"
            )
        if machine.transport == "local":
            warnings.append(
                f"remote machine {machine.name}: transport local runs the dispatcher as the server's own user "
                "(weaker isolation than a dedicated user reaching it over SSH)"
            )
        if machine.dispatcher != "forced":
            warnings.append(
                f"remote machine {machine.name}: dispatcher is an explicit path, so its key opens a shell there; "
                "a forced command in authorized_keys is the only protection against a compromised server"
            )
        if machine.send_to_any_pane:
            warnings.append(f"remote machine {machine.name}: sendToAnyPane allows typing into plain shells")
        if machine.allow_permission_answers:
            warnings.append(
                f"remote machine {machine.name}: allowPermissionAnswers lets a voice send answer Claude Code's "
                "permission questions (a choice number, always risky)"
            )
        if machine.transport == "ssh":
            warnings += _identity_warnings(
                machine,
                uid=uid if uid is not None else os.getuid(),
                home=home if home is not None else os.path.expanduser("~"),
                other_homes=list(other_homes) if other_homes is not None else _passwd_homes(),
            )
    return warnings
