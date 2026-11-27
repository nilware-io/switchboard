"""Commits and pull requests per project with fake gh and git runners: argv, author and period
filters, deduplication, subject lines only, failures as notes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from voice_coding_mcp.timesheet.config import TimesheetConfig
from voice_coding_mcp.timesheet.work import WorkCollector, clean_line

START = int(datetime(2026, 11, 16, tzinfo=UTC).timestamp() * 1000)
END = int(datetime(2026, 11, 23, tzinfo=UTC).timestamp() * 1000)
OWNER = "owner@example.com"


def settings(**extra: Any) -> TimesheetConfig:
    data: dict[str, Any] = {
        "clients": {"acme": {"name": "Acme"}},
        "projects": {"web": {"client": "acme", "repos": ["acme/web"], "paths": ["/srv/projects/web"]}},
        "authorEmails": [OWNER],
        "githubLogins": ["owner-login"],
        **extra,
    }
    return TimesheetConfig.model_validate(data)


class FakeRunner:
    def __init__(self, answers: dict[str, tuple[int, bytes]] | None = None, *, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []
        self.answers = answers or {}
        self.fail = fail

    async def __call__(self, argv: Sequence[str], limit: float, env: Mapping[str, str]) -> tuple[int, bytes]:
        self.calls.append(list(argv))
        self.envs.append(dict(env))
        if self.fail:
            raise TimeoutError
        for key, answer in self.answers.items():
            if key in " ".join(argv):
                return answer
        return 0, b"[]" if "pr" in argv else b""


def gh_commit(sha: str, date: str, subject: str = "Fix the thing") -> str:
    return json.dumps(
        {
            "sha": sha,
            "date": date,
            "email": OWNER,
            "subject": subject,
            "url": f"https://github.com/acme/web/commit/{sha}",
        }
    )


async def test_gh_commits_and_pulls_and_local_git() -> None:
    commits = "\n".join(
        [
            gh_commit("a" * 40, "2026-11-17T10:00:00Z"),
            gh_commit("b" * 40, "2026-11-30T10:00:00Z"),
            "not json",
            json.dumps({"sha": "zz", "date": "2026-11-17T10:00:00Z"}),
        ]
    )
    pulls = [
        {
            "number": 5,
            "title": "Attachments",
            "url": "https://github.com/acme/web/pull/5",
            "createdAt": "2026-11-10T10:00:00Z",
            "mergedAt": "2026-11-18T09:00:00Z",
        },
        {
            "number": 6,
            "title": "Old",
            "url": "https://github.com/acme/web/pull/6",
            "createdAt": "2026-10-01T10:00:00Z",
            "mergedAt": None,
        },
        {"number": 7, "title": "x\u0007y", "url": "javascript:alert(1)", "createdAt": "2026-11-17T10:00:00Z"},
    ]
    git = "\x1e".join(
        [
            f"{'a' * 40}\x1f2026-11-17T11:00:00+01:00\x1f{OWNER.upper()}\x1fFix the thing",
            f"{'c' * 40}\x1f2026-11-18T11:00:00+01:00\x1fsomeone@else.org\x1fNot mine",
            f"{'d' * 40}\x1f2026-11-19T11:00:00+01:00\x1f{OWNER}\x1fLocal branch work\n\nbody text",
        ]
    )
    runner = FakeRunner(
        {
            "repos/acme/web/commits": (0, commits.encode()),
            "created:": (0, json.dumps(pulls).encode()),
            "merged:": (0, json.dumps(pulls[:1]).encode()),
            " log --all": (0, git.encode()),
        }
    )
    collector = WorkCollector(settings(), runner=runner, env={"PATH": "/usr/bin", "HOME": "/home/x", "SECRET": "s"})
    products = await collector.collect(START, END)
    assert products.notes == []
    shas = [c.sha for c in products.commits["web"]]
    assert shas == ["a" * 40, "d" * 40]
    assert [c.subject for c in products.commits["web"]] == ["Fix the thing", "Local branch work"]
    assert [(p.number, p.title) for p in products.pulls["web"]] == [(5, "Attachments")]
    for argv in runner.calls:
        assert argv[0] in ("gh", "git")
    gh_commit_call = next(c for c in runner.calls if "repos/acme/web/commits" in c)
    assert f"author={OWNER}" in gh_commit_call
    assert "since=2026-11-16T00:00:00Z" in gh_commit_call
    pr_call = next(c for c in runner.calls if c[:3] == ["gh", "pr", "list"])
    assert pr_call[pr_call.index("--author") + 1] == "owner-login"
    git_call = next(c for c in runner.calls if c[0] == "git")
    assert git_call[git_call.index("-C") + 1] == "/srv/projects/web"
    assert "core.hooksPath=/dev/null" in git_call
    for env in runner.envs:
        assert "SECRET" not in env
        assert env["GIT_TERMINAL_PROMPT"] == "0"


async def test_failures_become_notes() -> None:
    products = await WorkCollector(settings(), runner=FakeRunner(fail=True), env={}).collect(START, END)
    assert products.commits == {}
    assert sorted(products.notes) == [
        "commits of acme/web could not be read with gh",
        "commits of the clone web could not be read with git",
        "pull requests of acme/web could not be read with gh",
    ]


async def test_nonzero_exit_is_a_failure() -> None:
    runner = FakeRunner({"repos/acme/web/commits": (1, b"")})
    products = await WorkCollector(settings(), runner=runner, env={}).collect(START, END)
    assert "commits of acme/web could not be read with gh" in products.notes


async def test_without_identities_nothing_is_read() -> None:
    runner = FakeRunner()
    products = await WorkCollector(settings(authorEmails=[], githubLogins=[]), runner=runner, env={}).collect(
        START, END
    )
    assert runner.calls == []
    assert products.notes == ["no timesheet.authorEmails or githubLogins: commits and pull requests not read"]


def test_clean_line_keeps_the_first_printable_line() -> None:
    assert clean_line("Subject\u0007 here\n\nBody with secrets", 100) == "Subject here"
    assert clean_line(None, 10) == ""
    assert clean_line("x" * 300, 200) == "x" * 200
