"""Load and validate the config file: JSON schema, defaults and checks.

The file uses camelCase keys (`publicUrl`, `tasks.syncWaitSeconds`); Python attributes are
snake_case (`config.public_url`, `config.tasks.sync_wait_seconds`). `parse_config` accepts only
the camelCase keys, unknown keys are errors. Section models are frozen pydantic models; tests build
variants with `model_copy(update={...})` or by keyword (snake_case names are accepted in code).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, cast, get_args

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from pydantic.alias_generators import to_camel

from .json_file import loads_json
from .projects import is_inside
from .remote.config import RemoteConfig, WatchConfig, check_remote, remote_warnings
from .urls import is_loopback_ip, parse_url

DEFAULT_CONFIG_PATH = "/etc/voice-coding-mcp/config.json"

PermissionMode = Literal["auto", "acceptEdits", "dontAsk", "plan", "manual"]
SettingSource = Literal["user", "project", "local"]
EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]
AccessMode = Literal["isolated", "desktop"]

PERMISSION_MODES: tuple[PermissionMode, ...] = get_args(PermissionMode)

FORBIDDEN_EXTRA_ARGS: tuple[str, ...] = (
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--permission-mode",
    "--mcp-config",
    "--strict-mcp-config",
    "--setting-sources",
    "--bare",
    # Would add settings, permission rules, directories or prompts around the server's own.
    "--settings",
    "--allowedTools",
    "--allowed-tools",
    # Would widen the tool set of ask_session_readonly and send_message_to_session runs.
    "--tools",
    "--add-dir",
    "--permission-prompts",
    "--system-prompt",
    "--append-system-prompt",
    # The server names every run (the relay's name is what the reply watcher looks for).
    "--name",
    "-n",
)
"""Flags the server controls itself or that would weaken the sandbox."""


class ConfigError(Exception):
    """Invalid or unreadable config. The message is shown to the operator as is."""


# --- field types -----------------------------------------------------------------------------


def _integral(value: object) -> object:
    """JSON `25.0` counts as an integer; bools are never numbers."""
    if isinstance(value, bool):
        raise ValueError("Input should be a valid integer")
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _not_bool(value: object) -> object:
    if isinstance(value, bool):
        raise ValueError("Input should be a valid number")
    return value


def _absolute(value: str) -> str:
    if not os.path.isabs(value):
        raise ValueError("must be an absolute path")
    return value


def _http_url(*schemes: str) -> AfterValidator:
    names = " or ".join(schemes)

    def check(value: str) -> str:
        parsed = parse_url(value)
        if parsed is None or parsed.scheme not in schemes:
            raise ValueError(f"must be a valid {names} URL")
        return value

    return AfterValidator(check)


LOOPBACK_REDIRECT_HOSTS = frozenset({"127.0.0.1", "[::1]", "localhost"})
"""Hosts (as `ParsedUrl.hostname`) where a redirect URI may use plain http."""


def _redirect_uri(value: str) -> str:
    """https, or plain http only on a loopback host (127.0.0.1, ::1, localhost)."""
    parsed = parse_url(value)
    if parsed is None or parsed.scheme not in ("https", "http"):
        raise ValueError("must be a valid https URL")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_REDIRECT_HOSTS:
        raise ValueError(f"must use https (plain http is allowed only for 127.0.0.1, [::1] and localhost): {value}")
    return value


def _extra_arg(arg: str) -> str:
    flag = arg.split("=", 1)[0]
    if flag in FORBIDDEN_EXTRA_ARGS:
        raise ValueError(f"{flag} is not allowed (the server sets it or it weakens the sandbox)")
    return arg


def _no_duplicates(values: list[SettingSource]) -> list[SettingSource]:
    if len(set(values)) != len(values):
        raise ValueError("must not contain duplicates")
    return values


Int = Annotated[int, BeforeValidator(_integral)]
Number = Annotated[float, BeforeValidator(_not_bool)]
NonEmptyStr = Annotated[str, Field(min_length=1)]
AbsolutePath = Annotated[str, Field(min_length=1), AfterValidator(_absolute)]
HttpUrl = Annotated[str, _http_url("https", "http")]
HttpsUrl = Annotated[str, _http_url("https")]
RedirectUri = Annotated[str, AfterValidator(_redirect_uri)]
ExtraArg = Annotated[str, AfterValidator(_extra_arg)]


class _Section(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )


class ListenConfig(_Section):
    host: NonEmptyStr = "127.0.0.1"
    port: Annotated[Int, Field(ge=1, le=65535)] = 8787
    allow_non_loopback: bool = False
    """Explicit opt-in to listen on a non-loopback address."""


class ClaudeConfig(_Section):
    bin: NonEmptyStr = "claude"
    model: NonEmptyStr | None = None
    effort: EffortLevel | None = None
    max_turns: Annotated[Int, Field(gt=0)] | None = None
    permission_mode: PermissionMode = "auto"
    access: AccessMode = "isolated"
    """What task runs reach: `isolated` (only `mcpServers`) or `desktop` (the owner's MCP servers,
    connectors and plugins, minus the voice server); see `tasks/access.py`."""
    # Not "local" by default: a third-party repository can commit .claude/settings.local.json too.
    setting_sources: Annotated[list[SettingSource], AfterValidator(_no_duplicates)] = Field(
        default_factory=lambda: cast(list[SettingSource], ["user"])
    )
    config_dir: AbsolutePath | None = None
    mcp_servers: dict[str, dict[str, Any]] = Field(default_factory=dict)
    extra_args: list[ExtraArg] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("permission_mode", mode="before")
    @classmethod
    def _check_permission_mode(cls, value: object) -> object:
        if not isinstance(value, str) or value not in PERMISSION_MODES:
            raise ValueError(f"must be one of {', '.join(PERMISSION_MODES)} (bypassPermissions is never allowed)")
        return value


class TasksConfig(_Section):
    sync_wait_seconds: Annotated[Int, Field(ge=1, le=600)] = 25
    timeout_minutes: Annotated[Int, Field(ge=1)] = 15
    max_concurrent: Annotated[Int, Field(ge=1)] = 2
    max_queued: Annotated[Int, Field(ge=0)] = 10
    resume_within_hours: Annotated[Number, Field(ge=0)] = 12
    keep_days: Annotated[Int, Field(ge=1)] = 14


class SessionsConfig(_Section):
    days: Annotated[Number, Field(gt=0)] = 7
    """list_sessions shows sessions active within this many days."""
    reply_wait_minutes: Annotated[Number, Field(gt=0)] = 10
    """How long send_message_to_session waits for the target session's reply."""


