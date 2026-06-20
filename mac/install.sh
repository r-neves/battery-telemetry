#!/usr/bin/env bash
# Install the Mac battery telemetry job as a launchd LaunchAgent.
# Reads interval_seconds from config.json and runs the script on that interval.
set -euo pipefail

MAC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$MAC_DIR/.." && pwd)"
PYTHON="$REPO_DIR/.venv/bin/python"
SCRIPT="$MAC_DIR/battery_to_mqtt.py"
CONFIG="$MAC_DIR/config.json"
LABEL="com.battery-telemetry.mac"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$MAC_DIR/battery-telemetry.log"

if [[ ! -x "$PYTHON" ]]; then
    echo "venv not found. Run from repo root:  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi
if [[ ! -f "$CONFIG" ]]; then
    echo "Missing $CONFIG. Copy config.example.json to config.json and edit it first." >&2
    exit 1
fi

# Quick sanity check that the script runs and config parses.
echo "Testing publish (dry-run)…"
"$PYTHON" "$SCRIPT" --config "$CONFIG" --dry-run >/dev/null

INTERVAL="$("$PYTHON" -c "import json,sys; print(int(json.load(open('$CONFIG')).get('interval_seconds',60)))")"

mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s#__PYTHON__#$PYTHON#g" \
    -e "s#__SCRIPT__#$SCRIPT#g" \
    -e "s#__INTERVAL__#$INTERVAL#g" \
    -e "s#__LOG__#$LOG#g" \
    "$MAC_DIR/com.battery-telemetry.mac.plist.template" > "$PLIST"

# Reload if already installed.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "Installed and started: $LABEL (every ${INTERVAL}s)"
echo "  plist: $PLIST"
echo "  logs:  $LOG"
echo "Uninstall:  launchctl bootout gui/$(id -u)/$LABEL && rm '$PLIST'"
