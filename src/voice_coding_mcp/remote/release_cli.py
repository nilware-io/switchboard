"""`voice-coding-mcp release ...` and `voice-coding-mcp remote-update` (docs/REFERENCE.md, "CLI";
design in docs/SWITCHBOARD.md, "Self-update").

`release build`, `release sign` and `release verify` need no config: they run on the owner's own
computer, where the release key lives (`sign` lets `ssh-keygen` ask for the key's passphrase on the
terminal; it never passes through this program). `release publish` and `release list` run on the
hub and use its `stateDir`. `remote-update <machine>` shows what an update would do and installs it
only with `--yes`.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..clock import now_ms
from ..config import Config
from ..log import silent_logger
from . import dispatcher as d
from .agents import CONTROL_SOCKET_NAME, control_request
from .audit import AuditLog
from .config import command_timeout_seconds
from .incidents import IncidentLog, zone_of
from .inventory import Inventory
from .protocol import VersionResponse
from .release import (
    FIRST_UPDATABLE,
    Release,
    ReleaseError,
    build_manifest,
    latest,
    load_bundle,
    newer,
    publish,
    published,
    sign_build,
    source_files,
    utc_now,
    verify_release,
    write_build,
)
from .send import UpdateOffer
from .speech import unreachable_reason, update_refusal
from .tools import send_update, update_incident
from .transport import make_transport

OFFLINE_ACTIONS = ("build", "sign", "verify")
UPDATE_CONTROL_SECONDS = 180.0

Out = Callable[[str], None]


@dataclass(frozen=True)
class ReleaseArgs:
    action: str
    source: str | None = None
    out: str = "."
    min_version: str = FIRST_UPDATABLE
    key: str | None = None
    signers: str | None = None
    path: str | None = None
    machine: str | None = None
    yes: bool = False


def add_parsers(command: Callable[[str, str], argparse.ArgumentParser]) -> None:
    release = command("release", "build, sign, verify and publish signed updates of the machine programs")
    actions = release.add_subparsers(dest="release_action", metavar="<action>", required=True)

    def action(name: str, text: str) -> argparse.ArgumentParser:
        sub = actions.add_parser(name, help=text, description=text)
        sub.add_argument("--config", metavar="PATH", default=argparse.SUPPRESS, help="config file")
        return sub

    build = action("build", "write a release directory (manifest.json and the three programs) to read and sign")
    build.add_argument(
        "--source", metavar="DIR", help="take the programs from DIR, such as deploy/remote (default: this package)"
    )
    build.add_argument("--out", metavar="DIR", default=".", help="where to write it (default: .)")
    build.add_argument(
        "--min-version",
        metavar="X.Y.Z",
        default=FIRST_UPDATABLE,
        help=f"oldest installed dispatcher it may update (default: {FIRST_UPDATABLE})",
    )
    sign = action("sign", "sign a release directory with your release key (ssh-keygen asks for its passphrase)")
    sign.add_argument(
        "--key", required=True, metavar="FILE", help="your release key, such as ~/.ssh/switchboard_release"
    )
    sign.add_argument("path", metavar="DIR", help="the directory release build wrote")
    verify = action("verify", "check a bundle exactly as a machine does, against an allowed-signers file")
    verify.add_argument("--signers", required=True, metavar="FILE", help="the allowed-signers file the machines pin")
    verify.add_argument("path", metavar="BUNDLE", help="the .bundle file")
    pub = action("publish", "store a signed bundle on the hub (stateDir/releases), for remote_update")
    pub.add_argument("path", metavar="BUNDLE", help="the .bundle file release sign wrote")
    action("list", "list the published releases")
    update = command("remote-update", "show, and with --yes install, the newest published release on a machine")
    update.add_argument("machine", help="the machine (its name in remote.machines)")
    update.add_argument("--yes", action="store_true", help="install it (without: only show what would happen)")


def args_of(ns: argparse.Namespace) -> ReleaseArgs:
    if ns.command == "remote-update":
        return ReleaseArgs(action="remote-update", machine=ns.machine, yes=ns.yes)
    return ReleaseArgs(
        action=ns.release_action,
        source=getattr(ns, "source", None),
        out=getattr(ns, "out", "."),
        min_version=getattr(ns, "min_version", FIRST_UPDATABLE),
        key=getattr(ns, "key", None),
        signers=getattr(ns, "signers", None),
        path=getattr(ns, "path", None),
    )


def describe(release: Release) -> str:
    return (
        f"switchboard-remote {release.version} (files {', '.join(release.targets)}; for installed "
        f"{release.min_version} or newer), created {release.created}, signed by {release.fingerprint} "
        f"({release.key_type}), bundle sha256 {release.sha256}"
    )


def run_offline(args: ReleaseArgs, out: Out, err: Out) -> int:
    """`release build`, `release sign`, `release verify`: no config, no hub."""
    try:
        if args.action == "build":
            if d.version_tuple(args.min_version) is None:
                err("--min-version must look like 0.13.0\n")
                return 2
            files = source_files(args.source)
            manifest = build_manifest(files, created=utc_now(), min_version=args.min_version)
            directory = write_build(args.out, files, manifest)
            out(
                f"Wrote {directory}. Read the files there, then sign it: voice-coding-mcp release sign --key <key> {directory}\n"
            )
            return 0
        if args.action == "sign":
            assert args.key is not None
            assert args.path is not None
            path, release = sign_build(args.path, args.key)
            out(f"Wrote {path}: {describe(release)}\n")
            out("Copy it to the hub and run voice-coding-mcp release publish there.\n")
            return 0
        assert args.signers is not None
        assert args.path is not None
        release = verify_release(args.path, args.signers)
        out(f"Good signature: {describe(release)}\n")
        return 0
    except ReleaseError as error:
        err(f"Error: {error}\n")
        return 1


def run_hub(config: Config, args: ReleaseArgs, out: Out, err: Out) -> int:
    """`release publish` and `release list`."""
    try:
        if args.action == "publish":
            assert args.path is not None
            release = publish(args.path, config.state_dir)
            out(f"Published {describe(release)}\n")
            return 0
        releases, problems = published(config.state_dir)
        for release in releases:
            out(describe(release) + "\n")
        for problem in problems:
            err(f"not a valid bundle: {problem}\n")
        if not releases:
            out("No release is published.\n")
        return 1 if problems else 0
    except ReleaseError as error:
        err(f"Error: {error}\n")
        return 1


async def run_remote_update(config: Config, args: ReleaseArgs, env: Mapping[str, str], out: Out, err: Out) -> int:
    """`remote-update <machine> [--yes]`."""
    machine = next((m for m in config.remote.machines if m.name == args.machine), None)
    if machine is None:
        names = ", ".join(m.name for m in config.remote.machines) or "none"
        err(f"No machine called {args.machine}. Configured: {names}.\n")
        return 2
    name = machine.name
    if machine.transport == "local":
        err(update_refusal(name, "local") + "\n")
        return 1
    release = latest(config.state_dir)
    if release is None:
        err("No release is published (voice-coding-mcp release publish <bundle>).\n")
        return 1
    control = f"{config.state_dir}/{CONTROL_SOCKET_NAME}"
    if machine.transport == "agent":
        status = await control_request(control, {"t": "status"})
        entries = status.get("machines") if status is not None else None
        entry = next((e for e in entries or [] if isinstance(e, dict) and e.get("name") == name), None)
        if status is None or entry is None:
            err("The hub is not running; only the hub holds agent connections.\n")
            return 1
        version = entry.get("version") if entry.get("connected") else None
        verbs = entry.get("verbs") or []
        if not isinstance(version, str):
            err(f"{name} is not connected right now.\n")
            return 1
    else:
        transport = make_transport(machine, config.remote, config.state_dir, env)
        result = await Inventory(config.remote, {name: transport}, silent_logger).version(name)
        if not result.ok or not isinstance(result.data, VersionResponse):
            err(f"{name} is not reachable: {unreachable_reason(result.error, result.detail)}\n")
            return 1
        version, verbs = result.data.version, list(result.data.verbs)
        if "update" in verbs and not result.data.allow_self_update:
            err(f"{name}: switchboard-remote {version} runs without --allow-self-update.\n")
            return 1
    if "update" not in verbs:
        err(f"{name}: switchboard-remote {version} cannot update itself; install {release.version} by hand once.\n")
        return 1
    if not newer(release, version):
        out(f"{name}: switchboard-remote {version} is not older than the newest release {release.version}.\n")
        return 0
    out(f"{name}: switchboard-remote {version} -> {describe(release)}\n")
    if not args.yes:
        out("Nothing was changed. Run again with --yes to install it.\n")
        return 0
    if machine.transport == "agent":
        answer = await control_request(
            control, {"t": "update", "machine": name, "sha256": release.sha256}, seconds=UPDATE_CONTROL_SECONDS
        )
        outcome = str(answer.get("outcome")) if answer is not None else "unknown"
    else:
        offer = UpdateOffer(
            from_version=version,
            to_version=release.version,
            fingerprint=release.fingerprint,
            sha256=release.sha256,
            path=release.path,
        )
        try:
            bundle = load_bundle(release)
        except ReleaseError as error:
            err(f"Error: {error}\n")
            return 1
        timeout = float(command_timeout_seconds(config.remote, machine)) + 30.0
        outcome, _, response = await send_update(transport, bundle, timeout)
        log = IncidentLog(
            config.state_dir,
            max_bytes=config.incidents.max_kilobytes * 1024,
            keep_files=config.incidents.keep_files,
            tz=zone_of(config.incidents),
        )
        log.write(update_incident(name, offer, outcome, response, now_ms()))
        AuditLog(config.state_dir, log_prompts=config.log_prompts).record_update(
            machine=name, offer=offer, outcome=outcome, client="cli"
        )
    if outcome == "sent":
        out(f"Installed {release.version} on {name}.\n")
        return 0
    if outcome == "unknown":
        err(f"{name} stopped answering; the outcome is unknown. Check with remote-check {name}.\n")
        return 1
    err("Not installed: " + update_refusal(name, outcome.removeprefix("not_sent:")) + "\n")
    return 1
