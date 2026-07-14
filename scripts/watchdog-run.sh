#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=watchdog-common.sh
source "$SCRIPT_DIR/watchdog-common.sh"

load_service_config
require_file "$PROJECT_ROOT/watch_vault.py"
[[ -x "$PROJECT_ROOT/.venv/bin/python" ]] || die "Virtualenv Python is not executable: $PROJECT_ROOT/.venv/bin/python"

export PYTHONUNBUFFERED=1
exec "$PROJECT_ROOT/.venv/bin/python" -u "$PROJECT_ROOT/watch_vault.py" \
    "$VAULT_PATH" \
    --observer native
