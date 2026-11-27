# voice-coding-mcp

A small remote MCP server that lets you drive Claude Code hands-free from the voice mode of the Claude apps. You add it to Claude as a [custom connector](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp) (a remote MCP server you add yourself); when you ask for something in one of your projects, the server runs headless Claude Code (`claude -p`) inside that project and returns a short answer meant to be read aloud. Longer tasks run in the background and you ask for the result later.

Not affiliated with Anthropic. "Claude" and "Claude Code" are used only to describe what this project works with.

It runs in one of these modes:

- **Mac mode (main)**: the server runs on your own Mac, next to your own Claude Code sessions, and is published through [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) only while you switch it on (`deploy/mac/vcm-mac on` and `off`). Besides running tasks, it can list your live Claude Code sessions, answer questions about them and pass messages into them.
- **VPS mode (alternative)**: the server runs on a Linux VPS behind Caddy, with its own copies of your projects. Your own computer is not exposed, but the server cannot reach your local sessions, and work that needs macOS is not possible there.
- **Linux workstation mode (Switchboard)**: the server runs on your Linux or WSL workstation, recommended as a dedicated `switchboard` user, and is published through Tailscale Funnel only while you switch it on (`deploy/linux/sb-linux on` and `off`). It reaches the Claude Code sessions you run inside tmux, on that machine and on others over SSH. See [Switchboard](#switchboard).

**Risks in short.** Whoever holds a valid token for the connector can run Claude Code on that machine with the server user's rights. In Mac mode that is your own macOS user, and your Mac is reachable from the internet while the Funnel is on. Your Claude account holds those tokens, so it becomes a key to your machine. Prompt injection in files Claude Code reads can make a task act against you. Read [Security](#security) before you deploy.

## Requirements

Both modes:

- [uv](https://docs.astral.sh/uv/) and Python 3.12. On the Mac uv installs Python 3.12 itself on first use; on the VPS `deploy/setup.sh` uses Ubuntu's.
- Claude Code installed for the user the server runs as.
- A Claude plan that supports custom connectors.

Mac mode:

- macOS with the Tailscale app, signed in, with MagicDNS, HTTPS certificates and Funnel enabled for the tailnet.
- curl and jq. jq ships with macOS 15 and later; otherwise `brew install jq`.
- Claude Code signed in as your user, as you use it anyway.

VPS mode:

- A Linux server with a public IP and a DNS name. The runbook targets Oracle Cloud Always Free (Ampere A1, 2 OCPU, 12 GB, Ubuntu 24.04 aarch64); any Ubuntu 24.04 host works. `deploy/setup.sh` installs a pinned uv release and uses Ubuntu's Python 3.12.
- A Claude subscription token from `claude setup-token` for the service user.
- Caddy (or another TLS reverse proxy) in front of the server.

## Quick start

Full steps for both modes are in [docs/DEPLOY.md](docs/DEPLOY.md).

### Mac mode (main)

```sh
git clone https://github.com/HonzaPavel/voice-coding-mcp.git && cd voice-coding-mcp
mkdir -p ~/.config/voice-coding-mcp
cp config.example.json ~/.config/voice-coding-mcp/config.json && chmod 600 ~/.config/voice-coding-mcp/config.json
# edit it: stateDir and projectRoots under your home directory, claude.bin (or set VCM_CONFIG to use another path)
# publicUrl does not matter here: vcm-mac on sets it to your ts.net address
deploy/mac/vcm-mac on          # starts the server, opens the Funnel, prints the connector URL and the pair command
uv run voice-coding-mcp pair --config ~/.config/voice-coding-mcp/config.json
# Claude: Settings, Connectors, Add custom connector, URL https://<machine>.<tailnet>.ts.net/mcp
deploy/mac/vcm-mac off         # when you are done
```

Then, once:

- In the connector's settings in Claude, set every tool to "Always allow". Otherwise voice mode stops to ask for confirmation.
- Open `claude` once in each project folder and accept the trust prompt. Headless runs do not ask, but reopening a task with `resume_task_in_remote_control` needs a trusted folder.

Enter the pairing code only on a consent page that your own Claude app opened right after you ran `pair`. A page that asks for a code you did not just create, or that you reached from a link, is not yours: close it.

### VPS mode (alternative)

```sh
sudo git clone https://github.com/HonzaPavel/voice-coding-mcp.git /opt/voice-coding-mcp
sudo /opt/voice-coding-mcp/deploy/setup.sh vcm.example.com
# install Claude Code as vcm, run `claude setup-token`, put the token into /etc/voice-coding-mcp/env
sudo systemctl enable --now voice-coding-mcp
sudo voice-coding-mcp pair
# Claude: Settings, Connectors, Add custom connector, URL https://vcm.example.com/mcp
```

The full runbook, including deploy keys and the Claude side of pairing, is [Part B of DEPLOY.md](docs/DEPLOY.md#part-b-linux-vps-alternative).

## Switchboard

This fork (nilware-io/switchboard) adds tools to talk to Claude Code sessions that already run inside tmux, on the machine the server runs on and on other machines over SSH. The design is [docs/SWITCHBOARD.md](docs/SWITCHBOARD.md); the step-by-step setup on Windows with WSL is [docs/SETUP-WSL.md](docs/SETUP-WSL.md). Machines can also connect out to the hub instead of the hub connecting to them (for a WSL box behind NAT, laptops, LAN machines): no sshd or open port on the machine, setup in [docs/SETUP-AGENTS.md](docs/SETUP-AGENTS.md).

- `list_remote_sessions`, `remote_status`: the machines from the config, their allowed tmux sessions, and a short spoken summary of what a session's screen shows.
- `pending_input`: which sessions wait for you, and what each asks. An optional watcher pushes a notification when one starts waiting or finishes a long turn.
- `remote_send` and `remote_send_confirm`: type an instruction into a session after a read-back and your yes in a later turn; off by default, opt-in per machine in two places.
- `remote_send_keys` and `remote_resize`: answer a menu on a session's screen with keys (arrows, enter, escape, a choice number) or make a window bigger, with the same read-back and confirm; same opt-ins as `remote_send`.
- `remote_clear_input`: clear what is typed in an idle Claude Code session's input (it blocks `remote_send`), with the same read-back and confirm; Claude Code's grey prompt suggestion counts as nothing typed. Needs `switchboard-remote` 0.11.0.
- `usage_report` and `voice-coding-mcp usage`: active hours, billable amount and token cost per project or session.
- `timesheet_summary` and `voice-coding-mcp timesheet`: a draft timesheet per client and project (owner hours, agent hours, commits, pull requests) that you adjust and approve before exporting it; nothing is sent anywhere.

How it is built:

- The server runs `switchboard-remote` (a small stdlib-only Python dispatcher with a fixed set of verbs) on each machine, over SSH with a dedicated key that a forced command in `authorized_keys` ties to the dispatcher and to the tmux sessions you list there. Your own sessions on the workstation are machine `dev`, reached over SSH to `localhost`.
- Recommended layout: the server and its tasks run as a dedicated Linux user without sudo, under the systemd unit `deploy/linux/switchboard.service`, which cannot see your home. `deploy/linux/sb-linux on` starts it and opens the Funnel, `off` closes both. A same-user layout (`SB_MODE=user`, machine `dev` with `transport: "local"`) needs no sshd, but a manipulated task then runs as you.

```sh
sb-linux on        # start the hub, open the Funnel, print the connector URL and the pair command
sb-linux status
sb-linux off       # close the Funnel and stop the hub
```

Safety in short: read-only unless a machine opts in to sending in both the server config and its forced command; screen text is quoted as untrusted data, never treated as an instruction; risky sends (push, delete, deploy and similar) need an explicit yes to the risk; every send is audited; keep the Funnel off when you are not using it. If something is compromised, also remove the hub's keys from every `authorized_keys` and rotate them ([steps](docs/SETUP-WSL.md#if-something-is-compromised)).

## Turning it off and revoking access

- Mac: `deploy/mac/vcm-mac off` closes the Funnel and stops the server. Tokens stay valid, so `vcm-mac on` works again without pairing.
- VPS: `sudo systemctl stop voice-coding-mcp` (and `disable` to keep it off after a reboot).
- Linux or WSL (Switchboard): `sb-linux off` closes the Funnel and stops the hub.
- `voice-coding-mcp unpair` closes an open pairing window early.
- `voice-coding-mcp revoke-all --yes` removes every client and token; the connector has to be paired again. `voice-coding-mcp status` lists the connected clients. On the Mac add `--config <path>`, on the VPS use `sudo voice-coding-mcp ...`.

### If something is compromised

If you suspect that someone else used the connector, that a task was manipulated, or that your Claude account was accessed:

1. Remove the connector in Claude (Settings, Connectors).
2. Run `voice-coding-mcp revoke-all --yes`.
3. Stop the server: `deploy/mac/vcm-mac off` on the Mac, `sudo systemctl stop voice-coding-mcp` on the VPS, `sb-linux off` on a Linux or WSL hub. With Switchboard machines, also remove the hub's keys from every `authorized_keys`, read `voice-coding-mcp audit` and rotate the hub's SSH keys ([details](docs/SETUP-WSL.md#if-something-is-compromised)).
4. Look for persistence as the user Claude Code runs as: shell profiles (`~/.zshrc`, `~/.bashrc`, `~/.profile` and similar), `~/Library/LaunchAgents` on the Mac or user systemd units on Linux, `crontab -l`, `~/.claude/settings.json` (hooks and allow rules), and git hooks (`.git/hooks`) in every project.
5. On a VPS, replace the `claude setup-token` token in `/etc/voice-coding-mcp/env` with a new one.
6. Pair again only after that.

## Security

This server lets whoever holds a valid OAuth token run Claude Code on the machine it runs on. The details and known limits are in [DESIGN.md, Security model](docs/DESIGN.md#security-model); to report a vulnerability see [SECURITY.md](SECURITY.md).

- **Only the owner can pair.** A client can only be added during a short pairing window (10 minutes by default) opened on the machine itself with `voice-coding-mcp pair`, using a one-time code. An IP is locked out after 5 wrong codes, and the window closes after 50 wrong codes in total. Outside the window `/authorize` and `/register` are closed. No static bearer tokens. Pair, then let the window close.
- **Mac mode exposes your Mac.** While the Funnel is on, everything the server answers on its port (MCP, OAuth, health check) is reachable at a public `https://<machine>.<tailnet>.ts.net` address. Every MCP call needs a token, but a bug in this server or its dependencies would be reachable from anywhere. Keep the Funnel off when you are not using it.
- **Tasks run with the server user's rights.** In Mac mode that is your own macOS user: Claude Code can read and change everything you can, within its permission mode (`auto` by default, never bypassed). Nothing in this project sandboxes it further. Headless runs cannot ask, so anything that would need your approval is denied unless a rule you configured allows it: with `claude.settingSources` `["user"]` your own `~/.claude/settings.json` applies, including its allow rules and hooks. On the VPS only the root-owned managed settings count.
- **VPS mode isolates your own computer.** Claude Code runs as a dedicated user without sudo, the server code is owned by root and the systemd unit is hardened, so a compromise reaches that server and what you put there, not your Mac. Give that user only per-repository deploy keys.
- **Untrusted input.** Voice input and everything Claude Code reads are treated as data, never as approval; nothing is approved by voice. By default (`claude.access` `isolated`) Claude Code runs with `--strict-mcp-config`, without claude.ai connectors and without project or local settings from the repository (hooks from cloned repositories do not run). Projects can only be picked from configured roots.
- **`claude.access` `desktop` gives voice tasks everything your desktop Claude Code has.** All your claude.ai connectors (mail, calendar, issue trackers and so on), your user, plugin and approved project MCP servers, and in folders you trusted in Claude Code also the project and local settings (including their hooks). Only the voice server itself is excluded. A prompt injection in something a task reads then meets all of these. Turn it on only if you want that; `ask_session_readonly` and message relays stay isolated either way. See [REFERENCE.md](docs/REFERENCE.md#configuration).
- **Live sessions are reachable (Mac mode).** The server can list your recent Claude Code sessions, answer questions from a read-only copy and send messages into running ones, but only for sessions whose working directory is inside `projectRoots`. A relayed message is marked as coming from another session, not from you. Treat access to the connector like access to your terminal.
- **Your Claude account is a key.** The connector's tokens are stored in your Claude account, so anyone with access to that account can run tasks on your machine.
- **The address will be found.** The HTTPS certificate puts the host name into public Certificate Transparency logs, and scanners probe it within minutes. In VPS mode Caddy can optionally limit everything except `/authorize` to Claude's egress range `160.79.104.0/21`; Mac mode has no such restriction.
- **Known limit: tasks can reach the server state.** Claude Code runs as the same user as the server, so a task that gets code execution (for example through prompt injection) could rewrite `oauth.json` or `pairing.json` in `stateDir` and keep access, or persist in other ways. On the Mac nothing in this project prevents it; on the VPS root-owned managed settings and denied cron/at reduce it. If in doubt, follow [If something is compromised](#if-something-is-compromised).
- **Secrets and logs.** Tokens, codes and secrets are stored only as SHA-256 hashes, and logs show tokens only as a short fingerprint. Prompts and Claude's stderr are logged only when `logPrompts` is on. Notifications send the project name, and with `notify.includeResult` the result text (never for remote screen-status jobs), to the third party in `notify.url`. The optional session watcher (`watch`) pushes machine and tmux session names, and the redacted question only with `watch.includeQuestion`, never screen text.
- **Accepted trade-offs.** A refresh repeated within 60 s of a rotation gets the same answer (Claude may refresh twice at once); later reuse revokes the grant. Anyone can tell whether a pairing window is open, and while it is open a stranger who knows the URL can disrupt pairing (availability only, never access): keep windows short and run `pair` again.

## How it works

```
Mac mode: phone, Claude app (voice) -> Anthropic -> Tailscale Funnel (HTTPS, *.ts.net)
            -> voice-coding-mcp on 127.0.0.1:8787 on your Mac -> `claude -p` per task in <projectRoot>/<project>
VPS mode: phone, Claude app (voice) -> Anthropic -> HTTPS (Caddy, Let's Encrypt)
            -> voice-coding-mcp on 127.0.0.1:8787 on the VPS -> `claude -p` per task in <projectRoot>/<project>
```

- The Claude app talks to the server over MCP Streamable HTTP, authenticated with OAuth 2.1. Claude can register through [CIMD or DCR](docs/REFERENCE.md#terms); both work.
- Each task is one `claude -p` process in the project directory. The prompt goes in through stdin, the spoken answer comes back from Claude Code's stream-json output.
- Voice mode gives up on a tool call after roughly 30 to 40 seconds, so `run_task` waits up to 25 s (`tasks.syncWaitSeconds`). If Claude Code is still working, it returns a task number and the model asks for the result with `get_task_result`. An optional push notification (for example ntfy) tells you when a long task is done.
- Follow-up requests in the same project continue the same Claude Code session if it was active within the last 12 hours (`tasks.resumeWithinHours`).
- Single owner, single process, state in JSON files. The full contract is in [docs/DESIGN.md](docs/DESIGN.md).

## Tools

- `list_projects`: project names.
- `run_task`, `run_tasks_parallel`: run Claude Code in a project (up to 3 at once); short tasks answer directly, longer ones return a task number.
- `get_task_result`, `list_tasks`, `cancel_task`: follow and cancel tasks.
- `list_sessions`, `ask_session_readonly`, `send_message_to_session`: your own Claude Code sessions on the same machine (Mac mode): list them, ask a read-only copy, or pass a message into a running one.
- `resume_task_in_remote_control`: reopen a finished task with [Remote Control](docs/REFERENCE.md#terms), so you can continue it by typing in the Claude app.
- `run_task` with `remote_control`: start a task as a new Remote Control session you can watch, approve and continue in the Claude app (ask for it by voice, for example "run it so I can follow it on the phone").

### Push notifications on the phone

Claude Code's own push notifications reach the phone only for Remote Control sessions (`run_task` with `remote_control`, or a task reopened with `resume_task_in_remote_control`), not for headless tasks. Turn them on once on the machine: run `claude` in a terminal, type `/config`, and turn on "Push when Claude decides" and "Push when actions required". On the phone, allow notifications for the Claude app, signed in to the same account.

Remote Control sessions start only in folders Claude Code trusts: run `claude` once in each project folder on the machine and confirm the trust question.

Sessions are named `<title> (voice <task number>)`, where the title is a short label the voice model makes up from your request (`<project> (voice <task number>)` without one). Voice sessions stay open until you cancel their task. To have idle ones stopped, set `remoteControl.idleCloseMinutes` (off by default, for example 60). It is meant to pair with `remoteControl.handoffToDesktop: true` on a Mac, which opens each stopped voice session in the Claude desktop app, so it stays in the app's session list.

Details, the configuration keys, the CLI and the HTTP endpoints are in [docs/REFERENCE.md](docs/REFERENCE.md).

## Development

```sh
uv sync --locked               # Python 3.12 venv in .venv, exact versions from uv.lock
uv run pytest
uv run ruff check
uv run ruff format --check
uv run mypy --strict src tests
```

Layout: the package is in `src/voice_coding_mcp`, tests in `tests`, deployment files in `deploy`, the design contract in [docs/DESIGN.md](docs/DESIGN.md). Tests need no network and use a fake `claude` executable. CI runs the same commands (`uv sync --locked` fails if `uv.lock` is out of date) on Linux (x86_64 and arm64) and macOS, and checks the deploy scripts with shellcheck. Dependencies resolve only to releases at least 14 days old; for an urgent security fix see [Dependency updates](docs/REFERENCE.md#dependency-updates).

## Status

Prototype. Several behaviors of the Claude voice mode still need to be verified with the real app (see [To verify with the real app](docs/DESIGN.md#to-verify-with-the-real-app)).

Security issues: please report them privately, see [SECURITY.md](SECURITY.md).

## License

MIT, see [LICENSE](LICENSE).
