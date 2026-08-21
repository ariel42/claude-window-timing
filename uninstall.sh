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

# Asking what this does must never be a way of doing it. argparse prints the
# help and exits 0, which used to fall straight through to the success line
# below and tell somebody their install had been removed for reading about it.
for arg in "$@"; do
    case "$arg" in
        -h|--help)
            exec python3 "$SCRIPT_DIR/claude_window_timing.py" uninstall --help ;;
    esac
done

# Everything that needs judgement lives in the Python, where it is tested.
set +e
python3 "$SCRIPT_DIR/claude_window_timing.py" uninstall "$@"
status=$?
set -e

# A rejected command line is not a broken configuration. Exit 2 is argparse
# saying it did not understand the flag, and tearing the install down on the
# strength of a typo -- then printing "Uninstalled successfully", having not
# done the --purge that was asked for -- is three false statements on one
# screen. Only a genuine failure to read the configuration falls through.
if [ "$status" -eq 2 ]; then
    echo "" >&2
    echo "Nothing was removed." >&2
    exit 2
fi

if [ "$status" -ne 0 ]; then
    echo "Could not read the account configuration; removing units by name." >&2
    systemctl --user list-units --all --plain --no-legend 'claude-window-timing@*.timer' \
        2>/dev/null | sed -n 's/^\(claude-window-timing@[^.]*\.timer\).*/\1/p' \
        | while read -r unit; do systemctl --user disable --now "$unit" 2>/dev/null || true; done
    rm -f "$USER_UNIT_DIR"/claude-window-timing*.service \
          "$USER_UNIT_DIR"/claude-window-timing*.timer
    # The per-account stagger lives in a drop-in directory, which `rm -f` above
    # cannot remove — leaving it behind makes the next install look like an
    # upgrade of something that is no longer there.
    rm -rf "$USER_UNIT_DIR"/claude-window-timing*.timer.d
    systemctl --user daemon-reload 2>/dev/null || true
fi

echo ""
echo "Uninstalled successfully."
