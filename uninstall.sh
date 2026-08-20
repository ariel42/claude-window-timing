#!/usr/bin/env bash
# uninstall.sh — remove the claude-window-timing systemd *user* units.
# Runtime files (checkpoints, state, logs) under state/ are left in place, unless
# --purge is given. Ping directories are never deleted: they hold your logins.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "Claude Code Early Window — Uninstall"
echo "======================================"

# Everything that needs judgement lives in the Python, where it is tested.
python3 "$SCRIPT_DIR/claude_window_timing.py" uninstall "$@" || {
    echo "Could not read the account configuration; removing units by name." >&2
    systemctl --user list-units --all --plain --no-legend 'claude-window-timing@*.timer' \
        2>/dev/null | sed -n 's/^\(claude-window-timing@[^.]*\.timer\).*/\1/p' \
        | while read -r unit; do systemctl --user disable --now "$unit" 2>/dev/null || true; done
    rm -f "$USER_UNIT_DIR"/claude-window-timing*.service \
          "$USER_UNIT_DIR"/claude-window-timing*.timer
    systemctl --user daemon-reload 2>/dev/null || true
}

echo ""
echo "Uninstalled successfully."
