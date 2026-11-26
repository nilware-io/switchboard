#!/usr/bin/env bash
# Provision voice-coding-mcp on Ubuntu 24.04 (aarch64 or x86_64). Run as root:
#
#   sudo git clone <repo-url> /opt/voice-coding-mcp
#   sudo /opt/voice-coding-mcp/deploy/setup.sh vcm.example.com
#
# Idempotent: safe to run again (for example after an update). It never
# overwrites config.json, the env file or ~/.claude/settings.json once they
# exist, and it never starts voice-coding-mcp itself. It takes no secrets.
#
# With VCM_AGENT_RELAY=1 it also sets up the relay account for machines that
# connect out to the hub (docs/SWITCHBOARD.md, "Outbound agents"): the system
# user sbrelay, its root-owned key file, the socket directory and the sshd
# drop-in. It never adds a key; that stays a manual, checked step.

set -euo pipefail

APP_USER=vcm
APP_DIR=/opt/voice-coding-mcp
ETC_DIR=/etc/voice-coding-mcp
STATE_DIR=/var/lib/voice-coding-mcp
HOME_DIR=/home/$APP_USER
# uv from its official GitHub release, pinned by version and SHA-256 (per architecture).
# To update: pick a release at least two weeks old, take the checksums from its
# uv-<arch>.tar.gz.sha256 assets, and change these three lines together.
UV_VERSION=0.13.7
UV_SHA256_AARCH64=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc
UV_SHA256_X86_64=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83
# Python from the distribution (Ubuntu 24.04 ships 3.12); uv never downloads one.
PYTHON_BIN=/usr/bin/python3.12

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

usage() {
	echo "usage: $0 <public-host>   (for example vcm.example.com)" >&2
	exit 2
}

[[ $# -eq 1 ]] || usage
HOST=$1
# Strict check: the host is substituted into the Caddyfile and config.json.
[[ $HOST =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
	|| die "invalid host name: $HOST"

[[ $(id -u) -eq 0 ]] || die "run as root (sudo $0 $HOST)"

SRC_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ $SRC_DIR == "$APP_DIR" ]] \
	|| die "run this script from a checkout at $APP_DIR (found $SRC_DIR)"

# shellcheck source=/dev/null
. /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] \
	|| echo "warning: tested on Ubuntu 24.04 only, this is ${PRETTY_NAME:-unknown}" >&2

export DEBIAN_FRONTEND=noninteractive

# Write $2 to file $1 only if the content differs. Returns 0 if it changed.
write_if_changed() {
	local dest=$1 content=$2
	if [[ -f $dest ]] && [[ $(cat "$dest") == "$content" ]]; then
		return 1
	fi
	printf '%s\n' "$content" >"$dest"
	return 0
}

# ---------------------------------------------------------------- packages

log "Base packages"
apt-get update -q
apt-get install -y -q ca-certificates curl gnupg git jq ripgrep \
	bubblewrap socat iptables-persistent python3
install -d -m 0755 /etc/apt/keyrings

log "Caddy from its official signed repository"
if [[ ! -s /etc/apt/keyrings/caddy-stable.gpg ]]; then
	curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --dearmor --yes -o /etc/apt/keyrings/caddy-stable.gpg
	chmod 0644 /etc/apt/keyrings/caddy-stable.gpg
fi
write_if_changed /etc/apt/sources.list.d/caddy-stable.list \
	"deb [signed-by=/etc/apt/keyrings/caddy-stable.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" || true

apt-get update -q
apt-get install -y -q caddy
[[ -x $PYTHON_BIN ]] || die "$PYTHON_BIN missing (Ubuntu 24.04 ships Python 3.12 as python3)"
echo "$($PYTHON_BIN --version), $(caddy version | head -n1)"

log "uv $UV_VERSION from its official release (checksum pinned)"
case $(uname -m) in
	aarch64) uv_target=aarch64-unknown-linux-gnu uv_sha=$UV_SHA256_AARCH64 ;;
	x86_64) uv_target=x86_64-unknown-linux-gnu uv_sha=$UV_SHA256_X86_64 ;;
	*) die "unsupported architecture $(uname -m)" ;;
esac
if [[ $(/usr/local/bin/uv --version 2>/dev/null) != "uv $UV_VERSION"* ]]; then
	uv_tmp=$(mktemp -d)
	curl -fsSL --proto '=https' --tlsv1.2 -o "$uv_tmp/uv.tar.gz" \
		"https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$uv_target.tar.gz"
	echo "$uv_sha  $uv_tmp/uv.tar.gz" | sha256sum -c --quiet - \
		|| die "uv download does not match the pinned SHA-256"
	tar -xzf "$uv_tmp/uv.tar.gz" -C "$uv_tmp"
	install -o root -g root -m 0755 "$uv_tmp/uv-$uv_target/uv" /usr/local/bin/uv
	rm -rf "$uv_tmp"
