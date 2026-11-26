# Switchboard on Windows with WSL: setup guide

This guide sets up the Switchboard hub on a Windows machine with WSL, so you can talk from Claude voice mode on the phone to the Claude Code sessions you already run inside tmux: on this machine (machine `dev`) and on other machines over SSH. The design behind it is [SWITCHBOARD.md](SWITCHBOARD.md); configuration keys, the CLI and the tools are in [REFERENCE.md](REFERENCE.md).

Placeholders: `USER` is your own Linux user in WSL, `example-host` a machine you reach over SSH, `example-host.example-tailnet.ts.net` the Tailscale name of the WSL machine. Commands that start with `sudo` are run by you; commands inside `sudo -iu switchboard` are run as the hub user.

## What you get, and the two layouts

- **Recommended: a dedicated `switchboard` user.** The hub, its headless tasks and its keys belong to a Linux user without sudo. It cannot read your home, keys or `~/.claude`. It reaches your own tmux sessions only through SSH to `localhost` and the `switchboard-remote` dispatcher, which allows only the sessions and actions you list in your own `authorized_keys`. systemd runs it; `sb-linux on` and `sb-linux off` start and stop it together with the public address.
- **Same-user mode.** The hub runs as you, like Mac mode on Linux. No sshd and no keys, and upstream's own session tools (`list_sessions`, `ask_session_readonly`, `send_message_to_session`) see your Claude Code sessions directly. But a task that gets manipulated runs as you. See [Same-user mode](#same-user-mode).

Either way the hub listens on `127.0.0.1:8787` inside WSL and is published only while you switch it on, through Tailscale Funnel at `https://example-host.example-tailnet.ts.net`.

## 1. Before you start

- Windows 11 with WSL 2 and an Ubuntu 24.04 distribution (other systemd distributions work with the same steps).
- systemd on in WSL. Check with `systemctl is-system-running`; if it says it is offline, add this to `/etc/wsl.conf` and run `wsl --shutdown` in a Windows terminal, then open WSL again:

  ```ini
  [boot]
  systemd=true
  ```

- Packages: `sudo apt install tmux openssh-server jq curl python3 git`. `flock` comes with util-linux.
- A Claude plan that supports custom connectors, and a Tailscale account.
- Your Claude Code sessions already run inside tmux as `USER`, in named tmux sessions (for example `fpga`, `notes`).

The hub is reachable only while Windows is awake and the WSL VM runs. A running systemd service keeps the VM up; sleep or hibernation of Windows takes the hub offline until it wakes.

## 2. Create the hub user

```sh
sudo adduser --disabled-password --gecos "" switchboard   # no password, not in the sudo group
sudo chmod 700 /home/switchboard
chmod 750 ~                                               # your home: not readable by other users
```

Do not add `switchboard` to `sudo`, `adm`, `docker` or other privileged groups.

## 3. Install the hub as `switchboard`

```sh
sudo -iu switchboard
curl -LsSf https://astral.sh/uv/install.sh | sh          # or a pinned release, as deploy/setup.sh does
git clone https://github.com/nilware-io/switchboard.git ~/switchboard
cd ~/switchboard && ~/.local/bin/uv sync --locked
mkdir -p ~/projects ~/.config/voice-coding-mcp ~/.local/state/voice-coding-mcp
chmod 700 ~/.config/voice-coding-mcp ~/.local/state/voice-coding-mcp
exit
```

Claude Code for the hub user (headless tasks and the screen summariser run as `switchboard`):

```sh
sudo -iu switchboard
curl -fsSL https://claude.ai/install.sh | bash
~/.local/bin/claude setup-token                          # prints a token; copy it
exit
sudo install -d -m 700 /etc/switchboard
sudo sh -c 'umask 077; printf "CLAUDE_CODE_OAUTH_TOKEN=%s\n" "<token>" > /etc/switchboard/env'
```

