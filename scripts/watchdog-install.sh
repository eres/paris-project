#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

vault_arg="${1:-}"
if [[ -n "$vault_arg" ]]; then
    [[ -d "$vault_arg/.obsidian" ]] || die "Not an Obsidian vault: $vault_arg"
    VAULT_PATH="$(cd -- "$vault_arg" && pwd -P)"
elif [[ -f "$CONFIG_PATH" ]]; then
    load_service_config
else
    die "Usage: $0 /absolute/path/to/Obsidian-vault"
fi

[[ -x "$PROJECT_ROOT/.venv/bin/python" ]] || die "Virtualenv Python is not executable: $PROJECT_ROOT/.venv/bin/python"
require_file "$PROJECT_ROOT/watch_vault.py"

mkdir -p "$HOME/Library/LaunchAgents" "$LOG_DIR"
umask 077
printf 'VAULT_PATH=%q\n' "$VAULT_PATH" >"$CONFIG_PATH"

temp_plist="$(mktemp "$PROJECT_ROOT/.watchdog-plist.XXXXXX")"
trap 'rm -f "$temp_plist"' EXIT

cat >"$temp_plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PROJECT_ROOT/scripts/watchdog-run.sh</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$PROJECT_ROOT</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ProcessType</key>
    <string>Background</string>
    <key>ThrottleInterval</key>
    <integer>60</integer>
    <key>StandardOutPath</key>
    <string>$STDOUT_LOG</string>
    <key>StandardErrorPath</key>
    <string>$STDERR_LOG</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/bin:/bin:/usr/sbin:/sbin</string>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
EOF

plutil -lint "$temp_plist" >/dev/null

if service_is_loaded; then
    launchctl bootout "$SERVICE_TARGET"
fi

install -m 600 "$temp_plist" "$PLIST_PATH"
bootstrap_service
launchctl kickstart -k "$SERVICE_TARGET"

printf 'Installed and started %s\n' "$LABEL"
printf 'Vault: %s\n' "$VAULT_PATH"
printf 'Status: %s/scripts/watchdog-status.sh\n' "$PROJECT_ROOT"
