#!/usr/bin/env bash
# uninstall.sh — remove the claude-window-timing systemd *user* units.
# Runtime files (checkpoints, state, logs) under state/ are left in place, unless
# --purge is given. Ping directories are never deleted: they hold your logins.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
USER_UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

echo "Claude Code Window Timing — Uninstall"
echo "====================================="

# Everything that needs judgement lives in the Python, where it is tested.
python3 "$SCRIPT_DIR/claude_window_timing.py" uninstall "$@" || {
    echo "Could not read the account configuration; removing units by name." >&2
    # Both spellings: this project was renamed, and the fallback exists for
    # exactly the case where the Python cannot tell us which units are ours.
    for prefix in claude-window-timing claude-early-window; do
        systemctl --user list-units --all --plain --no-legend "${prefix}@*.timer" \
            2>/dev/null | sed -n "s/^\(${prefix}@[^.]*\.timer\).*/\1/p" \
            | while read -r unit; do systemctl --user disable --now "$unit" 2>/dev/null || true; done
        rm -f "$USER_UNIT_DIR/${prefix}"*.service "$USER_UNIT_DIR/${prefix}"*.timer
    done
    systemctl --user daemon-reload 2>/dev/null || true
}

echo ""
echo "Uninstalled successfully."