class RemoteControlConfig(_Section):
    max_sessions: Annotated[Int, Field(ge=0)] = 3
    """At most this many Remote Control sessions opened by the server at once (tasks reopened with
    resume_task_in_remote_control and tasks started with run_task remote_control); 0 turns both off
    (run_task then runs headless)."""
    handoff_to_desktop: bool = False
    """macOS only: after a voice session was stopped (cancel_task or the idle close), open it in the
    Claude desktop app (`claude://resume?session=<id>`), which keeps it in its own session list."""
    idle_close_minutes: Annotated[Int, Field(ge=0)] = 0
    """Stop the server's Remote Control sessions that have been idle (or disconnected from the app)
    this long; 0 (the default) turns the idle close off. Opt-in, meant to pair with
    `handoff_to_desktop`, which then keeps each closed session in the desktop app's list."""


DEFAULT_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
)
DEFAULT_CIMD_CLIENT_IDS: tuple[str, ...] = ("https://claude.ai/oauth/mcp-oauth-client-metadata",)


class OAuthConfig(_Section):
    access_token_hours: Annotated[Number, Field(gt=0)] = 24
    refresh_token_days: Annotated[Number, Field(gt=0)] = 90
    pairing_minutes: Annotated[Number, Field(gt=0)] = 10
    redirect_uris: list[RedirectUri] = Field(default_factory=lambda: list(DEFAULT_REDIRECT_URIS))
    cimd_client_ids: list[HttpsUrl] = Field(default_factory=lambda: list(DEFAULT_CIMD_CLIENT_IDS))
    allow_loopback_redirects: bool = False


