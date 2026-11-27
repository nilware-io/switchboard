"""Work products per project: the owner's commits and pull requests in a period (docs/SWITCHBOARD.md,
"Timesheet", "Commits and pull requests").

- GitHub repositories (`repos`), with `gh`: commits of the default branch by each of
  `authorEmails` (`gh api repos/R/commits?author=EMAIL&since&until`, first lines of the messages
  only, cut on GitHub's side with `--jq`), and pull requests by each of `githubLogins` created or
  merged in the period (`gh pr list --search`).
- Local clones (`paths`, inside projectRoots), with `git log --all` on the machine that drafts the
  timesheet: commits whose author email is one of `authorEmails`.

Only hashes, times, subject lines, titles, numbers and URLs are kept, never diffs or bodies. Nothing
is written to any repository. Every command runs from argv (no shell) with a time limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..clock import parse_iso
from .config import TimesheetConfig

SUBJECT_MAX = 200
TITLE_MAX = 200
MAX_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 1000
_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_URL_RE = re.compile(r"https://[A-Za-z0-9./_#?=&%-]{1,300}")

Runner = Callable[[Sequence[str], float, Mapping[str, str]], Awaitable[tuple[int, bytes]]]
"""(argv, time limit in seconds, env) -> (exit code, stdout). Raises TimeoutError or OSError."""


@dataclass(frozen=True)
class Commit:
    sha: str
    time_ms: int
    subject: str
    repo: str
    """`owner/name` or the local clone's folder name."""
    url: str | None = None


@dataclass(frozen=True)
class PullRequest:
    repo: str
    number: int
    title: str
    url: str
    created_ms: int
    merged_ms: int | None


