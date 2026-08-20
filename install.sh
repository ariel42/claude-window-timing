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
# Written without an f-string on purpose: this is the one piece of Python here
# that has to run on the version it is about to reject. An f-string is a syntax
# error before 3.6, so the interpreter would refuse to parse the check and the
# user would get a traceback instead of the sentence explaining what is wrong.
python3 - <<'EOF'
import sys
if sys.version_info < (3, 6):
    sys.stderr.write("ERROR: Python 3.6+ required (found %s)\n" % sys.version)
    sys.exit(1)
EOF

# Required in both modes. Pinging obviously needs it; switching needs it too,
# because a parked login is created by running `claude` and signing in, and
# because it is the thing being pointed at a different account. Installing
# without it would write a configuration that cannot do anything yet.
CLAUDE_BIN="$(command -v claude 2>/dev/null || echo "$HOME/.local/bin/claude")"
if [ ! -x "$CLAUDE_BIN" ]; then
    echo "ERROR: Claude Code CLI not found." >&2
    echo "       Required on every machine this runs on — for running the pings," >&2
    echo "       and for switching accounts on a machine that does not ping." >&2
    echo "       Required even if you only ever use Claude Code through the" >&2
    echo "       editor extension." >&2
    echo "       Install it from: https://claude.ai/download" >&2
    exit 1
fi

exec python3 "$SCRIPT_DIR/claude_window_timing.py" setup "$@"
