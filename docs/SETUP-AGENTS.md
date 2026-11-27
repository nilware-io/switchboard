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

2. Read both, then install them. The dispatcher is one Python file of about 1950 lines (what to check: SETUP-WSL.md step 5.2; serve mode, `serve` and `Server`, reads only its stdin and writes only its stdout). The connector, `switchboard-connect`, is one Python file of about 450 lines with only the standard library: check that it runs `ssh` with `-F /dev/null`, `BatchMode`, `IdentitiesOnly`, `IdentityAgent=none`, no forwarding and `StrictHostKeyChecking=yes` against your known-hosts file (`ssh_argv`), that it never writes that file, and that it starts only the command you give it after `--`.

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

   As for SSH machines, `allowSend`, `allowPermissionAnswers` and `sendToAnyPane` are the hub's half of each opt-in; the dispatcher's flags in the next step are the other half.

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

7. Verify:

   ```sh
   journalctl --user -u switchboard-connect -n 20      # on the machine: "connecting to hub.example.org as sbrelay", no errors
   sudo voice-coding-mcp status                        # on the hub: "laptop: connected since ..."
   sudo voice-coding-mcp remote-check laptop           # its dispatcher version and sessions, and the key file lint
   ```

   Then by voice: "what is my session on laptop doing".

   `voice-coding-mcp usage` on the hub (the CLI) runs in its own process and holds no machine's connection, so it does not read agent machines: it reports them from the records the hub's own `usage_report` cached, with a note, and that is not counted as a failure. Ask by voice for fresh numbers, or use `--cached` to say so plainly.

8. Off: `systemctl --user stop switchboard-connect` (and `disable` to keep it off after a reboot). The hub then says "laptop is not connected right now." You decide when the machine can be reached.

## Updating the dispatcher (0.9.0: machine health)

Dispatcher 0.9.0 adds the read-only `health` verb (free disk space, read-only file systems, memory; on WSL also the Windows disk C:), which the hub polls for the [incident log](SWITCHBOARD.md#incident-log). It needs no new flag. On each machine, from your own copy at the new verified commit: read `git diff <old>..<new> -- deploy/remote`, then install `switchboard-remote` and `switchboard-connect` again as in part 2.2 (`sha256sum` both copies), and `systemctl --user restart switchboard-connect`. `remote-check` shows `switchboard-remote 0.9.0` with `health` among its verbs. Until a machine is updated the hub polls it with `version` instead and reports no health incidents for it. For SSH machines the same install (SETUP-WSL.md steps 5.2 and 6.1) is enough; the forced command does not change.

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
| `status`: the hub is not running | `status` and `remote-check` ask the running hub over `stateDir/agents-control.sock`; start it first |

## If something is compromised

- A machine's relay key (`~/.ssh/switchboard_relay`) leaked: delete its line from `/etc/ssh/switchboard-relay/authorized_keys` on the hub, check `audit.jsonl` and `status` (replacements, addresses), then make a new key on the machine (part 2.3 and 2.5). Whoever held the key could pose as that machine and receive what the hub sent to it, including the text of confirmed sends.
- The hub: stop the units on the machines (`systemctl --user disable --now switchboard-connect`), then follow the hub's own steps ([DEPLOY.md](DEPLOY.md)). The machines hold no key that the hub could misuse, and the hub holds none that opens a machine; but while connected, a compromised hub can do on each machine what its dispatcher allows. If the hub's host key may have leaked, make a new one and pin it again on every machine.
