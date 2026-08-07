"""
Claude Code Early Window
Keeps a Claude Code usage window rolling in the background so that you start work
inside a fresh, almost-untouched window. Requires Python 3.6+, Linux, and the
Claude Code CLI.

Usage:
  python3 claude_early_window.py --init    # one-time setup (called by install.sh)
  python3 claude_early_window.py           # early-window run (called by systemd timer)
  python3 claude_early_window.py --status  # show the current window / anchor state
"""

import json
import os
import pty
import re
import shlex
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
STATE_FILE        = os.path.join(SCRIPT_DIR, "early_window_state.json")
STATUSLINE_FILE   = os.path.join(SCRIPT_DIR, "early_window_statusline.jsonl")

LOG_RETENTION_HOURS = 48

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
#
# INTERVAL_MIN is the single source of truth for the ping cadence: install.sh
# reads it from here when it writes the systemd timer. 30 divides the 5-hour
# window evenly, so consecutive windows sit back-to-back, and it stays well under
# the ~1-hour prompt-cache TTL so every ping is a (rate-limit-exempt) cache read.
INTERVAL_MIN = 30

# A window reset reported as 17:00:00 is pinged at 17:00:30. Firing *early* is the
# only real failure mode — the ping would land inside the old window, be wasted,
# and leave us waiting another full interval — so we deliberately aim late. The
# ping itself needs ~8s of CLI startup before it reaches the API, so the request
# actually arrives around +38s. That lateness is harmless; earliness is not.
RESET_GUARD_SEC = 30

WINDOW_HOURS = 5

# systemd units. The anchor is a transient one-shot timer created with systemd-run;
# it exists only between being scheduled and firing.
SERVICE_UNIT = "claude-early-window.service"
ANCHOR_UNIT  = "claude-early-window-anchor"

# Give up on re-anchoring after this many consecutive anchors that still came back
# rate-limited — if the reset time we parsed were wrong, this stops a hot loop.
MAX_ANCHOR_STREAK = 3


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
# State
# ---------------------------------------------------------------------------

def read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def write_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, STATE_FILE)


