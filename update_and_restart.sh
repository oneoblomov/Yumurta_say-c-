#!/bin/bash
# update_and_restart.sh - Release tabanli guncelleme sarmalayicisi

set -euo pipefail

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$WORK_DIR/update.log"
ACTION="${1:-auto}"

if [ "$#" -gt 0 ]; then
    shift
fi

cd "$WORK_DIR"

echo "$(date): update_and_restart action=$ACTION" >> "$LOG_FILE"
python3 "$WORK_DIR/manage_update.py" "$ACTION" "$@" >> "$LOG_FILE" 2>&1
echo "$(date): update_and_restart action=$ACTION bitti" >> "$LOG_FILE"