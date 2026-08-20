#!/usr/bin/env bash
# install.sh — check the prerequisites, then hand over to the setup wizard.
#
# Everything interesting lives in claude_early_window.py: accounts, checkpoints,
# systemd units and the launcher. Keeping the shell script to the checks it is
# actually good at means one implementation of the setup logic, and one that can
# be tested.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Install Python 3.6 or later." >&2
    exit 1
fi
python3 - <<'EOF'
import sys
if sys.version_info < (3, 6):
    print(f"ERROR: Python 3.6+ required (found {sys.version})", file=sys.stderr)
    sys.exit(1)
EOF

# Required in both modes. Pinging obviously needs it; switching needs it too,
# because a parked login is created by running `claude` and signing in, and
# because it is the thing being pointed at a different account. Installing
# without it would write a configuration that cannot do anything yet.
CLAUDE_BIN="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"
if [ ! -x "$CLAUDE_BIN" ]; then
    echo "ERROR: Claude Code CLI not found." >&2
    echo "       Install it from: https://claude.ai/download" >&2
    echo "       It is needed even to set this machine up for switching only:" >&2
    echo "       signing in to an account is done by running claude itself." >&2
    exit 1
fi

# systemd is what runs the pings. A machine that only switches accounts has no
# timers, so requiring it there would turn a working setup into an error for a
# component it never uses.
WANTS_PINGS=1
for arg in "$@"; do
    [ "$arg" = "--no-pings" ] && WANTS_PINGS=0
done

if [ "$WANTS_PINGS" = "1" ]; then
    if ! command -v systemctl &>/dev/null; then
        echo "ERROR: systemctl not found — running the pings requires systemd." >&2
        echo "       To set this machine up for switching accounts only:" >&2
        echo "           ./install.sh --no-pings" >&2
        exit 1
    fi

    # systemd-run creates the one-shot anchors that re-align each schedule with
    # its real window boundary. Without it the tool still pings on its fixed
    # cadence; it just cannot correct its phase after a missed ping, or hold an
    # account back to space the accounts out.
    if ! command -v systemd-run &>/dev/null; then
        echo "WARNING: systemd-run not found — boundary anchoring and spacing will"
        echo "         be disabled."
        echo ""
    fi
fi

exec python3 "$SCRIPT_DIR/claude_early_window.py" setup "$@"
