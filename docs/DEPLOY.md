# Deployment runbook

voice-coding-mcp can run in two ways. Pick one:

| | Part A: Mac (main) | Part B: Linux VPS (alternative) |
|---|---|---|
| Where it runs | Your own Mac, next to your Claude Code sessions | A dedicated VPS (the runbook uses Oracle Cloud Always Free) |
| Reachable from the internet | Only while switched on (`vcm-mac on` to `vcm-mac off`), via Tailscale Funnel | Always, via Caddy and Let's Encrypt |
| Claude Code runs as | Your own macOS user | A dedicated user `vcm` without sudo |
| Your own sessions | Can list, query and message them | Not reachable |
| Isolation of your own computer | None while on | Your computer is not exposed |

A third way, a Linux or WSL workstation that also reaches your tmux sessions (Switchboard), has its own guide: [SETUP-WSL.md](SETUP-WSL.md).

Pairing the Claude app works the same in both; the steps are in [Part B, section 8](#8-pair-the-claude-app).

## Part A: Mac on demand with Tailscale Funnel (main)

The server runs on your own Mac and is reachable from the internet only while you switch it on. Tailscale Funnel publishes it at a stable `https://<machine>.<tailnet>.ts.net` address; TLS ends on the Mac, no port is opened on your router, and the address stays the same across on and off, so the Claude connector stays paired. `deploy/mac/vcm-mac` does the switching. Keep in mind that Claude Code then runs as your own user with access to everything you can read, so the VPS sandbox notes in Part B do not apply. Your own `~/.claude/settings.json` (allow rules, hooks) also applies to remote tasks, and a task could change the server's OAuth state in `stateDir`; if in doubt, run `voice-coding-mcp revoke-all --yes` and pair again. See [Security](../README.md#security) in the README before you start, and [If something is compromised](../README.md#if-something-is-compromised) for what to do when in doubt.

One-time setup:

1. Install the Tailscale app for macOS (https://tailscale.com/download/mac), open it and sign in with a personal account. `vcm-mac` uses the CLI inside `/Applications/Tailscale.app` and falls back to `tailscale` on `PATH` (set `VCM_TAILSCALE` to override). It prefers the app binary because the app's `/usr/local/bin/tailscale` is a shell wrapper; either way, when it stops a waiting `tailscale funnel` it stops the whole process tree, so no CLI is left waiting for approval.
2. In the Tailscale admin console, DNS page: make sure MagicDNS is enabled and enable HTTPS certificates. Funnel needs both, plus the `funnel` node attribute in the tailnet policy file.
3. Optionally rename the machine in the admin console (Machines) to something short, and do it before the first `vcm-mac on`: the name is part of the connector URL. The HTTPS certificate puts the name into public Certificate Transparency logs, where scanners pick it up and start probing it within minutes, so pick a neutral one (no personal name or project name). If you rename it later, Tailscale keeps the serve entry for the old name (`Web` and `AllowFunnel` under `<oldname>.<tailnet>.ts.net:443`), and `tailscale funnel --https=443 off` only acts on the current name. Check `tailscale funnel status` and remove the old entry; use `tailscale funnel reset` only if nothing else is served from this Mac. You also need to pair the connector again with the new URL.
4. Install uv (`brew install uv` or https://docs.astral.sh/uv/). jq ships with macOS 15 and later; otherwise `brew install jq`. `vcm-mac` starts the server with `uv run --frozen --project <repo>`, which creates the repo's `.venv` from `uv.lock` on first use and downloads Python 3.12 itself if it is missing.
5. Create the config (default `~/.config/voice-coding-mcp/config.json`, or set `VCM_CONFIG` to another path), mode 600, from `config.example.json`: `stateDir` and `projectRoots` under your home directory, `claude.bin` pointing at your Claude Code, `listen.port` free (default 8787). `publicUrl` (an origin only, no path) is filled in by `vcm-mac on` (it rewrites the config with your `ts.net` address, keeping mode 600), so the example value can stay. `vcm-mac` reads `listen.host` and `listen.port` from the config.
6. Run `deploy/mac/vcm-mac on`. If Funnel is not enabled in the tailnet yet, it prints the approval link from Tailscale and stops; open the link, approve (this adds the HTTPS certificate and the `funnel` attribute), and run `vcm-mac on` again. If Tailscale instead reports that HTTPS or the `funnel` node attribute is missing, `vcm-mac` says which one and where to enable it (DNS page, or `nodeAttrs` in the tailnet policy file). It also stops early when MagicDNS is off. `vcm-mac on` counts as done only when `tailscale funnel status` shows the Funnel to the server's port, since the CLI can exit without error and configure nothing. The first request over the new name can take a moment while the certificate is issued; DNS for Funnel can take up to 10 minutes.
7. On success it prints the connector URL `https://<machine>.<tailnet>.ts.net/mcp`. Pair once as described in [Pair the Claude app](#8-pair-the-claude-app) in Part B (the Claude side is the same): run the `pair` command it prints instead of `sudo voice-coding-mcp pair` (`uv run --project <repo> voice-coding-mcp pair --config <config>`), then add the custom connector in Claude with that URL and set every tool of the connector to "Always allow".
8. Open `claude` once in each project folder and accept the trust prompt. Headless tasks do not check it, but `resume_task_in_remote_control` refuses an untrusted folder.

Everyday use:

```sh
deploy/mac/vcm-mac on       # start the server (detached, under caffeinate) and open the Funnel
deploy/mac/vcm-mac status   # on or off, connector URL, server pid, then `voice-coding-mcp status`
deploy/mac/vcm-mac off      # close the Funnel and stop the server; safe to run twice
```

It stays on until `vcm-mac off`, also after the terminal or the Claude Code session that started it is closed. You can run both commands from a Remote Control session on your phone. Notes:

- `off` acts only if `tailscale funnel status` shows a proxy to the server's `<listen.host>:<port>`. It takes the port from the `funnel.port` file that `on` writes, else from the config; if neither can be read (for example without jq) it warns that it assumes 8787 and that a Funnel to another port has to be closed by hand (`tailscale funnel status`). It then runs `tailscale funnel --https=443 off`, which removes everything served on public port 443 (other ports stay). It never runs `tailscale funnel reset`; if the server's proxy is still listed afterwards (for example because Tailscale asks for confirmation when port 443 has several handlers), it prints a warning with the command to run by hand.
- The server log is `vcm-mac-server.log` in `<config dir>/logs` (or `VCM_LOG_DIR`), the pid file is in `<config dir>/run` (or `VCM_RUN_DIR`). Both are created private (umask 077). The server is started with `serve --pid-file <run dir>/server.pid`, writes its own pid there and holds a `flock` on the file while it runs. `vcm-mac` treats the server as running exactly when that lock is held (checked with `/usr/bin/lockf -kns -t 0 <file> true`, which takes the lock only for an instant), so a pid file left by a crash, `kill -9` or a reboot is recognized as stale and never leads to signalling an unrelated process. `on` and `off` run under `/usr/bin/lockf -t 0 <run dir>/vcm-mac.lock`, so a second `on` or `off` started meanwhile is refused; the kernel releases that lock when the run ends, also after a crash. `status` takes no lock.
- `off` sends SIGTERM to the pid in the pid file (the Python server; uv and caffeinate exit after it), waits up to 25 s, then sends SIGKILL to that pid only.
- `caffeinate -is` keeps the Mac from idle sleep while the server runs (system sleep only on power adapter). Closing the lid still sleeps the Mac, and then the connector cannot reach it.
- While it is off, the Claude app shows the connector as unreachable; tokens stay valid (refresh tokens for `oauth.refreshTokenDays`), so after `vcm-mac on` it works again without pairing.

## Part B: Linux VPS (alternative)

How to run voice-coding-mcp on an Oracle Cloud Always Free VPS (Ampere A1, Ubuntu 24.04) and connect it to the Claude app. The examples use the host `vcm.example.com`; replace it with yours.

Overview of what ends up on the server:

| Path | What | Owner, mode |
|---|---|---|
| `/opt/voice-coding-mcp` | Server code (git checkout plus the Python venv `.venv`, built by `uv sync --frozen`) | root, not writable by `vcm` |
| `/usr/local/bin/uv` | uv, pinned release with checked SHA-256 | root 0755 |
| `/etc/voice-coding-mcp/config.json` | Configuration | root:vcm 0640 |
| `/etc/voice-coding-mcp/env` | `CLAUDE_CODE_OAUTH_TOKEN` | root:vcm 0640 |
| `/var/lib/voice-coding-mcp` | State (OAuth hashes, tasks, sessions) | vcm 0700 |
| `/home/vcm/projects` | Projects Claude Code works in | vcm 0700 |
| `/etc/caddy/Caddyfile` | TLS reverse proxy | root 0644 |
| `/usr/local/bin/voice-coding-mcp` | CLI wrapper, runs as `vcm` | root 0755 |

### 1. Create the Oracle instance

1. Oracle Cloud console, Compute, Instances, Create instance.
2. Image: Canonical Ubuntu 24.04 (the regular image, not Minimal). With an A1 shape the console picks the aarch64 build.
3. Shape: Ampere, `VM.Standard.A1.Flex`, 2 OCPU and 12 GB memory. If the console reports "Out of capacity", try another availability domain or try again later.
4. Networking: a VCN with a public subnet, "Assign a public IPv4 address" on. Consider a reserved public IP so the DNS record survives re-creating the instance (check your free allowance first).
5. SSH key: upload your public key. Boot volume: the default (about 47 GB) is enough; Always Free covers 200 GB of block storage in total.
6. Open the ports in the VCN: Networking, Virtual cloud networks, your VCN, Security Lists, Default Security List, Add Ingress Rules:
   - Source `0.0.0.0/0`, TCP, destination port `80` (Let's Encrypt HTTP challenge and redirect).
   - Source `0.0.0.0/0`, TCP, destination port `443`.
   - Optionally restrict the existing port 22 rule to your own IP range.

   The instance has its own iptables firewall on top of this; `setup.sh` opens 80 and 443 there.
7. If you used the OCI CLI or Terraform to create the instance, delete the admin API key afterwards: remove `~/.oci/config` and the key file on your machine and delete the API key in the console (Profile, API keys). Never copy it to the VPS.

### 2. DNS

Create an A record at your DNS provider: `vcm.example.com` pointing to the instance's public IP. A short TTL (300 s) helps while setting up. Check it before continuing:

```sh
dig +short vcm.example.com A
```

Caddy requests the certificate as soon as it starts, so the record should resolve before step 3.

### 3. Run setup.sh

SSH in as `ubuntu` and run:

```sh
sudo apt-get update && sudo apt-get install -y git
sudo git clone <repo-url> /opt/voice-coding-mcp
sudo /opt/voice-coding-mcp/deploy/setup.sh vcm.example.com
```

The script is idempotent. It installs git, curl, jq, ripgrep, bubblewrap, socat, Python 3.12 (Ubuntu's `python3`), uv (official GitHub release, version and SHA-256 pinned in setup.sh) and Caddy (official repository), opens 80 and 443 in iptables and persists the rules, creates the user `vcm` without sudo, installs the app (`uv sync --frozen --no-dev` into a root-owned venv in `/opt/voice-coding-mcp/.venv`, exact versions from `uv.lock`, no Python download), the config, an empty env file, the systemd unit, the CLI wrapper and the Caddyfile. It also writes root-owned Claude Code managed settings to `/etc/claude-code/managed-settings.json` (only managed hooks and permission rules, no bypass mode, no claude.ai connectors, Claude's file tools denied on the server state, the config and `~/.claude/settings.json`) and adds `vcm` to `/etc/cron.deny` and `/etc/at.deny`. Because only managed permission rules apply, `allow` rules in `~vcm/.claude/settings.json` are ignored; put any you need in a root-owned drop-in such as `/etc/claude-code/managed-settings.d/local.json`, which Claude Code merges with `managed-settings.json` and setup.sh leaves alone. It does not start voice-coding-mcp. It prints the remaining steps at the end.

Check that Caddy got a certificate:

```sh
sudo journalctl -u caddy --since "10 min ago" | grep -i certificate
curl -sI https://vcm.example.com/healthz   # 502 until the app runs, but TLS must work
```

### 4. Install Claude Code for vcm

Use the official native installer as `vcm` (not root):

```sh
sudo -iu vcm
curl -fsSL https://claude.ai/install.sh | bash
claude --version          # installed to ~/.local/bin/claude
```

Create a long-lived token for the subscription, still as `vcm`:

```sh
claude setup-token
```

It prints a URL. Open it in your browser, sign in to the Claude account the tasks should use, and paste the code back. The command prints a token (valid for one year). Leave the `vcm` shell (`exit`).

Put the token into the env file as your admin user. Use the editor, never `echo` (shell history) or command arguments:

```sh
sudo -e /etc/voice-coding-mcp/env
```

```
CLAUDE_CODE_OAUTH_TOKEN=<the token>
```

Keep `ANTHROPIC_API_KEY` unset everywhere: it would take precedence over the subscription token.

Claude Code's auto-updater is disabled for tasks (`DISABLE_AUTOUPDATER=1`). Update it yourself from time to time with `sudo -iu vcm claude update`.

### 5. Clone projects with deploy keys

Every repository gets its own deploy key, so a leaked key exposes one repository only. As `vcm`:

```sh
sudo -iu vcm
git config --global user.name "Your Name (vcm)"
git config --global user.email "you@example.com"

repo=myproject
ssh-keygen -t ed25519 -N "" -C "vcm-$repo" -f ~/.ssh/deploy_$repo
cat >> ~/.ssh/config <<EOF
Host github-$repo
  HostName github.com
  User git
  IdentityFile ~/.ssh/deploy_$repo
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
cat ~/.ssh/deploy_$repo.pub
```

Add the public key in GitHub: repository, Settings, Deploy keys, Add deploy key. Tick "Allow write access" only if Claude Code should push. Then:

```sh
git clone git@github-$repo:<owner>/$repo.git ~/projects/$repo
```

Do not give `vcm` personal access tokens, cloud admin keys or SSH keys that reach other machines. Claude Code can read everything `vcm` can.

Project settings from a repository (`.claude/settings.json` and `.claude/settings.local.json`, both of which a repository can commit) are ignored by default (`claude.settingSources` is `user`), so hooks from a cloned repository do not run unattended.

### 6. Edit config.json

```sh
sudo -e /etc/voice-coding-mcp/config.json
```

`setup.sh` set `publicUrl` to `https://vcm.example.com`. Check at least `projectRoots` (default `/home/vcm/projects`), `claude.model`, `tasks.syncWaitSeconds` and `notify`. See the [configuration reference](REFERENCE.md#configuration) and [DESIGN.md](DESIGN.md#config). `publicUrl` must be an origin only (no path).

For an optional push notification when a long task finishes, point `notify.url` at an ntfy topic (`https://ntfy.sh/<long random topic>`) and subscribe to it in the ntfy app on the phone.

### 7. Start the service

```sh
sudo systemctl enable --now voice-coding-mcp
sudo systemctl status voice-coding-mcp
sudo journalctl -u voice-coding-mcp -f
curl -s https://vcm.example.com/healthz      # {"ok":true}
curl -s http://127.0.0.1:8787/healthz        # same, from the server itself
sudo voice-coding-mcp status                 # pairing window, clients, task counts
```

### 8. Pair the Claude app

1. On the server: `sudo voice-coding-mcp pair` (or `pair --minutes 20` for a longer window, at most 1440). It prints a code like `ABCDE-FGHJK`, its expiry in local time and the connector URL. The code is valid for `oauth.pairingMinutes` (10 minutes); each IP gets 5 wrong codes (then it is locked out until the window ends) and all IPs together 50 (then the window closes); lower case and the look-alikes I, L and O are accepted. `sudo voice-coding-mcp unpair` closes the window early. Enter the code only on a consent page that your own Claude app opened right after you ran `pair`; a page you reached from a link, or one asking for a code you did not just create, is not yours.
2. In Claude (claude.ai in a browser or Claude Desktop): Settings, Connectors, Add custom connector.
   - Name: for example `Voice coding`.
   - URL: `https://vcm.example.com/mcp`
   - If Advanced settings offers "Use Claude's published identity", choose it. Otherwise leave the client ID empty and Claude registers itself.
3. Click Add, then Connect. A browser tab opens the server's authorization page. Enter the pairing code and confirm.
4. Back in the connector's settings, set the tool permissions to "Always allow" for all tools. Otherwise voice mode stops to ask for confirmation.

After a successful pairing `/authorize` and `/register` are closed again. To pair another client later, run `pair` again. `sudo voice-coding-mcp revoke-all --yes` revokes every client and token (the connector then has to be paired again). `sudo voice-coding-mcp status` lists the connected clients. If you suspect a compromise, follow [If something is compromised](../README.md#if-something-is-compromised) in the README.

### 9. First voice test

1. In the Claude mobile app, start a new chat, make sure the connector is enabled in the tools menu, and switch to voice mode.
2. Say: "Which projects do I have?" (calls `list_projects`).
3. Say: "In project myproject, what was the last commit about?" A short task should answer within one tool call.
4. Ask for something longer ("run the tests in myproject and tell me what fails"). It should return a task number; then ask "Is task 1 done?".
5. Watch the log meanwhile: `sudo journalctl -u voice-coding-mcp -f`.

Note what happens while waiting between result checks (does the voice model stay quiet), and how project names are recognized. These are open questions in `docs/DESIGN.md`.

### Updates

```sh
cd /opt/voice-coding-mcp
sudo git pull --ff-only
sudo deploy/setup.sh vcm.example.com    # uv sync --frozen, reinstalls unit and Caddyfile, restarts if running
```

Minimal variant when only the code changed:

```sh
cd /opt/voice-coding-mcp
sudo git pull --ff-only
sudo UV_PYTHON=/usr/bin/python3.12 UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --no-editable --compile-bytecode
sudo systemctl restart voice-coding-mcp
```

Stopping the service (SIGTERM) ends queued and running tasks as failed ("server stopped (SIGTERM)") and closes within 20 s; a crash leaves them to be marked "server restarted" on the next start. Restart when nothing is running (`sudo voice-coding-mcp status` shows the task counts).

Ubuntu installs security updates automatically (unattended-upgrades). Reboot when `/var/run/reboot-required` exists.

### Backups

- `/var/lib/voice-coding-mcp`: OAuth state (hashes only), tasks, sessions per project. Writes are atomic, so a copy while running is consistent per file:

  ```sh
  sudo tar -C /var/lib -czf vcm-state-$(date +%F).tar.gz voice-coding-mcp
  ```

  Losing it is not a disaster: pair again and the task history is gone.
- `/etc/voice-coding-mcp`: config and the token. The env file is a secret; store a backup only encrypted, or simply create a new token with `claude setup-token`.
- Projects: push your work to the remote; the VPS is not the only copy.
- Oracle offers boot volume backups (a few are included in Always Free) as a coarse safety net.

### Oracle pitfalls

- Always Free Ampere A1 is 2 OCPU and 12 GB, counted as 1 500 OCPU hours and 9 000 GB hours per month. One instance of that size running all month uses the whole allowance, so do not add a second A1 instance or resize this one unless you accept charges.
- Idle reclamation: Oracle may stop Always Free instances that are idle for 7 days, meaning CPU usage (95th percentile), network usage and memory usage are all below 20 percent. This server is idle most of the time. Upgrading the account to Pay As You Go (still free within the limits) avoids reclamation; otherwise keep some regular load. Check the instance's Metrics page now and then.
- Delete the admin API key (`~/.oci/config` and the key file) after creating the VPS, see step 1.
- Keep other workloads (scrapers, bots) on the same VPS under a different user or in a container, never as `vcm`: Claude Code can read everything `vcm` can, and the other workload should not be able to touch the projects or the token.
- An ephemeral public IP is lost when the instance is terminated (not on reboot). Update the DNS record if it changes.

### Troubleshooting

- Tasks fail with an authentication error, or `journalctl -u voice-coding-mcp` shows at startup that the logged-in user's credentials are used (no `CLAUDE_CODE_OAUTH_TOKEN`): check `/etc/voice-coding-mcp/env` and restart.
- Caddy has no certificate: DNS does not point at the instance yet, or port 80 is blocked (VCN security list or iptables, check `sudo iptables -L INPUT -n --line-numbers`).
- The connector says it cannot connect: `curl -s https://vcm.example.com/.well-known/oauth-protected-resource/mcp` must return JSON with `resource` equal to the URL typed in the dialog.
- `/authorize` answers 403: the pairing window is closed or used up; run `sudo voice-coding-mcp pair` again.
- Claude Code's own sandbox (bubblewrap) needs unprivileged user namespaces. Ubuntu 24.04 restricts them through AppArmor (`kernel.apparmor_restrict_unprivileged_userns`). The systemd unit does not block namespaces, but if Claude Code reports a sandbox failure, check that setting and the AppArmor log (`sudo journalctl -k | grep -i apparmor`).
