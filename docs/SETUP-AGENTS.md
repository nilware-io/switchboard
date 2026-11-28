# Machines that connect out to the hub: setup guide

With the `agent` transport a machine opens the connection itself: it dials out to the hub over SSH and keeps that connection open, and the hub sends its requests down it. Use it for a WSL box behind NAT, a laptop on changing networks or a LAN machine behind a home router: the machine needs no sshd, no `authorized_keys` line for the hub and no open port, and it is reachable exactly while its connection runs. The design and the threat model are in [SWITCHBOARD.md](SWITCHBOARD.md#outbound-agents-machines-connect-to-the-hub); the configuration keys and commands are in [REFERENCE.md](REFERENCE.md).

Placeholders: `hub.example.org` is the hub's SSH address as the machines reach it; `vcm` is the hub's user (the server layout of [DEPLOY.md](DEPLOY.md)); `laptop` is the machine's name in the hub config; `USER` is your own user on that machine.

The hub side needs the server layout of [DEPLOY.md](DEPLOY.md): the relay runs from the root-owned install in `/opt/voice-coding-mcp`, which `sbrelay` can read, and the hub's sshd is reachable from the machines. A hub on the WSL layout of [SETUP-WSL.md](SETUP-WSL.md) cannot serve agent machines as that guide sets it up: its venv lies in `/home/switchboard` (mode 700, so sshd running as `sbrelay` cannot start its Python; and nothing the hub user can write may run as another account), and its sshd listens on loopback only, so no other machine reaches the relay login. A WSL box is a machine here (part 2), connecting out to a server hub.

## How it works

```
laptop (USER):  switchboard-connect
                  ssh -T sbrelay@hub.example.org   (its own key, the hub's host key pinned)
                  stdin and stdout wired to: switchboard-remote --serve --sessions ...   (the policy)
hub:            sshd -> account sbrelay -> forced command for laptop's key: the relay --machine laptop
                  relay -> /run/switchboard-relay/hub.sock -> the hub (vcm)
```

- The dispatcher on the machine is the same `switchboard-remote` as for SSH machines, in serve mode: same verbs, same checks, same refusals. Its policy (`--sessions`, `--allow-usage`, `--allow-send`, ...) is its own command line in your unit file; the hub cannot change it.
- The hub knows which machine is calling from which key logged in: each key's line in the relay account's root-owned `authorized_keys` names the machine. Nothing the machine sends can claim another name.
- The relay account `sbrelay` can do one thing: run the relay, which connects to one socket. No shell, no terminal, no forwarding.

## 1. The hub, once (as root)

On a server hub installed with `deploy/setup.sh`, run it again with the relay part turned on; it does steps 1 to 4 and prints the fingerprint of step 5:

```sh
sudo VCM_AGENT_RELAY=1 /opt/voice-coding-mcp/deploy/setup.sh vcm.example.com
```

By hand, with the files from the hub's install (`/opt/voice-coding-mcp`, root-owned):

1. The relay account and its key file. No password; the shell is `/bin/sh` because sshd runs forced commands through the login shell. Its home is root-owned and empty, so the account can write nothing.

   ```sh
   sudo useradd --system --user-group --shell /bin/sh --home-dir /var/lib/sbrelay --no-create-home sbrelay
   sudo install -d -o root -g root -m 755 /var/lib/sbrelay /etc/ssh/switchboard-relay
   sudo install -o root -g root -m 644 /dev/null /etc/ssh/switchboard-relay/authorized_keys
   sudo usermod -p '*' sbrelay       # no password can match; a locked '!' account is refused by sshd with UsePAM no
   ```

   `sbrelay` must not be in the hub user's group, and the hub user not in `sbrelay`.

2. The socket directory, owned by the hub user with the group `sbrelay` and mode 2750. `deploy/relay/tmpfiles-switchboard-relay.conf` names `vcm` as the owner.

   ```sh
   sudo install -o root -g root -m 644 /opt/voice-coding-mcp/deploy/relay/tmpfiles-switchboard-relay.conf /etc/tmpfiles.d/switchboard-relay.conf
   sudo systemd-tmpfiles --create /etc/tmpfiles.d/switchboard-relay.conf
   ls -ld /run/switchboard-relay        # drwxr-s--- vcm sbrelay
   ```

   The hub's unit (`deploy/voice-coding-mcp.service`) has `ReadWritePaths=-/run/switchboard-relay`; a unit installed before this change needs to be installed again. The hub binds the relay socket once, at start, and a directory that is missing then is skipped (the `-`), so restart the hub after the directory exists (step 2.5 restarts it anyway when you add the first machine).

3. The sshd drop-in: key only, no forwarding, no terminal, one session, keepalives.

   ```sh
   sudo install -o root -g root -m 644 /opt/voice-coding-mcp/deploy/relay/sshd-switchboard-relay.conf /etc/ssh/sshd_config.d/switchboard-relay.conf
   sudo sshd -t
   sudo sshd -T -C user=sbrelay,host=x,addr=192.0.2.1 | grep -E 'disableforwarding|permittty|authorizedkeysfile|maxsessions|acceptenv'
   sudo systemctl reload ssh
   ```

   The check must show `disableforwarding yes`, `permittty no`, `maxsessions 1` and the key file `/etc/ssh/switchboard-relay/authorized_keys`, and preferably no `acceptenv` line: a global `AcceptEnv` (Debian and Ubuntu ship `AcceptEnv LANG LC_*`) lets the machine set those variables for the relay. The relay runs `python -I`, which ignores `PYTHON*` variables, so locale variables are harmless; anything wider does not belong there. If your `sshd_config` has `AllowUsers` or `AllowGroups`, add `sbrelay` there. The machines must reach the hub's SSH port (a cloud firewall or security list may need it opened for them).

4. Tell the hub. Add the machines with `"transport": "agent"` (step 2.5) and, only when you use other paths or another account name, a `remote.agents` section:

   ```json
   "agents": {"socketPath": "/run/switchboard-relay/hub.sock", "relayUser": "sbrelay"}
   ```

5. The host key the machines will pin. Read it here, on the hub's console or in an SSH session whose host key you already checked, not over the network path you are protecting:

   ```sh
   cat /etc/ssh/ssh_host_ed25519_key.pub
   ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub        # note the SHA256 fingerprint
   ```

## 2. Each machine (as yourself there)

1. Your own copy of the repository at a commit you checked, exactly as in [SETUP-WSL.md](SETUP-WSL.md#your-own-copy-the-only-source-for-what-runs-as-you-or-as-root) (`git clone`, `git checkout --detach <commit>`, `git rev-parse HEAD`, `git status --porcelain`). Two files from it run as you: the dispatcher and the connector. Never copy them from the hub.

2. Read both, then install them. The dispatcher is one Python file of about 5600 lines (what to check: SETUP-WSL.md step 5.2; serve mode, `serve` and `Server`, reads only its stdin and writes only its stdout). The connector, `switchboard-connect`, is one Python file of about 500 lines with only the standard library: check that it runs `ssh` with `-F /dev/null`, `BatchMode`, `IdentitiesOnly`, `IdentityAgent=none`, no forwarding and `StrictHostKeyChecking=yes` against your known-hosts file (`ssh_argv`), that it never writes that file, and that it starts only the command you give it after `--`.

   ```sh
   cd ~/src/switchboard
   git rev-parse HEAD                                  # the commit you checked
   less deploy/remote/switchboard-remote               # read it before installing
   less deploy/remote/switchboard-connect              # read it before installing
   install -D -m 755 deploy/remote/switchboard-remote ~/.local/bin/switchboard-remote
   install -D -m 755 deploy/remote/switchboard-connect ~/.local/bin/switchboard-connect
   sha256sum deploy/remote/switchboard-remote ~/.local/bin/switchboard-remote     # the two must match
   sha256sum deploy/remote/switchboard-connect ~/.local/bin/switchboard-connect   # the two must match
   ```

   It needs Python 3.8 or newer, OpenSSH and tmux.

3. A key for this machine's connection, used for nothing else. No passphrase: it runs unattended.

   ```sh
   install -d -m 700 ~/.ssh
   ssh-keygen -t ed25519 -N '' -C 'switchboard-connect laptop' -f ~/.ssh/switchboard_relay
   cat ~/.ssh/switchboard_relay.pub                    # for step 5
   ```

4. Pin the hub's host key. Write the key from step 1.5 (its type and base64 part) under the fixed name `switchboard-hub`, which the connector uses whatever address you give it, then compare the fingerprint with the one you noted on the hub:

   ```sh
   install -d -m 700 ~/.config/switchboard-connect
   printf 'switchboard-hub %s\n' 'ssh-ed25519 AAAA...' > ~/.config/switchboard-connect/known_hosts
   chmod 644 ~/.config/switchboard-connect/known_hosts
   ssh-keygen -lf ~/.config/switchboard-connect/known_hosts    # must equal the hub's fingerprint
   ```

   There is no trust on first use: a hub with another key is refused, and the connector never updates this file.

5. On the hub, as root, add one line for this key to `/etc/ssh/switchboard-relay/authorized_keys` (the format is in `deploy/relay/authorized_keys.example`):

   ```
   restrict,command="/opt/voice-coding-mcp/.venv/bin/python -I -m voice_coding_mcp.remote.relay --machine laptop" ssh-ed25519 AAAA... switchboard-connect laptop
   ```

   `--machine laptop` is the identity the hub trusts for this key. Add `from="<address>",` in front when the machine always connects from one address. In the hub config add the machine and restart the hub:

   ```json
   {"name": "laptop", "transport": "agent", "sessions": ["fpga", "notes"], "allowUsage": true}
   ```

   As for SSH machines, `allowSend`, `allowPermissionAnswers`, `sendToAnyPane` and `allowAttachments` are the hub's half of each opt-in; the dispatcher's flags in the next step are the other half.

6. The user unit. Install the example, then edit `--host` and the dispatcher's flags after `--` (the same flags as a forced command; add `--allow-send` only when you want to send by voice):

   ```sh
   cd ~/src/switchboard
   less deploy/remote/switchboard-connect.service
   install -D -m 644 deploy/remote/switchboard-connect.service ~/.config/systemd/user/switchboard-connect.service
   nano ~/.config/systemd/user/switchboard-connect.service
   systemctl --user daemon-reload
   systemctl --user enable --now switchboard-connect
   sudo loginctl enable-linger USER                    # once: start at boot and keep running without a login
   ```

   In WSL systemd must be on (`[boot] systemd=true` in `/etc/wsl.conf`, see [SETUP-WSL.md](SETUP-WSL.md#1-before-you-start)); the machine is reachable while the WSL VM runs.

   Attachments (images, audio and video for you in the Claude app, see [SWITCHBOARD.md](SWITCHBOARD.md#attachments)): add `--allow-attachments` to the dispatcher's flags after `--` in the unit (for example `... switchboard-remote --serve --sessions fpga,notes --allow-attachments`) and `"allowAttachments": true` to the machine in the hub config. Sessions put files into `~/.local/share/switchboard/outbox` on the machine (create it yourself, `mkdir -p ~/.local/share/switchboard/outbox`; `--outbox /other/path` for another folder). `status` on the hub warns when the two halves disagree.

7. Verify:

   ```sh
   journalctl --user -u switchboard-connect -n 20      # on the machine: "connecting to hub.example.org as sbrelay", no errors
   sudo voice-coding-mcp status                        # on the hub: "laptop: connected since ..."
   sudo voice-coding-mcp remote-check laptop           # its dispatcher version and sessions, and the key file lint
   ```

   Then by voice: "what is my session on laptop doing".

   `voice-coding-mcp usage` on the hub (the CLI) runs in its own process and holds no machine's connection, so it does not read agent machines: it reports them from the records the hub's own `usage_report` cached, with a note, and that is not counted as a failure. Ask by voice for fresh numbers, or use `--cached` to say so plainly.

8. Off: `systemctl --user stop switchboard-connect` (and `disable` to keep it off after a reboot). The hub then says "laptop is not connected right now." You decide when the machine can be reached.

## Updating the dispatcher (0.9.0: machine health; 0.10.0: timesheet data; 0.11.0: clearing the input; 0.12.0: session hooks; 0.13.0: signed self-update)

Dispatcher 0.9.0 adds the read-only `health` verb (free disk space, read-only file systems, memory; on WSL also the Windows disk C:), which the hub polls for the [incident log](SWITCHBOARD.md#incident-log). It needs no new flag. On each machine, from your own copy at the new verified commit: read `git diff <old>..<new> -- deploy/remote`, then install `switchboard-remote` and `switchboard-connect` again as in part 2.2 (`sha256sum` both copies), and `systemctl --user restart switchboard-connect`. `remote-check` shows `switchboard-remote 0.9.0` with `health` among its verbs. Until a machine is updated the hub polls it with `version` instead and reports no health incidents for it. For SSH machines the same install (SETUP-WSL.md steps 5.2 and 6.1) is enough; the forced command does not change.

Dispatcher 0.10.0 adds agent transcripts, entrypoints, work projects and prompt origins to `usage` for the [timesheet](SWITCHBOARD.md#timesheet). It needs no new flag; update it the same way. Until a machine is updated, its headless runs count as owner time, task notifications as owner prompts and its subagents are missing, and the draft timesheet notes it.

Dispatcher 0.11.0 adds the `clear` verb (`remote_clear_input`, under the same `--allow-send`) and reports Claude Code's grey prompt suggestion apart from typed text. Update it the same way; `remote-check` then lists `clear` among its verbs. Until a machine is updated, `remote_clear_input` says its dispatcher is too old and a suggestion still counts as typed text there.

## Updating to 0.12.0: session hooks

Dispatcher 0.12.0 adds the `events` verb (only with the new flag `--allow-events`), which hands the hub what this machine's Claude Code hooks noted. Update `switchboard-remote` and `switchboard-connect` as above, install `switchboard-hook` (next part), add `--allow-events` to the dispatcher's flags after `--` in the unit (`nano ~/.config/systemd/user/switchboard-connect.service`, then `systemctl --user daemon-reload` and `systemctl --user restart switchboard-connect`), and set `"events": {"enabled": true}` in the hub config. `remote-check` then lists `events` among the verbs. Without the flag the dispatcher answers `not_allowed` and the hub asks again every 10 minutes; nothing else changes.

## Updating to 0.13.0: signed self-update

Dispatcher and connector 0.13.0 can update themselves: the hub offers a release you built and signed on your own computer with your release key ([SETUP-WSL.md](SETUP-WSL.md#release-key-and-signed-updates)), and the machine installs it only after its own `ssh-keygen` verified the signature against the key you pin here. The design and every check are in [SWITCHBOARD.md](SWITCHBOARD.md#self-update). A dispatcher older than 0.13.0 (for example one still at 0.6.0) has no `update` verb, so each machine needs this one manual step; every later update then goes by voice ("update laptop") or with `voice-coding-mcp remote-update laptop --yes` on the hub.

It needs OpenSSH 8.1 or newer on the machine (`ssh -V`; Ubuntu 20.04 and newer, Debian 11 and newer, macOS 10.15.4 and newer have it) at `/usr/bin/ssh-keygen`, and the three programs installed under their own names in one directory that only you can write (the default `~/.local/bin`).

On the machine, as yourself (here `laptop`, an agent machine):

1. Install 0.13.0 from your own verified copy, as in part 2.2 (read `git diff <old>..<new> -- deploy/remote` first; from 0.6.0 that is a long read: at least the `update` verb, `install_update` and `check_bundle` in the dispatcher, and `run` and `main` in the connector):

   ```sh
   cd ~/src/switchboard
   git fetch
   git checkout --detach <commit>                      # the commit or release tag you checked, 0.13.0 or newer
   git rev-parse HEAD && git status --porcelain        # exactly that commit, nothing else
   less deploy/remote/switchboard-remote deploy/remote/switchboard-connect deploy/remote/switchboard-hook
   install -D -m 755 deploy/remote/switchboard-remote ~/.local/bin/switchboard-remote
   install -D -m 755 deploy/remote/switchboard-connect ~/.local/bin/switchboard-connect
   install -D -m 755 deploy/remote/switchboard-hook ~/.local/bin/switchboard-hook    # only if you use the session hooks
   sha256sum deploy/remote/switchboard-* ~/.local/bin/switchboard-*                 # each pair must match
   ```

   An update replaces only the programs it finds installed next to the dispatcher: install the hook here if you want updates to keep it current.

2. Pin your release key. Copy the public line of `~/.ssh/switchboard_release.pub` from your computer (by hand or over a channel you trust, never from the hub) and compare its fingerprint with the one `ssh-keygen -lf ~/.ssh/switchboard_release.pub` prints there:

   ```sh
   install -d -m 700 ~/.config/switchboard-remote
   printf 'switchboard-release namespaces="switchboard-release" %s\n' 'ssh-ed25519 AAAA...' > ~/.config/switchboard-remote/release_signers
   chmod 644 ~/.config/switchboard-remote/release_signers
   awk '{print $3" "$4}' ~/.config/switchboard-remote/release_signers | ssh-keygen -lf -    # must print your key's fingerprint
   ```

   `HOME`, `~/.config` and `~/.config/switchboard-remote` must not be writable by group or others (`chmod go-w`), or every update is refused (`signers_unsafe`).

3. Allow it. In the unit, add `--allow-self-update` to the dispatcher's flags after `--` (for example `... switchboard-remote --serve --sessions fpga,notes --allow-events --allow-self-update`), then:

   ```sh
   nano ~/.config/systemd/user/switchboard-connect.service
   systemctl --user daemon-reload
   systemctl --user restart switchboard-connect
   ```

   On an ssh machine add it to the forced command in `~/.ssh/authorized_keys` instead (`command="/usr/bin/python3 -I /home/USER/.local/bin/switchboard-remote --sessions 'fpga' --allow-self-update"`); the next call uses it.

4. On the hub, `sudo voice-coding-mcp remote-check laptop` now shows `switchboard-remote 0.13.0`, `update` among the verbs and `self-update allowed`.

From then on: publish a release on the hub ([SETUP-WSL.md](SETUP-WSL.md#release-key-and-signed-updates)) and say "update laptop". The hub verifies the published bundle against its own pinned key (`/etc/voice-coding-mcp/release_signers`, required for publishing) and reads back "Ready to update switchboard-remote on laptop from 0.13.0 to 0.13.1, signed by the release key SHA256:..., verified on the hub"; check that fingerprint, then say yes. laptop checks the signature, starts the new dispatcher once, replaces the files (keeping `switchboard-remote.prev` and the others) and its connector restarts onto the new code within a few seconds. Every attempt is in the incident log (`voice-coding-mcp incidents --events --machine laptop`).

To go back: `python3 -I ~/.local/bin/switchboard-remote --rollback` on the machine, then `systemctl --user restart switchboard-connect`. The machine keeps refusing the version you left, so the next release you sign must be newer.

To stop updates on a machine: remove `--allow-self-update` (or the key line). To change the release key: replace the line in `release_signers` on every machine and on the hub.

## Session hooks and notify_owner

`switchboard-hook` is one more Python file of about 900 lines, standard library only, that runs as you on this machine. Claude Code runs it for four hooks: a permission prompt or a question (Notification), the end of a turn (Stop), a turn that ended on an API error (StopFailure) and `gh pr create` or `gh pr ready` (PostToolUse on Bash). It also serves the `notify_owner` MCP tool, which a session calls to tell you something itself. Each time it writes one small file into `~/.local/state/switchboard-hook/events` (0700, files 0600, at most 200 of at most 4 KB) and exits; it never connects anywhere. The dispatcher hands the spool to the hub over this machine's own connection when the hub asks (`--allow-events`), and deletes what the hub took. It writes no transcript, no assistant text, no tool output and no screen text: a hook's own short message (such as "Claude needs your permission to use Bash"), the error code of a failed turn, a pull request URL checked to be `https://github.com/<owner>/<repo>/pull/<n>`, the session's id and name, the project folder's name and the tmux pane.

What to check when you read it (`less deploy/remote/switchboard-hook`): `run_hook` prints nothing and returns 0 whatever happens, and `main` arms a 5 s alarm that exits 0; `event_from_payload` keeps only the fields above; `write_event` writes only into the spool; `merged_settings` removes only handlers whose command ends in `switchboard-hook hook` and adds its own; `update_mcp` runs only `claude mcp remove` and `claude mcp add` for `switchboard-notify`.

```sh
cd ~/src/switchboard
less deploy/remote/switchboard-hook                 # read it before installing
install -D -m 755 deploy/remote/switchboard-hook ~/.local/bin/switchboard-hook
sha256sum deploy/remote/switchboard-hook ~/.local/bin/switchboard-hook   # the two must match
/usr/bin/python3 -I ~/.local/bin/switchboard-hook install --dry-run      # shows the change to ~/.claude/settings.json
/usr/bin/python3 -I ~/.local/bin/switchboard-hook install
```

`install` merges four entries into `~/.claude/settings.json` (or `$CLAUDE_CONFIG_DIR/settings.json`; `--settings FILE` for another file): it keeps every other key and hook, replaces only its own entries (so running it again changes nothing), prints the change as a diff and first copies the old file to `settings.json.switchboard-backup-<time>-<random>` (0600). Each entry runs `/usr/bin/python3 -I ~/.local/bin/switchboard-hook hook` with `"async": true` and a 10 s timeout, so Claude Code never waits for it. Then it runs `claude mcp remove --scope user switchboard-notify` and `claude mcp add --scope user switchboard-notify -- /usr/bin/python3 -I ~/.local/bin/switchboard-hook mcp` (when `claude` is not on `PATH` it prints that command for you; `--no-mcp` skips it). The paths are the Python that runs the installer and the installed file; `--python` and `--script` choose others. `--allow-tool` also adds `mcp__switchboard-notify__notify_owner` to `permissions.allow`, so sessions may call it without asking you; leave it out to approve the first call in each session yourself. `uninstall` removes exactly what `install` added (with a backup) and the MCP server. Claude Code reads the settings at the start of a session: restart running sessions, or open `/hooks` in one to check.

`notify_owner(message, level?, url?)`: `level` is `info`, `warning` or `urgent`; `url` only a `https://github.com/<owner>/<repo>/...` link, so a session cannot send you to another site. At most 5 notes per session and 20 per machine in an hour. Its text is the session's own words: the hub shows it as such ("the session says: ...") and never treats it as an instruction or an approval.

## 3. Moving a machine from the ssh transport

1. Set the machine up as in part 2 while its `ssh` entry still works.
2. In the hub config change `transport` to `agent` and remove `host`, `user`, `port`, `identityFile`, `knownHostsFile` and `dispatcher` (the config refuses them on an agent machine). Keep the `name`: the usage cache, the watcher's state and the audit lines stay continuous. Keep the opt-ins, and carry the flags of the old forced command over to the unit's `--serve` line. Restart the hub and run `remote-check`.
3. On the machine remove the hub's line from `~/.ssh/authorized_keys`; on the hub delete that machine's private key and its `known_hosts` entry. If the machine's sshd served only the hub, turn it off. Going back is the same steps the other way round.

## Troubleshooting

| What you see | Why, and what to do |
|---|---|
| Journal: `connection ended: auth` | The hub refused the key: the `authorized_keys` line is missing, has another key or the account is not allowed (`AllowUsers`). The connector retries every few minutes |
| Journal: `connection ended: host_key` | The hub's key is not the pinned one. Compare fingerprints again (part 2.4); never fix it by deleting the file |
| Journal: `switchboard relay: hub not running` | sshd ran the relay but the hub does not listen: it is stopped, or `status` on the hub shows why the socket is not served (directory owner, group or mode) |
| Journal: `connection ended: refused_by_hub` | The hub does not know the machine name in the key's line: add the machine to the config with `"transport": "agent"` and restart the hub |
| `status`: `bad_policy` warning | The unit's dispatcher flags are broken; every request is refused until they are fixed |
| `status`: replaced many times in the last hour | Two connectors use the same key (a second machine or a manual run next to the unit), or the key was copied. One key per machine |
| Journal: `connection ended: auth`, and the hub's auth log says `account is locked` | `sbrelay` has a locked password (`!`) and sshd runs with `UsePAM no`: `sudo usermod -p '*' sbrelay` (part 1.1) |
| Journal: `connection ended: closed` every few seconds, `status` shows `write_stalled` | The dispatcher stopped reading what the hub sends (a hung dispatcher, or a full channel): the hub closes such a connection after 5 s and the connector reconnects. If it repeats, check the dispatcher's stderr in the journal |
| Nothing reaches Discord from this machine | `remote-check laptop` must list `events`; the unit's dispatcher needs `--allow-events`; `ls ~/.local/state/switchboard-hook/events` shows files waiting while the hub is away; `/hooks` in a session shows the four entries. A spool directory that others can enter is refused (`events_unsafe`): `chmod 700` it |
| `remote_update` says "Not updated: ..." | The sentence names the machine's refusal: `signers_unsafe` (the signers file or a directory above it is writable by group or others, or a symlink: `chmod go-w`), `no_signers` (no key line), `bad_signature` (the release was signed by another key than the one pinned, or the line lacks `namespaces="switchboard-release"`), `not_newer` (that version or a newer one was installed before; sign a newer one), `install_unsafe` (`~/.local/bin` or a program in it is writable by others or a symlink), `self_test_failed` (the new dispatcher did not start on this machine's Python; nothing was changed), `old_openssh` (OpenSSH older than 8.1). `voice-coding-mcp incidents --events --machine laptop` lists every attempt |
| `status`: the hub is not running | `status` and `remote-check` ask the running hub over `stateDir/agents-control.sock`; start it first |

## If something is compromised

- A machine's relay key (`~/.ssh/switchboard_relay`) leaked: delete its line from `/etc/ssh/switchboard-relay/authorized_keys` on the hub, check `audit.jsonl` and `status` (replacements, addresses), then make a new key on the machine (part 2.3 and 2.5). Whoever held the key could pose as that machine and receive what the hub sent to it, including the text of confirmed sends.
- The release key (`~/.ssh/switchboard_release` on your computer) or its passphrase leaked: replace the key line in `~/.config/switchboard-remote/release_signers` on every machine (or remove `--allow-self-update`) and in `/etc/voice-coding-mcp/release_signers` on the hub, make a new key, delete the published bundles on the hub (`stateDir/releases`) and check `voice-coding-mcp incidents --events` for updates you did not ask for. Until the line is replaced, whoever holds the key and the hub can install code on every machine that allows self-update.
- The hub: stop the units on the machines (`systemctl --user disable --now switchboard-connect`), then follow the hub's own steps ([DEPLOY.md](DEPLOY.md)). The machines hold no key that the hub could misuse, and the hub holds none that opens a machine; but while connected, a compromised hub can do on each machine what its dispatcher allows. If the hub's host key may have leaked, make a new one and pin it again on every machine.