fi
/usr/local/bin/uv --version

# ---------------------------------------------------------------- firewall

log "Instance firewall: allow TCP 80 and 443"
# Oracle's Ubuntu images ship INPUT rules ending in a REJECT rule
# (/etc/iptables/rules.v4). ACCEPT rules must go before it.
fw_changed=0
for port in 80 443; do
	rule=(-p tcp -m conntrack --ctstate NEW --dport "$port" -j ACCEPT)
	if iptables -C INPUT "${rule[@]}" 2>/dev/null; then
		continue
	fi
	reject_line=$(iptables -L INPUT -n --line-numbers | awk '$2 == "REJECT" { print $1; exit }')
	if [[ -n $reject_line ]]; then
		iptables -I INPUT "$reject_line" "${rule[@]}"
	else
		iptables -A INPUT "${rule[@]}"
	fi
	fw_changed=1
done
if [[ $fw_changed -eq 1 ]]; then
	netfilter-persistent save
fi
iptables -L INPUT -n --line-numbers

# ---------------------------------------------------------------- user

log "Service user $APP_USER (no sudo)"
if ! id "$APP_USER" >/dev/null 2>&1; then
	useradd --create-home --home-dir "$HOME_DIR" --shell /bin/bash --user-group "$APP_USER"
fi
for group in sudo admin wheel; do
	if id -nG "$APP_USER" | tr ' ' '\n' | grep -qx "$group"; then
		gpasswd -d "$APP_USER" "$group"
	fi
done
[[ ! -e /etc/sudoers.d/$APP_USER ]] || die "/etc/sudoers.d/$APP_USER exists, remove it"
chmod 0700 "$HOME_DIR"

install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$HOME_DIR/projects"
install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$HOME_DIR/.claude"
if [[ ! -e $HOME_DIR/.claude/settings.json ]]; then
	# The server itself is a claude.ai connector; never load connectors
	# inside the tasks it runs.
	printf '%s\n' '{"disableClaudeAiConnectors": true}' >"$HOME_DIR/.claude/settings.json"
	chown "$APP_USER:$APP_USER" "$HOME_DIR/.claude/settings.json"
	chmod 0600 "$HOME_DIR/.claude/settings.json"
fi

install -d -o "$APP_USER" -g "$APP_USER" -m 0700 "$STATE_DIR"

# ---------------------------------------------------------------- hardening

# Claude Code runs as $APP_USER, the same user as the server. These settings
# make it harder for a task (for example one steered by prompt injection) to
# loosen its own limits or persist. They are not a full boundary: a shell
# command run by Claude can still reach these paths as $APP_USER (see the
# security model in docs/DESIGN.md).
#
# Managed settings: Linux path /etc/claude-code/managed-settings.json, keys per
# https://code.claude.com/docs/en/settings-reference and
# https://code.claude.com/docs/en/managed-settings. Owned by root and read-only
# for $APP_USER, so a task cannot edit them. Every key:
#   allowManagedHooksOnly: only hooks from this file run; hooks in
#     ~/.claude/settings.json, a repository's .claude/ files or plugins are
#     ignored, so a task cannot add a hook that runs on every later task.
#   allowManagedPermissionRulesOnly: allow/ask/deny rules come only from this
#     file; rules in user, project and local settings and --allowedTools are
#     ignored, so a task cannot grant itself new permissions. Put any allow
#     rules you need here.
#   permissions.disableBypassPermissionsMode "disable": bypassPermissions mode
#     (--dangerously-skip-permissions) can never be entered.
#   disableClaudeAiConnectors: Claude Code does not fetch or connect the
#     claude.ai connectors (this server is one of them).
#   permissions.deny: Claude's file tools may not touch the server state
#     (OAuth hashes, pairing window, tasks), the server config and env file,
#     or the Claude Code user settings. A Read deny also blocks Edit and
#     Write on the same path; the Edit deny covers every file editing tool,
#     NotebookEdit included. There are no Write(...) rules because Claude
#     Code does not match Write rules against paths (Edit rules apply). The
#     leading // marks an absolute path.
log "Claude Code managed settings for $APP_USER (/etc/claude-code)"
install -d -o root -g root -m 0755 /etc/claude-code
managed_settings=$(jq -n --arg state "$STATE_DIR" --arg etc "$ETC_DIR" --arg home "$HOME_DIR" '{
	allowManagedHooksOnly: true,
	allowManagedPermissionRulesOnly: true,
	disableClaudeAiConnectors: true,
	permissions: {
		disableBypassPermissionsMode: "disable",
		deny: [
			"Read(/\($state)/**)", "Edit(/\($state)/**)",
			"Read(/\($etc)/**)", "Edit(/\($etc)/**)",
			"Read(/\($home)/.claude/settings.json)", "Edit(/\($home)/.claude/settings.json)"
		]
	}
}')
if write_if_changed /etc/claude-code/managed-settings.json "$managed_settings"; then
	echo "wrote /etc/claude-code/managed-settings.json"
