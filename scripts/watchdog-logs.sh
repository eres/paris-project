#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

mkdir -p "$LOG_DIR"
touch "$STDOUT_LOG" "$STDERR_LOG"
exec tail -n 100 -F "$STDOUT_LOG" "$STDERR_LOG"