def fmt_time(epoch):
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def fmt_delta(seconds):
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return "{}{}h{:02d}m{:02d}s".format(sign, seconds // 3600,
                                        (seconds % 3600) // 60, seconds % 60)


# ---------------------------------------------------------------------------
# Learning when the current window resets
# ---------------------------------------------------------------------------
#
# Two sources, in order of preference:
#
#   1. Claude Code's statusLine payload. For Pro/Max accounts it carries
#      rate_limits.five_hour.resets_at as Unix epoch seconds — exact, and present
#      after every *successful* API response. We attach a statusLine to the ping
#      process only, via `--settings` with inline JSON, so nothing about the
#      user's own Claude Code configuration is touched.
#   2. The 429 refusal text ("You've hit your session limit · resets 9:30pm
#      (Asia/Jerusalem)"). Only available when a ping is actually blocked, and
#      only to the minute, but that is exactly the case where source 1 is silent.

def statusline_settings():
    """Inline --settings JSON that points Claude's statusLine back at this script."""
    command = " ".join(shlex.quote(part) for part in (
        sys.executable or "/usr/bin/python3",
        os.path.abspath(__file__),
        "--capture-statusline",
    ))
    return json.dumps({
        "statusLine": {"type": "command", "command": command, "padding": 0}
    })


def capture_statusline():
    """
    Append the statusLine payload to STATUSLINE_FILE as one JSON object per line.

    Claude Code invokes this repeatedly during a session and only the later
    invocations carry rate_limits, so we append rather than overwrite and pick the
    freshest usable record afterwards. Prints nothing: the status line stays blank.
    This must stay silent and side-effect-free — it runs inside the Claude UI loop.
    """
    try:
        raw = sys.stdin.read()
        json.loads(raw)  # validate before storing
        with open(STATUSLINE_FILE, "a") as f:
            f.write(raw.replace("\n", " ") + "\n")
    except Exception:
        pass


def statusline_records():
    """
    Every complete statusLine payload captured so far, oldest first.

    A half-written final line is skipped rather than treated as data — the file is
    read while Claude Code is still appending to it, so "no valid JSON yet" is an
    ordinary state, not an error.
    """
    records = []
    try:
        with open(STATUSLINE_FILE) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except (IOError, OSError):
        pass
    return records


def statusline_api_ms(records=None):
    """
    Highest total_api_duration_ms reported by the statusLine so far.

    Claude Code only writes the session JSONL on exit, so a run cannot watch that
    file to know when the reply landed. The statusLine can: this counter stays at
    its starting value until the API call completes, then jumps. That is a far
    better completion signal than guessing from when the screen stops changing.
    """
    if records is None:
        records = statusline_records()
    return max([(r.get("cost") or {}).get("total_api_duration_ms") or 0
                for r in records] or [0])


def read_statusline_limits():
    """Return the most recent {'five_hour': {...}, 'seven_day': {...}} seen, or {}."""
    latest = {}
    for record in statusline_records():
        limits = record.get("rate_limits")
        if isinstance(limits, dict) and limits:
            latest = limits
    return latest


_LIMIT_NAMES = (("five_hour", "5-hour"), ("seven_day", "weekly"))


def fmt_pct(used):
    return "?%" if used is None else "{}%".format(used)


def format_usage(limits=None):
    """
    One line summarising both limits: how much is used, and when each resets.

    Logged on every ping. It costs nothing extra — the figures already arrive with
    the statusLine report the run needs anyway — and it turns the log into a
    record of usage over time rather than only a record of pings.
    """
    if limits is None:
        limits = read_statusline_limits()
    now = time.time()
    parts = []
    for key, name in _LIMIT_NAMES:
        window = limits.get(key) or {}
        used, resets = window.get("used_percentage"), window.get("resets_at")
        if used is None and resets is None:
            continue
        piece = "{} {}".format(name, fmt_pct(used))
        if resets:
            piece += " (resets {}, in {})".format(fmt_time(resets),
                                                  fmt_delta(resets - now))
        parts.append(piece)
    return "Usage: " + " · ".join(parts) if parts else ""


def _local_tz_name():
    """Best-effort IANA name of the machine's timezone (e.g. 'Asia/Jerusalem')."""
    try:
        link = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in link:
            return link.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    try:
        with open("/etc/timezone") as f:
            return f.read().strip()
    except (IOError, OSError):
        return ""


_MONTHS = {name: n + 1 for n, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"])}

# Matches both shapes Claude Code produces, the second with a date because a
# weekly reset can be days away:
#   "... resets 9:30pm (Asia/Jerusalem)"
#   "... resets Aug 10, 10pm (Asia/Jerusalem)"
_RESET_TEXT_RE = re.compile(
    r"resets\s+(?:([A-Za-z]{3,9})\s+(\d{1,2}),?\s+)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\s*\(([^)]+)\)", re.I)


def parse_reset_from_text(text, now=None):
    """
    Pull the reset time, and which limit it belongs to, out of a refusal message.

        "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)"
        "You've hit your weekly limit · resets Aug 10, 10pm (Asia/Jerusalem)"

    Returns (epoch_seconds, "session"|"weekly"), or None when the message names no
    limit we recognise, has no parseable time, or reports a timezone other than
    this machine's — Python 3.6 has no zoneinfo, so rather than guess at an offset
    we decline. The statusLine source is unaffected either way.
    """
    if not text:
        return None

    lowered = text.lower()
    if "weekly limit" in lowered:
        kind = "weekly"
    elif "session limit" in lowered:
        kind = "session"
    else:
        return None

    match = _RESET_TEXT_RE.search(text)
    if not match:
        return None

    month_name, day, hour, minute, meridiem, tz = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if meridiem.lower() == "pm" and hour != 12:
        hour += 12
    elif meridiem.lower() == "am" and hour == 12:
        hour = 0

    local_tz = _local_tz_name()
    if local_tz and tz.strip() and tz.strip() != local_tz:
        return None

    now_dt = datetime.fromtimestamp(now if now is not None else time.time())
    try:
        if month_name:
            month = _MONTHS.get(month_name.lower()[:3])
            if not month:
                return None
            target = now_dt.replace(month=month, day=int(day), hour=hour,
                                    minute=minute, second=0, microsecond=0)
            if target < now_dt:                       # the date is next year
                target = target.replace(year=now_dt.year + 1)
        else:
            target = now_dt.replace(hour=hour, minute=minute,
                                    second=0, microsecond=0)
            if target <= now_dt:                      # the reset is tomorrow
                target += timedelta(days=1)
    except ValueError:                                # e.g. Feb 30, or Feb 29 rolled
        return None
    return target.timestamp(), kind


# A ping has to satisfy *both* limits, so both decide when the next one can land.
FIVE_HOUR_HORIZON = WINDOW_HOURS * 3600 + 600
WEEKLY_HORIZON    = 7 * 24 * 3600 + 3600


def _weekly_is_blocking(seven_day, refusal_text, was_limited):
    """
    Is the weekly limit what is actually stopping pings right now?

    It matters only when it is exhausted. The rest of the time its reset is days
    away and has nothing to do with when the next 5-hour window can start.
    """
    if (seven_day.get("used_percentage") or 0) >= 100:
        return True
    return bool(was_limited and "weekly" in (refusal_text or "").lower())


def next_window_start(limits, refusal_text, was_limited):
    """
    The earliest moment a ping can both get through *and* start a new window.

    Normally that is simply when the 5-hour window resets. But a ping also has to
    get past the weekly limit, so when that one is exhausted the answer is
    whichever of the two resets **last** — aiming at the 5-hour boundary would be
    pointless if the weekly limit is still going to refuse the ping when it lands,
    and aiming at the weekly reset would be premature if the 5-hour window has not
    finished yet. Taking the later of the two is right in both directions.

    Returns (epoch, horizon, label); horizon is how far ahead this reading is
    allowed to be before we treat it as nonsense. (None, None, "") if unknown.
    """
    five  = limits.get("five_hour") or {}
    seven = limits.get("seven_day") or {}
    candidates = []

    if five.get("resets_at"):
        candidates.append((five["resets_at"], FIVE_HOUR_HORIZON, "5-hour window"))
    if seven.get("resets_at") and _weekly_is_blocking(seven, refusal_text, was_limited):
        candidates.append((seven["resets_at"], WEEKLY_HORIZON, "weekly limit"))

    # A refused ping may produce no statusLine figures at all, and its text names
    # the limit it is talking about — so always take a look at it too.
    if was_limited:
        parsed = parse_reset_from_text(refusal_text)
        if parsed:
            epoch, kind = parsed
            if kind == "weekly":
                candidates.append((epoch, WEEKLY_HORIZON, "weekly limit"))
            else:
                candidates.append((epoch, FIVE_HOUR_HORIZON, "5-hour window"))

    if not candidates:
        return None, None, ""
    return max(candidates, key=lambda c: c[0])


# ---------------------------------------------------------------------------
# The anchor: a one-shot run placed exactly on the next window boundary
# ---------------------------------------------------------------------------
#
# Normally the timer's own 30-minute cadence lands on the boundary by itself,
# because 30 minutes divides 5 hours evenly. It stops doing so whenever a ping is
# missed — an outage, a suspended machine, or the user's own work exhausting the
# window so that pings are refused. Then the first ping of the next window is up
# to a full interval late, and *every* window after it inherits that late phase.
#
# The anchor fixes the phase in one shot. It starts the *same* service unit, so
# systemd's OnUnitActiveSec=30min re-anchors the regular series off it too: an
# anchor at 17:00:30 slides the whole series to 17:30:30, 18:00:30 … 22:00:30 —
# back exactly on the next boundary. After one correction the series is in phase
# again and the anchor goes quiet until something else knocks it out.

class _NoSystemd(object):
    """Stand-in result for when the systemd binaries are not installed."""
    returncode = 1
    stdout = "systemd not available"


def _run(cmd):
    """Run a systemd command, tolerating its absence rather than crashing the ping."""
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              universal_newlines=True)
    except (OSError, ValueError):
        return _NoSystemd()


def _systemctl(*args):
    return _run(["systemctl", "--user"] + list(args))


def cancel_anchor():
    """Clear any pending anchor so a new one can take its place."""
    for unit in (ANCHOR_UNIT + ".timer", ANCHOR_UNIT + ".service"):
        _systemctl("stop", unit)
    _systemctl("reset-failed", ANCHOR_UNIT + ".timer", ANCHOR_UNIT + ".service")


def anchor_pending():
    """Return the pending anchor's scheduled time as a string, or '' if none."""
    result = _systemctl("show", ANCHOR_UNIT + ".timer",
                        "--property=NextElapseUSecRealtime", "--value")
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    return value if value and value not in ("n/a", "0") else ""


def schedule_anchor(target_epoch):
    """
    Create a transient one-shot timer that starts the ping service at target_epoch.

    OnCalendar (wall clock) rather than OnActiveSec (monotonic) is deliberate:
    monotonic timers do not advance while the machine is suspended, which is one
    of the very situations this is meant to recover from.

    The anchor starts the ping service with --no-block so it finishes immediately
    instead of waiting for the ping to return. Otherwise the anchor unit would
    still be active *during* the run it triggered, and that run deciding it needs
    another anchor would end up trying to cancel its own parent.
    """
    cancel_anchor()
    stamp = datetime.fromtimestamp(target_epoch).strftime("%Y-%m-%d %H:%M:%S")
    result = _run(
        ["systemd-run", "--user", "--collect",
         "--unit", ANCHOR_UNIT,
         "--description", "Claude Early Window — one-shot window-boundary anchor",
         "--on-calendar", stamp,
         "--timer-property=AccuracySec=1s",
         "systemctl", "--user", "start", "--no-block", SERVICE_UNIT])
    if result.returncode != 0:
        log("WARNING: could not schedule anchor: {}".format(
            (result.stdout or "").strip()))
        return False
    log("Anchor scheduled for {} (in {})".format(
        stamp, fmt_delta(target_epoch - time.time())))
    return True


def maybe_schedule_anchor(boundary, horizon, label, state, was_limited):
    """
    Decide whether the next window boundary needs a one-shot anchor.

    Only the last regular run before the boundary schedules one. Waiting until
    then is deliberate: that run has the freshest reading of the reset time, the
    shortest gap in which anything can change, and the least clock drift. Runs
    further out do nothing, because a later run will always get another look.

    That also means a boundary days away — a weekly limit — costs nothing to
    track: the ordinary pings carry on every 30 minutes regardless, so if the
    limit lifts early (an upgrade, say) the very next ping picks it straight up.
    """
    now = time.time()
    if not boundary:
        return

    # Sanity-bound the reading before acting on it: a reset in the past is stale,
    # and one further out than the limit it came from could ever run is nonsense.
    # This is the guard against clock skew or an unexpected payload.
    if not (now < boundary <= now + horizon):
        log("Reset time {} is out of range for the {} — ignoring.".format(
            fmt_time(boundary), label))
        return

    streak = state.get("anchor_streak", 0)
    if was_limited and streak >= MAX_ANCHOR_STREAK:
        log("Anchored {} times in a row and still rate-limited — no more anchors "
            "until a ping succeeds (ordinary pings carry on).".format(streak))
        return

    target = boundary + RESET_GUARD_SEC
    if target - now > INTERVAL_MIN * 60:
        log("Next ping can start a window at {} (in {}, set by the {}) — "
            "more than one interval away, no anchor needed yet.".format(
                fmt_time(boundary), fmt_delta(boundary - now), label))
        return

    log("Next ping can start a window at {} (in {}, set by the {}) — "
        "within one interval.".format(
            fmt_time(boundary), fmt_delta(boundary - now), label))
    if schedule_anchor(target):
        state["anchor_target"] = target
        state["anchor_label"] = label
        state["anchor_streak"] = streak + 1 if was_limited else 1


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


def _discard_session(session_id):
    """Remove a session file from a failed setup attempt so a retry starts clean."""
    try:
        os.remove(_session_file(session_id))
    except OSError:
        pass


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
                    startup_wait=8, completion_timeout=60, statusline_wait=10):
    """
    Spawn an interactive Claude session in a PTY, send prompt_text, wait for the
    reply to settle, then /exit and verify the turn was recorded.

      * --tools ""                       strips built-in tool definitions
      * --strict-mcp-config --mcp-config minimises the system prompt to no MCP servers
      * --model haiku --effort low       cheapest possible turn
      * --settings                       attaches our statusLine to *this process
                                         only*, so the run can report when the
                                         usage window resets

    Completion is detected adaptively from PTY output (output appears, then goes
    quiet) rather than by a fixed sleep. Claude Code only flushes the session JSONL
    on exit, so success is confirmed *after* /exit by checking that a new assistant
    turn was persisted; its token usage is logged (cache read, which is
    rate-limit-exempt, vs cache write) so each run's real cost is visible.

    Returns {"completed": bool, "limited": bool, "text": str}.
    """
    if not os.path.isfile(CLAUDE_PATH):
        log(f"ERROR: Claude CLI not found at {CLAUDE_PATH}. "
            "Install Claude Code from https://claude.ai/download")
        return {"completed": False, "limited": False, "text": ""}

    # Start each run with a clean statusLine capture so stale readings from the
    # previous run can never be mistaken for this one's.
    try:
        os.remove(STATUSLINE_FILE)
    except OSError:
        pass

    master_fd, slave_fd = pty.openpty()
    cmd = [CLAUDE_PATH,
           "--tools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--settings", statusline_settings(),
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
        return {"completed": False, "limited": False, "text": ""}

    os.close(slave_fd)
    os.set_blocking(master_fd, False)

    time.sleep(startup_wait)
    try:
        os.read(master_fd, 8192)  # drain startup output
    except BlockingIOError:
        pass

    # Wait for one complete statusLine report before taking the baseline. On a
    # --resume the very first report already carries the API duration inherited
    # from the restored session, so a baseline taken before it arrives would be 0
    # and that inherited figure would instantly look like our own reply landing.
    # Waiting for a parsed record — not merely for the file to exist, which happens
    # a moment earlier — closes that race. If no report ever comes (no
    # subscription, so no statusLine data) the baseline stays 0, the counter stays
    # 0, and completion falls through to the PTY heuristic below.
    statusline_deadline = time.time() + statusline_wait
    while time.time() < statusline_deadline and not statusline_records():
        time.sleep(0.25)
        try:
            os.read(master_fd, 65536)
        except BlockingIOError:
            pass

    # Claude Code only persists the session JSONL on a clean exit, not mid-run, so
    # completion cannot be detected by watching that file. Two signals are used
    # instead, in order of reliability:
    #
    #   1. The statusLine's API duration counter, which jumps once the reply has
    #      actually landed. This is a fact reported by Claude Code itself.
    #   2. If that never moves — no subscription, so no statusLine payload — fall
    #      back to watching the PTY go quiet for `idle_threshold` seconds, with
    #      `min_settle` enforced first because the pause before the model starts
    #      replying (especially on a cache miss) can itself be several seconds.
    #
    # Draining the PTY throughout also keeps the child from blocking on a full buffer.
    baseline = len(_assistant_entries(session_id))
    api_baseline = statusline_api_ms()
    log(f"Sending: '{prompt_text}'")
    os.write(master_fd, (prompt_text + "\r").encode())

    min_settle = 10.0
    idle_threshold = 5.0
    settle_after_reply = 2.0
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

        if statusline_api_ms() > api_baseline:
            # Reply confirmed. Give the UI a moment to finish rendering before
            # /exit, so the transcript is written with the turn complete.
            drain_until = time.time() + settle_after_reply
            while time.time() < drain_until:
                time.sleep(0.25)
                try:
                    os.read(master_fd, 65536)
                except BlockingIOError:
                    pass
            break

        if (not chunk and saw_output
                and (time.time() - start) >= min_settle
                and (time.time() - last_activity) >= idle_threshold):
            break  # output settled — assume the turn finished

    os.write(master_fd, b"/exit\r")

    # Keep draining while it shuts down: Claude Code emits a burst of terminal
    # escape sequences on exit, and a full PTY buffer would block it from exiting
    # cleanly — which is exactly when the session file would not get written.
    for _ in range(16):
        if proc.poll() is not None:
            break
        time.sleep(0.5)
        try:
            os.read(master_fd, 65536)
        except BlockingIOError:
            pass

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
    text, limited = "", False
    if completed:
        record = entries[-1]
        usage = record.get("message", {}).get("usage", {})
        for block in record.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
        # A refused ping is recorded as an ordinary assistant turn whose text is the
        # refusal. Key off the structured fields Claude Code stores alongside it
        # rather than the wording, which is English-only and free to change.
        limited = (record.get("apiErrorStatus") == 429
                   or record.get("error") == "rate_limit"
                   or "hit your" in text.lower())
        log("Turn confirmed{}: cache_read={} cache_write={} in={} out={}".format(
            " [RATE-LIMITED]" if limited else "",
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0)))
        if limited:
            log("Refusal: {}".format(text.strip()))
    else:
        log("WARNING: no new assistant turn recorded — the ping may not have counted.")

    usage = format_usage()
    if usage:
        log(usage)

    log(f"Exited with code: {proc.returncode}")
    return {"completed": completed, "limited": limited, "text": text}


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

    result = run_interactive(["--session-id", checkpoint_id], "hi", checkpoint_id)

    # The checkpoint is frozen once and replayed by every future ping, so it must
    # not be built out of a refusal. That would bake the refusal into the prompt
    # for good, and install.sh would report success over a degraded setup.
    if result["limited"]:
        log("ERROR: Claude refused the first message, so there is no reply to "
            "build the checkpoint from:")
        log("  " + result["text"].strip())
        log("Wait for the limit to reset, then run ./install.sh again.")
        _discard_session(checkpoint_id)
        sys.exit(1)

    if not result["completed"]:
        log("ERROR: Failed to create checkpoint session.")
        _discard_session(checkpoint_id)
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
    result = run_interactive(["--resume", checkpoint_id], "bye", checkpoint_id)
    if not result["completed"]:
        log("WARNING: early-window run did not confirm a completed turn.")

    state = read_state()
    state["last_run"] = time.time()

    # Work out the earliest moment the next ping could start a window — which
    # depends on both the 5-hour and the weekly limit — then decide whether that
    # moment needs a one-shot anchor. A successful ping reports both limits
    # exactly via the statusLine; a refused one says so in the refusal text.
    limits = read_statusline_limits()
    if limits:
        state["rate_limits"] = limits
        state["limits_source"] = "statusline"

    boundary, horizon, label = next_window_start(
        limits, result["text"], result["limited"])
    if boundary:
        state["boundary"] = boundary
        state["boundary_label"] = label
        if not limits:
            state["limits_source"] = "refusal-text"
    else:
        log("No reset time reported this run — leaving the schedule as it is.")

    if result["completed"] and not result["limited"]:
        state["anchor_streak"] = 0        # back to normal; forget past corrections

    maybe_schedule_anchor(boundary, horizon, label, state, result["limited"])
    write_state(state)

    log("Early-window run finished.\n")


