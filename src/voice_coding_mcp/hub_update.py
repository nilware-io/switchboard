#!/usr/bin/env python3
"""hub-update: moves the hub's checkout to a release tag signed with the owner's release key.

Standard library only, one file. `deploy/hub-update` is a byte-identical copy of this file. Design:
docs/SWITCHBOARD.md, "Self-update". Run it by hand on the hub, as the owner of the checkout (root
for the server layout of docs/DEPLOY.md):

    sudo /usr/bin/python3 -I /opt/voice-coding-mcp/deploy/hub-update v0.13.0

It fetches only that tag, checks that the tag object's own name is that tag, that its signature
verifies with `git verify-tag` against the pinned allowed-signers file (default
/etc/voice-coding-mcp/release_signers, owned by root or this user, writable by nobody else; the
key's line must allow the `git` namespace) and that its version is newer than the last one this
program installed, and only then checks out the signed commit (by hash), runs `uv sync` and
restarts the unit. An unsigned tag, a lightweight tag, a tag signed by another key, a renamed tag
or an older one is refused and nothing changes. It never runs from a voice call.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Sequence

TAG_RE = re.compile(r"v([0-9]{1,4})\.([0-9]{1,4})\.([0-9]{1,4})")
OID_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
DEFAULT_SIGNERS = "/etc/voice-coding-mcp/release_signers"
DEFAULT_UNIT = "voice-coding-mcp"
STATE_FILE = "switchboard-hub-update.json"
"""In the checkout's `.git` directory: the last tag this program installed (never in the tree)."""
TOOL_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
SSH_KEYGEN = "/usr/bin/ssh-keygen"
MIN_GIT = (2, 34)
"""`git verify-tag` checks ssh signatures since git 2.34."""
SUBPROCESS_SECONDS = 600


class UpdateError(Exception):
    """A refusal: the message says why; nothing was changed unless it says so."""


def version_of(tag: str) -> tuple[int, int, int] | None:
    found = TAG_RE.fullmatch(tag)
    if found is None:
        return None
    return int(found.group(1)), int(found.group(2)), int(found.group(3))