fi
chown root:root /etc/claude-code/managed-settings.json
chmod 0644 /etc/claude-code/managed-settings.json

# No cron or at jobs for $APP_USER: a task must not schedule commands that
# outlive it. Both tools refuse users listed in their deny file (unless an
# allow file exists, which then lists the only permitted users).
log "Deny cron and at for $APP_USER"
for deny in /etc/cron.deny /etc/at.deny; do
	if ! grep -qx "$APP_USER" "$deny" 2>/dev/null; then
		printf '%s\n' "$APP_USER" >>"$deny"
		echo "added $APP_USER to $deny"
	fi
	chown root:root "$deny"
	chmod 0644 "$deny"
done
for allow in /etc/cron.allow /etc/at.allow; do
	if [[ -f $allow ]] && grep -qx "$APP_USER" "$allow"; then
		echo "warning: $APP_USER is listed in $allow, remove it" >&2
	fi
done

# ---------------------------------------------------------------- app

log "Application in $APP_DIR (owned by root, read-only for $APP_USER)"
[[ -f $APP_DIR/uv.lock ]] || die "$APP_DIR/uv.lock missing"
chown -R root:root "$APP_DIR"
chmod -R go-w "$APP_DIR"
# Exact versions from uv.lock (--frozen: never re-resolve), no dev tools, the package installed
# into the venv rather than linked to the checkout, bytecode compiled now because the service
# user cannot write to $APP_DIR.
(
	cd "$APP_DIR"
	UV_PYTHON=$PYTHON_BIN UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=$APP_DIR/.venv \
		/usr/local/bin/uv sync --frozen --no-dev --no-editable --compile-bytecode
)
chown -R root:root "$APP_DIR"
chmod -R go-w,o+rX "$APP_DIR"
"$APP_DIR/.venv/bin/python" --version

log "Configuration in $ETC_DIR"
install -d -o root -g "$APP_USER" -m 0750 "$ETC_DIR"
if [[ ! -e $ETC_DIR/config.json ]]; then
	[[ -f $APP_DIR/config.example.json ]] || die "$APP_DIR/config.example.json missing"
	tmp=$(mktemp)
	jq --arg url "https://$HOST" '.publicUrl = $url' "$APP_DIR/config.example.json" >"$tmp"
	install -o root -g "$APP_USER" -m 0640 "$tmp" "$ETC_DIR/config.json"
	rm -f "$tmp"
	echo "created $ETC_DIR/config.json (publicUrl https://$HOST)"
else
	echo "keeping existing $ETC_DIR/config.json"
fi
if [[ ! -e $ETC_DIR/env ]]; then
	tmp=$(mktemp)
	cat >"$tmp" <<'EOF'
# Environment for voice-coding-mcp.service. Edit with: sudo -e /etc/voice-coding-mcp/env
# Token printed by `claude setup-token` (run as the vcm user).
# Do not set ANTHROPIC_API_KEY here, it would take precedence over the subscription.
CLAUDE_CODE_OAUTH_TOKEN=
EOF
	install -o root -g "$APP_USER" -m 0640 "$tmp" "$ETC_DIR/env"
	rm -f "$tmp"
	echo "created $ETC_DIR/env (token still empty)"
else
	echo "keeping existing $ETC_DIR/env"
fi

log "CLI wrapper /usr/local/bin/voice-coding-mcp"
# shellcheck disable=SC2016 # the wrapper is written literally, it expands when it runs
wrapper='#!/bin/sh
# Runs the voice-coding-mcp CLI as the service user. Use with sudo.
set -eu
if [ "$(id -u)" -ne 0 ]; then
	echo "usage: sudo voice-coding-mcp <pair [--minutes N]|unpair|status|revoke-all --yes>" >&2
	exit 1
fi
cd /
exec runuser -u vcm -- env HOME=/home/vcm /opt/voice-coding-mcp/.venv/bin/voice-coding-mcp "$@" --config /etc/voice-coding-mcp/config.json'
if write_if_changed /usr/local/bin/voice-coding-mcp "$wrapper"; then
	echo "installed wrapper"
fi
chmod 0755 /usr/local/bin/voice-coding-mcp

# ---------------------------------------------------------------- systemd

log "systemd unit (installed, not started)"
install -o root -g root -m 0644 "$APP_DIR/deploy/voice-coding-mcp.service" \
	/etc/systemd/system/voice-coding-mcp.service
