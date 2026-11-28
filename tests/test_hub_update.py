"""`deploy/hub-update` (`hub_update.py`): the hub moves only to a release tag signed with the pinned
key, checked with `git verify-tag` in a temporary repository with temporary keys; unsigned,
lightweight, foreign-key, renamed and older tags are refused and change nothing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.remote.release_helpers import REPO, needs_git, needs_openssh, new_key, signers_line
from voice_coding_mcp import hub_update as h

pytestmark = [needs_openssh, needs_git]

GIT_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_AUTHOR_NAME": "Release Test",
    "GIT_AUTHOR_EMAIL": "release@example.org",
    "GIT_COMMITTER_NAME": "Release Test",
    "GIT_COMMITTER_EMAIL": "release@example.org",
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], env=GIT_ENV, capture_output=True, text=True, check=True)
    return done.stdout.strip()


class Hub:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.key = new_key(root, "release")
        self.upstream = root / "upstream"
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.upstream)], env=GIT_ENV, check=True)
        self.commit("one")
        self.tag("v0.1.0")
        self.checkout = root / "hub"
        subprocess.run(["git", "clone", "-q", str(self.upstream), str(self.checkout)], env=GIT_ENV, check=True)
        etc = root / "etc"
        etc.mkdir(mode=0o755)
        self.signers = etc / "release_signers"
        self.signers.write_text(signers_line(self.key, "switchboard-release,git"))
        self.signers.chmod(0o644)
        self.lines: list[str] = []

    def commit(self, message: str) -> str:
        git(self.upstream, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.upstream, "rev-parse", "HEAD")

    def tag(self, name: str, key: Path | None = None, *, signed: bool = True) -> None:
        if signed:
            git(
                self.upstream,
                "-c",
                "gpg.format=ssh",
                "-c",
                f"user.signingkey={key or self.key}",
                "tag",
                "-s",
                name,
                "-m",
                f"release {name}",
            )
        else:
            git(self.upstream, "tag", "-a", name, "-m", f"release {name}")

    def run(self, tag: str, *extra: str) -> int:
        args = h.parse_args(
            [tag, "--repo", str(self.checkout), "--signers", str(self.signers), "--no-sync", "--no-restart", *extra]
        )
        try:
            return h.update(args, {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root)}, self.lines.append)
        except h.UpdateError as error:
            self.lines.append(f"refused: {error}")
            return 1

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")


@pytest.fixture
def hub(tmp_path: Path) -> Hub:
    return Hub(tmp_path)


def test_a_signed_newer_tag_is_checked_out_by_its_commit(hub: Hub) -> None:
    second = hub.commit("two")
    hub.tag("v0.2.0")
    assert hub.run("v0.2.0") == 0
    assert hub.head() == second
    assert any(line.startswith('v0.2.0: Good "git" signature for switchboard-release') for line in hub.lines)
    state = (hub.checkout / ".git" / h.STATE_FILE).read_text()
    assert '"tag": "v0.2.0"' in state


def test_a_dry_run_changes_nothing(hub: Hub) -> None:
    before = hub.head()
    hub.commit("two")
    hub.tag("v0.2.0")
    assert hub.run("v0.2.0", "--dry-run") == 0
    assert hub.head() == before
    assert not (hub.checkout / ".git" / h.STATE_FILE).exists()


def test_unsigned_foreign_lightweight_and_renamed_tags_are_refused(hub: Hub, tmp_path: Path) -> None:
    before = hub.head()
    hub.commit("two")
    hub.tag("v0.2.0", signed=False)
    hub.tag("v0.3.0", new_key(tmp_path, "intruder"))
    git(hub.upstream, "tag", "v0.4.0")
    git(hub.upstream, "update-ref", "refs/tags/v0.5.0", git(hub.upstream, "rev-parse", "refs/tags/v0.1.0"))
    for tag, why in (
        ("v0.2.0", "does not verify"),
        ("v0.3.0", "does not verify"),
        ("v0.4.0", "lightweight"),
        ("v0.5.0", "names itself 'v0.1.0'"),
    ):
        assert hub.run(tag) == 1, tag
        assert why in hub.lines[-1], (tag, hub.lines[-1])
    assert hub.head() == before


def test_an_older_or_the_same_tag_is_refused_after_an_update(hub: Hub) -> None:
    hub.commit("two")
    hub.tag("v0.2.0")
    assert hub.run("v0.2.0") == 0
    assert hub.run("v0.1.0") == 1
    assert "no downgrade" in hub.lines[-1]
    assert hub.run("v0.2.0") == 1


def test_an_untrusted_signers_file_or_a_dirty_checkout_is_refused(hub: Hub) -> None:
    hub.commit("two")
    hub.tag("v0.2.0")
    hub.signers.chmod(0o666)
    assert hub.run("v0.2.0") == 1
    assert "writable by group or others" in hub.lines[-1]
    hub.signers.chmod(0o644)
    (hub.checkout / "tracked").write_text("x")
    git(hub.checkout, "add", "tracked")
    assert hub.run("v0.2.0") == 1
    assert "local changes" in hub.lines[-1]


def test_a_signers_line_without_the_git_namespace_does_not_verify_tags(hub: Hub) -> None:
    hub.signers.write_text(signers_line(hub.key, "switchboard-release"))
    hub.commit("two")
    hub.tag("v0.2.0")
    assert hub.run("v0.2.0") == 1
    assert "does not verify" in hub.lines[-1]


def test_bad_tag_names_and_remotes_are_refused(hub: Hub) -> None:
    assert hub.run("main") == 1
    assert hub.run("v0.2.0", "--remote", "--upload-pack=touch /tmp/x") == 1
    assert "plain name" in hub.lines[-1]


def test_the_deploy_copy_is_byte_identical_and_executable() -> None:
    deployed = REPO / "deploy" / "hub-update"
    assert deployed.read_bytes() == (REPO / "src" / "voice_coding_mcp" / "hub_update.py").read_bytes()
    assert os.access(deployed, os.X_OK)
