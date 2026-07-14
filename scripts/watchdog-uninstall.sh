#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

if service_is_loaded; then
    launchctl bootout "$SERVICE_TARGET"
fi
rm -f "$PLIST_PATH" "$CONFIG_PATH"
printf 'Uninstalled %s (logs were preserved in %s)\n' "$LABEL" "$LOG_DIR"