@dataclass
class WorkProducts:
    commits: dict[str, list[Commit]] = field(default_factory=dict)
    """project -> commits (deduplicated by hash)."""
    pulls: dict[str, list[PullRequest]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def clean_line(value: object, limit: int) -> str:
    """The first line of a text, printable characters only, cut to `limit`."""
    if not isinstance(value, str):
        return ""
    first = value.split("\n", 1)[0]
    return "".join(ch for ch in first if ch.isprintable()).strip()[:limit]


async def run_command(argv: Sequence[str], limit_seconds: float, env: Mapping[str, str]) -> tuple[int, bytes]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=dict(env),
    )
    try:
        assert process.stdout is not None
        chunks: list[bytes] = []
        size = 0
        async with asyncio.timeout(limit_seconds):
            while size <= MAX_OUTPUT_BYTES:
                chunk = await process.stdout.read(64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_OUTPUT_BYTES:
                raise OSError("output too large")
            code = await process.wait()
        out = b"".join(chunks)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(Exception):
            await process.wait()
        raise
    return code, out


def _iso_utc(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _command_env(env: Mapping[str, str]) -> dict[str, str]:
    """PATH, HOME and gh's own variables; git without the system config, prompts or a pager."""
    keep = ("PATH", "HOME", "GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_CONFIG_DIR", "XDG_CONFIG_HOME", "LANG")
    out = {k: v for k, v in env.items() if k in keep}
    out.update({"GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1", "GIT_PAGER": "cat", "NO_COLOR": "1"})
    out["GIT_CONFIG_NOSYSTEM"] = "1"
    return out


class WorkCollector:
    def __init__(
        self,
        config: TimesheetConfig,
        *,
        runner: Runner | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._config = config
        self._run = runner or run_command
        self._env = _command_env(env if env is not None else os.environ)
        self._emails = {e.lower() for e in config.author_emails}

    async def _call(self, argv: list[str]) -> bytes | None:
        try:
            code, out = await self._run(argv, float(self._config.command_timeout_seconds), self._env)
        except (TimeoutError, OSError):
            return None
        return out if code == 0 else None

    async def gh_commits(self, repo: str, start_ms: int, end_ms: int) -> list[Commit] | None:
        found: dict[str, Commit] = {}
        jq = '.[] | {sha: .sha, date: .commit.author.date, email: .commit.author.email, subject: (.commit.message | split("\\n") | .[0]), url: .html_url}'
        for email in self._config.author_emails:
            argv = [
                self._config.gh_bin,
                "api",
                "-X",
                "GET",
                f"repos/{repo}/commits",
                "-f",
                f"author={email}",
                "-f",
                f"since={_iso_utc(start_ms)}",
                "-f",
                f"until={_iso_utc(end_ms)}",
                "-f",
                "per_page=100",
                "--paginate",
                "--jq",
                jq,
            ]
            out = await self._call(argv)
            if out is None:
                return None
            for line in out.decode("utf-8", "replace").splitlines()[:MAX_ITEMS]:
                commit = self._commit_of(line, repo)
                if commit is not None and start_ms <= commit.time_ms < end_ms:
                    found.setdefault(commit.sha, commit)
        return sorted(found.values(), key=lambda c: c.time_ms)

    def _commit_of(self, line: str, repo: str) -> Commit | None:
        try:
            data = json.loads(line)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        sha, when = data.get("sha"), parse_iso(data.get("date")) if isinstance(data.get("date"), str) else None
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha) or when is None:
            return None
        url = data.get("url")
        return Commit(
            sha=sha,
            time_ms=when,
            subject=clean_line(data.get("subject"), SUBJECT_MAX),
            repo=repo,
            url=url if isinstance(url, str) and _URL_RE.fullmatch(url) else None,
        )

    async def gh_pulls(self, repo: str, start_ms: int, end_ms: int) -> list[PullRequest] | None:
        found: dict[int, PullRequest] = {}
        window = f"{_iso_utc(start_ms)}..{_iso_utc(end_ms - 1000)}"
        for login in self._config.github_logins:
            for event in ("created", "merged"):
                argv = [
                    self._config.gh_bin,
                    "pr",
                    "list",
                    "-R",
                    repo,
                    "--state",
                    "all",
                    "--author",
                    login,
                    "--search",
                    f"{event}:{window}",
                    "--limit",
                    "200",
                    "--json",
                    "number,title,url,createdAt,mergedAt",
                ]
                out = await self._call(argv)
                if out is None:
                    return None
                try:
                    items = json.loads(out)
                except ValueError:
                    return None
                for item in items if isinstance(items, list) else []:
                    pull = self._pull_of(item, repo)
                    if pull is not None:
                        found.setdefault(pull.number, pull)
        inside = [
            p
            for p in found.values()
            if start_ms <= p.created_ms < end_ms or (p.merged_ms is not None and start_ms <= p.merged_ms < end_ms)
        ]
        return sorted(inside, key=lambda p: p.number)

    @staticmethod
    def _pull_of(item: object, repo: str) -> PullRequest | None:
        if not isinstance(item, dict):
            return None
        number, url = item.get("number"), item.get("url")
        created = parse_iso(item["createdAt"]) if isinstance(item.get("createdAt"), str) else None
        merged = parse_iso(item["mergedAt"]) if isinstance(item.get("mergedAt"), str) else None
        if (
            not isinstance(number, int)
            or isinstance(number, bool)
            or not 0 < number < 10**9
            or not isinstance(url, str)
            or not _URL_RE.fullmatch(url)
            or created is None
        ):
            return None
        return PullRequest(
            repo=repo,
            number=number,
            title=clean_line(item.get("title"), TITLE_MAX),
            url=url,
            created_ms=created,
            merged_ms=merged,
        )

    async def git_commits(self, path: str, start_ms: int, end_ms: int) -> list[Commit] | None:
        argv = [
            self._config.git_bin,
            "--no-pager",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.hooksPath=/dev/null",
            "-C",
            path,
            "log",
            "--all",
            "--no-color",
            f"--since={_iso_utc(start_ms - 31 * 86_400_000)}",
            "--format=%H%x1f%aI%x1f%ae%x1f%s%x1e",
        ]
        out = await self._call(argv)
        if out is None:
            return None
        repo = os.path.basename(path.rstrip("/"))
        found: dict[str, Commit] = {}
        for record in out.decode("utf-8", "replace").split("\x1e")[: MAX_ITEMS * 10]:
            parts = record.strip("\n").split("\x1f")
            if len(parts) != 4:
                continue
            sha, date, email, subject = parts
            when = parse_iso(date)
            if not _SHA_RE.fullmatch(sha) or when is None or email.lower() not in self._emails:
                continue
            if start_ms <= when < end_ms:
                found.setdefault(
                    sha, Commit(sha=sha, time_ms=when, subject=clean_line(subject, SUBJECT_MAX), repo=repo)
                )
        return sorted(found.values(), key=lambda c: c.time_ms)

    async def collect(self, start_ms: int, end_ms: int) -> WorkProducts:
        products = WorkProducts()
        if not self._config.author_emails and not self._config.github_logins:
            if any(p.repos or p.paths for p in self._config.projects.values()):
                products.notes.append("no timesheet.authorEmails or githubLogins: commits and pull requests not read")
            return products

        async def one_project(name: str) -> None:
            mapping = self._config.projects[name]
            commits: dict[str, Commit] = {}
            pulls: list[PullRequest] = []
            for repo in mapping.repos:
                if self._config.author_emails:
                    got = await self.gh_commits(repo, start_ms, end_ms)
                    if got is None:
                        products.notes.append(f"commits of {repo} could not be read with gh")
                    for commit in got or []:
                        commits.setdefault(commit.sha, commit)
                if self._config.github_logins:
                    found = await self.gh_pulls(repo, start_ms, end_ms)
                    if found is None:
                        products.notes.append(f"pull requests of {repo} could not be read with gh")
                    pulls += found or []
            for path in mapping.paths:
                if not self._config.author_emails:
                    break
                local = await self.git_commits(path, start_ms, end_ms)
                if local is None:
                    products.notes.append(f"commits of the clone {os.path.basename(path)} could not be read with git")
                for commit in local or []:
                    known = commits.get(commit.sha)
                    if known is None:
                        commits[commit.sha] = commit
            if commits:
                products.commits[name] = sorted(commits.values(), key=lambda c: (c.time_ms, c.sha))
            if pulls:
                products.pulls[name] = pulls

        await asyncio.gather(*(one_project(name) for name in self._config.projects))
        products.notes.sort()
        return products