# ---------------------------------------------------------------------------
# Status (for humans)
# ---------------------------------------------------------------------------

def status():
    now = time.time()
    print("Claude Code Early Window — status")
    print("=" * 34)

    installed = os.path.exists(SESSION_ID_FILE) and os.path.exists(CHECKPOINT_BACKUP)
    print("Checkpoint    : {}".format(
        open(SESSION_ID_FILE).read().strip() if installed else "MISSING — run ./install.sh"))

    state = read_state()
    if state.get("last_run"):
        print("Last ping     : {} ({} ago)".format(
            fmt_time(state["last_run"]), fmt_delta(now - state["last_run"])))

    limits = state.get("rate_limits", {})
    for key, name in _LIMIT_NAMES:
        window = limits.get(key) or {}
        if not window.get("resets_at"):
            continue
        print("{:<14}: {} used, resets {} (in {})".format(
            "5-hour window" if key == "five_hour" else "Weekly limit",
            fmt_pct(window.get("used_percentage")),
            fmt_time(window["resets_at"]),
            fmt_delta(window["resets_at"] - now)))

    boundary = state.get("boundary")
    if boundary:
        stale = "" if boundary > now else " — passed, awaiting next ping"
        print("Next start-of-window opportunity: {} (in {}){}".format(
            fmt_time(boundary), fmt_delta(boundary - now), stale))
        print("  set by the {}   [via {}]".format(
            state.get("boundary_label", "?"), state.get("limits_source", "?")))
    else:
        print("Next start-of-window opportunity: not known yet — run one ping first")

    pending = anchor_pending()
    print("Anchor        : {}".format(
        "pending — " + pending if pending else "none scheduled"))

    result = _systemctl("list-timers", "--all", "claude-early-window.timer")
    for line in (result.stdout or "").splitlines():
        if "claude-early-window.timer" in line:
            print("Next ping     : {}".format(" ".join(line.split()[:4])))


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = set(sys.argv[1:])
    # Must be first: Claude Code invokes this inside its UI loop and expects a
    # status line on stdout, nothing else.
    if "--capture-statusline" in args:
        capture_statusline()
    elif "--init" in args:
        init()
    elif "--status" in args:
        status()
    elif not args:
        main()
    else:
        # Never fall through to a real ping on a typo or on --help: sending one
        # is a side effect the user did not ask for.
        print(__doc__.strip())
        sys.exit(0 if args <= {"--help", "-h"} else 2)
