#!/usr/bin/env bash
# install.sh — check the prerequisites, then hand over to the setup wizard.
#
# Everything interesting lives in claude_window_timing.py: accounts, checkpoints,
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

CLAUDE_BIN="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"
if [ ! -x "$CLAUDE_BIN" ]; then
    echo "ERROR: Claude Code CLI not found." >&2
    echo "       Install it from: https://claude.ai/download" >&2
    exit 1
fi

if ! command -v systemctl &>/dev/null; then
    echo "ERROR: systemctl not found — this tool requires systemd." >&2
    exit 1
fi

# systemd-run creates the one-shot anchors that re-align each schedule with its
# real window boundary. Without it the tool still pings on its fixed cadence; it
# just cannot correct its phase after a missed ping, or hold an account back to
# space the accounts out.
if ! command -v systemd-run &>/dev/null; then
    echo "WARNING: systemd-run not found — boundary anchoring and spacing will"
    echo "         be disabled."
    echo ""
fi

exec python3 "$SCRIPT_DIR/claude_window_timing.py" setup "$@"
