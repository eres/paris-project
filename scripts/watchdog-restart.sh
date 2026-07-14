#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

require_file "$PLIST_PATH"
if service_is_loaded; then
    launchctl bootout "$SERVICE_TARGET"
fi
bootstrap_service
launchctl kickstart -k "$SERVICE_TARGET"
printf 'Restarted %s\n' "$LABEL"
