"""
Claude Code Early Window
Keeps a Claude Code usage window rolling in the background so that you start work
inside a fresh, almost-untouched window. Requires Python 3.6+, Linux, and the
Claude Code CLI.

Usage:
  python3 claude_early_window.py --init   # one-time setup (called by install.sh)
  python3 claude_early_window.py          # early-window run (called by systemd timer)
"""

import json
import os
import pty
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# Paths — all derived from the script's own location, no hardcoded user paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME       = os.path.expanduser("~")
USER       = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

# Claude CLI: prefer PATH lookup, fall back to ~/.local/bin/claude
CLAUDE_PATH = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")

# Claude Code stores session files under ~/.claude/projects/<cwd-as-path>/
# where every "/" in the cwd is replaced with "-".
SESSION_DIR = os.path.join(
    HOME, ".claude", "projects", SCRIPT_DIR.replace("/", "-")
)

LOG_FILE          = os.path.join(SCRIPT_DIR, "claude_early_window.log")
SESSION_ID_FILE   = os.path.join(SCRIPT_DIR, "early_window_session_id.txt")
CHECKPOINT_BACKUP = os.path.join(SCRIPT_DIR, "early_window_checkpoint.jsonl.bak")

LOG_RETENTION_HOURS = 48


# Environment variables to pass through to the Claude subprocess *if* present.
# These cover keychain-based OAuth on Linux (D-Bus / XDG) and locale; nothing here
# affects billing. We deliberately do NOT inherit the full environment: vars like
# CLAUDECODE / CLAUDE_CODE_CHILD_SESSION (set when this script itself is launched
# from within Claude Code) make the child behave as a nested session and silently
# disable session persistence, and ANTHROPIC_API_KEY would divert billing to the
# pay-as-you-go API instead of the subscription window.
_ENV_PASSTHROUGH = (
    "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR", "XDG_DATA_HOME",
    "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "LANG", "LC_ALL", "LC_CTYPE",
)


def build_claude_env():
    """Build a clean, minimal environment for the Claude subprocess."""
    env = {
        "HOME":  HOME,
        "USER":  USER,
        "TERM":  "xterm-256color",
        "SHELL": "/bin/bash",
        "PATH":  os.path.join(HOME, ".local", "bin")
                 + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    }
    for key in _ENV_PASSTHROUGH:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line)


def rotate_log():
    if not os.path.exists(LOG_FILE):
        return
    cutoff = datetime.now() - timedelta(hours=LOG_RETENTION_HOURS)
    with open(LOG_FILE, "r") as f:
        lines = f.readlines()
    kept = []
    for line in lines:
        try:
            ts = datetime.strptime(line[1:20], "%Y-%m-%d %H:%M:%S")
            if ts >= cutoff:
                kept.append(line)
        except ValueError:
            if not line.strip():
                kept.append(line)  # preserve blank separator lines between runs
    with open(LOG_FILE, "w") as f:
        f.writelines(kept)


# ---------------------------------------------------------------------------
# Checkpoint backup / restore
# ---------------------------------------------------------------------------

def _session_file(session_id):
    return os.path.join(SESSION_DIR, f"{session_id}.jsonl")