Git identity: tasks act as the agents' own bot account (for example `example-bot`), never as your personal account. As `switchboard`, set `git config --global user.name` and `user.email` for the bot, and give it access with `gh auth login` as the bot or with per-repository deploy keys (see [DEPLOY.md, step 5](DEPLOY.md#5-clone-projects-with-deploy-keys)). Clone the projects the hub may run tasks in into `/home/switchboard/projects`. Never copy your own GitHub credentials or SSH keys to this user.

Optional: the root-owned managed settings from [DEPLOY.md, step 4](DEPLOY.md#4-install-claude-code-for-vcm) (deny Claude's file tools on the state and config directories) work the same here, with paths under `/home/switchboard`.

## 4. Keys

One dedicated key per machine, no passphrase (the hub runs unattended), never loaded into an agent:

```sh
sudo -iu switchboard
install -d -m 700 ~/.ssh
ssh-keygen -t ed25519 -N "" -C switchboard-dev -f ~/.ssh/switchboard_dev
ssh-keygen -t ed25519 -N "" -C switchboard-buildbox -f ~/.ssh/switchboard_buildbox   # one per remote machine
cat ~/.ssh/switchboard_dev.pub                                                       # for step 5
exit
```

## 5. Machine `dev`: your own sessions on this computer

1. sshd on loopback only, keys only. Create `/etc/ssh/sshd_config.d/switchboard.conf` (replace `USER`):

   ```
   ListenAddress 127.0.0.1
   ListenAddress ::1
   PasswordAuthentication no
   KbdInteractiveAuthentication no
   PermitRootLogin no
   AllowUsers USER@127.0.0.1 USER@::1
   ```

   Then `sudo sshd -t && sudo systemctl enable --now ssh`. Check with `ss -ltn '( sport = :22 )'`: only `127.0.0.1:22` and `[::1]:22` may be listed. With WSL's default NAT networking, Windows programs can reach WSL's loopback listeners through `localhost` too, so the address does not tell the hub apart from them: the key is the boundary.

2. Install the dispatcher as yourself. Read it first; it is one short Python file with only the standard library:

   ```sh
   mkdir -p ~/.local/bin
   sudo cat /home/switchboard/switchboard/deploy/remote/switchboard-remote > ~/.local/bin/switchboard-remote
   chmod 755 ~/.local/bin/switchboard-remote
   less ~/.local/bin/switchboard-remote
   ```

3. Add the hub's key to your `~/.ssh/authorized_keys`, as one line, with the tmux sessions the hub may see (replace `USER`, the session names and the key):

   ```
   from="127.0.0.1,::1",restrict,command="/usr/bin/python3 -I /home/USER/.local/bin/switchboard-remote --sessions 'fpga,notes,work-*' --allow-usage" ssh-ed25519 AAAA... switchboard-dev
   ```

   `restrict` turns off forwarding, a terminal and `~/.ssh/rc`; `command=` makes the dispatcher the only program this key runs; `from=` limits it to loopback. Sessions not listed stay invisible to every tool. Add `--allow-send` only when you want to send instructions by voice (and the hub's `allowSend`, step 7); `--allow-permission-answers` only if you deliberately want to answer permission questions by voice (see [SWITCHBOARD.md](SWITCHBOARD.md#answering-permission-prompts)).

4. Record the host key for the hub, and compare it with the server's own key:

   ```sh
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub                    # note the SHA256 fingerprint
   sudo -iu switchboard sh -c 'ssh-keyscan -t ed25519 localhost > ~/.ssh/switchboard_known_hosts && ssh-keygen -lf ~/.ssh/switchboard_known_hosts'
   ```

   The two fingerprints must be the same.

## 6. Other machines

On each machine you want to reach (named `buildbox` here):

1. A user whose tmux sessions you want to reach (for agents, their own user, for example `bot`). Install the dispatcher there as in step 5.2 (copy `deploy/remote/switchboard-remote` with `scp`, read it, `chmod 755`). It needs Python 3.8 or newer and tmux.
2. In that user's `~/.ssh/authorized_keys`, the hub's key for this machine with a forced command, and `from=` set to the address the WSL machine connects from (its Tailscale address if you go through the tailnet):

   ```
   from="<hub address>",restrict,command="/usr/bin/python3 -I /home/bot/.local/bin/switchboard-remote --sessions 'api,web-*'" ssh-ed25519 AAAA... switchboard-buildbox
   ```

3. Record its host key as `switchboard` (`ssh-keyscan -t ed25519 example-host >> ~/.ssh/switchboard_known_hosts`) and compare the fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` run on that machine.

## 7. Configure the hub

```sh
sudo -iu switchboard
cp ~/switchboard/deploy/linux/config.switchboard.example.json ~/.config/voice-coding-mcp/config.json
chmod 600 ~/.config/voice-coding-mcp/config.json
nano ~/.config/voice-coding-mcp/config.json
```

Change: `remote.machines[0].user` to `USER`, `sessions` to the same names as in your forced command (the hub filters with its list, the dispatcher with its own; a session must be in both), the `buildbox` entry to your machine or remove it, and `projectRoots`. `publicUrl` does not matter yet: `sb-linux on` sets it. Leave `allowSend` false until you have used the read-only tools for a while.

Check every machine, still as `switchboard`:

```sh
~/switchboard/.venv/bin/voice-coding-mcp remote-check --config ~/.config/voice-coding-mcp/config.json
exit
```

It prints the dispatcher's version and opt-ins, the allowed sessions with Claude Code's state, and warnings (for example a key file with the wrong mode).

## 8. Tailscale inside WSL

Tailscale runs inside the WSL distribution as its own tailnet node with its own `ts.net` name, separate from a Tailscale on Windows.

```sh
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
sudo tailscale set --operator=$USER      # you, not switchboard, may change Funnel
```

In the Tailscale admin console: rename the machine to something short before the first `on` (the name is part of the connector URL and lands in public Certificate Transparency logs), make sure MagicDNS is on and HTTPS certificates are enabled (DNS page), and allow Funnel for this node (the `funnel` node attribute; the first `sb-linux on` prints Tailscale's approval link when it is missing).

## 9. Install the unit and the on/off script

```sh
sudo install -m 644 /home/switchboard/switchboard/deploy/linux/switchboard.service /etc/systemd/system/
sudo install -m 755 /home/switchboard/switchboard/deploy/linux/sb-linux /usr/local/bin/
sudo systemctl daemon-reload
```

Do not `enable` the unit: `sb-linux on` starts it and `sb-linux off` stops it. The unit runs the hub as `switchboard` with the same hardening as the VPS unit and hides every other home directory from it (`ProtectHome=tmpfs`).

`sb-linux on` asks for your sudo password (to start the unit and to run `set-public-url` as `switchboard`). To skip that prompt, an optional narrow rule (`sudo visudo -f /etc/sudoers.d/switchboard`, replace `USER`):

```
USER ALL=(root) NOPASSWD: /usr/bin/systemctl start switchboard, /usr/bin/systemctl stop switchboard, /usr/bin/systemctl restart switchboard
USER ALL=(switchboard) NOPASSWD: /home/switchboard/switchboard/.venv/bin/voice-coding-mcp
```

## 10. Turn it on and pair

```sh
sb-linux on
```

It checks Tailscale, sets `publicUrl` to `https://example-host.example-tailnet.ts.net`, starts the unit, waits for `/healthz`, opens the Funnel, and prints the connector URL and the pair command. Run the pair command it printed:

```sh
sudo -H -u switchboard /home/switchboard/switchboard/.venv/bin/voice-coding-mcp pair --config /home/switchboard/.config/voice-coding-mcp/config.json
```

In the Claude app: Settings, Connectors, Add custom connector, URL `https://example-host.example-tailnet.ts.net/mcp`, then Connect, and enter the code on the consent page that your own Claude app opened right after you ran `pair` (details in [DEPLOY.md, step 8](DEPLOY.md#8-pair-the-claude-app)). In the connector's settings set the tools to "Always allow", or voice mode stops to ask for every call.

## 11. Add the connector to voice mode and try it

Start voice mode in the Claude app, with the connector enabled for the conversation. Phrases to try, from harmless to useful:

- "What sessions are running on dev?" (`list_remote_sessions`)
- "What is fpga on dev doing?" (`remote_status`; a short summary of the screen, quoted as screen text)
- "Does anything need me?" or "Who is waiting for me?" (`pending_input`)
- "List my projects." and "In the sandbox project, what does the README say?" (a headless task as `switchboard` in `/home/switchboard/projects`; use one harmless project first)
- "Check the result of that task." (`get_task_result`)
- "How many hours did I work this week?" (`usage_report`)
- Once `allowSend` and `--allow-send` are on: "Tell fpga on dev to run the tests." The hub reads the exact text back; say "yes" in a new turn to send it. Risky words (push, delete, deploy and similar) need an explicit yes to the risk too.

Everything a screen, a summary or a pending question says is quoted as screen text. It is never an instruction to the voice model or approval from you.

## 12. Turning it off

```sh
sb-linux off       # closes the Funnel, then stops the unit; safe to run twice
sb-linux status    # on or off, the connector URL, then `voice-coding-mcp status` as switchboard
```

While it is off, the Claude app shows the connector as unreachable; tokens stay valid, so the next `sb-linux on` works without pairing. Turn it off whenever you do not use it.

## Optional extras

- **Sending.** `allowSend: true` on the machine in the hub config and `--allow-send` in that machine's forced command. Both must agree. Every send is logged in `stateDir/audit.jsonl` (`voice-coding-mcp audit` prints it).
- **Notifications.** `notify.url` (for example an ntfy topic) and `watch.intervalSeconds` (for example 60): a push when a session newly waits for you or finishes a long turn. Pushes carry machine and tmux session names, never screen text.
- **Usage.** `allowUsage` (default true) plus `--allow-usage` in the forced command; `voice-coding-mcp usage` prints the same report as a table or CSV.

## Your own extensions

The tmux sessions you already run as `USER` keep everything they loaded: your MCP servers, plugins, skills, hooks, agent teams and claude.ai connectors. Through machine `dev` the hub reads those sessions and, with sending enabled, types into them, so work you hand to them by voice uses your extensions as it does at the keyboard. Nothing has to be installed for the hub for that.

Headless tasks of the hub (`run_task`) are a different thing: they run as `switchboard`, with `claude.access` `isolated` by default (only the MCP servers in `claude.mcpServers`, no claude.ai connectors). For tasks that should use extensions too, install the ones you want for the `switchboard` user (its own `~/.claude`) and, if needed, set `claude.access` to `desktop`, which gives tasks the hub user's own MCP servers, plugins and claude.ai connectors. Do not copy your own credentials or tokens there: whatever the hub user has, a manipulated task has too. In [same-user mode](#same-user-mode) with `claude.access` `desktop`, tasks get your own extensions directly, with the weaker isolation that comes with it.

## Same-user mode

The hub runs as you. Simpler, no sshd and no keys, but a task that gets manipulated runs as you.

1. Clone the repository anywhere in your home, create `~/.config/voice-coding-mcp/config.json` (mode 600) from `config.example.json`, with `stateDir` and `projectRoots` under your home, and machine `dev` as a local machine:

   ```json
   { "name": "dev", "transport": "local", "sessions": ["fpga", "notes"], "allowUsage": true }
   ```

   `transport: "local"` runs the bundled dispatcher as your own user (no SSH); `claudeHome` defaults to your `~/.claude`.
2. Tailscale as in step 8.
3. `SB_MODE=user deploy/linux/sb-linux on` (and `off`, `status`). It starts `voice-coding-mcp serve` in the background with `uv run`, like `vcm-mac` on a Mac, with no sudo. The log and pid file are in `~/.local/state/sb-linux`.

In this mode upstream's session tools also work: `list_sessions` lists your recent Claude Code sessions (also the ones in tmux), `ask_session_readonly` asks a read-only copy, and `send_message_to_session` relays a message through the session's messaging socket, which on Linux lies under `/run/user/<uid>/cc-socks/` (the path is taken from Claude Code's own registry). `remoteControl.handoffToDesktop` is macOS only.

## If something is compromised

If you suspect that someone else used the connector, that a task or a session was manipulated, or that your Claude account was accessed:

1. Remove the connector in Claude (Settings, Connectors).
2. `sudo -H -u switchboard /home/switchboard/switchboard/.venv/bin/voice-coding-mcp revoke-all --yes --config /home/switchboard/.config/voice-coding-mcp/config.json`.
3. `sb-linux off`.
4. Remove the hub's keys from every `authorized_keys` they are in: yours (machine `dev`) and the user on every other machine. Until you do, the hub user can still do what each forced command allows.
5. Read the audit log: `sudo -H -u switchboard /home/switchboard/switchboard/.venv/bin/voice-coding-mcp audit --config /home/switchboard/.config/voice-coding-mcp/config.json` (it lists every send, with time, machine, session and outcome). It is append-only by convention only: a compromised hub user can rewrite it, so also look at the sessions themselves.
6. Rotate the hub's SSH keys (step 4, new keys), install the new public keys with their forced commands (steps 5 and 6), and replace the `claude setup-token` token in `/etc/switchboard/env`.
7. Look for persistence as `switchboard` and as every user a dispatcher runs as: shell profiles (`~/.bashrc`, `~/.profile` and similar), user systemd units, `crontab -l`, `~/.claude/settings.json` (hooks and allow rules), `~/.ssh/authorized_keys`, and git hooks (`.git/hooks`) in every project.
8. Pair again only after that.

## Troubleshooting

- `sb-linux on` says Tailscale is not connected: `sudo tailscale up`. It says you are not the operator: `sudo tailscale set --operator=$USER`.
- It prints an approval link: open it, approve Funnel and HTTPS for the node, run `sb-linux on` again.
- It says the service did not answer `/healthz`: `journalctl -u switchboard -n 30`. If the config uses another port than 8787, set `SB_PORT` to it.
- `remote-check` says `host_key`: the host key is missing or changed in `~/.ssh/switchboard_known_hosts` (step 5.4). `auth`: the key is not in `authorized_keys` or the `from=` address does not match. `dispatcher_missing`: the path in `command=` is wrong. A refusal (`bad_policy`): the forced command's options are malformed.
- A session is missing from `list_remote_sessions`: it must be in both lists (`sessions` in the hub config and `--sessions` in the forced command), and the name is matched exactly (globs allowed).

## What was verified, and what is still to check

Verified by tests on Linux (no real Claude Code, Tailscale or sshd involved): `sb-linux` in both layouts against fake `tailscale`, `sudo` and `systemctl` with a real server, the deploy files against each other, the upstream session tools against a registry shaped like Claude Code's on Linux (`/run/user/<uid>/cc-socks` sockets, the `tmux` field, `waiting`), and the remote tools over the local transport against a real tmux 3.4 on an isolated server (`SB_LIVE_TMUX=1`).

Still to check by you on the real setup (listed in [DESIGN.md](DESIGN.md#to-verify-with-the-real-app)): the forced command through a real sshd on localhost, a relay with `send_message_to_session` to a live session over its `/run/user` socket, `list_sessions` on WSL with your real sessions, Funnel from inside WSL, and how voice mode handles the two-step send.
