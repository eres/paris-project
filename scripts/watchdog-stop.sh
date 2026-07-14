#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

if service_is_loaded; then
    launchctl bootout "$SERVICE_TARGET"
    printf 'Stopped %s\n' "$LABEL"
else
    printf '%s is already stopped\n' "$LABEL"
fi
