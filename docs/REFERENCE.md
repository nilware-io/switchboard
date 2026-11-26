# Reference

Configuration, CLI, MCP tools and HTTP endpoints of voice-coding-mcp. Setup is in the [README](../README.md) and [DEPLOY.md](DEPLOY.md); the behavior contract, with the reasons behind it, is in [DESIGN.md](DESIGN.md).

## Configuration

JSON file, path from `--config` or `VCM_CONFIG`, default `/etc/voice-coding-mcp/config.json` (the VPS layout; `deploy/mac/vcm-mac` defaults to `~/.config/voice-coding-mcp/config.json`). See [config.example.json](../config.example.json). The defaults below are the VPS paths; in Mac mode put `stateDir` and `projectRoots` under your home directory.

| Key | Default | Meaning |
|---|---|---|
| `publicUrl` | (required) | Public `https` origin of the server, without a path, query or fragment (for example `https://vcm.example.com`). The MCP endpoint is `publicUrl + /mcp`, the OAuth issuer is `publicUrl`. A non-ASCII host is rejected, use punycode. In Mac mode `vcm-mac on` sets it |
| `listen.host`, `listen.port` | `127.0.0.1`, `8787` | Listen address; must be loopback unless `listen.allowNonLoopback` is set, since the server sits behind a TLS proxy on the same machine |
| `listen.allowNonLoopback` | `false` | Allow a non-loopback `listen.host`. Then anyone who reaches that address talks to the server in plain HTTP, bypassing the proxy (and on the VPS Caddy's IP restriction); leave it off unless you put your own TLS proxy on another host |
| `stateDir` | `/var/lib/voice-coding-mcp` | State: OAuth hashes, tasks, sessions, pairing window |
| `projectRoots` | `["/home/vcm/projects"]` | Directories whose subdirectories are projects. Not `/`, not the home directory or its ancestors, not containing `stateDir` |
| `claude.bin` | `claude` | Claude Code executable |
| `claude.model`, `claude.maxTurns` | `null` | Passed as `--model`, `--max-turns` when set |
| `claude.effort` | `null` | Passed as `--effort` when set: one of `low`, `medium`, `high`, `xhigh`, `max` |
| `claude.permissionMode` | `auto` | One of `auto`, `acceptEdits`, `dontAsk`, `plan`, `manual`. Never `bypassPermissions` |
| `claude.access` | `isolated` | What task runs (`run_task`, `run_tasks_parallel`, `resume_task_in_remote_control`) can reach. `isolated`: only `claude.mcpServers` (strict MCP config), no claude.ai connectors, `claude.settingSources`. `desktop`: like Claude Code on your desktop, with all your claude.ai connectors, user, plugin and approved `.mcp.json` MCP servers, minus the voice server itself (denied by `publicUrl`); setting sources `user,project,local` in folders you trusted in Claude Code, else `user` only, and `claude.settingSources` is not used. `ask_session_readonly` and `send_message_to_session` runs stay isolated in both modes. See [DESIGN.md](DESIGN.md#task-access) |
| `claude.settingSources` | `["user"]` | Settings Claude Code loads (isolated task runs, and ask and relay runs in both modes). Adding `project` or `local` trusts `.claude/settings.json` or `.claude/settings.local.json` of every project, and a repository can commit either |
| `claude.configDir` | `null` | `CLAUDE_CONFIG_DIR` for tasks |
| `claude.mcpServers` | `{}` | MCP servers available to tasks (written to the strict MCP config; with `claude.access` `desktop` added on top of your own servers, not strict) |
| `claude.extraArgs` | `[]` | Extra arguments for task runs. Rejected, also in `--flag=value` form: `--dangerously-skip-permissions`, `--allow-dangerously-skip-permissions`, `--permission-mode`, `--permission-prompts`, `--mcp-config`, `--strict-mcp-config`, `--setting-sources`, `--settings`, `--allowedTools`, `--allowed-tools`, `--tools`, `--add-dir`, `--system-prompt`, `--append-system-prompt`, `--bare`, `--name`, `-n` |
| `claude.env` | `{}` | Extra environment variables for tasks |
| `tasks.syncWaitSeconds` | `25` | How long `run_task` waits before returning a task number |
| `tasks.timeoutMinutes` | `15` | Hard limit per task |
| `tasks.maxConcurrent` | `2` | Parallel runs. At most one `run_task` run per project and one ask or message run per session at a time |
| `tasks.maxQueued` | `10` | Queue length |
| `tasks.resumeWithinHours` | `12` | Resume the project's last session if it was active within this time |
| `tasks.keepDays` | `14` | Task files older than this are deleted (at startup and periodically) |
| `sessions.days` | `7` | `list_sessions` shows sessions active within this many days |
| `sessions.replyWaitMinutes` | `10` | How long `send_message_to_session` waits for the session's reply |
| `remoteControl.maxSessions` | `3` | At most this many Remote Control sessions opened by the server at once, counting both tasks reopened with `resume_task_in_remote_control` and tasks started with `run_task` `remote_control`. Only sessions connected to the app (or still connecting) count; a background session that lost Remote Control while its process lives does not. The error names the open sessions with their task numbers, to close one with `cancel_task`. When full, `run_task` with `remote_control` runs the task headless and says so; `0` turns `resume_task_in_remote_control` off and makes `remote_control` always run headless |
| `remoteControl.handoffToDesktop` | `false` | macOS only (ignored elsewhere, with a startup warning). After a voice session was stopped by `cancel_task` or the idle close, and once it is gone from the live registry, the server runs `open claude://resume?session=<id>` so the Claude desktop app adopts it into its own session list (the app otherwise drops stopped CLI sessions from the list). At shutdown a pending handoff gets up to 5 s before it is cancelled. The adopted session keeps its name, and the task records the handoff (`handedOverAt`, `desktopSessionId`), so `get_task_result` and `list_tasks` say the session is in the desktop app. Failures are only logged |
| `remoteControl.idleCloseMinutes` | `0` | Opt-in, meant to pair with `remoteControl.handoffToDesktop` (which keeps each closed session in the desktop app's list). Stops the server's own Remote Control sessions (session tasks and reopened tasks) that have been idle this long (registry status idle, so not waiting for an approval or an answer; timed from `statusUpdatedAt`), or disconnected from the app this long. A session task stopped this way reads as done. Checked every minute, and checked again just before the stop; `0` (the default) turns it off |
| `oauth.accessTokenHours` | `24` | Access token lifetime |
| `oauth.refreshTokenDays` | `90` | Refresh token lifetime (rotated on use) |
| `oauth.pairingMinutes` | `10` | Pairing window length |
| `oauth.redirectUris` | Claude's callbacks | Allowed redirect URIs (`https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback`). Must be `https`; plain `http` only for the loopback hosts `127.0.0.1`, `localhost` and `[::1]` |
| `oauth.cimdClientIds` | Claude's published identity | Client ID Metadata Document URLs that may be fetched (see [CIMD](#terms)) |
| `oauth.allowLoopbackRedirects` | `false` | Allow `http` redirects to `127.0.0.1`, `localhost` or `[::1]` on any port, for testing with MCP Inspector. The server logs a warning when it is on |
| `notify.url`, `notify.headers` | `null`, `{}` | POST a short text with the task number and project name here when a background task finishes (works with ntfy); also where the watcher (`watch`) pushes. On a public ntfy server anyone who knows the topic reads the pushes: use a long random topic or an access token in `headers` |
| `notify.includeResult` | `false` | Include the result text in the notification. Never for remote jobs (`remote_status` summaries that ran in the background): their result is screen text, so their push names only the job ("Task 12 (status of fpga on dev) finished.") |
| `logPrompts` | `false` | Log prompts and Claude's stderr. When off, prompts are not logged and Claude's stderr is logged only as its length |
| `remote.machines` | `[]` | Machines whose tmux sessions the remote tools reach (Switchboard, see [SWITCHBOARD.md](SWITCHBOARD.md#config)), at most 32. Each entry: `name` (spoken name, `^[a-z][a-z0-9-]{0,31}$`, unique), `transport` (`ssh`, default, or `local`: the bundled dispatcher as the server's own user, at most one such machine, not with `listen.allowNonLoopback`), for `ssh` machines `host` (DNS name or IP literal, no `user@`, port or brackets), `user`, `port` (`22`), `identityFile` (absolute, plain characters `A-Za-z0-9._/-`), `knownHostsFile` (default `<stateDir>/known_hosts`, same charset), `dispatcher` (`forced` for an `authorized_keys` forced command, or the absolute path of `switchboard-remote` there, which gives no protection against a compromised server), `sessions` (1 to 64 tmux session names or `fnmatch` globs of `A-Za-z0-9_.*?-`, case-sensitive; sessions that match none are invisible), `claudeHome` (`local` machines only), `allowUsage` (`true`), `allowSend`, `allowPermissionAnswers` and `sendToAnyPane` (`false`; validated now, used from M3). Startup warnings: a lone `*`, a `local` machine, an explicit dispatcher path, `sendToAnyPane`, and an identity file that is missing, not a regular file, not the server user's, readable by others or in another user's home |
| `remote.ssh.bin` | `ssh` | OpenSSH client |
| `remote.ssh.connectTimeoutSeconds` | `5` | `ConnectTimeout`, 1 to 30 |
| `remote.ssh.commandTimeoutSeconds` | `10` | Wall clock per dispatcher call, 1 to 60 (each tool call also stops at `tasks.syncWaitSeconds - 3`) |
| `remote.captureLines` | `60` | Screen lines `remote_status` reads, 10 to 500 |
| `remote.summary.enabled` | `true` | Summarise a screen with a `claude -p` run; `false` uses the heuristic (last meaningful screen line) only |
| `remote.summary.model` | `haiku` | `--model` of the summariser, `^[A-Za-z0-9._-]{1,64}$`. `claude.model`, `claude.effort` and `claude.extraArgs` never apply to it |
| `remote.summary.timeoutSeconds` | `15` | Hard limit for one summariser run, 5 to 120 |
| `watch.intervalSeconds` | `0` | Notification watcher (Switchboard, see [SWITCHBOARD.md](SWITCHBOARD.md#notification-watcher)): `0` is off, else how often it runs the `pending_input` check, 30 to 3600. Needs `notify.url`. It pushes "fpga on dev needs a decision." (permission) or "fpga on dev asks you something." (question) once per new wait (a wait seen only on the screen after 2 checks in a row), again only after the wait was gone for a check; announced waits are kept in `stateDir/watch.json` so a restart does not repeat them. Pushes name the machine and the tmux session (or the project folder of a local session), never a Claude Code session name and never pane text |
| `watch.longTurnMinutes` | `10` | Push "fpga on dev finished its turn after 25 minutes." when a session that was working at least this long becomes idle or waiting (no second push when the same change already pushed a wait); `0` turns these pushes off. 0 to 1440 |
| `watch.notifyUnreachable` | `false` | Push "Machine buildbox cannot be reached." after 3 failed checks in a row, and "Machine buildbox answers again." when it answers |
| `watch.includeQuestion` | `false` | Append the redacted question to a wait push (quoted, at most 120 characters). Separate from `notify.includeResult`: the question is screen or registry text and the push goes to a third party |

## CLI

```
uv run voice-coding-mcp <command> [options]
```

On the VPS use the wrapper, which runs as the service user and adds `--config /etc/voice-coding-mcp/config.json`: `sudo voice-coding-mcp <command>`. On the Mac pass `--config` yourself (`vcm-mac on` prints the exact `pair` command). `--config` may come before or after the command; the other options belong after their command. `voice-coding-mcp -h` and `voice-coding-mcp <command> -h` show the help.

| Command | What it does |
|---|---|
| `serve` | Run the server (started by systemd on a VPS or by `vcm-mac` on a Mac). Without `CLAUDE_CODE_OAUTH_TOKEN` it logs (info) that tasks use the logged-in user's credentials, which is expected on a Mac. Stops gracefully on SIGTERM or SIGINT: no new connections, queued and running tasks end as failed ("server stopped (SIGTERM)"), at most 20 s. `--pid-file PATH` (see below) |
| `pair [--minutes N]` | Open the pairing window and print a one-time code, its expiry in local time and the connector URL. Default `oauth.pairingMinutes` (10), at most 1440. An IP is locked out for the rest of the window after 5 wrong codes; after 50 wrong codes in total the window closes. A new `pair` replaces the old code |
| `unpair` | Close the pairing window |
| `status` | Show the connector URL, the pairing window, connected clients (no secrets; client names with control characters removed; last use as "just now", "5 minutes ago", "3 hours ago", "2 days ago" or "never") and task counts by status |
| `revoke-all --yes` | Remove every client and token; the connector has to be paired again. Refuses without `--yes` |
| `remote-check [machine]` | Call `version` and `list` on every machine of `remote.machines` (or one) and print what the dispatcher reports: its version, verbs and opt-ins, the allowed sessions with Claude Code's state, the machine's config warnings, and a warning when the server user's Claude Code `settings.json` defines hooks (the summariser loads user settings, so in same-user and `local` mode the owner's own hooks and enabled plugins run for every summary). Exit 1 when a machine failed, 2 for an unknown machine |
| `remote-dispatcher` | Print the `switchboard-remote` dispatcher script (needs no config), for example `voice-coding-mcp remote-dispatcher > ~/.local/bin/switchboard-remote` on the machine to reach |

| Option or variable | Meaning |
|---|---|
| `--config <path>` | Config file. Otherwise `VCM_CONFIG`, otherwise `/etc/voice-coding-mcp/config.json` |
| `VCM_LOG_LEVEL` | `debug`, `info` (default), `warn` or `error`, for `serve` |
| `-h`, `--help`, `-v`, `--version` | Help text, version |

Exit codes: 0 success, 1 runtime error, 2 usage or config error. A usage error prints the usage line and the reason to stderr.

`serve --pid-file PATH` writes the server's pid to `PATH` (mode 600; a missing parent directory is created with mode 700) and holds an exclusive `flock` on the file for the server's whole lifetime. On a clean exit (including a graceful SIGTERM or SIGINT) the file is removed. When another process holds the lock, `serve` prints `Error: another server holds the pid file PATH (pid N)` and exits with 1 without starting. A file left behind by a crash or `kill -9` is stale exactly when its lock is free, and the next `serve` takes it over. `vcm-mac` starts the server with `--pid-file <run dir>/server.pid` and decides from this lock whether its server is running.

`serve` logs JSON lines to stdout (journald on the VPS, `vcm-mac-server.log` on the Mac): one line per request with method, path without query, status, duration, client IP and a token fingerprint, never headers or bodies. Prompts and Claude's stderr appear only with `logPrompts`.

The CLI and the running server share `stateDir`: changes to `oauth.json` and `pairing.json` are serialized across processes with file locks (`oauth.lock`, `pairing.lock`), and the server notices changes made by the CLI.

## MCP tools

Every tool returns short plain text suitable for reading aloud. The server instructions tell the model that projects are folders (call `list_projects` when unsure), to pass the request in the user's own words and language, and how to handle a task number.

| Tool | Input | Behavior |
|---|---|---|
| `list_projects` | none | Project names (read-only) |
| `run_task` | `project`, `prompt`, `new_session?`, `remote_control?`, `title?` | Starts Claude Code in the project and waits up to `syncWaitSeconds`. Returns the answer, or the task number and the latest steps if still running. With `remote_control: true` the task starts instead as a new [Remote Control](#terms) session (`claude --bg --remote-control "<title> (voice <task>)" ...`, prompt on stdin) that you can watch, approve and continue in the Code section of the Claude app, with Claude Code's push notifications; it returns at once with the task number, the session name and the link (not read aloud). Always a new conversation. The folder must be trusted by Claude Code (run `claude` there once), else the task fails with a spoken hint. When `remoteControl.maxSessions` sessions are open (or it is `0`), the task runs headless as usual and the answer says so. A session has no timeout. `title` is a short label of 3 to 6 words the voice model makes up in the conversation's language; the session (and the headless run's conversation, via `--name`) is named `<title> (voice <task>)`, or `<project> (voice <task>)` without a title, never from the prompt. Titles are cut to 60 characters, control characters and a leading `-` are removed |
| `run_tasks_parallel` | `tasks: [{project, prompt, title?}]`, 1 to 3 items | Starts all at once, waits up to `syncWaitSeconds` in total, reports each on its own line |
| `get_task_result` | `task_id?`, `wait_seconds?` | Waits for the task (default: the latest one) and returns its answer or state. `wait_seconds` defaults to and is capped at `syncWaitSeconds`; 0 checks without waiting (read-only). For a session task it answers at once: working, waiting for your approval, waiting for your answer, finished its turn (session still open), or done (the session has ended), with the session name and its latest answer |
| `list_tasks` | `project?`, `limit?` | Recent tasks with status, newest first, default 5, at most 20 (read-only). Session tasks show their session state and name |
| `cancel_task` | `task_id` | Cancels a queued or running task. For a session task it runs `claude stop <id>`; the conversation is kept and `resume_task_in_remote_control` reopens it. For any task (also a finished one) whose Remote Control session is still running, connected or not, it stops that session and says "Session ... closed"; "already finished" only when there is nothing to stop |
| `list_sessions` | `project?`, `limit?` | Your recent Claude Code sessions on this machine (inside the project roots, last `sessions.days` days): name, project, running or not, last activity (read-only) |
| `ask_session_readonly` | `session`, `question` | Answers with the context of that session from a read-only fork (`--resume --fork-session --permission-mode plan`, only the Read, Grep and Glob tools, no MCP servers); the session itself is untouched. Returns like `run_task` |
| `resume_task_in_remote_control` | `task_id` | Reopens a finished task's Claude Code session in the background with [Remote Control](#terms) (`claude --bg --resume <id> --remote-control "<title> (voice <task>)" --name "<title> (voice <task>)"`), so you can continue it by typing in the Code section of the Claude app. Returns the session name and the link; calling it again returns the open session, and so does calling it for a task that runs as a session (`run_task` with `remote_control`). A session whose process lives but that lost its connection to the app (no `bridgeSessionId` in the live registry) is stopped first and opened again; if it has not ended within 5 s, nothing is opened and the answer asks to try again in a moment. The reopened session gets the same Stop hook as a session task, so `get_task_result` and `list_tasks` report its latest answer and say the task was continued in the Claude app. `get_task_result` and `list_tasks` say when a task's session lost its connection to the app, or that it was handed over to the desktop app. A session that is open in the desktop app on the Mac (or in a terminal) is never opened a second time: with Remote Control switched on there you get that session, else the answer says to switch Remote Control on there. Reopening a task whose session was stopped continues the latest conversation from the app, under the same session id: the server first runs `claude rm <id8>` for the stopped background session (the conversation is kept), because the CLI otherwise starts a copy. Not for running tasks or `send_message_to_session` tasks. Likely does not work in VPS mode: a `claude setup-token` token cannot establish Remote Control sessions (untested). The project folder must be trusted by Claude Code (open `claude` there once on the machine). While the session is open, a new `run_task` in that project starts a fresh conversation instead of resuming it |
| `list_remote_sessions` | `machine?` | The configured machines (all, or the one named) with their allowed tmux sessions (at most 8 per machine, then "and N more"): for each, whether Claude Code runs there and its state (working, waiting for your answer, waiting for your permission, idle), its Claude Code session name in quotes when it differs from the tmux name, and why an unreachable machine could not be reached. Machines are called in parallel (read-only) |
| `remote_status` | `machine?`, `target?`, `lines?` | Reads the screen of one tmux pane (`target`: a tmux session, `session:window[.pane]`, or a Claude Code session name; may be left out when the machine has one Claude Code pane; `machine` may be left out when only one is configured) and says what it is doing: the registry state first, then `A summary of the screen: "..."` from the summariser or `The screen says: "..."` with the last meaningful line. Pane text is stripped of terminal sequences and secrets are replaced by `[secret]` first. A summary that takes longer than the call's deadline continues as a task and the answer gives its number for `get_task_result`. Its push (`notify.url`) names only the job, never the screen text. A session with several Claude Code panes gets a question back (read-only, open world) |
| `pending_input` | `machine?` | Which Claude Code sessions wait for you, and what each asks, permission decisions first: `On dev, fpga asks for permission: "Bash command git push origin main".`, `On dev, notes asks: "should I keep the old endpoint?".`, then "Approve it at the keyboard or in the Claude app." for permission decisions, and the machines that could not be checked. A pane waits when its registry status is `waiting` (the question from its screen, else `waitingFor`) or when it is idle and the last 15 lines of its screen show Claude Code's permission dialog or end with a question (at most 6 captures per machine; waiting panes first). Without `machine` it checks every machine in parallel plus the server's own Claude Code registry for sessions outside tmux inside the project roots ("On this machine, ..."). Never runs the summariser, so it answers within the call's deadline. Quoted text is redacted, untrusted screen text (read-only) |
| `send_message_to_session` | `session`, `message` | Running sessions only. A short relay run without MCP servers hands the exact message to the session (Claude Code's SendMessage). The message counts as delivered when the relay made exactly one successful SendMessage to that session with your original text, even if the relay then fails or does not confirm; denied or failed attempts do not count, and if nothing was sent the task fails (with the denial, if there was one). Then the server follows the session's transcript and returns its reply as the task result. Returns like `run_task` |

The remote tools reach machines only through `switchboard-remote` (see [SWITCHBOARD.md](SWITCHBOARD.md#the-dispatcher)): over SSH with a dedicated key that the remote `authorized_keys` binds to the dispatcher with a forced command (`command="/usr/bin/python3 -I /home/USER/.local/bin/switchboard-remote --sessions 'fpga,notes'"`, plus `restrict` and `from=`), or as a local child process for a `local` machine. Text they quote from screens is untrusted.

Session names are the session titles (the desktop app title, or "project: first prompt" when there is none), matched like project names. The target session sees a message from another session, not your approval; a session in a different permission mode may hold it until you approve it in the desktop app, and the result then says that the message was not picked up. Sessions are read from Claude Code's internal files (`~/.claude/projects`, `~/.claude/sessions`, or under `claude.configDir`), see [DESIGN.md](DESIGN.md#the-owners-sessions). Session ids, wherever they come from, must be UUID-shaped before they are passed to `--resume`.

Name matching (projects and sessions) ignores case and accents (for example, a non-English title: "prihlaseni" finds the Czech "Přihlášení") and is loose: "alpha app" finds `alpha-app`. Queries shorter than 3 characters only match exactly. A name that contains the query (prefix or anywhere) needs a query of at least 3 characters. A name counts as contained in the query only as whole words of the query, so "website redesign" does not pick a project called `web`. Small typos are forgiven: one wrong letter for queries up to 4 characters, two above.

Answers longer than 2000 characters are shortened in the tool result; the task file keeps up to 200 000 characters. No tool is marked destructive, so with "Always allow" Claude does not stop to ask for confirmation in voice mode.

Tasks wait in one queue in arrival order (FIFO); a task whose project or session is busy waits while later tasks for other projects run, and tasks for the same project, or for the same session, keep their order.

## HTTP endpoints

| Path | Notes |
|---|---|
| `POST /mcp` | MCP Streamable HTTP, stateless, plain JSON answers (no SSE), bearer token required (401 with `WWW-Authenticate` otherwise). Body limit 1 MB. `GET`, `HEAD` and `DELETE` answer 405 |
| `GET /.well-known/oauth-protected-resource[/mcp]` | Protected resource metadata |
| `GET /.well-known/oauth-authorization-server` | Authorization server metadata |
| `POST /register` | [DCR](#terms). Only while a pairing window is open; rate limited per IP like `/authorize`; at most 20 clients (when full, the oldest client without a live grant or a pending authorization code is evicted). `client_name` is stored with control characters removed |
| `GET`/`POST /authorize` | Only while a pairing window is open; rate limited per IP (30 per minute) |
| `POST /token`, `POST /revoke` | Always open, not rate limited (all Claude connector traffic shares Anthropic's egress IPs) |
| `GET`/`HEAD /healthz` | `{"ok":true}`, no auth and no Host check, for local monitoring |

OAuth request bodies (JSON and forms) are limited to 16 KB. Every other request must carry `Host` equal to the host of `publicUrl` (403 otherwise). A browser `Origin`, when present, must be the `publicUrl` origin, `https://claude.ai` or `https://claude.com`. Per-IP limits and the per-IP count of wrong pairing codes treat IPv6 clients by their /64 prefix.

## Dependency updates

`pyproject.toml` sets `[tool.uv] exclude-newer = "14 days"`, and Dependabot uses the same 14-day cooldown. Dependabot relocks with uv, so it cannot propose a release younger than 14 days, security fixes included ([dependabot-core#16984](https://github.com/dependabot/dependabot-core/issues/16984)). For an urgent fix, exempt that one package in `pyproject.toml`:

```toml
[tool.uv]
exclude-newer = "14 days"
exclude-newer-package = { <package> = "1 day" }   # a duration, a date such as "2026-11-20", or false
```

Then run `uv lock --upgrade-package <package>` and commit `pyproject.toml` and `uv.lock`. Remove the override once the release is older than 14 days and run `uv lock` again.

## Terms

- **Custom connector**: a remote MCP server you add to Claude yourself (Settings, Connectors). See Anthropic's [guide to custom connectors](https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp).
- **Remote Control**: a Claude Code feature that lets you continue a session running on your machine from the Claude app or claude.ai. See the [Remote Control docs](https://code.claude.com/docs/en/remote-control).
- **DCR** (Dynamic Client Registration, [RFC 7591](https://datatracker.ietf.org/doc/html/rfc7591)): the client registers itself at `/register` and gets a client ID.
- **CIMD** (Client ID Metadata Document, [IETF draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-client-id-metadata-document/)): the client ID is an `https` URL whose JSON document describes the client, so no registration is needed. This server fetches only the URLs in `oauth.cimdClientIds`.
