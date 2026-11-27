"""The `timesheet` config section (docs/SWITCHBOARD.md, "Timesheet")."""

from __future__ import annotations

import re
import zoneinfo
from fnmatch import fnmatchcase
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from ..usage.config import Int

CLIENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
PROJECT_RE = re.compile(r"[A-Za-z0-9._-][A-Za-z0-9 ._-]{0,63}")
"""A project key: a folder basename as the usage records report it."""
ALIAS_RE = re.compile(r"[A-Za-z0-9 ._*?-]{1,64}")
"""An alias: a folder basename or an `fnmatch` glob (`*`, `?`; no brackets)."""
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
EMAIL_RE = re.compile(r"[^@\s,;<>\"']{1,128}@[^@\s,;<>\"']{1,126}")
LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})")
BIN_RE = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]{0,254}")
PATH_RE = re.compile(r"/[A-Za-z0-9._/ -]{1,255}")
MAX_PROJECTS = 200
MAX_CLIENTS = 100


def _matches(pattern: re.Pattern[str], what: str) -> AfterValidator:
    def check(value: str) -> str:
        if not pattern.fullmatch(value):
            raise ValueError(f"{value!r} is not a valid {what}")
        return value

    return AfterValidator(check)


def _time_zone(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone {value!r}; use an IANA name such as Europe/Zurich") from None
    return value


def _bin(value: str) -> str:
    if not BIN_RE.fullmatch(value) or value.startswith("-"):
        raise ValueError(f"{value!r} must be a command name or an absolute path")
    return value


class _Section(BaseModel):
    model_config = ConfigDict(
        strict=True,
        frozen=True,
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )


class ClientConfig(_Section):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    """Display name, as it should appear on the timesheet."""

    @model_validator(mode="after")
    def _printable(self) -> ClientConfig:
        if not self.name.isprintable() or not self.name.strip():
            raise ValueError("client name must be printable text")
        return self


class ProjectMapping(_Section):
    client: Annotated[str, _matches(CLIENT_ID_RE, "client id")]
    """Exactly one client per project: a key of `timesheet.clients`."""
    aliases: Annotated[list[Annotated[str, _matches(ALIAS_RE, "alias")]], Field(max_length=32)] = Field(
        default_factory=list
    )
    """Other folder names of the same project (a worktree, another machine's clone): names or globs."""
    repos: Annotated[list[Annotated[str, _matches(REPO_RE, "GitHub repository (owner/name)")]], Field(max_length=8)] = (
        Field(default_factory=list)
    )
    """GitHub repositories whose commits and pull requests count for this project (read with gh)."""
    paths: Annotated[list[Annotated[str, _matches(PATH_RE, "absolute path")]], Field(max_length=8)] = Field(
        default_factory=list
    )
    """Local clones read with `git log` on the machine that drafts the timesheet; inside projectRoots."""


class TimesheetConfig(_Section):
    time_zone: Annotated[str | None, AfterValidator(_time_zone)] = None
    """The owner's IANA time zone for days and weeks; None: the hub's local time zone."""
    clients: Annotated[dict[str, ClientConfig], Field(max_length=MAX_CLIENTS)] = Field(default_factory=dict)
    projects: Annotated[dict[str, ProjectMapping], Field(max_length=MAX_PROJECTS)] = Field(default_factory=dict)
    author_emails: Annotated[list[Annotated[str, _matches(EMAIL_RE, "email address")]], Field(max_length=16)] = Field(
        default_factory=list
    )
    """The owner's commit identities; only commits by these authors are listed."""
    github_logins: Annotated[list[Annotated[str, _matches(LOGIN_RE, "GitHub login")]], Field(max_length=8)] = Field(
        default_factory=list
    )
    """The owner's GitHub logins; only pull requests by these authors are listed."""
    rounding_minutes: Annotated[Int, Field(ge=0, le=60)] = 0
    """Round each day's hours of a project to this many minutes (0: no rounding)."""
    rounding_mode: Literal["nearest", "up"] = "nearest"
    owner_overlap: Literal["split", "full"] = "split"
    """Owner time in several projects at the same moment: split equally, or counted in full in each."""
    record_voice: bool = True
    """Record the times of the owner's Switchboard tool calls (no text) for owner time."""
    voice_approval: bool = True
    """Allow approving a draft by voice (timesheet_approve, then timesheet_approve_confirm)."""
    gh_bin: Annotated[str, AfterValidator(_bin)] = "gh"
    git_bin: Annotated[str, AfterValidator(_bin)] = "git"
    command_timeout_seconds: Annotated[Int, Field(ge=5, le=120)] = 20

    @model_validator(mode="after")
    def _mapping(self) -> TimesheetConfig:
        for client in self.clients:
            if not CLIENT_ID_RE.fullmatch(client):
                raise ValueError(f"client id {client!r} must be 1 to 64 lowercase letters, digits or ._-")
        seen: dict[str, str] = {}
        for name, project in self.projects.items():
            if not PROJECT_RE.fullmatch(name):
                raise ValueError(f"project {name!r} must be a folder name of letters, digits, spaces or ._-")
            if project.client not in self.clients:
                raise ValueError(f"project {name!r}: client {project.client!r} is not in timesheet.clients")
            for alias in project.aliases:
                if alias in seen and seen[alias] != name:
                    raise ValueError(f"alias {alias!r} is used by projects {seen[alias]!r} and {name!r}")
                seen[alias] = name
        for alias, owner in seen.items():
            for name in self.projects:
                if name != owner and fnmatchcase(name, alias):
                    raise ValueError(f"alias {alias!r} of project {owner!r} also matches the project {name!r}")
        return self

    def project_of(self, folder: str) -> tuple[str | None, list[str]]:
        """(configured project, []) for a folder name the records report, or (None, candidates)
        when no project or several match. Exact project keys win, then exact aliases, then globs."""
        if folder in self.projects:
            return folder, []
        exact = [name for name, p in self.projects.items() if folder in p.aliases]
        if len(exact) == 1:
            return exact[0], []
        globbed = sorted({name for name, p in self.projects.items() for a in p.aliases if fnmatchcase(folder, a)})
        if len(globbed) == 1:
            return globbed[0], []
        return None, globbed
