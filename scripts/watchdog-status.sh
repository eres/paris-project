#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

if ! service_is_loaded; then
    printf '%s is not loaded\n' "$LABEL" >&2
    exit 1
fi

launchctl print "$SERVICE_TARGET"