def git_env(base: dict[str, str]) -> dict[str, str]:
    """Only the repository's own config counts: no global or system config, no prompts."""
    env = {
        "PATH": TOOL_PATH,
        "HOME": base.get("HOME", "/nonexistent"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in ("SSH_AUTH_SOCK", "GIT_SSH_COMMAND"):
        if key in base:
            # Fetching over ssh may need the operator's own agent or command.
            env[key] = base[key]
    return env


class Git:
    def __init__(self, repo: str, env: dict[str, str]) -> None:
        self.repo = repo
        self.env = env

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(
            ["git", "-C", self.repo, *args],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if check and done.returncode != 0:
            said = (done.stderr or done.stdout).strip().splitlines()
            name = next((arg for arg in args if not arg.startswith("-") and "=" not in arg), "")
            raise UpdateError(f"git {name} failed: {said[-1] if said else 'exit ' + str(done.returncode)}")
        return done

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()


def signers_problem(path: str) -> str | None:
    """Why the allowed-signers file cannot be trusted, or None: a regular file (no symlink) owned by
    root or this user that neither group nor others can write, in such a directory."""
    for target, want_dir in ((os.path.dirname(os.path.abspath(path)), True), (path, False)):
        try:
            info = os.lstat(target)
        except OSError:
            return f"{target} does not exist"
        kind_ok = stat.S_ISDIR(info.st_mode) if want_dir else stat.S_ISREG(info.st_mode)
        if not kind_ok:
            return f"{target} is not a {'directory' if want_dir else 'regular file'}"
        if info.st_uid not in (0, os.getuid()):
            return f"{target} is owned by another user"
        if info.st_mode & 0o022:
            return f"{target} is writable by group or others"
    return None


def git_version(git: Git) -> tuple[int, int]:
    found = re.search(r"git version ([0-9]+)\.([0-9]+)", git.out("--version"))
    return (int(found.group(1)), int(found.group(2))) if found else (0, 0)


def read_state(git_dir: str) -> dict[str, str]:
    try:
        with open(os.path.join(git_dir, STATE_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise UpdateError(f"cannot read {STATE_FILE}: {error}") from None
    if not isinstance(data, dict) or not isinstance(data.get("tag"), str) or version_of(data["tag"]) is None:
        raise UpdateError(f"{STATE_FILE} is not valid; remove it only if you know the installed tag")
    return {str(k): str(v) for k, v in data.items()}


def write_state(git_dir: str, state: dict[str, str]) -> None:
    path = os.path.join(git_dir, STATE_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def verify_tag(git: Git, tag: str, signers: str) -> tuple[str, str, str]:
    """(tag object id, commit id, git's signature line) after every check, all by object id so a ref
    that moves in between cannot swap what is checked out. Raises UpdateError."""
    ref = f"refs/tags/{tag}"
    tag_oid = git.out("rev-parse", "--verify", "--end-of-options", ref)
    if not OID_RE.fullmatch(tag_oid):
        raise UpdateError(f"{tag} does not resolve to an object id")
    if git.out("cat-file", "-t", tag_oid) != "tag":
        raise UpdateError(f"{tag} is a lightweight tag: only an annotated, signed tag is accepted")
    headers: dict[str, str] = {}
    for line in git.out("cat-file", "tag", tag_oid).split("\n"):
        if not line:
            break
        key, _, value = line.partition(" ")
        headers.setdefault(key, value)
    if headers.get("tag") != tag:
        raise UpdateError(f"the tag object under {tag} names itself {headers.get('tag')!r}: refused (renamed tag)")
    if headers.get("type") != "commit" or not OID_RE.fullmatch(headers.get("object", "")):
        raise UpdateError(f"{tag} does not point at a commit")
    done = git.run(
        "-c",
        f"gpg.ssh.allowedSignersFile={signers}",
        "-c",
        f"gpg.ssh.program={SSH_KEYGEN}",
        "-c",
        "gpg.minTrustLevel=fully",
        "verify-tag",
        "--end-of-options",
        tag_oid,
        check=False,
    )
    said = (done.stderr + done.stdout).strip()
    good = next((line for line in said.splitlines() if line.startswith('Good "git" signature')), "")
    if done.returncode != 0 or not good:
        last = said.splitlines()[-1] if said else f"exit {done.returncode}"
        raise UpdateError(f"the signature of {tag} does not verify with {signers}: {last}")
    return tag_oid, headers["object"], good


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    parser = argparse.ArgumentParser(prog="hub-update", description="Move the hub to a signed release tag.")
    parser.add_argument("tag", help="the release tag, such as v0.13.0")
    parser.add_argument("--repo", default=here, help=f"the hub's checkout (default: {here})")
    parser.add_argument("--signers", default=DEFAULT_SIGNERS, help=f"allowed-signers file (default: {DEFAULT_SIGNERS})")
    parser.add_argument("--remote", default="origin", help="the remote to fetch the tag from (default: origin)")
    parser.add_argument("--unit", default=DEFAULT_UNIT, help=f"the systemd unit to restart (default: {DEFAULT_UNIT})")
    parser.add_argument("--no-sync", action="store_true", help="do not run uv sync after the checkout")
    parser.add_argument("--no-restart", action="store_true", help="do not restart the unit")
    parser.add_argument("--dry-run", action="store_true", help="fetch and verify only; change nothing")
    return parser.parse_args(list(argv))


def _run_step(argv: Sequence[str], cwd: str, env: dict[str, str]) -> None:
    done = subprocess.run(list(argv), cwd=cwd, env=env, timeout=SUBPROCESS_SECONDS, check=False)
    if done.returncode != 0:
        raise UpdateError(f"{argv[0]} {argv[1]} failed (exit {done.returncode})")


def update(args: argparse.Namespace, env: dict[str, str], out: Callable[[str], None] = print) -> int:
    tag = str(args.tag)
    wanted = version_of(tag)
    if wanted is None:
        raise UpdateError("the tag must look like v0.13.0")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", str(args.remote)) or str(args.remote).startswith("-"):
        raise UpdateError("the remote must be a plain name, such as origin")
    signers = os.path.abspath(str(args.signers))
    problem = signers_problem(signers)
    if problem is not None:
        raise UpdateError(f"the allowed-signers file is not trusted: {problem}")
    git = Git(os.path.abspath(str(args.repo)), git_env(env))
    if git_version(git) < MIN_GIT:
        raise UpdateError("git is older than 2.34 and cannot verify ssh signatures")
    git_dir = git.out("rev-parse", "--absolute-git-dir")
    state = read_state(git_dir)
    installed = state.get("tag")
    if installed is not None:
        have = version_of(installed)
        if have is not None and wanted <= have:
            raise UpdateError(f"{tag} is not newer than the installed {installed}: refused (no downgrade)")
    if git.out("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(f"{git.repo} has local changes; refusing to check anything out over them")
    git.run("fetch", "--no-tags", "--end-of-options", str(args.remote), f"refs/tags/{tag}:refs/tags/{tag}")
    tag_oid, commit, good = verify_tag(git, tag, signers)
    previous = git.out("rev-parse", "HEAD")
    out(f"{tag}: {good}")
    out(f"{tag}: commit {commit} (now at {previous})")
    if args.dry_run:
        out("Dry run: nothing was changed.")
        return 0
    git.run("-c", "advice.detachedHead=false", "checkout", "--detach", commit)
    step_env = {key: value for key, value in env.items() if key.startswith("UV_")}
    step_env.update(
        {"PATH": TOOL_PATH + ":" + env.get("PATH", ""), "HOME": env.get("HOME", "/root"), "LC_ALL": "C.UTF-8"}
    )
    try:
        if not args.no_sync:
            uv = shutil.which("uv", path=step_env["PATH"])
            if uv is None:
                raise UpdateError("uv was not found (use --no-sync and sync by hand)")
            _run_step([uv, "sync", "--frozen", "--no-dev", "--no-editable", "--compile-bytecode"], git.repo, step_env)
    except UpdateError:
        git.run("-c", "advice.detachedHead=false", "checkout", "--detach", previous, check=False)
        out(f"Went back to {previous}; run uv sync there by hand if the environment changed.")
        raise
    write_state(git_dir, {"tag": tag, "tagObject": tag_oid, "commit": commit, "previous": previous})
    if not args.no_restart:
        _run_step(["systemctl", "restart", str(args.unit)], git.repo, step_env)
    out(f"Installed {tag}." + ("" if args.no_restart else f" Restarted {args.unit}."))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return update(args, dict(os.environ))
    except UpdateError as error:
        sys.stderr.write(f"hub-update: {error}\n")
        return 1
    except (OSError, subprocess.SubprocessError) as error:
        sys.stderr.write(f"hub-update: {type(error).__name__}: {error}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