systemctl daemon-reload
if systemctl is-active --quiet voice-coding-mcp; then
	echo "voice-coding-mcp is running, restarting it with the new code"
	systemctl restart voice-coding-mcp
fi

# ---------------------------------------------------------------- agent relay (opt-in)

RELAY_USER=sbrelay
RELAY_KEYS_DIR=/etc/ssh/switchboard-relay
if [[ ${VCM_AGENT_RELAY:-0} == 1 ]]; then
	log "Relay account $RELAY_USER for machines that connect out to the hub"
	if ! id "$RELAY_USER" >/dev/null 2>&1; then
		# No password; /bin/sh because sshd runs forced commands through the login shell.
		useradd --system --user-group --shell /bin/sh --home-dir "/var/lib/$RELAY_USER" --no-create-home "$RELAY_USER"
	fi
	if id -nG "$RELAY_USER" | tr ' ' '\n' | grep -qx "$APP_USER"; then
		die "$RELAY_USER must not be in group $APP_USER"
	fi
	if id -nG "$APP_USER" | tr ' ' '\n' | grep -qx "$RELAY_USER"; then
		die "$APP_USER must not be in group $RELAY_USER"
	fi
	install -d -o root -g root -m 0755 "/var/lib/$RELAY_USER" "$RELAY_KEYS_DIR"
	if [[ ! -e $RELAY_KEYS_DIR/authorized_keys ]]; then
		install -o root -g root -m 0644 /dev/null "$RELAY_KEYS_DIR/authorized_keys"
	fi
	chown root:root "$RELAY_KEYS_DIR/authorized_keys"
	chmod 0644 "$RELAY_KEYS_DIR/authorized_keys"
	install -o root -g root -m 0644 "$APP_DIR/deploy/relay/tmpfiles-switchboard-relay.conf" \
		/etc/tmpfiles.d/switchboard-relay.conf
	systemd-tmpfiles --create /etc/tmpfiles.d/switchboard-relay.conf
	install -o root -g root -m 0644 "$APP_DIR/deploy/relay/sshd-switchboard-relay.conf" \
		/etc/ssh/sshd_config.d/switchboard-relay.conf
	sshd -t
	sshd -T -C "user=$RELAY_USER,host=relay-check,addr=192.0.2.1" | grep -Ex 'disableforwarding yes|permittty no' \
		| wc -l | grep -qx 2 || die "the sshd drop-in for $RELAY_USER is not in effect"
	systemctl reload ssh 2>/dev/null || systemctl reload sshd
	echo "Relay ready. Add one key line per machine to $RELAY_KEYS_DIR/authorized_keys (format in"
	echo "$APP_DIR/deploy/relay/authorized_keys.example), then run: voice-coding-mcp remote-check"
	echo "Host key for the machines to pin (compare it on this console):"
	ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
fi

# ---------------------------------------------------------------- caddy

log "Caddy for https://$HOST"
caddyfile=$(sed "s/vcm\.example\.com/$HOST/g" "$APP_DIR/deploy/Caddyfile.example")
if [[ -f /etc/caddy/Caddyfile ]] && [[ $(cat /etc/caddy/Caddyfile) != "$caddyfile" ]] \
	&& [[ ! -e /etc/caddy/Caddyfile.orig ]]; then
	cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.orig
fi
if write_if_changed /etc/caddy/Caddyfile "$caddyfile"; then
	chmod 0644 /etc/caddy/Caddyfile
	caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
	systemctl enable caddy
	systemctl reload-or-restart caddy
	echo "Caddy reloaded"
else
	echo "Caddyfile unchanged"
fi

# ---------------------------------------------------------------- done

token_state="EMPTY"
if grep -Eq '^CLAUDE_CODE_OAUTH_TOKEN=.+' "$ETC_DIR/env"; then
	token_state="set"
fi

cat <<EOF

Setup finished. voice-coding-mcp has NOT been started.
Token in $ETC_DIR/env: $token_state

Remaining manual steps (details in $APP_DIR/docs/DEPLOY.md):
  1. Install Claude Code for $APP_USER with the official native installer:
       sudo -iu $APP_USER
       curl -fsSL https://claude.ai/install.sh | bash
  2. Still as $APP_USER, create a long-lived token:  claude setup-token
     Then, back as your admin user, paste it into the env file:
       sudo -e $ETC_DIR/env
  3. Clone projects as $APP_USER into $HOME_DIR/projects (per-repo deploy keys).
  4. Review $ETC_DIR/config.json:  sudo -e $ETC_DIR/config.json
  5. Start the service:
       sudo systemctl enable --now voice-coding-mcp
       sudo journalctl -u voice-coding-mcp -f
  6. Pair Claude:  sudo voice-coding-mcp pair
     and add the custom connector https://$HOST/mcp in Claude within 10 minutes.
EOF