def _assistant_entries(session_id):
    """Return the (deduplicated) assistant-turn objects recorded in the session file."""
    path = _session_file(session_id)
    if not os.path.exists(path):
        return []
    entries, seen = [], set()
    with open(path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if obj.get("type") == "assistant":
                uid = obj.get("uuid")
                if uid in seen:
                    continue
                seen.add(uid)
                entries.append(obj)
    return entries


def backup_checkpoint(session_id):
    src = _session_file(session_id)
    shutil.copy2(src, CHECKPOINT_BACKUP)
    log(f"Checkpoint backed up ({os.path.getsize(CHECKPOINT_BACKUP)} bytes)")


def restore_checkpoint(session_id):
    shutil.copy2(CHECKPOINT_BACKUP, _session_file(session_id))
    log("Checkpoint restored to frozen 'hi' state")


# ---------------------------------------------------------------------------
# Interactive Claude session (PTY)
# ---------------------------------------------------------------------------

def run_interactive(extra_args, prompt_text, session_id,
                    startup_wait=8, completion_timeout=60):
    """
    Spawn an interactive Claude session in a PTY, send prompt_text, wait for the
    reply to settle, then /exit and verify the turn was recorded.

      * --tools ""                       strips built-in tool definitions
      * --strict-mcp-config --mcp-config minimises the system prompt to no MCP servers
      * --model haiku --effort low       cheapest possible turn

    Completion is detected adaptively from PTY output (output appears, then goes
    quiet) rather than by a fixed sleep. Claude Code only flushes the session JSONL
    on exit, so success is confirmed *after* /exit by checking that a new assistant
    turn was persisted; its token usage is logged (cache read, which is
    rate-limit-exempt, vs cache write) so each run's real cost is visible.
    Returns True only if a new assistant turn was recorded.
    """
    if not os.path.isfile(CLAUDE_PATH):
        log(f"ERROR: Claude CLI not found at {CLAUDE_PATH}. "
            "Install Claude Code from https://claude.ai/download")
        return False

    master_fd, slave_fd = pty.openpty()
    cmd = [CLAUDE_PATH,
           "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--model", "haiku", "--effort", "low"] + extra_args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=SCRIPT_DIR,
            preexec_fn=os.setsid,
            env=build_claude_env(),
        )
    except Exception as e:
        log(f"ERROR: Failed to spawn Claude: {e}")
        os.close(master_fd)
        os.close(slave_fd)
        return False

    os.close(slave_fd)
    os.set_blocking(master_fd, False)

    time.sleep(startup_wait)
    try:
        os.read(master_fd, 8192)  # drain startup output
    except BlockingIOError:
        pass

    # Claude Code only persists the session JSONL on a clean exit, not mid-run, so
    # completion can't be detected by watching the file. Instead we wait on the PTY:
    # the turn is done once output has gone quiet for `idle_threshold` seconds. We
    # also enforce `min_settle` first, because the gap before the model starts
    # replying (especially on a cache miss, e.g. the very first ping) can be several
    # seconds with no output — exiting during that gap would abort the turn.
    # Draining the PTY throughout also keeps the child from blocking on a full buffer.
    baseline = len(_assistant_entries(session_id))
    log(f"Sending: '{prompt_text}'")
    os.write(master_fd, (prompt_text + "\r").encode())

    min_settle = 10.0
    idle_threshold = 5.0
    start = time.time()
    deadline = start + completion_timeout
    last_activity = start
    saw_output = False
    while time.time() < deadline:
        time.sleep(0.5)
        try:
            chunk = os.read(master_fd, 65536)
        except BlockingIOError:
            chunk = b""
        if chunk:
            saw_output = True
            last_activity = time.time()
        elif (saw_output
              and (time.time() - start) >= min_settle
              and (time.time() - last_activity) >= idle_threshold):
            break  # output settled — turn finished

    os.write(master_fd, b"/exit\r")

    for _ in range(8):
        if proc.poll() is not None:
            break
        time.sleep(1)

    if proc.poll() is None:
        log("Process still running — sending SIGTERM")
        proc.terminate()
        proc.wait()

    try:
        os.close(master_fd)
    except OSError:
        pass

    # Now that the process has exited, the session file is flushed: verify a new
    # assistant turn was recorded and log its token usage (cache read vs write).
    entries = _assistant_entries(session_id)
    completed = len(entries) > baseline
    if completed:
        usage = entries[-1].get("message", {}).get("usage", {})
        text = ""
        for block in entries[-1].get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
        limited = "hit your" in text.lower()
        log("Turn confirmed{}: cache_read={} cache_write={} in={} out={}".format(
            " [RATE-LIMITED]" if limited else "",
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0)))
    else:
        log("WARNING: no new assistant turn recorded — the ping may not have counted.")

    log(f"Exited with code: {proc.returncode}")
    return completed


# ---------------------------------------------------------------------------
# Init (one-time, called by install.sh)
# ---------------------------------------------------------------------------

def init():
    """
    Create the frozen checkpoint session with a single 'hi' message and back it up.
    Must be run once before the systemd timer starts. Called via --init flag.
    """
    if os.path.exists(SESSION_ID_FILE) and os.path.exists(CHECKPOINT_BACKUP):
        print("Checkpoint already exists.")
        print(f"  Session: {open(SESSION_ID_FILE).read().strip()}")
        print(f"  Backup:  {CHECKPOINT_BACKUP} "
              f"({os.path.getsize(CHECKPOINT_BACKUP)} bytes)")
        print("To reset, delete early_window_session_id.txt and "
              "early_window_checkpoint.jsonl.bak, then re-run ./install.sh.")
        return

    # Clean any partial state
    for f in (SESSION_ID_FILE, CHECKPOINT_BACKUP):
        if os.path.exists(f):
            os.remove(f)

    checkpoint_id = str(uuid.uuid4())
    log(f"Creating checkpoint session {checkpoint_id[:8]}... with 'hi'")

    ok = run_interactive(["--session-id", checkpoint_id], "hi", checkpoint_id)
    if not ok:
        log("ERROR: Failed to create checkpoint session.")
        sys.exit(1)

    with open(SESSION_ID_FILE, "w") as f:
        f.write(checkpoint_id)

    backup_checkpoint(checkpoint_id)
    log(f"Checkpoint ready: {checkpoint_id}")


# ---------------------------------------------------------------------------
# Early-window run (called by the systemd timer on each interval)
# ---------------------------------------------------------------------------

def main():
    rotate_log()
    log("Starting early-window run...")

    if not os.path.exists(SESSION_ID_FILE) or not os.path.exists(CHECKPOINT_BACKUP):
        log("ERROR: No checkpoint found. Run ./install.sh to initialise.")
        sys.exit(1)

    with open(SESSION_ID_FILE) as f:
        checkpoint_id = f.read().strip()

    # Restore the frozen 'hi' state so --resume always sees the same 2-message
    # context, regardless of what the previous run left behind.
    restore_checkpoint(checkpoint_id)

    log(f"Resuming checkpoint {checkpoint_id[:8]}... with 'bye'")
    ok = run_interactive(["--resume", checkpoint_id], "bye", checkpoint_id)
    if not ok:
        log("WARNING: early-window run did not confirm a completed turn.")
    log("Early-window run finished.\n")


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--init" in sys.argv:
        init()
    else:
        main()