class NotifyConfig(_Section):
    url: HttpUrl | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    include_result: bool = False


class _ConfigFile(_Section):
    public_url: NonEmptyStr
    listen: ListenConfig = Field(default_factory=ListenConfig)
    state_dir: AbsolutePath = "/var/lib/voice-coding-mcp"
    project_roots: Annotated[list[AbsolutePath], Field(min_length=1)]
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    sessions: SessionsConfig = Field(default_factory=SessionsConfig)
    remote_control: RemoteControlConfig = Field(default_factory=RemoteControlConfig)
    oauth: OAuthConfig = Field(default_factory=OAuthConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    log_prompts: bool = False
    remote: RemoteConfig = Field(default_factory=RemoteConfig)
    watch: WatchConfig = Field(default_factory=WatchConfig)


@dataclass(frozen=True)
class Config:
    """The validated config with derived values."""

    public_url: str
    """Normalized to the origin (same as `issuer`)."""
    listen: ListenConfig
    state_dir: str
    project_roots: tuple[str, ...]
    """realpath of every root."""
    claude: ClaudeConfig
    tasks: TasksConfig
    sessions: SessionsConfig
    remote_control: RemoteControlConfig
    oauth: OAuthConfig
    notify: NotifyConfig
    log_prompts: bool
    issuer: str
    """`publicUrl` without a trailing slash."""
    resource_url: str
    """Canonical MCP resource: `issuer + "/mcp"`."""
    public_host: str
    """Host (with port when not default) that the `Host` header must match."""
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    """Machines reached through the Switchboard dispatcher (docs/SWITCHBOARD.md)."""
    watch: WatchConfig = field(default_factory=WatchConfig)
    """The Switchboard notification watcher (off unless `intervalSeconds` > 0)."""


# --- checks ----------------------------------------------------------------------------------


def is_loopback_host(host: str) -> bool:
    """True for localhost and loopback IP addresses (127.0.0.0/8, ::1 with or without brackets)."""
    return host.strip().lower() == "localhost" or is_loopback_ip(host)


def _format_issues(error: ValidationError) -> str:
    lines = []
    for issue in error.errors():
        path = ".".join(str(part) for part in issue["loc"])
        message = issue["msg"].removeprefix("Value error, ")
        lines.append(f"{path or '(root)'}: {message}")
    return "\n".join(lines)


def _is_same_or_inside(child: str, parent: str) -> bool:
    return child == parent or is_inside(child, parent)


def _check_public_url(value: str) -> tuple[str, str]:
    """Returns (origin, host)."""
    url = parse_url(value)
    if url is None:
        raise ConfigError(f"publicUrl: not a valid URL: {value}")
    if url.scheme != "https" and not (url.scheme == "http" and is_loopback_host(url.hostname)):
        raise ConfigError("publicUrl: must use https (plain http is allowed only for loopback hosts)")
    if url.username or url.password:
        raise ConfigError("publicUrl: must not contain credentials")
    if not url.hostname.isascii():
        raise ConfigError(f"publicUrl: the host {url.hostname} is not ASCII; use its punycode form (xn--...) instead")
    if url.path != "/" or url.query or url.fragment:
        raise ConfigError("publicUrl: must be an origin without path, query or fragment, e.g. https://vcm.example.com")
    return f"{url.scheme}://{url.host}", url.host


def _check_project_roots(roots: list[str], state_dir: str, home_dir: str) -> tuple[str, ...]:
    home = os.path.realpath(home_dir)
    state = os.path.realpath(state_dir)
    resolved: list[str] = []
    for index, root in enumerate(roots):
        field = f"projectRoots.{index}"
        try:
            real = os.path.realpath(root, strict=True)
        except OSError:
            raise ConfigError(f"{field}: directory does not exist: {root}") from None
        if not os.path.isdir(real):
            raise ConfigError(f"{field}: not a directory: {root}")
        if real == os.sep:
            raise ConfigError(f"{field}: the filesystem root is not allowed")
        if _is_same_or_inside(home, real):
            raise ConfigError(f"{field}: must not be the home directory or one of its ancestors: {root}")
        if _is_same_or_inside(state, real):
            raise ConfigError(f"{field}: must not contain stateDir ({state_dir}): {root}")
        if real in resolved:
            raise ConfigError(f"{field}: duplicate root: {root}")
        resolved.append(real)
    return tuple(resolved)


def parse_config(data: object, *, home_dir: str | None = None) -> Config:
    """Validates a parsed config object (camelCase keys), applies defaults and derives values.

    `home_dir` is the home directory for the project root checks (default: the user's home).
    Raises ConfigError with one `path: message` line per problem.
    """
    try:
        parsed = _ConfigFile.model_validate(data, by_alias=True, by_name=False)
    except ValidationError as err:
        raise ConfigError(f"Invalid config:\n{_format_issues(err)}") from None

    issuer, public_host = _check_public_url(parsed.public_url)

    if not is_loopback_host(parsed.listen.host) and not parsed.listen.allow_non_loopback:
        raise ConfigError(
            f"listen.host: must be a loopback address (got {parsed.listen.host}); "
            "set listen.allowNonLoopback to true to override"
        )

    remote_problems = check_remote(
        parsed.remote, allow_non_loopback=parsed.listen.allow_non_loopback, state_dir=parsed.state_dir
    )
    if parsed.watch.interval_seconds > 0 and parsed.notify.url is None:
        remote_problems.append("watch.intervalSeconds: the watcher needs notify.url")
    if remote_problems:
        raise ConfigError("Invalid config:\n" + "\n".join(remote_problems))

    project_roots = _check_project_roots(
        parsed.project_roots, parsed.state_dir, home_dir if home_dir is not None else os.path.expanduser("~")
    )

    return Config(
        public_url=issuer,
        listen=parsed.listen,
        state_dir=parsed.state_dir,
        project_roots=project_roots,
        claude=parsed.claude,
        tasks=parsed.tasks,
        sessions=parsed.sessions,
        remote_control=parsed.remote_control,
        oauth=parsed.oauth,
        notify=parsed.notify,
        log_prompts=parsed.log_prompts,
        issuer=issuer,
        resource_url=f"{issuer}/mcp",
        public_host=public_host,
        remote=parsed.remote,
        watch=parsed.watch,
    )


def config_warnings(config: Config, *, platform: str | None = None) -> list[str]:
    """Settings that are valid but weaken security or have no effect here (`platform`, default
    `sys.platform`), as messages for a startup warning."""
    warnings: list[str] = []
    if config.remote_control.handoff_to_desktop and (platform or sys.platform) != "darwin":
        warnings.append("remoteControl.handoffToDesktop works only on macOS and is ignored here")
    if config.claude.access == "desktop":
        warnings.append(
            "claude.access is desktop: voice tasks get all your claude.ai connectors, MCP servers and plugins "
            "(only the voice server itself is excluded)"
        )
    if config.oauth.allow_loopback_redirects:
        warnings.append(
            "oauth.allowLoopbackRedirects is on: any client can register a http://localhost redirect URI "
            "during a pairing window; turn it off unless you are testing with a local client"
        )
    warnings += remote_warnings(config.remote)
    return warnings


def load_config(path: str, *, home_dir: str | None = None) -> Config:
    """Reads, parses and validates the config file at `path`. Errors name the file."""
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(f"Cannot read config file {path}: {err}") from None
    try:
        data = loads_json(text)
    except ValueError as err:
        raise ConfigError(f"Config file {path} is not valid JSON: {err}") from None
    try:
        return parse_config(data, home_dir=home_dir)
    except ConfigError as err:
        raise ConfigError(f"{path}: {err}") from None
