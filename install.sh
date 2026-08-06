#!/usr/bin/env bash
# install.sh — set up and deploy claude-early-window as a systemd *user* service.
# Creates the initial checkpoint session if needed, then installs a user timer.
#
# A user service (rather than a system service) is used deliberately: it runs in
# your own login/session context, so it inherits the D-Bus session and keyring
# needed for keychain-based Claude OAuth, and it never needs sudo to manage.
# The only privileged step is enabling "linger" so the timer keeps firing while
# you are logged out — and that is attempted gracefully.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"
USER_UNIT_DIR="$HOME/.config/systemd/user"

# Ping interval (minutes). 30 divides the 5-hour window evenly, so consecutive
# windows sit effectively back-to-back (no gap) — meaning you almost always land
# inside an active, nearly-untouched window when you start work. It also stays well
# under the ~1-hour prompt-cache TTL, so every ping is a (rate-limit-exempt) cache
# read, with margin to survive a missed ping. See the README timing section.
INTERVAL_MIN=30

echo "Claude Code Early Window — Install"
echo "===================================="

# ── Make sure systemctl --user is reachable from this shell ───────────────────
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

# ── Prerequisites ─────────────────────────────────────────────────────────────

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

echo "  python : $(python3 --version)"
echo "  claude : $("$CLAUDE_BIN" --version 2>/dev/null || echo 'version unknown')"
echo ""

# ── Checkpoint session (one-time) ─────────────────────────────────────────────

if [ -f "$SCRIPT_DIR/early_window_session_id.txt" ] && \
   [ -f "$SCRIPT_DIR/early_window_checkpoint.jsonl.bak" ]; then
    echo "Checkpoint already exists (session $(head -c 8 "$SCRIPT_DIR/early_window_session_id.txt")...) — skipping init."
else
    echo "Creating checkpoint session (opens an interactive Claude session briefly)..."
    cd "$SCRIPT_DIR"
    python3 claude_early_window.py --init

    if [ ! -f "$SCRIPT_DIR/early_window_session_id.txt" ] || \
       [ ! -f "$SCRIPT_DIR/early_window_checkpoint.jsonl.bak" ]; then
        echo "ERROR: Checkpoint creation failed. See log for details:" >&2
        echo "  $SCRIPT_DIR/claude_early_window.log" >&2
        exit 1
    fi
    echo "Checkpoint created: $(cat "$SCRIPT_DIR/early_window_session_id.txt")"
fi
echo ""

# ── Systemd user units ────────────────────────────────────────────────────────

echo "Deploying systemd user service and timer..."
mkdir -p "$USER_UNIT_DIR"

cat > "$USER_UNIT_DIR/claude-early-window.service" << EOF
[Unit]
Description=Claude Code Early Window

[Service]
Type=oneshot
WorkingDirectory=$SCRIPT_DIR
ExecStart=/usr/bin/python3 $SCRIPT_DIR/claude_early_window.py
# PATH so the script can locate the claude CLI; HOME is provided by the user manager.
Environment=PATH=$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=default.target
EOF

cat > "$USER_UNIT_DIR/claude-early-window.timer" << EOF
[Unit]
Description=Claude Code Early Window — ping every ${INTERVAL_MIN} minutes

[Timer]
# OnActiveSec fires shortly after the timer starts (relative to timer activation,
# so it works regardless of how long the user manager has been up); OnUnitActiveSec
# then repeats every INTERVAL_MIN after each run completes.
OnActiveSec=1min
OnUnitActiveSec=${INTERVAL_MIN}min
# systemd's default accuracy is 1 minute, which rounds the interval up to the next
# minute boundary and turns 59min into an effective ~60min cadence. Tighten it so
# the interval stays just under the ~1h prompt-cache TTL as intended.
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable claude-early-window.timer
systemctl --user restart claude-early-window.timer

# Keep the timer firing while logged out (best-effort; needs privilege).
if ! loginctl show-user "$CURRENT_USER" 2>/dev/null | grep -q "Linger=yes"; then
    echo "Enabling linger so the timer runs while you are logged out..."
    if command -v sudo &>/dev/null && sudo loginctl enable-linger "$CURRENT_USER" 2>/dev/null; then
        echo "  Linger enabled."
    elif loginctl enable-linger "$CURRENT_USER" 2>/dev/null; then
        echo "  Linger enabled."
    else
        echo "  WARNING: could not enable linger. The timer will only run while you"
        echo "           are logged in. To fix: sudo loginctl enable-linger $CURRENT_USER"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "Installed successfully."
echo ""
systemctl --user status claude-early-window.timer --no-pager || true
echo ""
echo "Logs : $SCRIPT_DIR/claude_early_window.log"
echo ""
echo "To reset the checkpoint:  rm early_window_session_id.txt early_window_checkpoint.jsonl.bak && ./install.sh"
echo ""
echo "To uninstall:  ./uninstall.sh"
