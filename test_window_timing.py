"""
Tests for claude_window_timing.

By default nothing here contacts Claude, spawns a session, or spends any of your
usage window. The pieces that decide *when* to ping, *which account* to use, and
*where conversations live* are the ones worth testing, because getting them wrong
is silent — the tool keeps running and just drifts, picks the wrong account, or
strands a conversation nobody can resume.

    python3 test_window_timing.py

Nothing here needs a Claude account, a network, or systemd. Where a whole
install has to be exercised, fake_claude.py stands in for the CLI and every
systemd call is recorded rather than run — except the anchor test, which does
create real transient units, under names no configured account can collide with
and cleaned up in a finally.
"""

import ast
import io
import json
import os
import pty
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_window_timing as ew


FAILURES = []


def stamp_checkpoint(account):
    """Mark a fabricated checkpoint as built for this account's ping directory."""
    account.ensure_state_dir()
    with open(account.checkpoint_cwd_file, "w") as f:
        f.write(account.ping_cwd)


def check(name, got, want):
    if got == want:
        print("  PASS  {}".format(name))
    else:
        print("  FAIL  {}\n          got  {!r}\n          want {!r}".format(name, got, want))
        FAILURES.append(name)


def check_true(name, cond):
    check(name, bool(cond), True)


def section(title):
    print("\n{}".format(title))


def _literal_value(node):
    """
    The value of a literal AST node, on every interpreter this tool supports.

    `ast.Str` and its `.s` were how this was spelled until 3.8; they were
    deprecated in 3.12 and removed in 3.14, so neither spelling alone reads
    every version — and the 3.14 failure is the quiet kind, a `getattr` default
    that turns a check of the source into a check of nothing.
    """
    if isinstance(node, ast.Constant):                   # 3.8 and later
        return node.value
    if node.__class__.__name__ in ("Str", "Bytes", "Num"):    # 3.6, 3.7
        return getattr(node, "s", None)
    return None


def _string_literals(source):
    """Every string literal in `source`."""
    for node in ast.walk(ast.parse(source)):
        value = _literal_value(node)
        if isinstance(value, str):
            yield value


# Anchors are real transient systemd units, named after the account. A test that
# borrows a plausible account name therefore schedules a real ping against the
# user's real deployment — and one that forgets to cancel leaves it armed. Every
# account these tests build is named from this prefix, which no configured
# account can collide with, and anything that touches systemd cleans up after
# itself in a finally.
TEST_PREFIX = "selftest"


def temp_account(name=TEST_PREFIX, index=0, config_dir=None):
    """
    An Account whose files all live in a fresh temporary directory.

    Redirecting STATE_ROOT rather than reaching into the account afterwards keeps
    the object exactly as production builds it, so the path derivation is part of
    what these tests exercise.
    """
    if not name.startswith(TEST_PREFIX):
        raise AssertionError(
            "test accounts must be named from TEST_PREFIX: {!r} could collide "
            "with a real account's systemd units".format(name))
    ew.STATE_ROOT = tempfile.mkdtemp()
    account = ew.Account(name, config_dir or os.path.join(ew.STATE_ROOT, "cfg"),
                         index)
    account.ensure_state_dir()
    return account


# ---------------------------------------------------------------------------
# Reading the reset time out of a refusal message
# ---------------------------------------------------------------------------

ZONE = "Asia/Jerusalem"


def _pretend_timezone(name=ZONE):
    """
    Answer `_local_tz_name` with a fixed zone. Returns a restore function.

    A refusal names the timezone Claude reported it in, and the parser declines
    one that is not this machine's rather than guess at an offset — right, and
    the reason these tests passed only where they were written: a clone running
    in UTC failed nine checks that have nothing to do with time zones.

    The expected epochs are built with a local `datetime`, and so is the
    parser's answer, so pinning the *reported* zone to whatever this machine
    calls local keeps the arithmetic identical wherever it runs.
    """
    original = ew._local_tz_name
    ew._local_tz_name = lambda: name

    def restore():
        ew._local_tz_name = original

    return restore


def test_refusal_text():
    section("Refusal text -> (reset time, which limit)")
    now = datetime(2026, 8, 6, 16, 40, 0).timestamp()
    T = lambda *a: datetime(*a).timestamp()
    restore = _pretend_timezone()

    check("5-hour refusal, time only",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)", now),
          (T(2026, 8, 6, 21, 30), "session"))
    check("5-hour refusal, whole hour",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 3pm (Asia/Jerusalem)", now),
          (T(2026, 8, 7, 15, 0), "session"))
    check("midnight is 12am, not noon",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 12am (Asia/Jerusalem)", now),
          (T(2026, 8, 7, 0, 0), "session"))
    check("noon is 12pm",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 12pm (Asia/Jerusalem)", now),
          (T(2026, 8, 7, 12, 0), "session"))
    check("weekly refusal carries a date",
          ew.parse_reset_from_text(
              "You've hit your weekly limit · resets Aug 10, 10pm (Asia/Jerusalem)", now),
          (T(2026, 8, 10, 22, 0), "weekly"))
    check("a weekly date already past rolls into next year",
          ew.parse_reset_from_text(
              "You've hit your weekly limit · resets Jan 3, 9am (Asia/Jerusalem)", now),
          (T(2027, 1, 3, 9, 0), "weekly"))
    check("an unrecognised limit is not acted on",
          ew.parse_reset_from_text(
              "You've hit some other limit · resets 9pm (Asia/Jerusalem)", now), None)
    check("a foreign timezone is declined rather than guessed",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 9pm (America/New_York)", now), None)
    check("ordinary reply text yields nothing",
          ew.parse_reset_from_text("Bye! Have a good one.", now), None)
    check("empty text yields nothing", ew.parse_reset_from_text("", now), None)

    # A machine whose own zone cannot be read: the comparison cannot be made,
    # so it is not made. Declining every refusal there would throw away the
    # only reading a rate-limited account ever produces.
    ew._local_tz_name = lambda: ""
    check("a machine with no zone name of its own does not decline",
          ew.parse_reset_from_text(
              "You've hit your session limit · resets 9pm (America/New_York)",
              now),
          (T(2026, 8, 6, 21, 0), "session"))
    restore()


# ---------------------------------------------------------------------------
# Which moment to aim at, given two limits
# ---------------------------------------------------------------------------

def test_next_window_start():
    section("Both limits decide the target -> aim at whichever frees up last")
    now = time.time()
    FIVE = now + 4 * 3600
    WEEK_LATE = now + 3 * 86400
    WEEK_EARLY = now + 1800

    def limits(week_pct, week_reset=WEEK_LATE, five_reset=FIVE):
        return {"five_hour": {"used_percentage": 74, "resets_at": five_reset},
                "seven_day": {"used_percentage": week_pct, "resets_at": week_reset}}

    got = ew.next_window_start(limits(85), "", False)
    check("a weekly limit that is not full is ignored entirely", (got[0], got[2]),
          (FIVE, "5-hour window"))

    got = ew.next_window_start(limits(100), "", True)
    check("weekly full and resetting later -> aim at the weekly reset",
          (got[0], got[2]), (WEEK_LATE, "weekly limit"))

    got = ew.next_window_start(limits(100, week_reset=WEEK_EARLY), "", True)
    check("weekly full but resetting first -> aim at the window boundary",
          (got[0], got[2]), (FIVE, "5-hour window"))

    restore = _pretend_timezone()
    weekly_text = "You've hit your weekly limit · resets Aug 10, 10pm ({})".format(ZONE)
    session_text = "You've hit your session limit · resets 9:30pm ({})".format(ZONE)

    got = ew.next_window_start({}, weekly_text, True)
    check("no statusLine at all: weekly refusal text is used, with a 7-day horizon",
          (got[1], got[2]), (ew.WEEKLY_HORIZON, "weekly limit"))
    got = ew.next_window_start({}, session_text, True)
    check("no statusLine at all: 5-hour refusal text is used, with a 5-hour horizon",
          (got[1], got[2]), (ew.FIVE_HOUR_HORIZON, "5-hour window"))

    partial = {"five_hour": {"used_percentage": 100, "resets_at": FIVE}}
    got = ew.next_window_start(partial, weekly_text, True)
    check("statusLine knows only the 5-hour limit; the text reveals the weekly one",
          got[2], "weekly limit")

    check("nothing known -> no target", ew.next_window_start({}, "", False),
          (None, None, ""))
    check("a successful ping never consults the refusal text",
          ew.next_window_start({}, weekly_text, False), (None, None, ""))
    restore()


# ---------------------------------------------------------------------------
# Which readings are safe to act on
# ---------------------------------------------------------------------------

def test_guard_rails():
    section("Guard rails on acting")
    now = time.time()
    account = temp_account()
    ew.cancel_anchor(account)

    def attempt(boundary, horizon=ew.FIVE_HOUR_HORIZON, label="5-hour window",
                limited=False, state=None):
        state = {} if state is None else state
        ew.maybe_schedule_anchor(account, boundary, horizon, label, state, limited)
        return state

    check_true("a reset in the past is ignored",
               "anchor_target" not in attempt(now - 60))
    check_true("a 5-hour reset further out than 5 hours is ignored",
               "anchor_target" not in attempt(now + 6 * 3600))
    check_true("a target more than one interval away schedules nothing",
               "anchor_target" not in attempt(now + 3600))
    check_true("still refused after %d anchors -> stand down" % ew.MAX_ANCHOR_STREAK,
               "anchor_target" not in attempt(
                   now + 300, limited=True, state={"anchor_streak": ew.MAX_ANCHOR_STREAK}))
    check_true("a weekly target days out is legitimate under its own horizon",
               "out of range" not in _log_of(
                   account,
                   lambda: attempt(now + 3 * 86400, ew.WEEKLY_HORIZON, "weekly limit")))
    check_true("the same target judged as a 5-hour one is rejected as nonsense",
               "out of range" in _log_of(
                   account,
                   lambda: attempt(now + 3 * 86400, ew.FIVE_HOUR_HORIZON, "5-hour window")))
    ew.cancel_anchor(account)


def _log_of(account, fn):
    """Capture what a call writes to the log, so log-only decisions are testable."""
    path = account.log_file
    before = os.path.getsize(path) if os.path.exists(path) else 0
    fn()
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        f.seek(before)
        return f.read()


# ---------------------------------------------------------------------------
# Scheduling, replacing and cancelling the anchor
# ---------------------------------------------------------------------------

def test_anchor_scheduling():
    section("Scheduling the anchor (creates real transient systemd units)")
    if not _have_systemd():
        print("  SKIP  no systemd --user session available")
        return
    now = time.time()
    account = temp_account()
    ew.cancel_anchor(account)

    state = {}
    ew.maybe_schedule_anchor(account, now + 1500, ew.FIVE_HOUR_HORIZON,
                             "5-hour window", state, False)
    first = ew.anchor_pending(account)
    check_true("a target within one interval is scheduled", bool(first))
    if "anchor_target" not in state:
        # Nothing below can mean anything without one, and reporting the rest
        # as failures would bury the one fact that matters.
        print("  SKIP  transient units cannot be created here")
        return
    check_true("the anchor is placed %ds after the boundary, never before"
               % account.guard_sec,
               abs(state["anchor_target"] - (now + 1500 + account.guard_sec)) < 2)

    ew.maybe_schedule_anchor(account, now + 1200, ew.FIVE_HOUR_HORIZON,
                             "5-hour window", {}, False)
    check_true("re-scheduling replaces the pending anchor",
               ew.anchor_pending(account) != first)
    check("exactly one anchor timer exists, never a stack",
          _anchor_timer_count(account), 1)

    ew.cancel_anchor(account)
    check("cancelling leaves nothing pending", ew.anchor_pending(account), "")
    check("cancelling removes the unit", _anchor_timer_count(account), 0)


def test_the_anchor_is_booked_in_the_zone_systemd_reads():
    """
    The anchor is a wall-clock instruction handed to systemd, which reads it in
    the system's timezone -- not in the TZ of whoever wrote it. Built from
    `datetime.fromtimestamp`, a process run with TZ set elsewhere books the
    anchor hours away from the boundary it was aiming at, and an OnCalendar
    time that has already passed never fires at all.

    Checked against the machine's own answer rather than a fixed zone, so this
    says the same thing wherever it runs.
    """
    section("The anchor is booked in the zone systemd reads")
    epoch = time.time() + 1800
    saved = os.environ.get("TZ")
    try:
        os.environ.pop("TZ", None)
        time.tzset()
        expected = datetime.fromtimestamp(epoch)
        moved = 0
        for zone in ("UTC", "America/New_York", "Asia/Tokyo"):
            os.environ["TZ"] = zone
            time.tzset()
            check("TZ={} does not move the stamp".format(zone),
                  ew._system_local(epoch), expected)
            if datetime.fromtimestamp(epoch) != expected:
                moved += 1
        # Whatever this machine's own zone is, it cannot be all three of
        # those, so an ordinary clock read did follow TZ even where the stamp
        # did not. Counted rather than compared by name: "Etc/UTC" and "UTC"
        # are the same zone under two spellings.
        check_true("while an ordinary clock read did follow TZ", moved >= 1)
        check_true("TZ is left exactly as it was found",
                   os.environ.get("TZ") == "Asia/Tokyo")
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def _have_systemd():
    """
    Whether a transient unit can actually be created from here.

    Three separate things have to hold, and each of them fails somewhere real:

      * `systemctl` exists at all;
      * the per-user *bus* is reachable — the binary is installed in plenty of
        places where it is not, such as a container, a cron job, or an ssh
        session with no login session behind it, and `systemctl --version`
        answers in all of them without contacting anything, so asking the
        manager for a property is what tells the two apart;
      * `systemd-run` exists, which install.sh already warns about on its own
        because the tool degrades to a fixed cadence without it.

    Getting this wrong means the suite cannot be run by somebody who has just
    cloned the repository, which is the one instruction the README gives.
    """
    return (ew._systemctl("show", "--property=Version", "--value").returncode == 0
            and ew._run(["systemd-run", "--version"]).returncode == 0)


def _anchor_timer_count(account):
    out = ew._systemctl("list-timers", "--all", "--no-pager").stdout or ""
    return sum(1 for line in out.splitlines()
               if account.anchor_unit + ".timer" in line)


# ---------------------------------------------------------------------------
# Reading the statusLine capture
# ---------------------------------------------------------------------------

def test_statusline_parsing():
    section("statusLine capture")
    account = temp_account()
    tmp = open(account.statusline_file, "w")
    try:
        tmp.write(json.dumps({"cost": {"total_api_duration_ms": 6962}}) + "\n")
        tmp.write(json.dumps({"cost": {"total_api_duration_ms": 8212},
                              "rate_limits": {"five_hour": {"resets_at": 111,
                                                            "used_percentage": 50}}}) + "\n")
        tmp.write('{"cost": {"total_api_dur')   # a half-written final line
        tmp.flush()

        check("a half-written line is skipped, not misread",
              len(ew.statusline_records(account)), 2)
        check("api duration takes the highest reported",
              ew.statusline_api_ms(account), 8212)
        check("limits take the most recent report carrying them",
              ew.read_statusline_limits(account),
              {"five_hour": {"resets_at": 111, "used_percentage": 50}})

        os.remove(account.statusline_file)
        check("a missing capture file reads as no records",
              ew.statusline_records(account), [])
        check("api duration with no records is 0", ew.statusline_api_ms(account), 0)
        check("limits with no records is empty", ew.read_statusline_limits(account), {})
    finally:
        tmp.close()


def test_resume_baseline_race():
    """
    The bug this guards against: on a --resume, the first statusLine report already
    carries the API duration inherited from the restored session. A baseline taken
    before that report arrives would be 0, the inherited figure would immediately
    look like our own reply landing, and the run would exit before Claude answered.
    """
    section("The resume baseline must not be taken before the first report")
    account = temp_account()
    check("before any report there is nothing to take a baseline from",
          ew.statusline_records(account), [])

    # File exists but is still empty — the moment the old check was fooled by.
    open(account.statusline_file, "w").close()
    check_true("an empty capture file does not count as a report",
               not ew.statusline_records(account))

    with open(account.statusline_file, "a") as f:
        f.write(json.dumps({"cost": {"total_api_duration_ms": 6962}}) + "\n")
    baseline = ew.statusline_api_ms(account)
    check("the baseline is the inherited figure, not 0", baseline, 6962)
    check_true("the inherited figure alone does not look like a fresh reply",
               not ew.statusline_api_ms(account) > baseline)

    with open(account.statusline_file, "a") as f:
        f.write(json.dumps({"cost": {"total_api_duration_ms": 8212}}) + "\n")
    check_true("only a genuine increase counts as the reply landing",
               ew.statusline_api_ms(account) > baseline)


# ---------------------------------------------------------------------------
# Degrading without systemd
# ---------------------------------------------------------------------------

def test_without_systemd():
    section("Without systemd the ping must still work, just without anchoring")
    real_run = subprocess.run

    def missing(cmd, **kwargs):
        if cmd and cmd[0] in ("systemctl", "systemd-run"):
            raise FileNotFoundError(2, "No such file or directory", cmd[0])
        return real_run(cmd, **kwargs)

    account = temp_account()
    subprocess.run = missing
    try:
        state = {}
        ew.maybe_schedule_anchor(account, time.time() + 600, ew.FIVE_HOUR_HORIZON,
                                 "5-hour window", state, False)
        check_true("scheduling an anchor does not raise", True)
        check_true("no anchor is recorded that was never created",
                   "anchor_target" not in state)
        check("no anchor is reported as pending", ew.anchor_pending(account), "")
    except Exception as exc:                                  # noqa: BLE001
        check("scheduling an anchor does not raise",
              "{}: {}".format(type(exc).__name__, exc), "no exception")
    finally:
        subprocess.run = real_run


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def test_formatting():
    section("Formatting")
    check("a positive delta", ew.fmt_delta(3661), "1h01m01s")
    check("a negative delta keeps its sign", ew.fmt_delta(-90), "-0h01m30s")
    check("zero", ew.fmt_delta(0), "0h00m00s")

    check("ordinary usage", ew.fmt_pct(74), "74%")
    check("usage near the top is still plain", ew.fmt_pct(98), "98%")
    check("unknown usage", ew.fmt_pct(None), "?%")


def test_usage_line():
    section("The usage line written to the log")
    now = time.time()
    five, week = now + 3600, now + 3 * 86400

    account = temp_account()
    line = ew.format_usage(
        account, {"five_hour": {"used_percentage": 95, "resets_at": five},
                  "seven_day": {"used_percentage": 87, "resets_at": week}})
    check_true("both limits appear", "5-hour" in line and "weekly" in line)
    check_true("each limit's percentage is shown", "95%" in line and "87%" in line)
    check_true("each reset time is shown", line.count("resets") == 2)
    check_true("the line is a single line", "\n" not in line)

    check("only one limit reported -> only that one is shown",
          "weekly" in ew.format_usage(
              account, {"seven_day": {"used_percentage": 20, "resets_at": week}}), True)
    check("nothing reported -> nothing logged", ew.format_usage(account, {}), "")
    check("a limit with no figures at all is skipped",
          ew.format_usage(account, {"five_hour": {}}), "")


# ---------------------------------------------------------------------------
# Setup must not freeze a refusal into the checkpoint
# ---------------------------------------------------------------------------

def test_init_refuses_to_checkpoint_a_refusal():
    """
    The checkpoint is created once and replayed by every future ping, so a refusal
    captured at setup would be baked into the prompt permanently — and install.sh
    would report success over it.
    """
    section("Setup rejects a refused first message")
    account = temp_account()
    original_run = ew.run_interactive
    refusal = "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)"
    ew.run_interactive = lambda *a, **k: {"completed": True, "limited": True,
                                          "text": refusal}
    try:
        try:
            ew.init(account)
            check("setup exits rather than continuing", "returned normally", "SystemExit")
        except SystemExit as exc:
            check("setup exits non-zero", exc.code, 1)
        check_true("no checkpoint id is written",
                   not os.path.exists(account.session_id_file))
        check_true("no checkpoint backup is written",
                   not os.path.exists(account.checkpoint_backup))
        with open(account.log_file) as f:
            logged = f.read()
        check_true("the refusal is reported to the user", refusal in logged)
    finally:
        ew.run_interactive = original_run


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# PTY teardown
# ---------------------------------------------------------------------------
#
# Once the prompt has been sent the ping has already reached Claude and started a
# window. A run that dies during teardown therefore never writes its state or
# books the boundary anchor, and the schedule stays late for good. These two
# tests pin the failure that actually happened: 5 of 98 runs over 48 hours died
# in the post-/exit drain because `except BlockingIOError` did not cover EIO.

def test_pty_drain_tolerates_a_departed_child():
    section("PTY drain -> survives the child closing the terminal")

    master, slave = pty.openpty()
    os.set_blocking(master, False)

    check("no output pending reads as empty", ew._drain(master), b"")

    os.write(slave, b"hello")
    time.sleep(0.05)
    check("pending output is still returned", ew._drain(master).strip(), b"hello")

    # On Linux a master whose slave has closed reports EIO rather than EOF, and
    # the child exiting is exactly what the teardown drain is waiting for.
    os.close(slave)
    check("a closed slave reads as empty, not EIO", ew._drain(master), b"")
    check("and stays that way when retried", ew._drain(master), b"")

    os.close(master)
    check("a write to a dead fd reports failure", ew._send(master, b"x"), False)


def test_run_interactive_survives_an_immediate_exit():
    section("run_interactive -> returns a result when Claude dies at once")

    tmp = tempfile.mkdtemp()
    fake = os.path.join(tmp, "claude")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\necho starting\nexit 0\n")
    os.chmod(fake, 0o755)

    account = temp_account()
    saved = ew.CLAUDE_PATH
    ew.CLAUDE_PATH = fake
    os.makedirs(account.session_dir)
    try:
        # Every drain site runs against an already-dead child here. Before the
        # fix this raised OSError out of the first one.
        result = ew.run_interactive(account, [], "hi", "no-such-session",
                                    startup_wait=0.2, completion_timeout=1.5,
                                    statusline_wait=0.3)
    finally:
        ew.CLAUDE_PATH = saved

    check("returns a result rather than raising",
          sorted(result), ["completed", "limited", "text"])
    check("reports the turn as not completed", result["completed"], False)


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
#
# One config directory is one Claude login — there is no other seam — so almost
# every multi-account failure reduces to two accounts accidentally sharing a
# path. These tests exist to make that impossible to do quietly.

def test_account_paths():
    section("An account's files and units are derived from its config dir")
    ew.STATE_ROOT = tempfile.mkdtemp()
    first  = ew.Account("1", "~/.claude", 0)
    second = ew.Account("2", "~/.claude-2", 1)

    check("a leading ~ is expanded", first.config_dir,
          os.path.join(os.path.expanduser("~"), ".claude"))
    check_true("each account looks for sessions under its own config dir",
               second.session_dir.startswith(second.config_dir))
    check_true("two accounts never share a session directory",
               first.session_dir != second.session_dir)
    check_true("the session dir keeps Claude Code's mangled-cwd layout",
               second.session_dir.endswith(
                   os.path.join("projects", ew._project_slug(second.ping_cwd))))

    # The whole point of the ping directory: a working directory whose contents
    # never change, because Claude Code puts the working directory's git state
    # into the cached part of every prompt. Running in this checkout meant the
    # cache died every time the repository did.
    check("dots become dashes, as Claude Code stores them",
          ew._project_slug("/home/u/.claude-1/pingcwd"),
          "-home-u--claude-1-pingcwd")
    check("and a path without dots is unaffected",
          ew._project_slug("/opt/dev/claude-window-timing"),
          "-opt-dev-claude-window-timing")

    check_true("a ping runs inside its own account directory",
               second.ping_cwd.startswith(second.config_dir))
    check_true("and not in this checkout",
               not second.ping_cwd.startswith(ew.SCRIPT_DIR))
    check_true("the two accounts do not share a working directory",
               first.ping_cwd != second.ping_cwd)

    for attr in ("state_dir", "session_id_file", "checkpoint_backup",
                 "state_file", "statusline_file", "log_file"):
        check_true("accounts do not share {}".format(attr),
                   getattr(first, attr) != getattr(second, attr))

    check("units are systemd template instances", second.service_unit,
          "claude-window-timing@2.service")
    check("timers match their service", second.timer_unit,
          "claude-window-timing@2.timer")
    check_true("the anchor is not a template instance — it is transient",
               "@" not in second.anchor_unit)
    check_true("anchors are per account",
               first.anchor_unit != second.anchor_unit)

    # Without this every account would ping in the same second forever: for two
    # accounts the 2h30m spacing is an exact multiple of the 30-minute interval.
    check("the first account keeps the plain guard", first.guard_sec,
          ew.RESET_GUARD_SEC)
    check("each later account is staggered by a constant", second.guard_sec,
          ew.RESET_GUARD_SEC + ew.PING_STAGGER_SEC)


def test_the_accounts_file_itself():
    """
    parse_accounts is well covered; the file around it is a separate set of
    ways to be wrong, and each one has to name the file rather than surface as
    a traceback from json.
    """
    section("Reading accounts.json, before anything is parsed")
    root = tempfile.mkdtemp()
    missing = os.path.join(root, "nothing.json")
    check("no file at all means one default account, not an error",
          [a.name for a in ew.load_accounts(missing)], ["1"])
    check("and it is a ping directory, never the user's own",
          ew.load_accounts(missing)[0].config_dir, ew.ping_config_dir("1"))

    broken = os.path.join(root, "broken.json")
    with open(broken, "w") as f:
        f.write('{"accounts": [')
    try:
        ew.load_accounts(broken)
        check("invalid JSON is a configuration error", "no error", "ConfigError")
    except ew.ConfigError as e:
        check_true("invalid JSON names the file and what json said",
                   broken in str(e) and "not valid JSON" in str(e))

    unreadable = os.path.join(root, "unreadable.json")
    with open(unreadable, "w") as f:
        f.write("{}")
    os.chmod(unreadable, 0o000)
    try:
        ew.load_accounts(unreadable)
        skipped = os.geteuid() == 0      # root reads it regardless
        check_true("an unreadable file is a configuration error", skipped)
    except ew.ConfigError as e:
        check_true("an unreadable file says so, naming the file",
                   unreadable in str(e) and "cannot read" in str(e))
    finally:
        os.chmod(unreadable, 0o600)

    try:
        ew.parse_accounts({"accounts": ["not an object"]})
        check("a non-object entry is rejected", "no error", "ConfigError")
    except ew.ConfigError as e:
        check_true("a non-object entry says which one",
                   "#1" in str(e) and "must be an object" in str(e))


def test_accounts_file():
    section("accounts.json is validated before anything acts on it")
    tmp = tempfile.mkdtemp()

    def load(data):
        path = os.path.join(tmp, "accounts.json")
        with open(path, "w") as f:
            json.dump(data, f)
        return ew.load_accounts(path)

    def rejects(name, data):
        try:
            load(data)
            check(name, "accepted", "ConfigError")
        except ew.ConfigError:
            check(name, "ConfigError", "ConfigError")

    accounts = load({"accounts": [
        {"name": "1", "config_dir": "~/.claude"},
        {"name": "2", "config_dir": "~/.claude-2", "label": "second"}]})
    check("both accounts load", [a.name for a in accounts], ["1", "2"])
    check("slot order follows file order", [a.index for a in accounts], [0, 1])
    check("labels are kept for display", accounts[1].display, "2 (second)")

    check("a missing file implies a single default account",
          [a.config_dir for a in ew.load_accounts(os.path.join(tmp, "nope.json"))],
          [ew.ping_config_dir("1")])

    # The mistake that quietly destroys the whole point: two entries that are
    # really the same Claude login.
    rejects("two accounts sharing a config dir are refused",
            {"accounts": [{"name": "1", "config_dir": "~/.claude"},
                          {"name": "2", "config_dir": "~/.claude"}]})
    rejects("a config dir repeated via a different spelling is refused",
            {"accounts": [{"name": "1", "config_dir": "~/.claude"},
                          {"name": "2", "config_dir": "~/.claude/../.claude"}]})
    rejects("duplicate names are refused",
            {"accounts": [{"name": "a", "config_dir": "~/.c1"},
                          {"name": "a", "config_dir": "~/.c2"}]})
    # Names become systemd unit names and directory names.
    for bad in ("has space", "with/slash", "@instance", "", "-leading"):
        rejects("the name {!r} is refused".format(bad),
                {"accounts": [{"name": bad, "config_dir": "~/.c"}]})
    # A typo here would silently point the account at the default directory —
    # which is how two subscriptions quietly become one.
    rejects("a misspelled field is refused rather than ignored",
            {"accounts": [{"name": "1", "configdir": "~/.claude-2"}]})
    check_true("but an underscore-prefixed comment is allowed",
               len(load({"accounts": [{"name": "1", "config_dir": "~/.c",
                                       "_note": "mine"}]})) == 1)
    rejects("a non-string config_dir is refused, not coerced",
            {"accounts": [{"name": "1", "config_dir": 5}]})
    rejects("an empty account list is refused", {"accounts": []})
    rejects("a missing account list is refused", {})
    rejects("a top-level list is refused", [])

    check("find_account returns the named one",
          ew.find_account(accounts, "2").name, "2")
    try:
        ew.find_account(accounts, "nope")
        check("an unknown account name is refused", "returned", "ConfigError")
    except ew.ConfigError as e:
        check_true("the error lists the accounts that do exist", "1, 2" in str(e))


def test_claude_env():
    section("The ping's environment pins the account, and nothing else leaks")
    account = ew.Account("2", "/tmp/cfg-2", 1)
    env = ew.build_claude_env(account)

    check("CLAUDE_CONFIG_DIR is set from the account, not inherited",
          env["CLAUDE_CONFIG_DIR"], "/tmp/cfg-2")

    # No account is special any more: every ping names its own directory, and
    # none of them is the one the user works in.
    other = ew.build_claude_env(ew.Account("1", ew.ping_config_dir("1"), 0))
    check_true("every account pins its own directory",
               other["CLAUDE_CONFIG_DIR"] == ew.ping_config_dir("1"))
    check_true("and none of them is the user's",
               other["CLAUDE_CONFIG_DIR"] != ew.USER_CONFIG_DIR
               and env["CLAUDE_CONFIG_DIR"] != ew.USER_CONFIG_DIR)
    check_true("every ping is still marked as a ping",
               other[ew.PING_MARKER_ENV] == env[ew.PING_MARKER_ENV] == "1")
    check_true("ANTHROPIC_API_KEY is never passed through, so billing stays on "
               "the subscription", "ANTHROPIC_API_KEY" not in env)
    check_true("CLAUDECODE is not inherited, so the child is not a nested session",
               "CLAUDECODE" not in env)

    # Claude Code spawns the status line itself, so the destination cannot
    # travel through the environment — it has to be on the command line, and
    # as a path rather than a name the child would have to resolve for itself.
    settings = json.loads(ew.statusline_settings(account))
    command = settings["statusLine"]["command"]
    check_true("the status line command names the file to write",
               command.rstrip().endswith(account.statusline_file))
    check_true("the status line command is the capture entry point",
               "capture-statusline" in command)


def test_the_status_line_writes_only_where_it_was_told():
    """
    The status line runs as a *subprocess* Claude Code spawns, so it re-imports
    this module from scratch: fresh module-level paths, a fresh read of
    accounts.json, none of the redirection a test has set up in its own
    process. For as long as it was handed an account *name* to resolve, that
    made it write wherever the on-disk configuration said that name lived — so
    running this suite on the machine running the service filed the stand-in
    CLI's invented reset times into the live install's readings, and the next
    real ping believed them and moved the window.

    Hence the two things checked here, both against a real subprocess: the
    payload lands in the file named on the command line, and the checkout's own
    state/ is not touched on the way.
    """
    section("The status line writes to the file it is given, and nowhere else")
    here = os.path.dirname(os.path.abspath(__file__))
    script = os.path.join(here, "claude_window_timing.py")
    live = os.path.join(here, "state")

    def snapshot(root):
        seen = {}
        for base, _, names in os.walk(root):
            for name in names:
                full = os.path.join(base, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                seen[full] = (st.st_size, st.st_mtime)
        return seen

    before = snapshot(live)
    target = os.path.join(tempfile.mkdtemp(), "nested", "statusline.jsonl")
    payload = json.dumps({"cost": {"total_api_duration_ms": 7},
                          "rate_limits": {"five_hour": {"used_percentage": 11}}})
    result = subprocess.run([sys.executable, script, "capture-statusline", target],
                            input=payload, universal_newlines=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    check("it exits cleanly", result.returncode, 0)
    # Anything on stdout is rendered as the status line inside Claude's UI.
    check("and says nothing at all", result.stdout, "")
    check_true("the directory is created if it has to be", os.path.exists(target))
    check("the payload is stored verbatim, one object per line",
          json.load(open(target)) if os.path.exists(target) else None,
          json.loads(payload))
    check("no file under this checkout's state/ was created or changed",
          snapshot(live), before)


def _usage_line_for(command):
    """The first line of `claude-window <command> --help`, as a user sees it."""
    import contextlib, io as _io
    parser = ew.build_parser()
    buf = _io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            parser.parse_args([command, "--help"])
    except SystemExit:
        pass
    return buf.getvalue().splitlines()[0] if buf.getvalue() else ""


def known_names(parser):
    names = set()
    for action in parser._actions:
        names.update(getattr(action, "choices", None) or {})
    return names


def test_the_command_surface():
    section("The shape of the command line")
    parser = ew.build_parser()

    # Flat verbs with the account as a positional, like systemctl — not
    # `<noun> <verb>`, which would invent structure around a single resource.
    commands = sorted(known_names(parser))
    for expected in ("status", "which", "ping", "log", "realign", "doctor",
                     "setup", "check", "accounts", "install-command"):
        check_true("`{}` is a command".format(expected), expected in commands)

    check("an account is a positional, not a flag",
          vars(parser.parse_args(["ping", "2"]))["account"], "2")
    check("and is optional where a default makes sense",
          vars(parser.parse_args(["ping"]))["account"], None)

    # Typing the bare command must be safe: it reports, it never spends quota.
    check("no command at all means status, not ping",
          parser.parse_args([]).command, None)

    # Machine-readable output is opt-in; a default of JSON would break anyone
    # reading the human output.
    check("status is human by default", parser.parse_args(["status"]).json, False)
    check_true("and JSON on request", parser.parse_args(["status", "--json"]).json)

    # The usage line reads `claude-window [-h] <command>`, so `-h which` is a
    # fair thing to type. argparse answers it by printing the general help and
    # dropping the word, which from the other side looks like nothing happened.
    commands = known_names(parser)
    for form in (["-h", "which"], ["--help", "which"], ["help", "which"]):
        check("{} asks about that command".format(" ".join(form)),
              ew.normalise_help(form, commands), ["which", "--help"])
    for form in (["which", "-h"], ["which", "--help"]):
        check("{} is already the right shape".format(" ".join(form)),
              ew.normalise_help(form, commands), form)
    check("bare `help` means the general help", ew.normalise_help(["help"], commands),
          ["--help"])
    check("`help` and a word that is not a command still helps",
          ew.normalise_help(["help", "nonsense"], commands), ["--help"])
    # Nothing here may quietly turn a real command into a help request.
    check("an ordinary command is untouched",
          ew.normalise_help(["ping", "2"], commands), ["ping", "2"])
    check("and neither is a command that merely takes a flag",
          ew.normalise_help(["realign", "--confirm"], commands),
          ["realign", "--confirm"])

    # The screen must not tell you two different things. argparse's generated
    # usage line puts -h *before* the command; every other line of help put it
    # after, and a reader has no way to know which is meant.
    usage = parser.format_usage()
    epilog = parser.epilog or ""
    check_true("the usage line shows the help form", "help [<command>]" in usage)
    check_true("and the closing line shows the same one",
               "help realign" in epilog)
    check_true("neither suggests putting -h before the command",
               "[-h] <command>" not in usage and "-h <command>" not in epilog)
    check_true("and the bare command is explained, since it is the default",
               "no command" in epilog)

    # Every subcommand's own help, not just the top-level screen. A custom
    # usage string on the parent is inherited by the subparsers as their prog
    # unless prog= is passed, and the result is that each one announces itself
    # with the parent's usage glued to the front. Checking the top level alone
    # missed it completely.
    for name in sorted(commands):
        line = _usage_line_for(name)
        check("`{}` announces itself plainly".format(name),
              line.startswith("usage: {} {} ".format(ew.COMMAND, name))
              or line == "usage: {} {}".format(ew.COMMAND, name), True)
        check_true("`{}` does not inherit the parent's usage".format(name),
                   "[<command>]" not in line and "help [" not in line)
        check_true("`{}` fits on one line".format(name), "\n" not in line.strip())

    for bad in (["stauts"], ["ping", "1", "extra"], ["use"]):
        try:
            parser.parse_args(bad)
            check("{!r} is refused".format(bad), "accepted", "SystemExit")
        except SystemExit as exc:
            check("{!r} is refused with a usage error".format(bad), exc.code, 2)


def test_two_accounts_stay_out_of_each_others_files():
    """
    The whole point of the account model: a ping for one account must not read or
    write anything belonging to another. This drives the real ping path — only
    the Claude subprocess is replaced — so it covers the checkpoint restore, the
    state write and the log, not just path arithmetic.
    """
    section("Two accounts ping without reaching into each other's files")

    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    saved_accounts_file, ew.ACCOUNTS_FILE = ew.ACCOUNTS_FILE, \
        os.path.join(root, "accounts.json")
    with open(ew.ACCOUNTS_FILE, "w") as f:
        json.dump({"accounts": [
            {"name": "1", "config_dir": os.path.join(root, "cfg1")},
            {"name": "2", "config_dir": os.path.join(root, "cfg2")}]}, f)

    accounts = ew.load_accounts()
    first, second = accounts
    pinged = []
    original_run = ew.run_interactive

    def fake_run(account, extra_args, prompt, session_id, **kwargs):
        pinged.append((account.name, session_id,
                       ew.build_claude_env(account)["CLAUDE_CONFIG_DIR"]))
        return {"completed": True, "limited": False, "text": "bye"}

    ew.run_interactive = fake_run
    try:
        for account in accounts:
            account.ensure_state_dir()
            with open(account.session_id_file, "w") as f:
                f.write("session-" + account.name)
            stamp_checkpoint(account)
            with open(account.checkpoint_backup, "w") as f:
                f.write("{}\n")
            ew.ping(account)

        check("each account replays its own checkpoint under its own login",
              pinged,
              [("1", "session-1", first.config_dir),
               ("2", "session-2", second.config_dir)])

        for account in accounts:
            check_true("account {} wrote its own state".format(account.name),
                       os.path.exists(account.state_file))
            check_true("account {} restored its checkpoint into its own config "
                       "dir".format(account.name),
                       os.path.exists(ew._session_file(
                           account, "session-" + account.name)))
        check_true("neither account's checkpoint appears in the other's tree",
                   not os.path.exists(ew._session_file(first, "session-2"))
                   and not os.path.exists(ew._session_file(second, "session-1")))
        check_true("each account keeps its own log",
                   "account 1" in open(first.log_file).read()
                   and "account 2" in open(second.log_file).read())

        # Claude Code spawns the status line itself, so this is the one place a
        # destination has to survive a round trip through the command line. It
        # is a path and not an account name on purpose: the child re-imports
        # this module and would resolve a name against whatever accounts.json
        # and state directory *its* copy of the script sees, which is how a
        # test run once wrote its readings into a live install.
        payload = json.dumps({"cost": {"total_api_duration_ms": 42}})
        saved_stdin, sys.stdin = sys.stdin, io.StringIO(payload)
        try:
            ew.cli(["capture-statusline", second.statusline_file])
        finally:
            sys.stdin = saved_stdin
        check("a captured status line lands in the file it was given",
              len(ew.statusline_records(second)), 1)
        check("and nowhere near the other account's",
              ew.statusline_records(first), [])

        settings = json.loads(ew.statusline_settings(second))
        command = settings["statusLine"]["command"]
        check_true("the command names that file outright",
                   second.statusline_file in command)
        check_true("and carries no bare account name to be resolved elsewhere",
                   " {}".format(second.name) not in
                   command[command.index("capture-statusline"):])

        # Typing the bare command must never spend quota: a ping changes when a
        # window starts, so it has to be asked for by name.
        buf = io.StringIO()
        saved_stdout, sys.stdout = sys.stdout, buf
        try:
            code = ew.cli([])
        finally:
            sys.stdout = saved_stdout
        check("the bare command reports status rather than pinging", code, 0)
        check("and sent no ping", len(pinged), 2)
        # Whoever types the bare command has been shown one view of a tool with
        # fifteen commands, and nothing else on screen says so.
        shown = buf.getvalue()
        check_true("it points at the other commands", "Other commands" in shown)
        check_true("and names some rather than only pointing at help",
                   "doctor" in shown and "realign" in shown)
        check_true("and says how to see the rest",
                   "{} help".format(ew.COMMAND) in shown)

        # Asking for status by name means you already know commands exist.
        buf = io.StringIO()
        sys.stdout = buf
        try:
            ew.cli(["status"])
        finally:
            sys.stdout = saved_stdout
        check_true("asking for status by name does not repeat the signpost",
                   "Other commands" not in buf.getvalue())

        check("a ping with no account named uses the first", ew.cli(["ping"]), 0)
        check("the default really was the first account", pinged[-1][0], "1")
        check("an unknown account is refused instead of guessed",
              ew.cli(["ping", "nope"]), 2)
        check("and refusing means no ping was sent", len(pinged), 3)
    finally:
        ew.run_interactive = original_run
        ew.ACCOUNTS_FILE = saved_accounts_file


# ---------------------------------------------------------------------------
# Sharing conversations between accounts
# ---------------------------------------------------------------------------
#
# This is the feature that decides whether hitting a limit mid-task means
# switching account or starting over, so the failure that matters most is not
# "it did not link" but "it linked and threw away what was there".

def _two_accounts():
    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    return (ew.Account("1", os.path.join(root, "cfg1"), 0),
            ew.Account("2", os.path.join(root, "cfg2"), 1))


def test_validation_catches_the_expensive_mistakes():
    section("Validation names the consequence, not just the symptom")
    first, second = _two_accounts()
    for account in (first, second):
        os.makedirs(account.config_dir, 0o700)

    def sign_in(account, uuid_, email, subscription="pro", tier="default",
                refresh_in_days=90):
        with open(account.config_json, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": uuid_,
                                        "emailAddress": email}}, f)
        with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "t", "subscriptionType": subscription,
                "rateLimitTier": tier,
                "refreshTokenExpiresAt": int(
                    (time.time() + refresh_in_days * 86400) * 1000)}}, f)

    def levels():
        return ew.validate_accounts([first, second])

    sign_in(first, "aaa", "one@example.com")
    check_true("an account that was never signed in is an error",
               any(f.level == "error" and "not signed in" in f.message
                   for f in levels()))
    # Telling someone to set CLAUDE_CONFIG_DIR=~/.claude would send Claude Code
    # to a different .claude.json and lose every trust decision they have made.
    check("an account is signed in by naming its own directory",
          ew.sign_in_command(ew.Account("2", "/tmp/cfg-2", 1)),
          "CLAUDE_CONFIG_DIR=/tmp/cfg-2 claude")

    # The mistake that keeps working while buying nothing.
    sign_in(second, "aaa", "one@example.com")
    same = [f for f in levels() if "same Claude account" in f.message]
    check_true("signing in twice as one account is an error",
               same and same[0].level == "error")
    check_true("and the message says why it matters",
               "buys nothing" in same[0].hint)

    sign_in(second, "bbb", "two@example.com")
    check("two distinct paid accounts are clean", levels(), [])

    sign_in(second, "bbb", "two@example.com", subscription="free")
    check_true("a free plan is an error",
               any(f.level == "error" and "free" in f.message for f in levels()))

    sign_in(second, "bbb", "two@example.com", tier="max_20x")
    mixed = [f for f in levels() if "different plans" in f.message]
    check_true("mixed plans warn without blocking",
               mixed and mixed[0].level == "warning")

    sign_in(second, "bbb", "two@example.com", refresh_in_days=2)
    check_true("a login about to expire warns before it bites",
               any(f.level == "warning" and "login expires" in f.message
                   for f in levels()))

    sign_in(second, "bbb", "two@example.com")
    os.chmod(second.config_dir, 0o755)
    check_true("a world-readable config directory warns — it holds a token",
               any("readable by other users" in f.message for f in levels()))
    os.chmod(second.config_dir, 0o700)

    open(os.path.join(second.config_dir, ".stfolder"), "w").close()
    check_true("a config directory inside a synced folder warns",
               any("synced folder" in f.message for f in levels()))

    check("errors are reported before warnings",
          [f.level for f in ew.validate_accounts([first, second])][:1],
          ["warning"])


# ---------------------------------------------------------------------------
# Choosing an account
# ---------------------------------------------------------------------------
#
# Getting this wrong is invisible: the wrong account still answers, it just
# wastes the window that was about to expire. So the rule is tested against every
# combination of availability rather than the happy path alone.

def test_stale_reset_rolls_forward():
    section("A reset time left behind by a missed ping is corrected, not distrusted")
    now = 1_000_000.0
    window = ew.WINDOW_HOURS * 3600

    def expiry(resets):
        return ew.next_expiry({"rate_limits": {"five_hour": {"resets_at": resets}}}, now)

    check("a future reset is left alone", expiry(now + 600), now + 600)
    check("a reset one window old rolls forward once",
          expiry(now - 600), now - 600 + window)
    check("several missed windows roll forward as many times",
          expiry(now - 3 * window - 600), now - 600 + window)
    check("a reset exactly now is treated as passed", expiry(now), now + window)
    check("no reading at all sorts last", ew.next_expiry({}, now), float("inf"))


def test_choosing_between_accounts():
    section("Choose the most perishable usable account")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    a = ew.Account("a", "/tmp/cfg-a", 0)
    b = ew.Account("b", "/tmp/cfg-b", 1)

    def state(expires_in=None, available_in=None, failures=0,
              five_pct=None, weekly=None, weekly_pct=None):
        s = {"consecutive_failures": failures}
        limits = {}
        if expires_in is not None:
            limits["five_hour"] = {"resets_at": now + expires_in,
                                   "used_percentage": five_pct}
        if weekly is not None:
            limits["seven_day"] = {"resets_at": now + weekly,
                                   "used_percentage": weekly_pct}
        if limits:
            s["rate_limits"] = limits
        if available_in is not None:
            s["available_at"] = now + available_in
        return s

    def chosen(sa, sb):
        return ew.choose_account([a, b], {"a": sa, "b": sb}, now)[0].name

    check("the window ending sooner wins",
          chosen(state(expires_in=600, available_in=-1),
                 state(expires_in=9000, available_in=-1)), "a")
    check("and the other way round",
          chosen(state(expires_in=9000, available_in=-1),
                 state(expires_in=600, available_in=-1)), "b")

    # Out of quota: the reason is never asked. A spent weekly limit, a lapsed
    # subscription and a revoked login are all just "not now".
    check("an account that cannot serve is skipped even if it expires sooner",
          chosen(state(expires_in=60, available_in=3600),
                 state(expires_in=9000, available_in=-1)), "b")
    check("when none can serve, the one returning first is named",
          chosen(state(available_in=7200), state(available_in=3600)), "b")

    check("an account whose pings keep failing is offered last",
          chosen(state(expires_in=60, available_in=-1, failures=ew.UNHEALTHY_AFTER),
                 state(expires_in=9000, available_in=-1)), "b")
    check("but a single failure is not held against it",
          chosen(state(expires_in=60, available_in=-1, failures=1),
                 state(expires_in=9000, available_in=-1)), "a")

    check("an account never pinged is still offered",
          chosen({}, state(expires_in=9000, available_in=-1)), "b")

    # This is advice and nothing more: nothing in the tool acts on it.
    check("the rule holds with no way to override it, because none is needed",
          chosen(state(expires_in=9000, available_in=-1),
                 state(expires_in=60, available_in=-1)), "b")

    # The case a successful ping cannot detect. Pings are cache reads and are not
    # deducted from the rate limit, so one can get through an account whose limit
    # is spent — leaving `available_at` saying "fine" about an account that will
    # refuse the first real request.
    check("a spent 5-hour limit is skipped even though the last ping succeeded",
          chosen(state(expires_in=60, available_in=-1, five_pct=100),
                 state(expires_in=9000, available_in=-1)), "b")
    check("a spent weekly limit is skipped for as long as it lasts",
          chosen(state(expires_in=60, available_in=-1,
                       weekly=3 * 86400, weekly_pct=100),
                 state(expires_in=9000, available_in=-1)), "b")
    check("a limit that is merely nearly spent is still usable",
          chosen(state(expires_in=60, available_in=-1, five_pct=99),
                 state(expires_in=9000, available_in=-1)), "a")

    # A percentage is only ever about the window it was measured in. Once that
    # window has ended the account has refilled, whatever the last reading said —
    # so a missed ping cannot leave an account looking permanently exhausted.
    check("a 100% reading whose window has already reset is not held against it",
          ew.account_availability(
              a, state(expires_in=-600, available_in=-1, five_pct=100), now).tier,
          ew.USABLE)

    # Two blocked accounts: back only when the *last* blocker clears, so the one
    # named is the one that actually returns first.
    check("with everything blocked, the soonest to return is named",
          chosen(state(expires_in=600, five_pct=100, weekly=6 * 86400,
                       weekly_pct=100),
                 state(expires_in=3600, five_pct=100)), "b")

    unusable = ew.account_availability(
        a, state(expires_in=600, five_pct=100, weekly=6 * 86400, weekly_pct=100),
        now)
    check("and it says which limit is holding it", unusable.note,
          "its weekly limit is spent")
    check("and when it comes back", round(unusable.until - now), 6 * 86400)


def test_an_unusable_login_is_never_recommended():
    section("Accounts that cannot serve a request at all are skipped")
    now = time.time()
    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    a = ew.Account("a", os.path.join(root, "cfg-a"), 0)
    b = ew.Account("b", os.path.join(root, "cfg-b"), 1)

    def sign_in(account, subscription="max", expires_in_days=90, token="t"):
        if not os.path.isdir(account.config_dir):
            os.makedirs(account.config_dir, 0o700)
        with open(account.config_json, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": account.name,
                                        "emailAddress": account.name + "@x"}}, f)
        with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": token, "subscriptionType": subscription,
                "refreshTokenExpiresAt": int(
                    (now + expires_in_days * 86400) * 1000)}}, f)

    # `a` always looks the most attractive on window arithmetic alone: it is the
    # one about to expire. Everything below is about not recommending it anyway.
    fresh = {"rate_limits": {"five_hour": {"resets_at": now + 600}}}
    later = {"rate_limits": {"five_hour": {"resets_at": now + 9000}}}

    def chosen():
        return ew.choose_account([a, b], {"a": fresh, "b": later}, now)[0].name

    sign_in(b)
    check("a directory that does not exist yet is no evidence of a fault",
          chosen(), "a")

    os.makedirs(a.config_dir, 0o700)
    check("but an account directory with no login in it is skipped",
          chosen(), "b")
    check("and says so", ew.identity_blocker(a), "not signed in")

    sign_in(a, subscription="free")
    check("a free plan cannot supply a window, so it is skipped", chosen(), "b")
    check("and says so", ew.identity_blocker(a), "no paid subscription (free)")

    sign_in(a, expires_in_days=-1)
    check("an expired sign-in is skipped", chosen(), "b")
    check("and says so", ew.identity_blocker(a), "its sign-in has expired")

    sign_in(a)
    check("a working login is recommended again with nothing else to do",
          chosen(), "a")

    # Worse than out of quota: quota returns on its own, this does not. Both are
    # unusable, so the ordering only shows in which one gets named.
    sign_in(a, subscription="free")
    check("a lapsed subscription ranks below an account merely out of quota",
          ew.choose_account([a, b], {"a": fresh,
                                     "b": dict(later, available_at=now + 3600)},
                            now)[0].name, "b")

    # A machine with a copied schedule.json and no config directories at all must
    # still answer, because that is the whole point of publishing the schedule.
    elsewhere = ew.Account("a", os.path.join(root, "nowhere"), 0)
    check("no local config directory means no verdict, not a bad one",
          ew.account_availability(elsewhere, fresh, now).tier, ew.USABLE)


def test_a_checkpoint_from_another_directory_is_rebuilt():
    section("a checkpoint that cannot be resumed where pings run is noticed")
    ew.STATE_ROOT = tempfile.mkdtemp()
    account = ew.Account("1", tempfile.mkdtemp(), 0)
    account.ensure_state_dir()

    check_true("nothing to be wrong about before there is a checkpoint",
               ew.checkpoint_is_for_this_cwd(account))

    with open(account.checkpoint_backup, "w") as f:
        f.write("{}\n")
    if os.path.exists(account.checkpoint_cwd_file):
        os.remove(account.checkpoint_cwd_file)
    check_true("a checkpoint with no recorded directory is treated as stale",
               not ew.checkpoint_is_for_this_cwd(account))

    with open(account.checkpoint_cwd_file, "w") as f:
        f.write("/somewhere/else")
    check_true("and so is one recorded against another directory",
               not ew.checkpoint_is_for_this_cwd(account))

    with open(account.checkpoint_cwd_file, "w") as f:
        f.write(account.ping_cwd)
    check_true("one built here is usable", ew.checkpoint_is_for_this_cwd(account))

    # And the same condition is what doctor reports, since a ping failing every
    # half hour for a reason only in the log is the failure mode to avoid.
    with open(account.session_id_file, "w") as f:
        f.write("11111111-2222-3333-4444-555555555555")
    with open(account.checkpoint_cwd_file, "w") as f:
        f.write("/somewhere/else")
    held, sys.stdout = sys.stdout, io.StringIO()
    try:
        ew.doctor([account])
        said = sys.stdout.getvalue()
    finally:
        sys.stdout = held
    check_true("doctor says so", "different working directory" in said)
    check_true("and names the fix", "install.sh" in said)


def test_an_impossible_usage_report_is_ignored():
    section("a usage report that contradicts arithmetic is not believed")
    now = time.time()

    # The real case: a run reported a fresh window resetting in exactly four
    # hours while the reset it already knew about was still an hour away.
    known = {"five_hour": {"resets_at": now + 3600, "used_percentage": 99}}
    placeholder = {"five_hour": {"resets_at": now + 4 * 3600, "used_percentage": 3}}
    why = ew.implausible_limits(placeholder, known, now)
    check_true("a rollover before the known reset is rejected", bool(why))
    check_true("the reason names the impossible reset",
               why and "has not passed yet" in why)

    # A genuine rollover, observed after the previous window actually ended.
    later = now + 3700
    check("a rollover after the known reset is believed",
          ew.implausible_limits({"five_hour": {"resets_at": later + 5 * 3600,
                                               "used_percentage": 0}},
                                known, later), None)

    # An ordinary reading inside the same window, usage climbing.
    check("usage rising inside one window is believed",
          ew.implausible_limits({"five_hour": {"resets_at": now + 3600,
                                               "used_percentage": 40}},
                                known, now), None)

    # Nothing to compare against yet.
    check("the first reading is always believed",
          ew.implausible_limits(placeholder, {}, now), None)

    # The weekly limit is guarded on the same principle.
    check_true("a weekly rollover before its reset is rejected",
               bool(ew.implausible_limits(
                   {"seven_day": {"resets_at": now + 72 * 3600}},
                   {"seven_day": {"resets_at": now + 3600}}, now)))


def test_schedule_carries_account_identity():
    section("schedule.json says which account, not merely which slot")
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.SCHEDULE_FILE = os.path.join(ew.STATE_ROOT, "schedule.json")
    account = ew.Account("1", tempfile.mkdtemp(), 0)
    account.ensure_state_dir()
    with open(account.config_json, "w") as f:
        json.dump({"oauthAccount": {"accountUuid": "uuid-abc",
                                    "emailAddress": "someone@example.com"}}, f)

    ew.publish_schedule([account])
    entry = json.load(open(ew.SCHEDULE_FILE))["accounts"][0]
    check("the uuid travels with the entry", entry.get("account_uuid"), "uuid-abc")
    check_true("the email does not, because this file gets copied around",
               "someone@example.com" not in json.dumps(entry))


def test_schedule_is_publishable_for_other_machines():
    section("schedule.json carries enough for a laptop to choose on its own")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.SCHEDULE_FILE = os.path.join(ew.STATE_ROOT, "schedule.json")
    a = ew.Account("1", ew.ping_config_dir("1"), 0)
    b = ew.Account("2", "/tmp/cfg-2", 1)
    for account, expires in ((a, now + 600), (b, now + 9600)):
        account.ensure_state_dir()
        ew.write_state(account, {"rate_limits": {"five_hour": {
            "resets_at": expires, "used_percentage": 3}}})

    ew.publish_schedule([a, b])
    document = json.load(open(ew.SCHEDULE_FILE))
    entries = {e["name"]: e for e in document["accounts"]}

    check("every account is listed", sorted(entries), ["1", "2"])
    check("each account carries its own directory",
          entries["2"]["config_dir"], "/tmp/cfg-2")
    # The phase is the durable part: expiry times move every window, but the
    # offset within the window does not, so a stale copy still chooses correctly.
    for name in ("1", "2"):
        check_true("account {} publishes a window phase".format(name),
                   0 <= entries[name]["window_phase"] < ew.WINDOW_HOURS * 3600)
    check("phases differ, which is what makes the choice meaningful",
          entries["1"]["window_phase"] != entries["2"]["window_phase"], True)

    # A bound must not arrive on the other machine looking like an observation.
    ew.write_state(b, {"rate_limits": {
        "five_hour": {"resets_at": now + 9600, "used_percentage": 3},
        "seven_day": {"used_percentage": 100}}})
    ew.publish_schedule([a, b])
    entries = {e["name"]: e
               for e in json.load(open(ew.SCHEDULE_FILE))["accounts"]}
    check("a spent limit with no reset time is published as a bound",
          entries["2"]["unusable_until_exact"], False)
    for account in (a, b):
        shutil.rmtree(account.state_dir, ignore_errors=True)
    _, _, avail, _ = ew.schedule_view([a, b])
    check("and reads back on the other machine as a bound",
          avail["2"].exact, False)
    check("while an observed time reads as observed", avail["1"].exact, True)


def test_a_second_machine_answers_from_the_schedule():
    """
    The claim that makes a laptop useful with nothing installed: copy
    schedule.json in beside the script and `which` answers from it.

    Worth testing rather than assuming, because everything here is arithmetic on
    a file that may be days old — and because the failure would be silent, an
    answer that looks exactly as confident as a live one.
    """
    section("A machine that pings nothing answers from a copied schedule")
    now = time.time()
    saved = (ew.STATE_ROOT, ew.SCHEDULE_FILE)
    try:
        pinger = tempfile.mkdtemp()
        ew.STATE_ROOT = os.path.join(pinger, "state")
        ew.SCHEDULE_FILE = os.path.join(pinger, "schedule.json")
        a = ew.Account("1", os.path.join(pinger, "cfg-1"), 0, "personal")
        b = ew.Account("2", os.path.join(pinger, "cfg-2"), 1, "work")
        for account, expires in ((a, now + 600), (b, now + 2.5 * HOUR + 600)):
            account.ensure_state_dir()
            ew.write_state(account, {
                "last_run": now - 60, "available_at": now - 60,
                "rate_limits": {"five_hour": {"resets_at": expires,
                                              "used_percentage": 12}}})
        # Account 2 is out of quota for the next hour: the verdict only the
        # pinging machine can reach, and the reason it has to travel.
        ew.write_state(b, dict(ew.read_state(b), available_at=now + 3600))
        ew.publish_schedule([a, b])

        # -- the laptop: the file, and nothing else -------------------------
        laptop = tempfile.mkdtemp()
        ew.STATE_ROOT = os.path.join(laptop, "state")
        published = os.path.join(laptop, "schedule.json")
        shutil.copy(ew.SCHEDULE_FILE, published)
        ew.SCHEDULE_FILE = published

        view = ew.schedule_view(ew.default_accounts())
        check_true("a copied schedule is enough to answer at all", view is not None)
        accounts, states, avail, written_at = view
        check("every account in the file is answerable",
              [x.name for x in accounts], ["1", "2"])
        check("labels travel too, so the answer names what the user named",
              [x.label for x in accounts], ["personal", "work"])
        check("the verdict travels, not just the numbers",
              avail["2"].tier, ew.WAITING)
        check_true("along with the reason for it",
                   "refused" in avail["2"].note or "limit" in avail["2"].note)
        check("and it picks the account that can actually be used",
              ew.choose_account(accounts, states, now, avail)[0].name, "1")

        # -- and still answers when the copy is old --------------------------
        document = json.load(open(published))
        age = 3 * 86400                  # 14.4 windows: a phase shift, not zero
        document["written_at"] -= age
        for entry in document["accounts"]:
            entry["expires_at"] -= age
            entry["last_run"] -= age
        with open(published, "w") as f:
            json.dump(document, f)

        accounts, states, avail, written_at = ew.schedule_view(
            ew.default_accounts())
        expiry = ew.next_expiry(states["1"], now)
        window = ew.WINDOW_HOURS * HOUR
        check_true("a stale expiry is rolled into the window running now",
                   now < expiry <= now + window)
        # The phase is the whole reason a days-old copy is still worth reading:
        # windows tile back to back, so the boundary running now sits at exactly
        # the offset the file recorded, however many windows ago that was.
        check("and lands on the phase the file recorded",
              round(expiry % window),
              round((now + 600 - age) % window))

        buf = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        try:
            ew.which(accounts, states, avail, written_at)
        finally:
            sys.stdout = out
        said = buf.getvalue()
        check_true("the answer says where it came from",
                   "schedule.json" in said and "72h00m" in said)
        check_true("and does not claim the local timers are late",
                   "check the timers" not in said)

        # A bare `status` on such a machine has nothing of its own to say, and
        # its "run ./install.sh" is the wrong advice for someone who never
        # meant to ping from here.
        buf = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        try:
            ew.status(accounts)
        finally:
            sys.stdout = out
        check_true("status points at the command that can answer",
                   "which` answers from it" in buf.getvalue())

        # -- what a damaged or older file does --------------------------------
        check("a file that is not there reads as nothing to go on",
              ew.read_schedule(os.path.join(laptop, "absent.json")), {})
        with open(published, "w") as f:
            f.write("{ not json")
        check("an unreadable file is ignored rather than half-believed",
              ew.schedule_view(ew.default_accounts()), None)
        with open(published, "w") as f:
            json.dump({"written_at": now, "accounts": []}, f)
        check("a file listing no accounts is nothing to go on",
              ew.schedule_view(ew.default_accounts()), None)
        with open(published, "w") as f:
            json.dump({"written_at": now, "accounts": [
                "junk", {"label": "no name"},
                {"name": "9", "expires_at": now + 600, "usable_now": False,
                 "unusable_until": now + 60, "unusable_because": "resting"}]}, f)
        view = ew.schedule_view(ew.default_accounts())
        check("entries with no name are skipped, not fatal",
              [a.name for a in view[0]], ["9"])
        # `tier` is newer than the rest of the file's shape; a copy written
        # before it existed still has to produce an answer.
        check("a file with no tier falls back on usable_now",
              view[2]["9"].tier, ew.WAITING)

        # -- the pinging machine trusts itself, never the file ---------------
        mine = ew.Account("1", os.path.join(laptop, "cfg-1"), 0)
        mine.ensure_state_dir()
        ew.write_state(mine, {"last_run": now})
        check("a machine with readings of its own ignores the schedule",
              ew.schedule_view([mine]), None)
    finally:
        ew.STATE_ROOT, ew.SCHEDULE_FILE = saved


def test_an_account_the_schedule_has_never_heard_of():
    """
    The laptop is configured for three accounts and the schedule copied to it
    knows two — the copy predates the third being added, or the machine that
    pings does not have it.

    Every line of `status` looks its accounts up by name in the two dictionaries
    the schedule produced, so the third one raised KeyError half way through
    printing: a traceback on the machine the README tells people to switch from,
    in the command they run to find out what is going on.
    """
    section("An account the published schedule has never heard of")
    now = time.time()
    saved = (ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.ALIGNMENT_FILE,
             ew._systemctl, ew._run)

    class Ok(object):
        returncode = 0
        stdout = ""

    try:
        root = tempfile.mkdtemp()
        ew.STATE_ROOT = os.path.join(root, "state")
        ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
        ew.SCHEDULE_FILE = os.path.join(root, "schedule.json")
        ew._systemctl = lambda *a: Ok()
        ew._run = lambda cmd: Ok()
        accounts = [ew.Account(name, os.path.join(root, "cfg-" + name), index,
                               label)
                    for index, (name, label) in enumerate(
                        (("1", "personal"), ("2", "work"), ("3", "spare")))]
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"written_at": now, "window_hours": 5, "accounts": [
                {"name": "1", "label": "personal", "tier": "usable",
                 "usable_now": True, "expires_at": now + 600,
                 "last_run": now - 60, "used_percentage": 12},
                {"name": "2", "label": "work", "tier": "usable",
                 "usable_now": True, "expires_at": now + 9000,
                 "last_run": now - 60, "used_percentage": 12}]}, f)

        said, _, code = _capture(lambda: ew.status(accounts))
        check("status answers rather than raising", code, 0)
        check_true("naming the account the file does know about",
                   "Use account 1" in said)
        check_true("and admitting what it cannot know about the other",
                   "cannot tell" in said
                   and "has not published this account" in said)
        check_true("the spacing report survives it too",
                   "Spacing" in said and "account 3" in said)

        said, _, code = _capture(lambda: ew.status_json(accounts))
        document = json.loads(said)
        check("the JSON answers too", code, 0)
        check_true("and says whose figures these are",
                   abs(document["published_at"] - now) < 2)
        entries = {e["name"]: e for e in document["accounts"]}
        check("every configured account is still reported",
              sorted(entries), ["1", "2", "3"])
        # The one that matters: a script must never be told that an account
        # nothing is known about is fine to spend.
        check("the unheard-of one reads as cannot-tell, never usable",
              entries["3"]["tier"], "unknown")
        check("and is not what the answer points at",
              document["use"]["account"], "1")
    finally:
        (ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.ALIGNMENT_FILE,
         ew._systemctl, ew._run) = saved


def test_a_login_that_stopped_working_is_reported():
    """
    `doctor` asks the CLI whether a login still works, rather than deciding for
    itself from the credentials file. Both directions matter: a real fault has
    to be named, and "cannot tell" must never be reported as a fault — a
    diagnostic that invents problems gets ignored, taking the real ones with it.
    """
    section("Whether a login still works is asked, not guessed")
    saved = (ew.SCRIPT_DIR, ew.CLAUDE_PATH, ew.STATE_ROOT)
    try:
        root = tempfile.mkdtemp()
        ew.SCRIPT_DIR = root             # where the stand-in reads its control file
        ew.CLAUDE_PATH = FAKE_CLAUDE
        ew.STATE_ROOT = os.path.join(root, "state")
        account = ew.Account("1", os.path.join(root, "cfg"), 0)

        def answers(report):
            control = os.path.join(account.config_dir, ".fake_claude.json")
            with open(control, "w") as f:
                json.dump({"auth": report}, f)
            return ew.account_auth_ok(account)

        os.makedirs(account.config_dir, 0o700)
        check("no credentials at all needs no subprocess to answer",
              ew.account_auth_ok(account), (False, "not signed in"))

        with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "t"}}, f)

        check("a working paid login is fine",
              answers({"loggedIn": True, "subscriptionType": "max"}), (True, "max"))
        check("a login the CLI no longer accepts is named",
              answers({"loggedIn": False}), (False, "not signed in"))
        # The credentials file can still hold a perfectly good token here: the
        # subscription behind it is what lapsed, and only the CLI knows.
        check("a lapsed subscription is a fault even with a token on disk",
              answers({"loggedIn": True, "subscriptionType": "free"}),
              (False, "no paid subscription (free)"))
        check("a plan the CLI declines to name is not guessed at",
              answers({"loggedIn": True}), (False, "no paid subscription (unknown)"))
        check("an answer this cannot parse counts as cannot tell",
              answers(False), (True, "no readable answer"))

        ew.CLAUDE_PATH = os.path.join(root, "no-such-cli")
        check("and so does having no CLI to ask",
              ew.account_auth_ok(account), (True, "could not run the CLI"))
    finally:
        ew.SCRIPT_DIR, ew.CLAUDE_PATH, ew.STATE_ROOT = saved


# ---------------------------------------------------------------------------
# Keeping the windows evenly spaced
# ---------------------------------------------------------------------------
#
# A phase can only ever be delayed, never advanced, so every correction costs a
# real stretch with no window running. The optimiser is worth testing carefully
# because a plausible-looking wrong answer here is expensive rather than broken:
# it still lines the accounts up, it just pays hours instead of minutes.

HOUR = 3600.0


def test_what_it_says_in_every_state_it_can_be_in():
    """
    `which` and `status` are the whole product for most of a day, and each of
    their sentences belongs to a state the user is in. The dangerous ones are
    where nothing can be used: "Use account 2" is a lie then, and a
    recommendation nobody can act on reads as an instruction anyway.

    So every headline, every one-line verdict, and the display lines `status`
    only prints when something is unusual, are pinned to the state that
    produces them.
    """
    section("What it says in each state it can be in")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    one = ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg-1"), 0)
    two = ew.Account("2", os.path.join(ew.STATE_ROOT, "cfg-2"), 1, "work")
    for account in (one, two):
        account.ensure_state_dir()
    # No config directory: "cannot tell" is not "broken", so the verdicts below
    # come from the readings alone, which is what each of them is about.

    fresh = {"rate_limits": {"five_hour": {"resets_at": now + 3600}},
             "last_run": now}

    def headline_for(state, count=1, account=one):
        avail = ew.account_availability(account, state, now)
        return ew.headline(account, avail, count), ew.describe_availability(
            state, avail, now)

    # Usable, and the only account there is: no comparison to imply.
    reason = ew.choose_account([one], {one.name: fresh}, now)[1]
    check_true("with one account it does not claim to have chosen",
               "the only account" in reason and "ends first" not in reason)

    # Usable, but nothing has ever been read.
    line, verdict = headline_for({})
    check("a fresh install is still told what to use", line, "Use account 1")
    check("with no window to promise", verdict, "no window information yet")
    check_true("and the recommendation says as much",
               "run a ping first" in ew.choose_account([one], {one.name: {}},
                                                       now)[1])

    # Waiting: a limit that names its own end.
    waiting = dict(fresh, rate_limits={"five_hour": {"resets_at": now + 1800,
                                                     "used_percentage": 100}})
    line, verdict = headline_for(waiting)
    check("one account, unusable for now, is not phrased as an instruction",
          line, "Account 1 is not usable yet")
    check_true("and the verdict says until when and why",
               verdict.startswith("unusable until") and "5-hour" in verdict)
    line, _ = headline_for(waiting, count=2)
    check("with more than one, it names the one coming back first",
          line, "No account is usable yet — 1 is next")

    # Unknown: pings that keep failing say nothing about the account.
    unknown = dict(fresh, consecutive_failures=ew.UNHEALTHY_AFTER)
    line, verdict = headline_for(unknown, count=2)
    check("nothing known to work is offered as a guess, not a verdict",
          line, "Nothing is known to be usable — try account 1")
    check("and the verdict admits it rather than calling it unusable", verdict,
          "cannot tell — its pings keep failing")

    # The spacing report when there is nothing to space. Account 2's weekly
    # limit outlasts its own window, so it will not start one.
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    ew.write_alignment({})
    blocked = {one.name: dict(fresh, available_at=now - 1),
               two.name: {"available_at": now - 1,
                          "rate_limits": {
                              "five_hour": {"resets_at": now + 600},
                              "seven_day": {"resets_at": now + 4 * 86400,
                                            "used_percentage": 100}}}}
    said = " ".join(ew.describe_alignment([one, two], blocked, now)[0])
    check_true("one account holding a window is not a fault to report",
               "Only one account is holding a window" in said
               and "nothing to space" in said)
    check_true("and it says which account is not holding one, and why",
               "account 2 (work)" in said and "weekly limit is spent" in said)

    # Needs action: only the user can clear it, so it outranks everything.
    os.makedirs(one.config_dir, 0o700)
    with open(one.config_json, "w") as f:
        json.dump({"oauthAccount": {"accountUuid": "u1"}}, f)
    with open(os.path.join(one.config_dir, ".credentials.json"), "w") as f:
        json.dump({"claudeAiOauth": {"accessToken": "t",
                                     "subscriptionType": "free"}}, f)
    line, verdict = headline_for(fresh)
    check("an account only the user can fix says so", line,
          "Account 1 cannot be used")
    check_true("naming the reason", "no paid subscription" in verdict)
    line, _ = headline_for(fresh, count=2)
    check("and with several, where to start", line,
          "No account can be used — start with 1")
    check_true("the recommendation points at the command that explains it",
               "doctor" in ew.choose_account([one], {one.name: fresh}, now)[1])


def test_the_age_of_the_figures_is_never_overstated():
    """
    `which` ends by saying how old its numbers are, which is the reader's only
    guard against acting on a stale one.

    With several accounts there are several ages, and summarising by the
    freshest describes the one account nobody was worried about: the answer
    rests on all of them at once, since each account is kept or skipped on the
    strength of its own figures. So the oldest is what gets reported, and the
    source is only called live when every one of them is.

    The offer to take a reading is the other half. It is worth making to
    somebody who passed `--no-live`, and nonsense to anybody else — a run that
    read the limits live has already done all there is to do, and pointing that
    reader at the command they are running is a loop.
    """
    section("How old the figures are, said without flattering them")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    accounts = [ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg-1"), 0),
                ew.Account("2", os.path.join(ew.STATE_ROOT, "cfg-2"), 1)]
    for account, state in zip(accounts, (
            {"last_run": now - 1500, "available_at": now - 1500,
             "limits_read_at": now - 1500, "limits_source": "statusline",
             "rate_limits": {"five_hour": {"resets_at": now + 4 * HOUR,
                                           "used_percentage": 40}}},
            {"last_run": now - 60, "available_at": now - 60,
             "limits_read_at": now - 60, "limits_source": "live",
             "rate_limits": {"five_hour": {"resets_at": now + 2 * HOUR,
                                           "used_percentage": 30}}})):
        account.ensure_state_dir()
        ew.write_state(account, state)

    said, _, _ = _capture(lambda: ew.which(accounts, live=False))
    check_true("the oldest reading is the one reported",
               "0h25m00s ago" in said)
    check_true("and one live account does not make the set live",
               "from the last ping" in said)
    check_true("with the offer to read them now",
               "`{} which` reads them from Claude now".format(ew.COMMAND)
               in said)

    said, _, _ = _capture(lambda: ew.which(accounts, live=True))
    check_true("a run that already read them live offers nothing further",
               "reads them from Claude now" not in said)

    for account in accounts:
        state = ew.read_state(account)
        state["limits_read_at"] = now - 5
        state["limits_source"] = "live"
        ew.write_state(account, state)
    said, _, _ = _capture(lambda: ew.which(accounts, live=True))
    check_true("and when every account was read live, it says so",
               "from a live reading, 0h00m05s ago" in said)

    # An account nobody has pinged yet has no age to report, and must not be
    # counted as one: that would date the whole set to the epoch.
    ew.write_state(accounts[0], {})
    said, _, _ = _capture(lambda: ew.which(accounts, live=False))
    check_true("an account with no readings at all is not counted as ancient",
               "1970" not in said and "Figures from" in said)


def test_the_status_display_shows_the_unusual_parts():
    """
    Most of `status` only appears when something is worth saying — a run of
    failed pings, a limit that is spent, a hold, a boundary already passed.
    Those lines are the ones nobody sees until the day they matter.
    """
    section("status shows the parts that only appear when they matter")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    saved = (ew._systemctl, ew._run, ew.SCHEDULE_FILE)
    class Ok(object):
        returncode = 0
        stdout = ""
    ew._systemctl = lambda *a: Ok()
    ew._run = lambda cmd: Ok()
    ew.SCHEDULE_FILE = os.path.join(ew.STATE_ROOT, "schedule.json")
    try:
        account = ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg-1"), 0, "personal")
        account.ensure_state_dir()
        with open(account.session_id_file, "w") as f:
            f.write("abc-123\n")
        stamp_checkpoint(account)
        open(account.checkpoint_backup, "w").close()
        ew.write_state(account, {
            "last_run": now - 120,
            "consecutive_failures": 2,
            "boundary": now - 60,             # passed, waiting on the next ping
            "boundary_label": "weekly limit",
            "limits_source": "refusal-text",
            "available_at": now + 900,
            "hold": {"from": now - 10, "until": now + 3600, "reason": "spacing"},
            "rate_limits": {
                "five_hour": {"resets_at": now + 900, "used_percentage": 100},
                "seven_day": {"resets_at": now + 86400, "used_percentage": 41}}})

        buf = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        try:
            code = ew.status([account])
        finally:
            sys.stdout = out
        said = buf.getvalue()

        check("it succeeds", code, 0)
        check_true("the checkpoint is named, so a missing one is obvious",
                   "Checkpoint    : abc-123" in said)
        check_true("a run of failed pings is surfaced",
                   "Failed pings  : 2 in a row" in said)
        check_true("both limits are shown with what is left of them",
                   "5-hour window : 100% used" in said
                   and "Weekly limit  : 41% used" in said)
        check_true("an account that cannot be used says when it can",
                   "Usable again  :" in said and "5-hour limit is spent" in said)
        check_true("a boundary already gone says it is waiting, not that it is due",
                   "passed, awaiting next ping" in said)
        check_true("and says which limit set it, and where that came from",
                   "set by the weekly limit" in said
                   and "[via refusal-text]" in said)
        check_true("a hold in force is stated with its reason",
                   "Holding       : until" in said and "spacing" in said)
    finally:
        ew._systemctl, ew._run, ew.SCHEDULE_FILE = saved


def test_spacing_optimiser():
    section("The cheapest way to space windows evenly")
    W = ew.WINDOW_HOURS * HOUR

    def plan(**phases):
        return ew.plan_alignment(phases, W)

    delays, total = plan(a=0.0)
    check("one account has nothing to space", (delays, total), ({"a": 0.0}, 0.0))

    delays, total = plan(a=0.0, b=2.5 * HOUR)
    check("already evenly spaced -> no delay at all", total, 0.0)

    # The worked example: b drifts 20 minutes late. Dragging b round to the next
    # slot costs 4h40m; nudging a costs 20 minutes. The account that did *not*
    # drift is the one to hold.
    delays, total = plan(a=0.0, b=2.5 * HOUR + 20 * 60)
    check("correcting drift holds the account that did not drift",
          round(delays["a"] / 60), 20)
    check("and leaves the drifted one alone", delays["b"], 0.0)
    check("for a total of 20 minutes, not 4h40m", round(total / 60), 20)

    # Two accounts that restarted together after an outage: the worst case, and
    # the reason a correction this size has to be asked for rather than assumed.
    delays, total = plan(a=0.0, b=0.0)
    check("fully synced accounts cost half a window to separate",
          round(total / 60), 150)
    check("and only one of them is held", sorted(delays.values()), [0.0, 2.5 * HOUR])

    # Three accounts want 1h40m spacing, not 2h30m.
    delays, total = plan(a=0.0, b=W / 3, c=2 * W / 3)
    check("three already-spaced accounts need no correction", round(total), 0)
    delays, total = plan(a=0.0, b=0.0, c=0.0)
    check("three synced accounts cost 1h40m + 3h20m", round(total / 60), 300)

    # Never advance: every delay must be forward, and inside one window.
    for phases in ({"a": 0.0, "b": 1.0 * HOUR},
                   {"a": 4.9 * HOUR, "b": 0.1 * HOUR},
                   {"a": 0.0, "b": 1.0 * HOUR, "c": 3.0 * HOUR, "d": 4.0 * HOUR}):
        delays, total = ew.plan_alignment(phases, W)
        check_true("delays are forward-only and under a window ({})".format(
            sorted(phases)), all(0 <= d < W + 1e-6 for d in delays.values()))
        spaced = sorted(((phases[n] + delays[n]) % W) for n in phases)
        gaps = [round((spaced[(i + 1) % len(spaced)] - spaced[i]) % W)
                for i in range(len(spaced))]
        check("the result really is evenly spaced ({})".format(sorted(phases)),
              set(gaps), {round(W / len(phases))})

    # Brute force says the same thing, which is the real check on the shortcut
    # of only trying the offsets that zero one account.
    import random
    random.seed(7)
    worst = 0.0
    for _ in range(200):
        count = random.choice([2, 3, 4])
        phases = {str(i): random.uniform(0, W) for i in range(count)}
        _, chosen = ew.plan_alignment(phases, W)
        spacing = W / count
        best = min(
            sum(((offset + i * spacing) - phases[n]) % W
                for i, n in enumerate(sorted(phases, key=lambda k: phases[k])))
            for offset in [x * W / 2000.0 for x in range(2000)])
        worst = max(worst, chosen - best)
    check_true("it matches an exhaustive search of offsets (within {:.0f}s)"
               .format(worst), worst < W / 1000.0)


def test_phase_is_lost_only_when_pings_cannot_get_through():
    """
    Who counts as N in the 5/N spacing.

    One question decides it: will this account start a window at its own next
    boundary? Getting it wrong is expensive in a way that never announces itself
    — a dead account holding a slot bunches the live ones into part of the day,
    and everything still looks like it is working.
    """
    section("An account keeps its place only while its pings land")
    now = time.time()
    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    account = ew.Account("a", os.path.join(root, "cfg-a"), 0)

    def state(expires_in, available_in=None, five_pct=None,
              weekly=None, weekly_pct=None):
        limits = {"five_hour": {"resets_at": now + expires_in,
                                "used_percentage": five_pct}}
        if weekly is not None:
            limits["seven_day"] = {"resets_at": now + weekly,
                                   "used_percentage": weekly_pct}
        s = {"rate_limits": limits}
        if available_in is not None:
            s["available_at"] = now + available_in
        return s

    def holds(s):
        return ew.is_participating(account, s, now)

    check_true("a healthy account participates", holds(state(3600, -1)))

    # Draining the 5-hour quota does not break the phase: the window still ends
    # on time and the boundary ping starts the next one. This is the case that
    # must *not* be excluded — the account is unusable now and still supplies a
    # window at exactly the moment the spacing is reserving a slot for.
    check_true("being out of quota until the boundary still participates",
               holds(state(3600, 3500)))
    check_true("and the same when it is the reported percentage that says so",
               holds(state(3600, -1, five_pct=100)))

    # A refusal that outlasts the boundary does break it — the window ends with
    # nothing getting through, so no new one begins.
    check_true("being unusable past the boundary loses the phase",
               not holds(state(3600, 7200)))
    check_true("a spent weekly limit loses it, refusal or no refusal",
               not holds(state(3600, -1, weekly=3 * 86400, weekly_pct=100)))
    check("and says why",
          ew.participation(account, state(3600, -1, weekly=3 * 86400,
                                          weekly_pct=100), now)[1],
          "its weekly limit is spent, which outlasts its current window")

    check_true("an account with no window information does not participate",
               not holds({}))

    # Silence is not a phase. Once a whole window has passed with no ping getting
    # through, the next window started at a moment nobody observed, so the
    # recorded phase is a guess — and spacing the others around a guess costs
    # real dead time.
    check_true("an account silent for a whole window loses its place",
               not holds(state(3600, -(ew.WINDOW_HOURS * 3600 + 60))))
    check_true("but one that answered within the window keeps it",
               holds(state(3600, -(ew.WINDOW_HOURS * 3600 - 60))))

    # Two different silences, and telling somebody the wrong one wastes their
    # afternoon. An account that has answered before and stopped is waited
    # out; one that has never answered at all is a setup that has never
    # worked, and no amount of waiting fixes it.
    quiet = state(3600, -(ew.WINDOW_HOURS * 3600 + 60))
    check("an account that fell silent says when it last answered",
          ew.participation(account, quiet, now)[1],
          "nothing has got through since {}".format(
              ew.fmt_time(quiet["available_at"])))
    check("one whose first ping has not landed yet says only that",
          ew.participation(account, state(3600), now)[1],
          "no ping has got through yet")
    # Once they have been failing, there is something for `doctor` to say, and
    # this is where somebody is when they need telling.
    check("one whose pings keep failing is sent to the command that explains",
          ew.participation(account,
                           dict(state(3600),
                                consecutive_failures=ew.UNHEALTHY_AFTER),
                           now)[1],
          "no ping has ever got through — see `{} doctor`".format(ew.COMMAND))

    # The reason no waiting fixes. Its files say no request can succeed, so no
    # boundary of its own is worth reserving a slot for.
    os.makedirs(account.config_dir, 0o700)
    with open(account.config_json, "w") as f:
        json.dump({"oauthAccount": {"accountUuid": "a"}}, f)
    with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
        json.dump({"claudeAiOauth": {"accessToken": "t",
                                     "subscriptionType": "free"}}, f)
    check_true("an account with no paid subscription is out of the rotation",
               not holds(state(3600, -1)))
    check("and says why", ew.participation(account, state(3600, -1), now)[1],
          "no paid subscription (free)")


def test_correction_policy():
    section("Small corrections happen; expensive ones are proposed")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    W = ew.WINDOW_HOURS * HOUR
    a = ew.Account(TEST_PREFIX + "-a", "/tmp/cfg-a", 0)
    b = ew.Account(TEST_PREFIX + "-b", "/tmp/cfg-b", 1)
    a.ensure_state_dir(); b.ensure_state_dir()

    def states(offset):
        return {a.name: {"rate_limits": {"five_hour": {"resets_at": now + 600}},
                         "available_at": now - 1},
                b.name: {"rate_limits": {"five_hour": {"resets_at": now + 600 + offset}},
                         "available_at": now - 1}}

    def settle():
        # Pretend the current set of accounts has been steady for a while.
        alignment = ew.read_alignment()
        alignment["participants_since"] = now - 2 * W
        ew.write_alignment(alignment)

    # Noise is left alone: chasing it would pay real dead time for nothing.
    st = states(2.5 * HOUR + 60)
    ew.alignment_plan([a, b], st, now); settle()
    check("a minute of drift is inside the deadband",
          ew.apply_alignment(a, [a, b], st, st[a.name], now, now + 600), 0.0)

    # A correction worth minutes just happens.
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], st, now); settle()
    delay = ew.apply_alignment(a, [a, b], st, st[a.name], now, now + 600)
    check("20 minutes is corrected without asking", round(delay / 60), 20)
    check_true("and the hold is recorded on the account", "hold" in st[a.name])
    # The hold has to hang off the boundary the caller passed in, not one this
    # function worked out for itself — the two differ whenever a weekly limit is
    # the thing standing in the way.
    check("the hold starts at the boundary it was given",
          round(st[a.name]["hold"]["from"] - now), 600)
    check("and ends the delay later",
          round(st[a.name]["hold"]["until"] - now), 600 + 20 * 60)

    # A correction worth hours is described and left for the user to approve.
    st = states(0.0)                     # both accounts fully synced
    ew.alignment_plan([a, b], st, now); settle()
    check("a multi-hour hold is not applied unasked",
          ew.apply_alignment(b, [a, b], st, st[b.name], now, now + 600), 0.0)
    said = open(b.log_file).read()
    check_true("it says so in the log instead of acting", "too long to do "
               "unasked" in said)
    check_true("with the real cost attached, and how to apply it",
               "2h30m00s in total" in said and "realign --confirm" in said)
    check_true("and nothing was booked", "hold" not in st[b.name])

    # Hysteresis: an account dropping out and coming back changes the ideal
    # spacing for everyone twice over, so the set has to hold steady before
    # that moves the target. `ever` is what says it has been here before.
    ew.write_alignment({"participants": [b.name], "ever": [a.name, b.name],
                        "participants_since": now - 10 * W})
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], st, now)   # an account came back
    check("an account returning to the set is not acted on at once",
          ew.apply_alignment(a, [a, b], st, st[a.name], now, now + 600), 0.0)

    # Losing one is the case that costs most to get wrong, and it is never a
    # first sighting however new the accounts are.
    ew.write_alignment({"participants": [a.name, b.name], "ever": [a.name, b.name],
                        "participants_since": now - 10 * W})
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], {a.name: st[a.name],
                               b.name: dict(st[b.name], available_at=None)}, now)
    check("an account dropping out is not acted on at once",
          ew.apply_alignment(a, [a, b], st, st[a.name], now, now + 600), 0.0)

    # An install upgraded from before `ever` existed has no history recorded,
    # but its current participants have obviously been seen — reading them as
    # new would hand a free re-space to the one account that must not get one.
    ew.write_alignment({"participants": [a.name, b.name],
                        "participants_since": now - 10 * W})
    ew.alignment_plan([a, b], {a.name: states(0)[a.name],
                               b.name: dict(states(0)[b.name],
                                            available_at=None)}, now)
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], st, now)   # b comes back
    check("an upgraded install does not treat its own accounts as new",
          ew.apply_alignment(a, [a, b], st, st[a.name], now, now + 600), 0.0)

    # But an account nobody has ever seen cannot be thrashing. Without this a
    # two-account install would restart its own clock on the second ping and
    # sit misaligned for a whole window on a setup minutes old.
    ew.write_alignment({"participants": [a.name], "ever": [a.name],
                        "participants_since": now - 10 * W})
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], st, now)   # b is seen for the first time
    check("an account seen for the first time is acted on straight away",
          round(ew.apply_alignment(a, [a, b], st, st[a.name], now,
                                   now + 600) / 60), 20)
    check_true("and it is remembered, so a later return has to settle",
               a.name in ew.read_alignment()["ever"]
               and b.name in ew.read_alignment()["ever"])

    # But a brand-new install has no earlier arrangement to thrash against, and
    # has paid for nothing, so it should not sit visibly misaligned for a whole
    # window before anyone is told.
    ew.write_alignment({})
    st = states(2.5 * HOUR + 20 * 60)
    ew.alignment_plan([a, b], st, now)   # first sighting ever
    check("a first-ever reading is acted on straight away",
          round(ew.apply_alignment(a, [a, b], st, st[a.name], now,
                                   now + 600) / 60), 20)

    # The verdict the user reads has to match what the tool will actually do.
    def verdict(offset, **kw):
        ew.write_alignment({})
        st2 = states(offset)
        ew.alignment_plan([a, b], st2, now)
        return " ".join(ew.describe_alignment([a, b], st2, now, **kw)[0])

    check_true("a correction worth minutes is described as automatic",
               "without asking" in verdict(2.5 * HOUR + 20 * 60))
    check_true("a correction worth hours never claims it is automatic",
               "without asking" not in verdict(0.0))
    check_true("and says what it would cost instead",
               "too much to do unasked" in verdict(0.0))
    # `realign` prints its own, fuller version of that advice; it must not also
    # fall through to claiming the correction happens by itself.
    quiet = verdict(0.0, suggest_realign=False)
    check_true("suppressing the pointer does not flip the verdict",
               "without asking" not in quiet and "realign" not in quiet)
    check_true("correct spacing is simply reported as correct",
               "Spacing is correct" in verdict(2.5 * HOUR))

    # A booked hold leaves the phases where they were, so the arithmetic still
    # shows the full error. Reporting only that would read as though the
    # correction the user just approved had not happened.
    ew.write_alignment({})
    st3 = states(0.0)
    st3[a.name]["hold"] = {"from": now + 600, "until": now + 600 + 2.5 * HOUR,
                           "reason": "realigning"}
    ew.alignment_plan([a, b], st3, now)
    reported = ew.describe_alignment([a, b], st3, now)[0]
    check_true("a booked correction is acknowledged",
               any("already booked" in line for line in reported))
    # Only the account actually holding: the other one, with both windows
    # synced, still legitimately needs a hold of its own.
    held_line = [l for l in reported if a.display in l][0]
    check_true("the held account is shown as held, not as needing a hold",
               "already held back to" in held_line and "to line up" not in held_line)

    # A weekly limit puts the next usable moment days past the 5-hour boundary.
    # Nothing may be scheduled relative to the wrong one of those two.
    blocked = {a.name: {"rate_limits": {"five_hour": {"resets_at": now + 600}},
                        "available_at": now + 3 * 86400},
               b.name: states(2.5 * HOUR)[b.name]}
    ew.alignment_plan([a, b], blocked, now); settle()
    check("an account blocked past its own boundary is left out of the spacing",
          ew.apply_alignment(a, [a, b], blocked, blocked[a.name], now,
                             now + 3 * 86400), 0.0)

    # And the point of leaving it out: N is the accounts that will actually
    # start a window, so the ones that still supply windows get the whole
    # 5 hours between them rather than being bunched into 5/3 of it.
    c = ew.Account(TEST_PREFIX + "-c", "/tmp/cfg-c", 2)
    c.ensure_state_dir()
    three = dict(states(1.0 * HOUR))
    three[c.name] = {"rate_limits": {"five_hour": {"resets_at": now + 900},
                                     "seven_day": {"resets_at": now + 4 * 86400,
                                                   "used_percentage": 100}},
                     "available_at": now - 1}
    ew.write_alignment({})
    said = " ".join(ew.describe_alignment([a, b, c], three, now)[0])
    check_true("with one of three accounts out, the target is 5/2 not 5/3",
               "2h30m00s apart" in said)
    check_true("and it says how many accounts are actually holding one",
               "2 of 3 accounts" in said)
    check_true("and names the reason the third is not",
               "weekly limit is spent" in said)


def test_realign_is_the_only_way_a_long_hold_happens():
    """
    `realign --confirm` is the one command that deliberately leaves an account
    with no window running, for hours. Nothing else in the tool will do that,
    which is exactly why what it does has to be pinned down: that it says the
    price before it asks, that it does nothing at all without --confirm, and
    that having been told once it stops asking.
    """
    section("realign: the correction you have to ask for")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    W = ew.WINDOW_HOURS * HOUR
    a = ew.Account(TEST_PREFIX + "-a", "/tmp/cfg-a", 0)
    b = ew.Account(TEST_PREFIX + "-b", "/tmp/cfg-b", 1)
    a.ensure_state_dir(); b.ensure_state_dir()

    anchors = []
    saved_anchor = ew.schedule_anchor
    ew.schedule_anchor = lambda account, target: anchors.append(
        (account.name, target)) or True
    try:
        def arrange(offset):
            for account, at in ((a, now + 600), (b, now + 600 + offset)):
                ew.write_state(account, {"available_at": now - 1,
                                         "rate_limits": {"five_hour":
                                                         {"resets_at": at}}})
            ew.write_alignment({"participants": [a.name, b.name],
                                "ever": [a.name, b.name],
                                "participants_since": now - 2 * W})

        def run(**kw):
            buf = io.StringIO()
            out, sys.stdout = sys.stdout, buf
            try:
                ew.realign([a, b], **kw)
            finally:
                sys.stdout = out
            return buf.getvalue()

        # Already spaced: there is nothing to offer, and no price to quote.
        arrange(2.5 * HOUR)
        said = run()
        check_true("correct spacing is reported and nothing is offered",
                   "Spacing is correct" in said and "--confirm" not in said)

        # Out of step: the cost comes before the offer, every time.
        arrange(0.0)
        said = run()
        check_true("the cost is stated in hours of dead window",
                   "costs 2h30m00s in total with no window running" in said)
        check_true("and it asks rather than acts", "Re-run with --confirm" in said)
        check("nothing was booked without --confirm",
              [ew.read_state(x).get("hold") for x in (a, b)], [None, None])
        check("and no anchor was moved", anchors, [])

        # Asked for: exactly one account is held, and only as far as it must be.
        said = run(confirm=True)
        holds = {x.name: ew.read_state(x).get("hold") for x in (a, b)}
        held = [n for n, h in holds.items() if h]
        check("one account is held, not both", len(held), 1)
        check("held by the spacing, no more",
              round((holds[held[0]]["until"] - holds[held[0]]["from"]) / 60), 150)
        check_true("the reason recorded says whose decision it was",
                   "your say-so" in holds[held[0]]["reason"])
        check_true("and it says when the window will now start",
                   "next window will start" in said)
        # The window cannot begin until the hold ends, so the anchor has to be
        # placed there and not on the untouched boundary.
        check("an anchor is booked for the end of the hold, plus the guard",
              [round(t - holds[held[0]]["until"]) for n, t in anchors
               if n == held[0]], [ew.find_account([a, b], held[0]).guard_sec])

        # Having been told once, the tool stops asking. This is the half of it
        # that used to go wrong: `status` said "already booked" while every
        # ping went on telling the user to confirm what they just confirmed.
        state = ew.read_state(ew.find_account([a, b], held[0]))
        states = {x.name: ew.read_state(x) for x in (a, b)}
        account = ew.find_account([a, b], held[0])
        before = os.path.getsize(account.log_file) if os.path.exists(
            account.log_file) else 0
        extra = ew.apply_alignment(account, [a, b], states, state, time.time(),
                                   ew.next_expiry(state, time.time()))
        after = _read_from(account.log_file, before)
        check_true("a booked hold is not re-proposed on the next ping",
                   "realign --confirm" not in after)
        check("and the anchor still lands at the end of the hold",
              round(ew.next_expiry(state, time.time()) + extra
                    - state["hold"]["until"]), 0)
        check_true("the hold itself is left exactly as it was",
                   ew.read_state(account).get("hold") == holds[held[0]]
                   or state["hold"] == holds[held[0]])
    finally:
        ew.schedule_anchor = saved_anchor


def _read_from(path, offset):
    """Whatever was appended to a log after `offset` bytes."""
    try:
        with open(path) as f:
            f.seek(offset)
            return f.read()
    except (IOError, OSError):
        return ""


def test_the_json_report_is_a_contract():
    """
    `status --json` is documented as the scripting interface, so its shape is a
    promise to somebody else's code. Checked for the keys that promise names,
    for the tiers travelling as words rather than as this module's integers,
    and for the one thing that would make it unusable: commentary on stdout.
    """
    section("status --json is a contract")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    # Hermetic on purpose: `switching` reports on the user's own files, and a
    # test that read the developer's real ~/.claude would pass or fail
    # depending on who ran it.
    saved_user = (ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT,
                  ew.SCHEDULE_FILE)
    ew.USER_CONFIG_DIR = os.path.join(ew.STATE_ROOT, "home", ".claude")
    ew.USER_CONFIG_JSON = os.path.join(ew.STATE_ROOT, "home", ".claude.json")
    ew.SWITCH_ROOT = os.path.join(ew.STATE_ROOT, "home", ".claude-switch")
    ew.SCHEDULE_FILE = os.path.join(ew.STATE_ROOT, "schedule.json")
    a = ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg-1"), 0, "personal")
    b = ew.Account("2", os.path.join(ew.STATE_ROOT, "cfg-2"), 1, "work")
    a.ensure_state_dir(); b.ensure_state_dir()
    ew.write_state(a, {"last_run": now - 60, "available_at": now - 60,
                       "boundary": now + 600, "boundary_label": "5-hour window",
                       "limits_source": "statusline",
                       "rate_limits": {"five_hour": {"resets_at": now + 600,
                                                     "used_percentage": 12}}})
    ew.write_state(b, {"last_run": now - 60,
                       "rate_limits": {"five_hour": {"resets_at": now + 9000,
                                                     "used_percentage": 100}}})

    buf = io.StringIO()
    out, sys.stdout = sys.stdout, buf
    try:
        code = ew.status_json([a, b])
    finally:
        sys.stdout = out
    check("it succeeds", code, 0)

    document = json.loads(buf.getvalue())      # fails loudly if anything else printed
    check("the top level says what it is and what to do",
          sorted(document),
          ["accounts", "generated_at", "published_at", "switching", "use"])
    check("figures of this machine's own are not attributed to another",
          document["published_at"], None)
    check("including which account the user's own Claude Code is on",
          sorted(document["switching"]), ["configured", "current_account"])
    check("reported as unconfigured until a store exists",
          document["switching"], {"configured": False, "current_account": None})
    check("the recommendation names an account and its directory",
          sorted(document["use"]), ["account", "config_dir", "reason"])
    check("it recommends the account that can actually be used",
          document["use"]["account"], "1")

    entries = {e["name"]: e for e in document["accounts"]}
    check("every account is reported", sorted(entries), ["1", "2"])
    for name in ("1", "2"):
        check("account {} carries the documented keys".format(name),
              sorted(entries[name]),
              ["available_at", "boundary", "boundary_label", "checkpoint",
               "config_dir", "consecutive_failures", "expires_at", "hold",
               "label", "last_run", "limits_read_at", "limits_source", "name",
               "rate_limits", "tier", "unusable_because", "unusable_until",
               "unusable_until_exact", "usable_now"])
    # The integers are an implementation detail; a caller should never see one.
    check("tiers travel as words", [entries[n]["tier"] for n in ("1", "2")],
          ["usable", "waiting"])
    (ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT,
     ew.SCHEDULE_FILE) = saved_user
    check("and agree with the boolean beside them",
          [entries[n]["usable_now"] for n in ("1", "2")], [True, False])
    check_true("an account that cannot be used says why in words",
               "limit is spent" in (entries["2"]["unusable_because"] or ""))
    check("a missing checkpoint is null, not an empty string",
          entries["1"]["checkpoint"], None)
    # A caller cannot judge the figures without knowing their age, which is
    # the same thing `which` says in words at the bottom of its answer.
    check("and the figures are dated, so a script can judge them too",
          entries["1"]["limits_read_at"], now - 60)


def test_what_a_ping_records_from_how_it_went():
    """
    Everything downstream — when the next window can start, whether the account
    is worth recommending, whether it counts towards the spacing — is read back
    out of what one ping wrote down. There are three ways a ping can go and
    each records something different, so each is walked here with the
    conversation itself stubbed out.

    The distinction that matters most is the last two. A refusal is Claude
    answering: it says when the account is back, and it is not evidence of
    anything being broken. Silence is not an answer at all, says nothing about
    the account, and only counts against it if it keeps happening.
    """
    section("What a ping writes down, for each way it can go")
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    saved = (ew.run_interactive, ew.read_statusline_limits, ew.schedule_anchor,
             ew.restore_checkpoint, ew._systemctl, ew._run)
    class Ok(object):
        returncode = 0
        stdout = ""
    ew.schedule_anchor = lambda account, target: True
    ew.restore_checkpoint = lambda account, session: None
    ew._systemctl = lambda *a: Ok()
    ew._run = lambda cmd: Ok()
    try:
        account = temp_account(TEST_PREFIX)
        ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
        # Signed in on a paid plan, so what the tiers below say comes from the
        # pings and not from the account's own files having something to add.
        os.makedirs(account.config_dir, 0o700)
        with open(account.config_json, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "u"}}, f)
        with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "t",
                                         "subscriptionType": "max"}}, f)
        with open(account.session_id_file, "w") as f:
            f.write("sess\n")
        stamp_checkpoint(account)
        open(account.checkpoint_backup, "w").close()

        def ping_with(result, limits):
            ew.run_interactive = lambda *a, **k: result
            ew.read_statusline_limits = lambda a, records=None: limits
            ew.ping(account)
            return ew.read_state(account)

        now = time.time()
        # 1. It got through. The statusLine is exact, so it is believed, and
        #    the account has just proved it can serve a request.
        state = ping_with({"completed": True, "limited": False, "text": "ok"},
                          {"five_hour": {"resets_at": now + 600,
                                         "used_percentage": 7}})
        check("a successful ping records the reset it was told",
              state["rate_limits"]["five_hour"]["resets_at"], now + 600)
        check("names the statusLine as where that came from",
              state["limits_source"], "statusline")
        # A live reading taken earlier leaves its own timestamp in state. Not
        # replacing it here made every later command report figures this ping
        # had just refreshed as up to half an hour old, and offer to refresh
        # what it had.
        check_true("and dates them, so nothing reports them as older than they are",
                   abs(state["limits_read_at"] - time.time()) < 5)
        check("and the boundary it implies", round(state["boundary"] - now), 600)
        check_true("availability is proved by the ping itself, dated now",
                   abs(state["available_at"] - time.time()) < 5)
        check("with the failure counter cleared",
              state["consecutive_failures"], 0)

        # 2. It was refused. No statusLine figures at all, so the refusal text
        #    is the only source — and the account is unusable until it resets.
        # Pinned rather than read, so these two run everywhere: a machine
        # whose zone name cannot be read used to skip them silently.
        restore_zone = _pretend_timezone()
        refusal = "You've hit your session limit · resets 9:30pm ({})".format(ZONE)
        state = ping_with({"completed": True, "limited": True, "text": refusal},
                          {})
        restore_zone()
        check("a refusal with no statusLine falls back on its own text",
              state["limits_source"], "refusal-text")
        check("and the account is unusable until exactly then",
              state["available_at"], state["boundary"])
        check("a refusal is an answer, so nothing counts as a failure",
              state["consecutive_failures"], 0)

        # 3. No answer at all. That is a local problem until it repeats, so
        #    nothing about the account's availability may be touched.
        was = state.get("available_at")
        state = ping_with({"completed": False, "limited": False, "text": ""}, {})
        check("silence counts once against the account",
              state["consecutive_failures"], 1)
        check("and says nothing about when it is usable",
              state.get("available_at"), was)
        state = ping_with({"completed": False, "limited": False, "text": ""}, {})
        check("and again, because it is the repetition that matters",
              state["consecutive_failures"], 2)
        state = ping_with({"completed": False, "limited": False, "text": ""}, {})
        check("until enough of them have piled up to mean something",
              state["consecutive_failures"], ew.UNHEALTHY_AFTER)

        # Even then, silence does not overwrite an answer Claude gave. The
        # refusal above said when this account is back, and "cannot tell"
        # is a worse answer than that, not a newer one.
        check("a known return time still outranks a run of silence",
              ew.account_availability(account, state, time.time()).tier,
              ew.WAITING)
        forgotten = dict(state)
        forgotten.pop("available_at")
        check("with nothing known at all, silence is all there is to report",
              ew.account_availability(account, forgotten, time.time()).tier,
              ew.UNKNOWN)
        check_true("and the log said the turn was never confirmed",
                   "did not confirm a completed turn" in open(account.log_file).read())
    finally:
        (ew.run_interactive, ew.read_statusline_limits, ew.schedule_anchor,
         ew.restore_checkpoint, ew._systemctl, ew._run) = saved


def test_a_hold_suppresses_the_ping_and_nothing_else():
    section("A hold skips the ping, and only for timing")
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    now = time.time()
    account = temp_account()
    with open(account.session_id_file, "w") as f:
        f.write("sid")
    stamp_checkpoint(account)
    with open(account.checkpoint_backup, "w") as f:
        f.write("{}\n")

    check("a hold in the future has not started yet",
          ew.active_hold({"hold": {"from": now + 60, "until": now + 600}}, now),
          None)
    check("a hold that has run out is over",
          ew.active_hold({"hold": {"from": now - 600, "until": now - 60}}, now),
          None)
    check_true("a hold spanning now is active",
               ew.active_hold({"hold": {"from": now - 60, "until": now + 600}},
                              now))

    calls = []
    original = ew.run_interactive
    ew.run_interactive = lambda *a, **k: calls.append(a) or {
        "completed": True, "limited": False, "text": ""}
    try:
        ew.write_state(account, {"hold": {"from": now - 60, "until": now + 600,
                                          "reason": "test"}})
        ew.ping(account, [account])
        check("no ping is sent while holding", calls, [])
        check_true("and the hold survives the run",
                   "hold" in ew.read_state(account))

        ew.write_state(account, {"hold": {"from": now - 600, "until": now - 60}})
        ew.ping(account, [account])
        check("once the hold is over the ping goes out", len(calls), 1)
        check_true("and the spent hold is cleared",
                   "hold" not in ew.read_state(account))

        # A hold booked for a future boundary — by `realign --confirm` — has to
        # survive every ordinary ping between now and then. Clearing it on the
        # next tick would discard the correction with nothing to show for it.
        ew.write_state(account, {"hold": {"from": now + 3600, "until": now + 7200,
                                          "reason": "realigning"}})
        ew.ping(account, [account])
        check("a ping still goes out before a future hold starts", len(calls), 2)
        check_true("and the pending hold is left alone",
                   ew.read_state(account).get("hold", {}).get("until")
                   == now + 7200)
    finally:
        ew.run_interactive = original
        # A hold schedules a real transient timer that would start the ping
        # service for this account name. Leave nothing armed.
        ew.cancel_anchor(account)


def test_an_unusable_account_is_still_pinged():
    """
    The recovery path, and the one thing that must never follow from "unusable".

    Everything else in the tool reacts to an account being out of action: it is
    not recommended, and it is dropped from the spacing. If the pings stopped
    too, none of that could ever be undone — a spent weekly limit, a renewed
    subscription or a re-login would be invisible, because the only thing that
    ever proves an account is back is an ordinary ping getting through. So the
    ping keeps going, on the ordinary schedule, whatever the account looks like.
    """
    section("An account that cannot be used is still pinged")
    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    now = time.time()
    account = temp_account()
    with open(account.session_id_file, "w") as f:
        f.write("sid")
    stamp_checkpoint(account)
    with open(account.checkpoint_backup, "w") as f:
        f.write("{}\n")

    # Everything that makes an account unusable, at once: no paid plan, a spent
    # weekly limit days out, a refusal on record, and a run of failed pings.
    os.makedirs(account.config_dir, 0o700)
    with open(account.config_json, "w") as f:
        json.dump({"oauthAccount": {"accountUuid": "u"}}, f)
    with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
        json.dump({"claudeAiOauth": {"accessToken": "t",
                                     "subscriptionType": "free"}}, f)
    unusable = {"available_at": now + 4 * 86400,
                "consecutive_failures": ew.UNHEALTHY_AFTER + 2,
                "rate_limits": {
                    "five_hour": {"resets_at": now + 600, "used_percentage": 100},
                    "seven_day": {"resets_at": now + 4 * 86400,
                                  "used_percentage": 100}}}

    avail = ew.account_availability(account, unusable, now)
    check_true("the account is genuinely unusable by every test",
               avail.tier != ew.USABLE
               and not ew.is_participating(account, unusable, now))

    calls = []
    original = ew.run_interactive
    ew.run_interactive = lambda *a, **k: calls.append(a) or {
        "completed": True, "limited": False, "text": ""}
    try:
        ew.write_state(account, unusable)
        ew.ping(account, [account])
        check("it is pinged anyway", len(calls), 1)
        # And the ping is what puts it straight back: a turn that completed is
        # proof of usability, recorded as such with no further ceremony.
        recovered = ew.read_state(account)
        check_true("a ping that gets through clears the block on the spot",
                   recovered["available_at"] <= time.time()
                   and recovered["consecutive_failures"] == 0)
    finally:
        ew.run_interactive = original
        ew.cancel_anchor(account)

    # The other half of the same promise, and the one that cannot be tested by
    # running anything: no code path anywhere stops a timer because an account
    # stopped being usable. Timers are only ever disabled by the two functions
    # that tear down accounts the user removed or uninstalled.
    source = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "claude_window_timing.py")).read()
    stoppers = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "id", "") == "_systemctl"
                    and any(_literal_value(a) == "disable"
                            for a in inner.args)):
                stoppers.add(node.name)
    check("only the teardown paths ever disable a timer",
          sorted(stoppers), ["install_units", "uninstall"])


def test_the_log_reads_in_the_order_it_was_written():
    """
    The merged log is a record of what happened, so its order has to be the
    order it happened in. A run writes several lines in the same second, and
    sorting whole lines alphabetises those — which puts "run finished" above the
    "Exited with code" that came before it, and reads like a different story.
    """
    section("The merged log keeps each run's own order")
    root = tempfile.mkdtemp()
    ew.STATE_ROOT = os.path.join(root, "state")
    first, second = ew.Account("1", "/tmp/cfg-1", 0), ew.Account("2", "/tmp/cfg-2", 1)
    for account, body in (
            (first, ["[2026-08-13 00:01:43] Turn confirmed: cache_read=6636",
                     "[2026-08-13 00:01:43] Usage: 5-hour 0%",
                     "[2026-08-13 00:01:43] Exited with code: 0",
                     "[2026-08-13 00:01:43] Ping run finished.",
                     ""]),
            (second, ["[2026-08-13 00:02:00] Starting ping run"])):
        account.ensure_state_dir()
        with open(account.log_file, "w") as f:
            f.write("\n".join(body) + "\n")

    buf = io.StringIO()
    saved, sys.stdout = sys.stdout, buf
    try:
        ew.show_log([first, second], None, 40, False)
    finally:
        sys.stdout = saved
    shown = [line.split("] ", 1)[1] for line in buf.getvalue().splitlines()]

    check("each run's lines stay in the order they were written",
          shown[:4], ["Turn confirmed: cache_read=6636", "Usage: 5-hour 0%",
                      "Exited with code: 0", "Ping run finished."])
    check("and later accounts still interleave by time",
          shown[-1], "Starting ping run")
    check("blank separators are not carried into the merged view",
          [line for line in buf.getvalue().splitlines() if not line.strip()], [])

    # `entries[-n:]` counts from the wrong end when n is not positive: -5 asks
    # for the oldest five lines of a log people read to see what just happened,
    # and 0 asks for all of them.
    for count, wanted in ((1, 1), (0, 0), (-5, 0)):
        buf = io.StringIO()
        saved, sys.stdout = sys.stdout, buf
        try:
            ew.show_log([first, second], None, count, False)
        finally:
            sys.stdout = saved
        check("asking for {} lines shows {}".format(count, wanted),
              len(buf.getvalue().splitlines()), wanted)


# ---------------------------------------------------------------------------
# Setup and diagnosis
# ---------------------------------------------------------------------------

def test_setup_lays_accounts_out_sensibly():
    section("What setup proposes, and what it writes")
    ew.STATE_ROOT = tempfile.mkdtemp()
    saved, ew.ACCOUNTS_FILE = ew.ACCOUNTS_FILE, os.path.join(ew.STATE_ROOT,
                                                             "accounts.json")
    try:
        planned = ew._plan_accounts([], 2)
        check("names are simple and ordered", [a.name for a in planned], ["1", "2"])
        # No account lives where the user works; every one is a ping directory.
        check("the first account is its own ping directory",
              planned[0].config_dir, ew.ping_config_dir("1"))
        check_true("and it is not the user's directory",
                   planned[0].config_dir != ew.USER_CONFIG_DIR)
        check("later accounts get their own directories",
              planned[1].config_dir, os.path.join(ew.HOME, ".claude-2"))
        check("slots follow the order given", [a.index for a in planned], [0, 1])

        # Re-running setup must not renumber or relocate what already exists.
        existing = [ew.Account("main", "~/.claude", 0, "work")]
        planned = ew._plan_accounts(existing, 3)
        check("existing accounts keep their names",
              [a.name for a in planned], ["main", "2", "3"])
        check("and their labels", planned[0].label, "work")

        # Names come from the slot, so an existing account that already
        # answers to the next number has to push it along. Written out
        # unchecked, the pair would be a file the loader refuses.
        planned = ew._plan_accounts([ew.Account("2", "~/.claude-2", 0)], 2)
        check("a new account never collides with one already there",
              [a.name for a in planned], ["2", "3"])
        check("and gets a directory of its own with it",
              planned[1].config_dir, ew.ping_config_dir("3"))

        planned = ew._plan_accounts(existing, 3)
        ew._write_accounts_file(planned)
        written = json.load(open(ew.ACCOUNTS_FILE))
        check("the file records every account", len(written["accounts"]), 3)
        # Written back with ~ so the file stays portable between machines.
        check("home-relative paths are written portably",
              written["accounts"][1]["config_dir"], "~/.claude-2")
        check("and it round-trips through the loader",
              [a.name for a in ew.load_accounts(ew.ACCOUNTS_FILE)],
              ["main", "2", "3"])
    finally:
        ew.ACCOUNTS_FILE = saved


def test_setup_and_init_refuse_the_obviously_wrong():
    """
    The wizard's guard rails, and the one `init` has. Both are things a person
    does by hand, in a hurry, once — which is exactly when a typo costs a
    rebuilt checkpoint and a restarted window phase.
    """
    section("Setup and init say no before they do harm")
    root = tempfile.mkdtemp()
    saved = (ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE, sys.stdin)
    ew.SCRIPT_DIR = root
    ew.STATE_ROOT = os.path.join(root, "state")
    ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
    try:
        def wizard(answers):
            buf = io.StringIO()
            out, sys.stdout = sys.stdout, buf
            sys.stdin = io.StringIO(answers)
            try:
                return ew.setup(), buf.getvalue()
            finally:
                sys.stdout = out

        code, said = wizard("three\n")
        check("a count that is not a number is refused", code, 2)
        check_true("saying what was wrong with it", "not a number" in said)

        code, said = wizard("0\n")
        check("zero accounts is refused", code, 2)
        check_true("with the reason", "at least one account" in said)

        # Four Pro subscriptions cost about a Max plan and cannot be pooled,
        # so this is worth saying once — but it is advice, not a veto.
        code, said = wizard("5\nn\n")
        check("more than four asks before going ahead", code, 0)
        check_true("and explains why it is asking",
                   "about the same as one Max plan" in said)
        check_true("nothing was written when the answer was no",
                   not os.path.exists(ew.ACCOUNTS_FILE))

        # init must never rebuild a checkpoint that already works: doing so
        # spends a ping and restarts the window phase it took days to settle.
        account = ew.Account("1", os.path.join(root, "cfg"), 0)
        account.ensure_state_dir()
        with open(account.session_id_file, "w") as f:
            f.write("kept-session\n")
        stamp_checkpoint(account)
        with open(account.checkpoint_backup, "w") as f:
            f.write("{}\n")
        buf = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        try:
            ew.init(account)
        finally:
            sys.stdout = out
        said = buf.getvalue()
        check_true("an existing checkpoint is reported, not replaced",
                   "already exists" in said and "kept-session" in said)
        check("and it is still the same one",
              open(account.session_id_file).read().strip(), "kept-session")
        check_true("with the way to rebuild it deliberately",
                   "delete" in said and account.state_dir in said)
    finally:
        ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE, sys.stdin = saved


def test_the_first_run_screen_says_the_things_that_stop_people():
    """
    The sign-in instructions are the most-read screen in the tool and the only
    one a stranger sees before deciding whether to trust it. Two sentences on
    it are load-bearing, and both exist because of a specific hesitation:
    signing in sends no message, so there is no bad moment to do it; and each
    directory needs its own login, so doing it cannot disturb the Claude Code
    they already use.

    The wizard harness elsewhere signs the accounts in first, so this screen
    never appears there. It appears here.
    """
    section("The first-run screen, for an account not yet signed in")
    root = tempfile.mkdtemp()
    saved = (ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE, ew.HOME, sys.stdin)
    ew.SCRIPT_DIR = root
    ew.STATE_ROOT = os.path.join(root, "state")
    ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
    ew.HOME = os.path.join(root, "home")
    os.makedirs(ew.HOME)
    try:
        buf = io.StringIO()
        out, sys.stdout = sys.stdout, buf
        sys.stdin = io.StringIO("2\ny\n\n")
        try:
            code = ew.setup()
        finally:
            sys.stdout = out
        said = buf.getvalue()

        check("setup stops rather than building on a bad footing", code, 1)
        check_true("and says so, instead of reporting success",
                   "Setup stopped" in said and "run it again" in said)
        check_true("the directories are named in full, one per account",
                   said.count("CLAUDE_CONFIG_DIR=") >= 2
                   and ew.ping_config_dir("2") in said)
        check_true("with the command spelled out to the end",
                   "then /login" in said)
        check_true("it says signing in starts no window, so there is no bad time",
                   "starts no usage" in said and "no wrong time" in said)
        check_true("and that a login here cannot disturb the one they use",
                   "log you out over there" in said)
        check_true("it never suggests the user's own directory",
                   os.path.join(ew.HOME, ".claude") + " " not in said)
    finally:
        ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE, ew.HOME, sys.stdin = saved


def _launcher_sandbox():
    """A home, a checkout's bin/, and a PATH nobody's real shell shares."""
    root = tempfile.mkdtemp()
    home = os.path.join(root, "home")
    os.makedirs(home)
    saved = (ew.HOME, ew.BIN_DIR, ew.link_dirs, ew.STATE_ROOT,
             ew.ACCOUNTS_FILE, ew.SCHEDULE_FILE, ew._systemctl, ew._run,
             os.environ.get("PATH"), os.environ.get("SHELL"), sys.stdin)
    ew.HOME = home
    ew.BIN_DIR = os.path.join(root, "repo", "bin")
    ew.STATE_ROOT = os.path.join(root, "repo", "state")
    ew.ACCOUNTS_FILE = os.path.join(root, "repo", "accounts.json")
    ew.SCHEDULE_FILE = os.path.join(root, "repo", "schedule.json")

    class Ok(object):
        returncode = 0
        stdout = ""

    ew._systemctl = lambda *a: Ok()
    ew._run = lambda cmd: Ok()
    os.environ["SHELL"] = "/bin/bash"
    os.environ["PATH"] = "/usr/bin:/bin"

    def restore():
        (ew.HOME, ew.BIN_DIR, ew.link_dirs, ew.STATE_ROOT, ew.ACCOUNTS_FILE,
         ew.SCHEDULE_FILE, ew._systemctl, ew._run, path, shell,
         sys.stdin) = saved
        for name, value in (("PATH", path), ("SHELL", shell)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(root, ignore_errors=True)

    return restore, root, home


def test_the_command_can_be_typed_after_installing():
    """
    An install that ends in "command not found" is not an install.

    Every instruction this tool gives -- in setup, in `which`, in `status`, in
    `doctor`'s hints -- begins with `claude-window`, and for a long time the
    installer only *printed* an `export PATH=...` line, which dies with the
    shell it was printed in. A new terminal knew nothing about it.

    A child process cannot change its parent's environment, so "works in the
    shell you installed from" has exactly one honest implementation: put the
    launcher in a directory that shell is already searching. That is what the
    link does, and it is why the fallback -- a line in a startup file, which
    only new shells read -- comes with the one command to run here and now.
    """
    section("Making `claude-window` typeable, here and in new shells")
    restore, root, home = _launcher_sandbox()
    try:
        launcher = ew.write_entry_point()
        check("the launcher is written inside the checkout",
              os.path.dirname(launcher), ew.BIN_DIR)
        # Resolved against HOME when asked. As a constant read at import it
        # named the real ~/.local/bin, and this suite would have linked into
        # the home of whoever ran it.
        check("the directories it may link into follow HOME",
              ew.link_dirs()[0], os.path.join(home, ".local", "bin"))

        # -- already reachable: say so, change nothing ---------------------
        os.environ["PATH"] = ew.BIN_DIR + ":/usr/bin"
        check("a checkout already on PATH needs nothing",
              ew.launcher_link(), ("reachable", launcher))
        said, _, done = _capture(ew.offer_launcher_link)
        check_true("and says so", done and "on your PATH" in said)
        check("with no finding from doctor", ew._launcher_findings(), [])

        # -- somebody else's launcher: never quietly replaced --------------
        other = os.path.join(root, "other-bin")
        os.makedirs(other)
        with open(os.path.join(other, ew.COMMAND), "w") as f:
            f.write("#!/bin/sh\necho other\n")
        os.chmod(os.path.join(other, ew.COMMAND), 0o755)
        os.environ["PATH"] = other + ":/usr/bin"
        state, found = ew.launcher_link()
        check("another checkout's launcher is recognised as foreign", state,
              "foreign")
        said, _, done = _capture(ew.offer_launcher_link)
        check_true("which is reported rather than replaced",
                   not done and "different checkout" in said)
        check("and the other file is left exactly as it was",
              open(os.path.join(other, ew.COMMAND)).read(),
              "#!/bin/sh\necho other\n")
        findings = ew._launcher_findings()
        check("doctor says which one wins", len(findings), 1)
        check_true("naming both", found in findings[0].hint
                   and launcher in findings[0].hint)

        # -- linkable: one link into a directory PATH already holds --------
        local = os.path.join(home, ".local", "bin")
        os.makedirs(local)
        os.environ["PATH"] = local + ":/usr/bin"
        check("a conventional bin directory on PATH is where it goes",
              ew.launcher_link(), ("linkable", os.path.join(local, ew.COMMAND)))
        findings = ew._launcher_findings()
        check("until then doctor says the command cannot be typed",
              len(findings), 1)
        check_true("and names the command that fixes it",
                   "install-command" in findings[0].hint)

        sys.stdin = io.StringIO("n\n")
        said, _, done = _capture(ew.offer_launcher_link)
        check_true("declining leaves nothing behind",
                   not done and not os.path.exists(os.path.join(local, ew.COMMAND)))
        check_true("and prints the line that would do it",
                   'export PATH="{}:$PATH"'.format(ew.BIN_DIR) in said)

        sys.stdin = io.StringIO("y\n")
        said, _, done = _capture(ew.offer_launcher_link)
        link = os.path.join(local, ew.COMMAND)
        check_true("accepting creates the link", done and os.path.islink(link))
        check("which points at this checkout's launcher",
              os.path.realpath(link), os.path.realpath(launcher))
        # The whole point: the directory was already on PATH, so the shell
        # that ran the install finds it without being restarted.
        check("so this very shell's PATH finds it", shutil.which(ew.COMMAND),
              link)
        check("and doctor has nothing left to say", ew._launcher_findings(), [])

        # -- a real file in the way is never removed -----------------------
        os.remove(link)
        with open(link, "w") as f:
            f.write("mine\n")
        _, err, done = _capture(lambda: ew.link_launcher(link))
        check_true("a file that is not our link stays",
                   not done and open(link).read() == "mine\n"
                   and "not a link" in err)
        os.remove(link)

        # -- nothing on PATH to link into: the startup file, plus this shell
        os.environ["PATH"] = "/usr/bin:/bin"
        ew.link_dirs = lambda: (os.path.join(home, "nowhere"),)
        check("with nowhere to link, it says so",
              ew.launcher_link(), ("manual", ew.BIN_DIR))
        rc = os.path.join(home, ".bashrc")
        with open(rc, "w") as f:
            f.write("# theirs\n")
        check("the startup file is the login shell's", ew.shell_rc_file(), rc)

        sys.stdin = io.StringIO("n\n")
        said, _, done = _capture(ew.offer_launcher_link)
        check("declining writes nothing", open(rc).read(), "# theirs\n")
        check_true("and still prints the line to paste",
                   'export PATH="{}:$PATH"'.format(ew.BIN_DIR) in said)

        sys.stdin = io.StringIO("y\n")
        said, _, done = _capture(ew.offer_launcher_link)
        body = open(rc).read()
        check_true("accepting appends one marked line",
                   done and body.startswith("# theirs\n")
                   and ew.PATH_MARKER in body and ew.BIN_DIR in body)
        check_true("and says how to fix the shell that cannot read it yet",
                   'export PATH="{}:$PATH"'.format(ew.BIN_DIR) in said)

        sys.stdin = io.StringIO("y\n")
        _capture(ew.offer_launcher_link)
        check("a second run does not add it twice",
              open(rc).read().count(ew.PATH_MARKER), 1)
        said, _, done = _capture(ew.offer_launcher_link)
        check_true("it says the line is already there, and what this shell needs",
                   done and "already puts" in said
                   and 'export PATH="{}:$PATH"'.format(ew.BIN_DIR) in said)

        # -- what a purge takes back ---------------------------------------
        os.environ["PATH"] = local + ":/usr/bin"
        ew.link_dirs = lambda: (local,)
        sys.stdin = io.StringIO("y\n")
        _capture(ew.offer_launcher_link)
        check_true("linked again for the purge to find", os.path.islink(link))
        removed = ew.uninstall([], purge=True)
        check_true("purge removes the link it made",
                   not os.path.lexists(link) and link in removed)
        check_true("and the line it added to the startup file",
                   ew.PATH_MARKER not in open(rc).read())
        check("leaving the rest of the file alone", open(rc).read(), "# theirs\n")

        # A launcher belonging to somebody else is not ours to remove.
        os.environ["PATH"] = other + ":/usr/bin"
        ew.uninstall([], purge=True)
        check_true("a foreign launcher survives a purge",
                   os.path.exists(os.path.join(other, ew.COMMAND)))
    finally:
        restore()


def test_the_pings_are_told_to_outlive_the_login():
    """
    The failure that waits until you log out.

    A user timer belongs to the user's own systemd manager, and without
    lingering that manager is torn down with the last session. Everything about
    the install stays perfect and the pings simply stop at the end of the
    working day — producing exactly the late-started window this tool exists to
    prevent, on the morning after.

    Setup says so once, at the end, where it is easy to miss. The machine that
    prompted this test had been installed for an hour with `Linger=no` and
    nothing had said a word.
    """
    section("The pings are told to outlive the login")
    saved = ew._run
    try:
        ew._run = lambda cmd: type("R", (), {
            "returncode": 0,
            "stdout": "UID=1000\nUser=someone\nLinger=no\n"})()
        findings = ew.linger_findings(pings=True)
        check("a machine that pings without lingering is an error",
              [(f.level, "Lingering is off" in f.message) for f in findings],
              [("error", True)])
        check_true("and the fix is the command, spelled out",
                   "loginctl enable-linger" in findings[0].hint)
        check("a machine that does not ping has nothing to keep alive",
              ew.linger_findings(pings=False), [])

        ew._run = lambda cmd: type("R", (), {
            "returncode": 0, "stdout": "UID=1000\nLinger=yes\n"})()
        check("with lingering on, nothing is said",
              ew.linger_findings(pings=True), [])

        # No loginctl, or a user it does not know: cannot tell, so say nothing.
        # Inventing a fault is worse than missing one.
        ew._run = lambda cmd: type("R", (), {"returncode": 1, "stdout": ""})()
        check("no answer at all is not a fault", ew.linger_findings(pings=True), [])
        ew._run = lambda cmd: ew._NoSystemd()
        check("nor is a machine with no loginctl on it",
              ew.linger_findings(pings=True), [])
    finally:
        ew._run = saved


def test_the_launcher_can_reach_the_user_manager():
    """
    `claude-window` is not only typed in a terminal: cron runs it, `ssh host
    claude-window doctor` runs it, and neither has a login session. Without
    XDG_RUNTIME_DIR `systemctl --user` cannot find its bus, and every question
    about the timers comes back exactly as it would for a timer that was never
    installed.

    The shell scripts have always defaulted it. The launcher they hand people
    did not.
    """
    section("The launcher can reach the user manager")
    saved = ew.BIN_DIR
    try:
        ew.BIN_DIR = tempfile.mkdtemp()
        body = open(ew.write_entry_point()).read()
        check_true("the launcher defaults XDG_RUNTIME_DIR",
                   'XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"'
                   in body)
        check_true("without overriding a session that set its own",
                   ":-" in body.split("XDG_RUNTIME_DIR=")[1])
        check_true("and still execs the script with every argument",
                   body.rstrip().endswith('"$@"'))
        # It has to survive `bash -n` as well as reading well.
        checked = subprocess.run(["bash", "-n", os.path.join(ew.BIN_DIR,
                                                             ew.COMMAND)],
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT)
        check("the launcher is valid shell", checked.returncode, 0)
    finally:
        shutil.rmtree(ew.BIN_DIR, ignore_errors=True)
        ew.BIN_DIR = saved


def test_the_timer_is_told_where_the_cli_is():
    """
    The install that looks perfect and never pings once.

    A timer inherits none of the shell setup that makes `claude` findable. An
    npm or nvm install puts the CLI under ~/.nvm/versions/node/<version>/bin,
    which only ~/.bashrc adds to PATH — so setup, run from that shell, finds it
    and builds the checkpoint, and then every ping for ever after logs "Claude
    CLI not found" and stops. Reported from a real second machine, where the
    install had been running for an hour, the log said that thirty times, and
    `doctor` said "Everything checks out".

    Two halves: the unit is told where the CLI is, and `doctor` says so when
    the unit it finds cannot reach one.
    """
    section("The timer is told where the Claude CLI is")
    saved = (ew.UNIT_DIR, ew.CLAUDE_PATH, ew.HOME, ew._systemctl, ew._run,
             ew.STATE_ROOT)
    root = tempfile.mkdtemp()

    class Ok(object):
        returncode = 0
        stdout = ""

    try:
        ew.UNIT_DIR = os.path.join(root, "units")
        os.makedirs(ew.UNIT_DIR)
        ew.HOME = os.path.join(root, "home")
        ew.STATE_ROOT = os.path.join(root, "state")
        ew._systemctl = lambda *a: Ok()
        ew._run = lambda cmd: Ok()

        # Where nvm puts it, and nothing else does.
        nvm = os.path.join(ew.HOME, ".nvm", "versions", "node", "v22.12.0", "bin")
        os.makedirs(nvm)
        cli = os.path.join(nvm, "claude")
        with open(cli, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(cli, 0o755)
        ew.CLAUDE_PATH = cli

        check_true("the CLI's own directory is on the unit's PATH",
                   nvm in ew.unit_path().split(os.pathsep))
        check_true("the ordinary directories are still there",
                   "/usr/bin" in ew.unit_path().split(os.pathsep))

        account = ew.Account("1", os.path.join(root, "cfg-1"), 0)
        _capture(lambda: ew.install_units([account]))
        unit = os.path.join(ew.UNIT_DIR, "claude-window-timing@.service")
        body = open(unit).read()
        check_true("and the unit that gets written carries it", nvm in body)
        check("so doctor has nothing to report", ew.unit_cli_findings(), [])

        # The unit an older install left behind: a fixed PATH, and no CLI in it.
        with open(unit, "w") as f:
            f.write(body.replace(
                "Environment=PATH=" + ew.unit_path(),
                "Environment=PATH=/usr/local/bin:/usr/bin:/bin"))
        findings = ew.unit_cli_findings()
        check("a timer that cannot reach the CLI is an error", len(findings), 1)
        check("and an error, not a warning", findings[0].level, "error")
        check_true("that says every ping is failing",
                   "every ping is failing" in findings[0].message)
        check_true("prints the PATH it looked in",
                   "/usr/local/bin:/usr/bin:/bin" in findings[0].hint)
        check_true("and names the fix, which is where the CLI gets recorded",
                   "./install.sh" in findings[0].hint)

        # A CLI that is there but not executable is not a CLI.
        os.chmod(cli, 0o644)
        with open(unit, "w") as f:
            f.write(body)
        check("an unexecutable CLI on the PATH counts as absent",
              len(ew.unit_cli_findings()), 1)
        os.chmod(cli, 0o755)

        # Nothing installed at all is not this check's business.
        os.remove(unit)
        check("no unit, nothing to say", ew.unit_cli_findings(), [])
    finally:
        (ew.UNIT_DIR, ew.CLAUDE_PATH, ew.HOME, ew._systemctl, ew._run,
         ew.STATE_ROOT) = saved
        shutil.rmtree(root, ignore_errors=True)


def test_a_timer_that_will_never_fire_again_is_noticed():
    """
    A systemd timer can be enabled, active, and still never fire again — a
    drop-in that resets systemd's monotonic timer list does exactly that, and
    nothing announces it. The account simply stops being pinged.
    """
    section("A timer with nothing scheduled is caught")
    ew.STATE_ROOT = tempfile.mkdtemp()
    account = ew.Account("1", "/tmp/cfg", 0)
    original = ew._systemctl

    def stub(monotonic, realtime, code=0):
        class R(object):
            returncode = code
        def fake(*args):
            r = R()
            r.stdout = monotonic if "Monotonic" in args[-2] else realtime
            return r
        return fake

    try:
        # A monotonic timer reports only the monotonic property; reading just the
        # realtime one would call this healthy timer broken.
        ew._systemctl = stub("1w 3d 14h 52min", "")
        check_true("a live cadence timer is fine",
                   ew._timer_will_fire_again(account))
        # A calendar timer, such as the anchor, reports the other one.
        ew._systemctl = stub("", "Sun 2026-08-09 00:18:22 IDT")
        check_true("a live calendar timer is fine",
                   ew._timer_will_fire_again(account))
        # What the reset-the-whole-list bug actually looked like.
        ew._systemctl = stub("", "infinity")
        check_true("a timer with nothing scheduled is caught",
                   not ew._timer_will_fire_again(account))
        ew._systemctl = stub("", "")
        check_true("and so is one reporting nothing at all",
                   not ew._timer_will_fire_again(account))
        # Without systemd there is nothing to report; never invent a fault.
        ew._systemctl = stub("", "", code=1)
        check_true("no systemd means no complaint",
                   ew._timer_will_fire_again(account))
    finally:
        ew._systemctl = original


def test_doctor_notices_a_deployment_going_wrong():
    section("doctor reports what is actually broken")
    ew.STATE_ROOT = tempfile.mkdtemp()
    account = ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg"), 0)
    account.ensure_state_dir()
    os.makedirs(account.config_dir, 0o700)

    # The failure that used to be invisible: a run that dies after its ping has
    # already succeeded still counts as a ping, but skips the state write and the
    # boundary anchor. Comparing the counts is the cheapest way to see it.
    with open(account.log_file, "w") as f:
        f.write("[x] Starting ping run\n" * 5)
        f.write("[x] Ping run finished\n" * 3)
    check("unfinished runs are counted", ew._log_run_counts(account), (5, 3))

    messages = [f.message for f in ew.validate_accounts([account])]
    check_true("an account with no login is reported",
               any("not signed in" in m for m in messages))

    ew.write_state(account, {"last_run": time.time() - 10 * ew.INTERVAL_MIN * 60})

    # -- the findings that only doctor produces, one at a time ---------------
    saved_systemctl, saved_user = ew._systemctl, ew.USER_CONFIG_JSON
    replies = {}

    class Reply(object):
        def __init__(self, out, code=0):
            self.stdout, self.returncode = out, code

    def fake_systemctl(*args):
        joined = " ".join(args)
        for key, reply in replies.items():
            if key in joined:
                return reply
        # A manager that answers, unless a case below says otherwise: every
        # timer question is meaningless when there is nothing to ask.
        return Reply("running" if "is-system-running" in joined else "")

    ew._systemctl = fake_systemctl
    try:
        def doctor_says(accounts=None):
            buf = io.StringIO()
            out, sys.stdout = sys.stdout, buf
            try:
                ew.doctor(accounts or [account])
            finally:
                sys.stdout = out
            return buf.getvalue()

        # No bus, no answers. Every timer question fails exactly as a missing
        # timer does, and reporting one per account -- with a fix that fails
        # the same way -- is the diagnostic crying wolf about the one thing it
        # exists to be trusted on.
        replies = {"is-system-running": Reply("Failed to connect to bus: No "
                                              "medium found", 1),
                   "is-enabled": Reply("")}
        said = doctor_says()
        check_true("an unreachable user manager is named for what it is",
                   "Cannot reach your systemd user manager" in said)
        check("and no timer is called broken on the strength of it",
              "is not enabled" in said, False)
        check_true("with the way to ask properly",
                   "XDG_RUNTIME_DIR=/run/user/" in said)

        replies = {"is-enabled": Reply("disabled")}
        said = doctor_says()
        check_true("a timer that is not enabled is an error with the fix",
                   "timer for account 1 is not enabled" in said
                   and "systemctl --user enable --now" in said)

        # Enabled, active, and yet never firing again: a drop-in that clears
        # systemd's monotonic list does exactly this, and nothing announces it.
        replies = {"is-enabled": Reply("enabled"),
                   "NextElapseUSecMonotonic": Reply("n/a"),
                   "NextElapseUSecRealtime": Reply("")}
        said = doctor_says()
        check_true("a timer with nothing scheduled is caught, not trusted",
                   "will never fire again" in said
                   and "Re-run ./install.sh" in said)

        # A clock that disagrees with the server undermines every decision here.
        ew.write_state(account, {"last_run": time.time(),
                                 "rate_limits": {"five_hour":
                                                 {"resets_at": time.time()
                                                  + 40 * 3600}}})
        said = doctor_says()
        check_true("a reset time that cannot be true is called implausible",
                   "reported reset time is implausible" in said
                   and "machine clock" in said)

        # The failure this design has that no other check would notice: every
        # account being pinged is healthy, and none of them is the one the user
        # actually works as.
        home = tempfile.mkdtemp()
        ew.USER_CONFIG_JSON = os.path.join(home, ".claude.json")
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "theirs",
                                        "emailAddress": "me@example.com"}}, f)
        with open(account.config_json, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "pinged",
                                        "emailAddress": "ping@example.com"}}, f)
        said = doctor_says()
        check_true("being signed in as an account nobody pings is reported",
                   "me@example.com" in said and "not one of the accounts being "
                   "pinged" in said)
        check_true("and the hint says both ways out of it",
                   "sign in as one of" in said and "accounts.json" in said)

        # ... and is silent when they are the same account, which is the
        # normal case and must never nag.
        with open(account.config_json, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "theirs",
                                        "emailAddress": "me@example.com"}}, f)
        check("nothing is said when it is the same account",
              ew._user_account_findings([account]), [])
        os.remove(ew.USER_CONFIG_JSON)
        check("nor when the user has never signed in there at all",
              ew._user_account_findings([account]), [])
    finally:
        ew._systemctl, ew.USER_CONFIG_JSON = saved_systemctl, saved_user
        os.remove(account.config_json)      # back to an account with no login
    ew.write_state(account, {"last_run": time.time() - 10 * ew.INTERVAL_MIN * 60})

    # doctor() is mostly glue, and glue is exactly where a wrong unpacking or a
    # renamed field hides: every individual check can be right while the command
    # itself raises. So run the whole thing, twice — once for a single account
    # and once for two, since the spacing report only exists in the second case.
    second = ew.Account("2", os.path.join(ew.STATE_ROOT, "cfg2"), 1)
    second.ensure_state_dir()
    os.makedirs(second.config_dir, 0o700)
    for accounts in ([account], [account, second]):
        try:
            code = ew.doctor(accounts)
            check_true("doctor runs to completion with {} account(s)".format(
                len(accounts)), code in (0, 1))
        except Exception as exc:                              # noqa: BLE001
            check("doctor runs to completion with {} account(s)".format(
                len(accounts)),
                "{}: {}".format(type(exc).__name__, exc), "no exception")
    findings = []
    for finding in ew.validate_accounts([account]):
        findings.append(finding.message)
    check_true("a config directory with no credentials is an error",
               any("not signed in" in m for m in findings))


# ---------------------------------------------------------------------------
# A whole install, from nothing
# ---------------------------------------------------------------------------
#
# Every piece of setup is tested on its own above, but the *sequence* is where
# ordering bugs live — the wizard once looked for a checkpoint before migrating
# the old one into place, and would have rebuilt what it already had. This drives
# the real wizard against a stand-in Claude in a pristine home directory, so it
# needs no account, no network and no usage.
#
# systemd is recorded rather than executed. Talking to the real user manager from
# a test is how a suite ends up rearranging the machine it runs on, and the units
# themselves are verified against a live deployment instead.

FAKE_CLAUDE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fake_claude.py")


def _clean_install(answers, accounts=2, claude_control=None,
                   home=None, repo=None, after=None, setup_kwargs=None):
    """
    Run the wizard in a fresh home directory. Returns (exit code, home, repo,
    recorded systemd calls).

    `after` runs once setup is finished but before the sandbox is taken down, so
    a test can drive a second command — uninstall, say — against the same
    redirected paths the install just used.

    The stand-in Claude is steered by a control file rather than environment
    variables, because build_claude_env() strips anything it does not
    recognise — which is the behaviour that keeps ANTHROPIC_API_KEY and
    CLAUDECODE out of a ping, and it applies just as firmly to test settings.
    """
    root = tempfile.mkdtemp()
    home = home or os.path.join(root, "home")
    repo = repo or os.path.join(root, "repo")
    for d in (home, repo):
        if not os.path.isdir(d):
            os.makedirs(d)

    saved = (ew.HOME, ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE,
             ew.CLAUDE_PATH, ew.UNIT_DIR,
             ew.BIN_DIR, ew.ALIGNMENT_FILE, ew.SCHEDULE_FILE,
             ew._systemctl, ew._run, sys.stdin, os.environ.get("HOME"),
             ew.STARTUP_WAIT_SEC, ew.COMPLETION_TIMEOUT_SEC,
             ew.STATUSLINE_WAIT_SEC,
             ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT)
    calls = []

    def record(cmd):
        calls.append(list(cmd))
        class Ok(object):
            returncode = 0
            stdout = ""
        return Ok()

    ew.HOME = home
    ew.SCRIPT_DIR = repo
    ew.STATE_ROOT = os.path.join(repo, "state")
    ew.ACCOUNTS_FILE = os.path.join(repo, "accounts.json")
    ew.CLAUDE_PATH = FAKE_CLAUDE
    ew.UNIT_DIR = os.path.join(home, ".config", "systemd", "user")
    ew.BIN_DIR = os.path.join(repo, "bin")
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    ew.SCHEDULE_FILE = os.path.join(repo, "schedule.json")
    # Derived from HOME at import time, so rebinding HOME alone would leave
    # these three pointing at the real user's files — which is the one thing a
    # test of this tool must never touch.
    ew.USER_CONFIG_DIR = os.path.join(home, ".claude")
    ew.USER_CONFIG_JSON = os.path.join(home, ".claude.json")
    ew.SWITCH_ROOT = os.path.join(home, ".claude-switch")
    ew._systemctl = lambda *a: record(("systemctl",) + a)
    ew._run = record
    # A stand-in Claude answers instantly, so the fixed pauses meant for a real
    # one are pure waiting.
    ew.STARTUP_WAIT_SEC, ew.COMPLETION_TIMEOUT_SEC, ew.STATUSLINE_WAIT_SEC = \
        0.4, 15, 4
    os.environ["HOME"] = home
    sys.stdin = io.StringIO(answers)
    if claude_control:
        for slot in range(1, accounts + 1):
            config = os.path.join(home, ".claude-{}".format(slot))
            if not os.path.isdir(config):
                os.makedirs(config, 0o700)
            with open(os.path.join(config, ".fake_claude.json"), "w") as f:
                json.dump(claude_control, f)

    # Sign both accounts in, as far as anything here can tell. Tolerant of
    # directories that already exist, because a second install over the same
    # home is exactly what the uninstall/reinstall test does.
    for index in range(accounts):
        config = os.path.join(home, ".claude-{}".format(index + 1))
        if not os.path.isdir(config):
            os.makedirs(config, 0o700)
        with open(os.path.join(config, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "t", "subscriptionType": "pro",
                "rateLimitTier": "default_claude_ai",
                "refreshTokenExpiresAt": int((time.time() + 90 * 86400) * 1000)}}, f)
        with open(os.path.join(config, ".claude.json"), "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "uuid-%d" % index,
                                        "emailAddress": "a%d@example.com" % index},
                       "hasCompletedOnboarding": True,
                       "projects": {repo: {"hasTrustDialogAccepted": True}}}, f)

    try:
        try:
            code = ew.setup(**(setup_kwargs or {}))
        except SystemExit as exc:
            # init() exits rather than freezing a refusal into the checkpoint.
            code = exc.code if exc.code is not None else 0
        if after is not None:
            after(home, repo, calls)
    finally:
        (ew.HOME, ew.SCRIPT_DIR, ew.STATE_ROOT, ew.ACCOUNTS_FILE,
         ew.CLAUDE_PATH, ew.UNIT_DIR, ew.BIN_DIR,
         ew.ALIGNMENT_FILE, ew.SCHEDULE_FILE, ew._systemctl,
         ew._run, sys.stdin, old_home, ew.STARTUP_WAIT_SEC,
         ew.COMPLETION_TIMEOUT_SEC, ew.STATUSLINE_WAIT_SEC,
         ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT) = saved
        if old_home is not None:
            os.environ["HOME"] = old_home
    return code, home, repo, calls


def test_nothing_touches_the_users_own_directory():
    """
    The invariant the whole rebuild exists for. ~/.claude and ~/.claude.json
    belong to the user; this tool creates ping directories and stays out.

    Checked two ways, because either alone is weak: statically, so new code
    cannot quietly start referring to those paths; and by running a whole
    install over a home directory that already has them, and confirming they
    come out byte-identical.
    """
    section("The user's own directory is not ours")

    # -- statically -------------------------------------------------------
    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "claude_window_timing.py")).read()
    allowed = {"USER_CONFIG_DIR  = os.path.join(HOME",
               "USER_CONFIG_JSON = os.path.join(HOME"}
    offenders = []
    for number, line in enumerate(source.split("\n"), 1):
        if "USER_CONFIG_DIR" not in line and "USER_CONFIG_JSON" not in line:
            continue
        if any(line.startswith(a) for a in allowed):
            continue
        if line.strip().startswith("#"):
            continue
        # Reading them to give advice is allowed; writing is not.
        if re.search(r"open\s*\(\s*USER_CONFIG|shutil\.\w+\(\s*USER_CONFIG|"
                     r"os\.(remove|unlink|rmdir|makedirs|symlink|rename)\s*\(\s*USER_CONFIG",
                     line):
            offenders.append((number, line.strip()))
    check("no code writes to the user's paths", offenders, [])

    # -- and by running the thing -----------------------------------------
    def snapshot(home):
        """
        Exactly what the invariant covers: ~/.claude and ~/.claude.json.

        Not the whole home directory — the tool legitimately writes systemd
        units under ~/.config, and a check that flagged those would be noise
        rather than a guard.
        """
        out = {}
        for path in [os.path.join(home, ".claude.json")]:
            if os.path.exists(path):
                out[path] = open(path, "rb").read()
        for root, _, files in os.walk(os.path.join(home, ".claude")):
            for f in files:
                path = os.path.join(root, f)
                try:
                    out[path] = open(path, "rb").read()
                except (IOError, OSError):
                    pass
        return out

    root = tempfile.mkdtemp()
    home, repo = os.path.join(root, "home"), os.path.join(root, "repo")
    os.makedirs(os.path.join(home, ".claude", "projects", "mine"))
    os.makedirs(repo)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"oauthAccount": {"accountUuid": "user-uuid",
                                    "emailAddress": "me@example.com"},
                   "projects": {"/somewhere": {"hasTrustDialogAccepted": True}}}, f)
    with open(os.path.join(home, ".claude", "projects", "mine", "a.jsonl"), "w") as f:
        f.write('{"type":"user"}\n')
    before = snapshot(home)

    code, got_home, got_repo, _ = _clean_install("2\ny\n\ny\n", home=home, repo=repo)
    check("setup succeeded over an existing user directory", code, 0)
    after = snapshot(home)
    check("the user's ~/.claude and ~/.claude.json are byte-identical afterwards",
          sorted(after.items()), sorted(before.items()))
    check_true("their conversation is still there",
               os.path.exists(os.path.join(home, ".claude", "projects", "mine",
                                           "a.jsonl")))
    check_true("and the ping directories were created beside it",
               os.path.isdir(os.path.join(home, ".claude-1"))
               and os.path.isdir(os.path.join(home, ".claude-2")))


def test_a_clean_install_from_nothing():
    section("A clean install, start to finish, with no account at all")
    code, home, repo, calls = _clean_install("2\ny\n\ny\n")
    check("setup reports success", code, 0)

    written = json.load(open(os.path.join(repo, "accounts.json")))
    check("both accounts are recorded", len(written["accounts"]), 2)
    check("accounts live in their own ping directories",
          [a["config_dir"] for a in written["accounts"]],
          ["~/.claude-1", "~/.claude-2"])

    for name in ("1", "2"):
        state = os.path.join(repo, "state", name)
        check_true("account {} got a checkpoint".format(name),
                   os.path.exists(os.path.join(state, "session_id.txt")))
        check_true("account {} backed it up".format(name),
                   os.path.getsize(os.path.join(state, "checkpoint.jsonl.bak")) > 0)

    # The checkpoint has to be a real two-message conversation, or every later
    # ping replays something that was never answered.
    first = open(os.path.join(repo, "state", "1", "session_id.txt")).read().strip()
    entries = [json.loads(l) for l in
               open(os.path.join(repo, "state", "1", "checkpoint.jsonl.bak"))]
    check("the checkpoint holds one exchange",
          [e["type"] for e in entries], ["user", "assistant"])

    # Nothing is shared any more, and nothing of the user's is touched.
    check_true("the user's own directory was never created",
               not os.path.exists(os.path.join(home, ".claude")))
    check_true("nor their config file",
               not os.path.exists(os.path.join(home, ".claude.json")))
    for name in ("1", "2"):
        d = os.path.join(home, ".claude-" + name)
        check_true("account {} keeps its checkpoint to itself".format(name),
                   os.path.isdir(os.path.join(d, "projects")))
        check_true("account {}'s config was written by us".format(name),
                   ew._read_json(os.path.join(d, ".claude.json"))
                   .get("hasCompletedClaudeInChromeOnboarding") is True)
    ping_cwd = os.path.join(home, ".claude-1", "pingcwd")
    check_true("account 1's checkpoint is in its own directory",
               os.path.exists(os.path.join(home, ".claude-1", "projects",
                                           ew._project_slug(ping_cwd),
                                           first + ".jsonl")))
    check_true("the ping working directory was created, and is empty",
               os.path.isdir(ping_cwd) and not [
                   e for e in os.listdir(ping_cwd) if not e.startswith(".")])
    check_true("setup did not leave a checkpoint under this checkout",
               not os.path.isdir(os.path.join(home, ".claude-1", "projects",
                                              ew._project_slug(repo))))

    units = os.path.join(home, ".config", "systemd", "user")
    check_true("the unit template is written",
               os.path.exists(os.path.join(units, "claude-window-timing@.service")))
    body = open(os.path.join(units, "claude-window-timing@.service")).read()
    check_true("and runs an explicit ping subcommand", "ping %i" in body)
    check_true("the second account is staggered so they do not collide",
               os.path.exists(os.path.join(
                   units, "claude-window-timing@2.timer.d", "stagger.conf")))
    stagger = open(os.path.join(
        units, "claude-window-timing@2.timer.d", "stagger.conf")).read()
    check_true("and its drop-in restates the repeat interval",
               "OnUnitActiveSec" in stagger)

    enabled = [c for c in calls if c[:2] == ["systemctl", "enable"]]
    check("a timer is enabled per account", len(enabled), 2)
    check_true("both by name",
               {c[2] for c in enabled} == {"claude-window-timing@1.timer",
                                           "claude-window-timing@2.timer"})

    check_true("the launcher is installed and executable",
               os.access(os.path.join(repo, "bin", ew.COMMAND), os.X_OK))
    # Nothing named `claude` may ever appear on the PATH this puts there: the
    # promise is that the tool sits beside Claude Code, not in front of it.
    check("and it is the only thing bin/ holds — nothing is intercepted",
          sorted(os.listdir(os.path.join(repo, "bin"))), [ew.COMMAND])


def test_install_uninstall_purge_and_install_again():
    """
    The lifecycle a stranger actually performs, in order, on one machine.

    Worth driving end to end rather than asserting on uninstall() alone: the two
    halves have to agree about every generated path, and the way that breaks is
    that an install leaves something behind which the uninstall has never heard
    of — so the next install inherits a file from the last one and nobody can
    tell. Purge is the strong form of that claim, so it is what gets tested.
    """
    section("Install, uninstall, purge, install again")
    root = tempfile.mkdtemp()
    home, repo = os.path.join(root, "home"), os.path.join(root, "repo")
    units = os.path.join(home, ".config", "systemd", "user")
    answers = "2\ny\n\ny\n"

    def generated(where=repo):
        return sorted(n for n in os.listdir(where) if not n.startswith("."))

    def credentials():
        return {n: open(os.path.join(home, ".claude-" + n,
                                     ".credentials.json"), "rb").read()
                for n in ("1", "2")}

    code, _, _, _ = _clean_install(answers, home=home, repo=repo)
    check("the first install succeeds", code, 0)
    first_checkpoint = open(os.path.join(repo, "state", "1",
                                         "session_id.txt")).read().strip()
    signed_in = {}

    # -- uninstall, the ordinary way: the timers stop, the work is kept --------
    removed = []

    def plain_uninstall(home_, repo_, calls):
        removed.extend(ew.uninstall(ew.load_accounts()))

    code, _, _, calls = _clean_install(answers, home=home, repo=repo,
                                       after=plain_uninstall)
    check("re-running the installer over an existing install succeeds", code, 0)
    check("and does not rebuild the checkpoint it already had",
          open(os.path.join(repo, "state", "1", "session_id.txt")).read().strip(),
          first_checkpoint)
    check_true("uninstall disables every timer",
               all(any(c[:3] == ["systemctl", "disable", "--now"]
                       and c[3] == "claude-window-timing@{}.timer".format(n)
                       for c in calls) for n in ("1", "2")))
    check("no unit file is left behind",
          [n for n in os.listdir(units) if n.startswith("claude-window-timing")],
          [])
    check_true("the checkpoint survives an ordinary uninstall",
               os.path.exists(os.path.join(repo, "state", "1", "session_id.txt")))

    # -- and again with --purge: nothing of ours is left in the directory ------
    def purging_uninstall(home_, repo_, calls):
        signed_in.update(credentials())     # as they are the instant before
        removed[:] = ew.uninstall(ew.load_accounts(), purge=True)

    code, _, _, _ = _clean_install(answers, home=home, repo=repo,
                                   after=purging_uninstall)
    check("the install before the purge succeeds", code, 0)
    check("purge leaves no generated file in the directory", generated(), [])
    for name in ("state", "accounts.json", "bin"):
        check_true("purge reports removing {}".format(name), name in removed)
    check("no unit file survives the purge either",
          [n for n in os.listdir(units) if n.startswith("claude-window-timing")],
          [])

    # The line purge does not cross. These directories hold logins the user
    # performed by hand; deleting them would sign two accounts out to save a
    # `rm`, and nobody asked for that.
    check_true("both ping directories are still there",
               os.path.isdir(os.path.join(home, ".claude-1"))
               and os.path.isdir(os.path.join(home, ".claude-2")))
    check("and are still signed in, byte for byte",
          credentials(), signed_in)

    # -- installing again onto the purged directory ---------------------------
    code, _, _, calls = _clean_install(answers, home=home, repo=repo)
    check("installing again after a purge succeeds", code, 0)
    fresh = open(os.path.join(repo, "state", "1",
                              "session_id.txt")).read().strip()
    check_true("and builds a new checkpoint rather than resurrecting the old one",
               fresh and fresh != first_checkpoint)
    check_true("the launcher is back",
               os.access(os.path.join(repo, "bin", ew.COMMAND), os.X_OK))
    check_true("the units are back",
               os.path.exists(os.path.join(units,
                                           "claude-window-timing@.service")))
    check("and every timer is enabled again",
          len([c for c in calls if c[:2] == ["systemctl", "enable"]]), 2)


def test_three_accounts_install_and_space_correctly():
    """
    Three accounts have never run for real, and the parts that only differ above
    two — the stagger for a third timer, the 5/N spacing target, and a three-way
    optimiser result — are exactly the ones a two-account test cannot reach.
    """
    section("Three accounts")
    code, home, repo, calls = _clean_install("3\ny\n\ny\n", accounts=3)
    check("setup reports success", code, 0)

    written = json.load(open(os.path.join(repo, "accounts.json")))
    check("three accounts are recorded",
          [a["config_dir"] for a in written["accounts"]],
          ["~/.claude-1", "~/.claude-2", "~/.claude-3"])

    for name in ("1", "2", "3"):
        check_true("account {} got a checkpoint".format(name),
                   os.path.exists(os.path.join(repo, "state", name,
                                               "session_id.txt")))

    # Nothing is shared: each account keeps its own checkpoint, and none of
    # them is anywhere near the user's directory.
    trees = set()
    for name in ("1", "2", "3"):
        d = os.path.join(home, ".claude-" + name, "projects")
        check_true("account {} has its own conversation tree".format(name),
                   os.path.isdir(d) and not os.path.islink(d))
        trees.add(os.path.realpath(d))
    check("all three are distinct", len(trees), 3)
    check_true("and the user's directory was never created",
               not os.path.exists(os.path.join(home, ".claude")))

    enabled = sorted(c[2] for c in calls if c[:2] == ["systemctl", "enable"])
    check("three timers are enabled", enabled,
          ["claude-window-timing@{}.timer".format(n) for n in ("1", "2", "3")])

    units = os.path.join(home, ".config", "systemd", "user")
    check_true("the first account has no stagger drop-in",
               not os.path.isdir(os.path.join(
                   units, "claude-window-timing@1.timer.d")))
    offsets = []
    for name in ("2", "3"):
        body = open(os.path.join(units, "claude-window-timing@{}.timer.d".format(name),
                                 "stagger.conf")).read()
        offsets.append([l for l in body.splitlines() if l.startswith("OnActiveSec=") and l != "OnActiveSec="][0])
    check("each later account starts a minute after the last", offsets,
          ["OnActiveSec=120s", "OnActiveSec=180s"])

    # Guards, which offset the *anchored* pings, must separate too.
    accounts = [ew.Account(str(i + 1), "/tmp/c%d" % i, i) for i in range(3)]
    check("the anchored pings are separated as well",
          [a.guard_sec for a in accounts],
          [ew.RESET_GUARD_SEC, ew.RESET_GUARD_SEC + ew.PING_STAGGER_SEC,
           ew.RESET_GUARD_SEC + 2 * ew.PING_STAGGER_SEC])

    # Three accounts want 1h40m apart, not 2h30m, and the optimiser has to reach
    # that from wherever the three windows actually landed.
    W = ew.WINDOW_HOURS * HOUR
    delays, total = ew.plan_alignment({"1": 0.0, "2": 0.0, "3": 0.0}, W)
    spaced = sorted((delays[n]) % W for n in delays)
    check("three synced accounts are pushed to 1h40m apart",
          [round(x / 60) for x in spaced], [0, 100, 200])
    check("costing 5 hours in total, which is why it must be asked for",
          round(total / 60), 300)
    check_true("and that is well past the automatic threshold",
               total > ew.AUTO_CORRECT_MAX_SEC)


def test_removing_an_account_stops_its_timer():
    """
    Nothing else would ever clean this up. Once an account is out of
    accounts.json the tool no longer knows it exists, so its timer would go on
    firing every interval and failing, forever, with only a failed unit to show
    for it.

    Built directly rather than by running the wizard: this is about unit
    bookkeeping, and a third full install would only make the suite slower.
    """
    section("An account that is removed stops being pinged")
    home = tempfile.mkdtemp()
    units = os.path.join(home, ".config", "systemd", "user")
    wants = os.path.join(units, "timers.target.wants")
    os.makedirs(wants)
    for name in ("1", "2", "3"):
        open(os.path.join(wants,
                          "claude-window-timing@{}.timer".format(name)), "w").close()
    os.makedirs(os.path.join(units, "claude-window-timing@3.timer.d"))

    saved = (ew.UNIT_DIR, ew._systemctl, ew._run, ew.HOME)
    seen = []

    def record(cmd):
        seen.append(list(cmd))
        class Ok(object):
            returncode = 0
            stdout = ""
        return Ok()

    ew.UNIT_DIR, ew.HOME = units, home
    ew._systemctl = lambda *a: record(("systemctl",) + a)
    ew._run = record
    try:
        check("all three are seen as installed", ew.installed_instances(),
              {"1", "2", "3"})
        ew.install_units([ew.Account("1", "~/.claude", 0),
                          ew.Account("2", "~/.claude-2", 1)])
    finally:
        ew.UNIT_DIR, ew._systemctl, ew._run, ew.HOME = saved

    flat = [" ".join(c) for c in seen]
    check_true("the removed account's timer is disabled",
               any("disable --now claude-window-timing@3.timer" in c for c in flat))
    # A pending anchor is a transient unit with no file, so it survives a
    # disable and would restart the service it belongs to.
    check_true("and any anchor it left armed is stopped",
               any("stop claude-window-timing-anchor-3.timer" in c for c in flat))
    check_true("the accounts that remain are still enabled",
               any("enable claude-window-timing@1.timer" in c for c in flat)
               and any("enable claude-window-timing@2.timer" in c for c in flat))
    check_true("and neither of them is disabled",
               not any("disable --now claude-window-timing@1.timer" in c
                       for c in flat)
               and not any("disable --now claude-window-timing@2.timer" in c
                           for c in flat))
    check_true("the removed account's stagger drop-in is gone",
               not os.path.isdir(os.path.join(
                   units, "claude-window-timing@3.timer.d")))


def test_a_clean_install_refuses_a_refused_first_message():
    """
    The checkpoint is frozen once and replayed by every future ping, so building
    it out of a refusal would bake that refusal in permanently — and setup would
    report success over a setup that can never work.
    """
    section("A clean install that meets a rate limit stops rather than pretends")
    code, home, repo, calls = _clean_install(
        "1\ny\n\ny\n", accounts=1, claude_control={"limited": "session"})
    check("setup does not report success", code != 0, True)
    check_true("no checkpoint is left behind",
               not os.path.exists(os.path.join(repo, "state", "1",
                                               "session_id.txt")))
    check("and no timer was enabled",
          [c for c in calls if c[:2] == ["systemctl", "enable"]], [])


def test_every_command_describes_what_it_actually_does():
    """
    Help text is prose, and prose goes stale silently. These are the claims that
    can be checked against the code rather than taken on trust — every one of
    them was wrong at some point.
    """
    section("Help text against behaviour")
    parser = ew.build_parser()
    actions, listed = {}, set()
    for action in parser._actions:
        for name, child in (getattr(action, "choices", None) or {}).items():
            actions[name] = child
        # Only the subparsers given a help string appear in the command list;
        # capture-statusline is deliberately not one of them, because Claude
        # Code invokes it and no person ever types it.
        for pseudo in getattr(action, "_choices_actions", []):
            listed.add(pseudo.dest)
    check_true("the internal command stays out of the listing",
               "capture-statusline" in actions and
               "capture-statusline" not in listed)

    # Every argument a *visible* command takes has to be explained. `use
    # ACCOUNT` did not say what ACCOUNT was.
    for name, child in sorted(actions.items()):
        if name not in listed:
            continue
        for arg in child._actions:
            if arg.dest in ("help",):
                continue
            check_true("`{} {}` explains itself".format(name, arg.dest),
                       bool(arg.help))

    # `log` with no account shows every account interleaved, not the first one.
    log_arg = [a for a in actions["log"]._actions if a.dest == "account"][0]
    check_true("`log` says its default is all accounts",
               "all" in (log_arg.help or "").lower())
    check_true("and does not claim it is the first",
               "the first" not in (log_arg.help or ""))
    ping_arg = [a for a in actions["ping"]._actions if a.dest == "account"][0]
    check_true("`ping` does say its default is the first, because it is",
               "first" in (ping_arg.help or ""))

    # A ping spends quota and can start a window. Nothing else here does.
    check_true("`ping` warns that it costs something",
               "quota" in (actions["ping"].description or ""))

    # Nothing in the surface may choose an account for the user any more.
    for gone in ("pick", "use", "auto", "share"):
        check_true("`{}` is gone — the tool no longer chooses".format(gone),
                   gone not in actions)
    check_true("`which` is still there, because advice is not choosing",
               "which" in actions)
    check_true("`install-command` says what it writes",
               ew.COMMAND in (actions["install-command"].description or ""))


# Two shapes and nothing looser, because prose is full of words that follow a
# command name: a backticked invocation, and a verb carrying flags. The module
# writes the command as `{}` and formats COMMAND in, so that is substituted
# first; the README spells it out and brackets its optional flags.
_INVOCATION_RES = (
    r"`{command}\s+([a-z][a-z-]*)((?:\s+\[?-{{1,2}}[a-z][a-z-]*\]?)*)",
    r"{command}\s+([a-z][a-z-]*)((?:\s+\[?-{{1,2}}[a-z][a-z-]*\]?)+)",
)


def _suggested_invocations(text, command="claude-window"):
    """Every `claude-window <verb> [flags]` this text tells somebody to type."""
    text = text.replace("{}", command)
    found = []
    for pattern in _INVOCATION_RES:
        for match in re.finditer(pattern.format(command=command), text):
            found.append((match.group(1), match.group(2).split()))
    return found


def test_every_command_the_output_suggests_can_be_typed():
    """
    A suggestion is only advice if it works when pasted.

    Both halves have gone wrong here. The README is written by hand and drifts
    from the parser; and `which` went on offering a flag that had been replaced
    by its opposite, so somebody following the advice got "unrecognized
    arguments" from the command that had just given it.

    So every invocation the tool prints, and every one the README documents, is
    put to the parser here.
    """
    section("Every command the output suggests can be typed")
    parser = ew.build_parser()
    options = {}
    for action in parser._actions:
        for name, subparser in (getattr(action, "choices", None) or {}).items():
            options[name] = set()
            for option in subparser._actions:
                options[name].update(option.option_strings)
    # `help <command>` is rewritten by normalise_help rather than being a
    # subparser of its own, so it is named here rather than looked up.
    verbs = set(ew.known_commands(parser)) | {"help"}

    def unusable(text):
        """Everything in `text` that could not be typed, without duplicates."""
        bad = []
        for verb, flags in _suggested_invocations(text):
            if verb not in verbs:
                bad.append(verb)
                continue
            for flag in flags:
                if flag.strip("[]") not in options.get(verb, ()):
                    bad.append("{} {}".format(verb, flag.strip("[]")))
        return sorted(set(bad))

    # First that the reading works at all: a test that silently matched nothing
    # would pass for ever while the thing it guards rotted.
    check("a flag that no longer exists is caught",
          unusable("run `{} which --fresh` to refresh"), ["which --fresh"])
    check("and so is a command that never did",
          unusable("run `{} rotate`"), ["rotate"])

    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, "claude_window_timing.py")) as f:
        source = f.read()
    printed = []
    for literal in _string_literals(source):
        printed.extend(_suggested_invocations(literal))
    check_true("the tool does suggest commands, so this is not vacuous",
               len(printed) > 20)
    check("and every one of them can be typed",
          sorted(set(v for v, _ in printed) - verbs), [])
    check("flags included",
          sorted(set("{} {}".format(v, f.strip("[]")) for v, flags in printed
                     for f in flags
                     if f.strip("[]") not in options.get(v, ()))), [])

    with open(os.path.join(here, "README.md")) as f:
        readme = f.read()
    documented = _suggested_invocations(readme)
    check_true("the README's command table was actually read",
               len(set(v for v, _ in documented)) >= 10)
    check("and documents only commands that exist", unusable(readme), [])


def test_every_command_routes_to_the_thing_it_names():
    """
    Every command is tested elsewhere by calling its function directly, which
    leaves the dispatch itself — the part every user actually goes through —
    resting on nothing. A single mistyped branch would route `doctor` at
    `status` and no test would notice.

    The other half is the promise on the front page: typing the command, or any
    of the ones that only look, can never spend quota. So a ping is stubbed and
    the test fails if anything reaches it that was not asked to.
    """
    section("Each command reaches its own implementation, and only it")
    root = tempfile.mkdtemp()
    saved = (ew.STATE_ROOT, ew.ACCOUNTS_FILE, ew.SCHEDULE_FILE, ew.BIN_DIR,
             ew._systemctl, ew._run, ew.ping, ew.init, ew.setup, ew.uninstall,
             ew.doctor, ew.CLAUDE_PATH)
    ew.STATE_ROOT = os.path.join(root, "state")
    ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
    ew.SCHEDULE_FILE = os.path.join(root, "schedule.json")
    ew.BIN_DIR = os.path.join(root, "bin")
    ew.CLAUDE_PATH = FAKE_CLAUDE

    class Ok(object):
        returncode = 0
        stdout = ""

    reached = []

    def spy(name, code=0):
        def handler(*args, **kwargs):
            reached.append(name)
            return code
        return handler

    ew._systemctl = lambda *a: Ok()
    ew._run = lambda cmd: Ok()
    ew.ping = spy("ping")
    ew.init = spy("init")
    ew.setup = spy("setup")
    ew.uninstall = lambda accounts, purge=False: reached.append(
        "uninstall --purge" if purge else "uninstall") or []
    ew.doctor = spy("doctor", 1)
    try:
        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"accounts": [
                {"name": "1", "config_dir": os.path.join(root, "cfg-1")},
                {"name": "2", "config_dir": os.path.join(root, "cfg-2")}]}, f)
        for name in ("1", "2"):
            account = ew.Account(name, os.path.join(root, "cfg-" + name), 0)
            account.ensure_state_dir()
            ew.write_state(account, {"last_run": time.time(),
                                     "available_at": time.time(),
                                     "rate_limits": {"five_hour": {
                                         "resets_at": time.time() + 3600,
                                         "used_percentage": 5}}})
            with open(account.log_file, "w") as f:
                f.write("[2026-08-11 10:00:00] account %s\n" % name)

        def run(argv):
            buf, err = io.StringIO(), io.StringIO()
            out, sys.stdout = sys.stdout, buf
            errs, sys.stderr = sys.stderr, err
            try:
                code = ew.cli(argv)
            finally:
                sys.stdout, sys.stderr = out, errs
            return code, buf.getvalue(), err.getvalue()

        # -- the ones that only look: they run for real, and must not ping ----
        code, said, _ = run([])
        check("the bare command reports status", code, 0)
        check_true("and names the other commands, which nothing else does",
                   "Other commands:" in said)
        # `switch` is half of what this tool does, and the bare command is
        # where somebody finds out it exists.
        check_true("switch among them", "switch" in said.split(
            "Other commands:")[1])

        code, said, _ = run(["status"])
        check("status succeeds", code, 0)
        check_true("without the discovery hint the bare form adds",
                   "Other commands:" not in said)

        check("status --json is JSON and nothing else",
              sorted(json.loads(run(["status", "--json"])[1])),
              ["accounts", "generated_at", "published_at", "switching", "use"])

        # A live reading is a real request against a real account. Who takes
        # one is therefore part of the dispatch, and the case that matters is
        # `--json`: it is the interface something polls in a loop, and a
        # reading per account per poll is a bill nobody meant to run up.
        readings = []
        real_refresh = ew.refresh_limits
        ew.refresh_limits = lambda accounts: readings.append(
            len(accounts)) or {}
        try:
            run([])
            check("the bare command never reads the limits", readings, [])
            run(["status", "--json"])
            check("and neither does the scripting interface", readings, [])
            run(["status", "--json", "--live"])
            check("unless it is asked to", readings, [2])
            del readings[:]
            run(["status"])
            check("a person running status gets a reading", readings, [2])
            del readings[:]
            run(["which"])
            check("and so does which", readings, [2])
            del readings[:]
            run(["which", "--no-live"])
            check("--no-live skips it", readings, [])
        finally:
            ew.refresh_limits = real_refresh

        code, said, _ = run(["which"])
        check("which succeeds", code, 0)
        check_true("and answers the question it is named after", "Use account" in said)

        code, said, _ = run(["accounts"])
        check("accounts lists them one per line", said.split(), ["1", "2"])

        code, said, _ = run(["log"])
        check("log succeeds", code, 0)
        check_true("and merges every account's", "account 1" in said
                   and "account 2" in said)

        code, said, _ = run(["log", "2"])
        check_true("one account's log is only that account's",
                   "account 2" in said and "account 1" not in said)

        check("realign succeeds", run(["realign"])[0], 0)

        # `check` is the one looking command with a verdict to report, so its
        # exit code has to follow the accounts rather than the run.
        code, said, _ = run(["check"])
        check("check fails while the accounts are not signed in", code, 1)
        check_true("and says which, and where to sign it in",
                   "Account 1" in said and "cfg-1" in said)
        for name in ("1", "2"):
            config = os.path.join(root, "cfg-" + name)
            os.makedirs(config, 0o700)
            with open(os.path.join(config, ".claude.json"), "w") as f:
                json.dump({"oauthAccount": {"accountUuid": "u" + name,
                                            "emailAddress": name + "@x"}}, f)
            with open(os.path.join(config, ".credentials.json"), "w") as f:
                json.dump({"claudeAiOauth": {
                    "accessToken": "t", "subscriptionType": "max",
                    "refreshTokenExpiresAt":
                        int((time.time() + 90 * 86400) * 1000)}}, f)
        code, said, _ = run(["check"])
        check("and succeeds once they are", code, 0)
        check_true("saying so plainly", "Everything checks out" in said)

        code, said, _ = run(["install-command"])
        check("install-command succeeds", code, 0)
        check_true("and writes the launcher",
                   os.access(os.path.join(ew.BIN_DIR, ew.COMMAND), os.X_OK))

        check("nothing so far has sent a ping", reached, [])

        # -- the ones that act: stubbed, and checked for arriving at all -----
        check("doctor's exit code is its own", run(["doctor"])[0], 1)
        run(["ping"]); run(["ping", "2"]); run(["init"]); run(["setup"])
        run(["uninstall"]); run(["uninstall", "--purge"])
        check("each acting command reached its own implementation",
              reached, ["doctor", "ping", "ping", "init", "setup",
                        "uninstall", "uninstall --purge"])

        # -- and the ways of getting it wrong --------------------------------
        code, _, err = run(["ping", "nope"])
        check("an unknown account is a usage error, not a crash", code, 2)
        check_true("and the message lists the accounts that do exist",
                   "1, 2" in err)

        # Before anything has run, `log` has nothing to show and has to say
        # which command produces some, not print an empty screen.
        for name in ("1", "2"):
            os.remove(ew.Account(name, os.path.join(root, "cfg-" + name),
                                 0).log_file)
        code, _, err = run(["log"])
        check("an empty log is reported rather than shown", code, 1)
        check_true("pointing at the command that fills it", "ping" in err)
        code, _, err = run(["log", "1"])
        check("and for one named account too", code, 1)
        check_true("with the same advice, not an empty screen", "ping" in err)

        code, _, err = run(["log", "-f"])
        check("following every account at once is refused", code, 2)
        check_true("with the command that would work", "log 1 -f" in err
                   or "claude-window log" in err)
    finally:
        (ew.STATE_ROOT, ew.ACCOUNTS_FILE, ew.SCHEDULE_FILE, ew.BIN_DIR,
         ew._systemctl, ew._run, ew.ping, ew.init, ew.setup, ew.uninstall,
         ew.doctor, ew.CLAUDE_PATH) = saved


def test_looking_at_the_schedule_does_not_change_it():
    """
    Noting a change in the participating set starts the settling clock, so a
    command that only reports would quietly move the schedule it is describing.
    doctor and status both did.
    """
    section("Reporting on the spacing changes nothing")
    now = time.time()
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    a = ew.Account(TEST_PREFIX + "-a", "/tmp/ca", 0)
    b = ew.Account(TEST_PREFIX + "-b", "/tmp/cb", 1)
    states = {n: {"rate_limits": {"five_hour": {"resets_at": now + 600 + i * 900}},
                  "available_at": now - 1}
              for i, n in enumerate((a.name, b.name))}

    check_true("nothing recorded yet", not os.path.exists(ew.ALIGNMENT_FILE))
    ew.describe_alignment([a, b], states, now)
    check_true("describing the spacing writes nothing",
               not os.path.exists(ew.ALIGNMENT_FILE))
    ew.alignment_plan([a, b], states, now, record=False)
    check_true("and neither does planning it read-only",
               not os.path.exists(ew.ALIGNMENT_FILE))

    # The ping is the thing that acts, so it is the thing that records.
    ew.alignment_plan([a, b], states, now, record=True)
    check_true("the ping's own call does record", os.path.exists(ew.ALIGNMENT_FILE))
    check("and records who is taking part",
          ew.read_alignment().get("participants"), sorted([a.name, b.name]))


def test_the_shell_scripts_call_commands_that_exist():
    """
    install.sh is the documented entry point, so a subcommand it invokes that
    the parser does not accept breaks the very first thing a new user runs. It
    broke exactly that way when the flags became subcommands, and uninstall.sh
    broke silently, since it tolerates the failure and falls back.
    """
    section("The shell scripts invoke real commands")
    here = os.path.dirname(os.path.abspath(__file__))
    parser = ew.build_parser()
    known = set()
    for action in parser._actions:
        known.update(getattr(action, "choices", None) or {})

    pattern = re.compile(r"claude_window_timing\.py\"?\s+(--?[\w-]+|[\w-]+)")
    seen = 0
    for name in ("install.sh", "uninstall.sh"):
        body = open(os.path.join(here, name)).read()
        for match in pattern.finditer(body):
            token, seen = match.group(1), seen + 1
            check_true("{} invokes `{}`, which exists".format(name, token),
                       token in known)
    check_true("both scripts were actually inspected", seen >= 2)


def test_doctor_spots_residue_from_an_earlier_install():
    """
    systemd remembers a failed unit long after its file is deleted, so an older
    or hand-rolled version of this tool leaves an entry that cannot run and
    cannot explain itself. It is the first thing anyone diagnosing will trip
    over.
    """
    section("Residue from an earlier install is named, not left to puzzle over")
    accounts = [ew.Account("1", "/tmp/c1", 0), ew.Account("2", "/tmp/c2", 1)]
    original = ew._systemctl

    def stub(listing):
        class R(object):
            returncode = 0
            stdout = listing
        return lambda *a: R()

    try:
        ew._systemctl = stub(
            "claude-extra-window.service loaded failed failed Claude Extra\n")
        found = ew._stray_unit_findings(accounts)
        check("a foreign claude-window unit is reported", len(found), 1)
        check_true("and the message says how to clear it",
                   "reset-failed" in found[0].hint)
        # Clearing the flag is not the end of it if a timer keeps starting the
        # thing: it fails again on the next tick, for ever.
        check_true("and how to stop it coming back",
                   "disable --now" in found[0].hint)

        # Our own units failing is a different finding, made elsewhere; saying it
        # twice, and calling them foreign, would be worse than silence.
        ew._systemctl = stub("claude-window-timing@1.timer loaded failed failed x\n"
                             "claude-window-timing-anchor-2.timer loaded failed f\n")
        check("our own units are not called foreign",
              ew._stray_unit_findings(accounts), [])

        ew._systemctl = stub("some-other.service loaded failed failed thing\n")
        check("unrelated failures are not our business",
              ew._stray_unit_findings(accounts), [])

        ew._systemctl = stub("")
        check("nothing failed means nothing said",
              ew._stray_unit_findings(accounts), [])
    finally:
        ew._systemctl = original


def test_uninstall_removes_the_units_and_nothing_else():
    """
    Uninstalling is the operation nobody tests until they need it, and by then a
    mistake is expensive. It must take the timers away, unlink what an earlier
    version put in the user's way, and leave every conversation and every login
    exactly where it is.
    """
    section("Uninstall")
    root = tempfile.mkdtemp()
    home = os.path.join(root, "home")
    units = os.path.join(home, ".config", "systemd", "user")
    os.makedirs(units)
    ew.STATE_ROOT = os.path.join(root, "state")

    saved = (ew.UNIT_DIR, ew.BIN_DIR, ew._systemctl, ew._run, ew.HOME)
    seen = []

    def record(cmd):
        seen.append(" ".join(cmd))
        class Ok(object):
            returncode = 0
            stdout = ""
        return Ok()

    ew.UNIT_DIR = units
    ew.BIN_DIR = os.path.join(root, "bin"); os.makedirs(ew.BIN_DIR)
    ew.HOME = home
    ew._systemctl = lambda *a: record(("systemctl",) + a)
    ew._run = record
    try:
        accounts = [ew.Account("1", os.path.join(home, ".claude-1"), 0),
                    ew.Account("2", os.path.join(home, ".claude-2"), 1)]

        # A deployment to tear down, including the single-account units an
        # install from before accounts.json would have left.
        for name in ("claude-window-timing@.service", "claude-window-timing@.timer",
                     "claude-window-timing.service"):
            open(os.path.join(units, name), "w").close()
        os.makedirs(os.path.join(units, "claude-window-timing@2.timer.d"))
        open(os.path.join(ew.BIN_DIR, ew.COMMAND), "w").close()

        # The user's own things, which must survive untouched.
        mine = os.path.join(home, ".claude", "projects", "mine")
        os.makedirs(mine)
        with open(os.path.join(mine, "a.jsonl"), "w") as f:
            f.write('{"type":"user"}\n')
        with open(os.path.join(home, ".claude.json"), "w") as f:
            f.write('{"oauthAccount": {"emailAddress": "me@example.com"}}')

        # The ping directories, each with a login and a conversation of its
        # own. Both have to come through untouched: the login because signing
        # somebody out is not an uninstaller's business, the conversation
        # because it is the checkpoint a re-install carries on from.
        for account in accounts:
            os.makedirs(os.path.join(account.config_dir, "projects", "p"))
            with open(os.path.join(account.config_dir, ".credentials.json"), "w") as f:
                f.write("{}")
            with open(os.path.join(account.config_dir, "projects", "p",
                                   "c.jsonl"), "w") as f:
                f.write('{"type":"assistant"}\n')

        removed = ew.uninstall(accounts)

        for name in ("1", "2"):
            check_true("account {}'s timer is disabled".format(name),
                       any("disable --now claude-window-timing@{}.timer".format(name)
                           in c for c in seen))
            # A pending anchor is transient and survives a disable, so it would
            # fire afterwards and restart the service.
            check_true("account {}'s anchor is stopped".format(name),
                       any("stop claude-window-timing-anchor-{}.timer".format(name)
                           in c for c in seen))
        for name in ("claude-window-timing@.service", "claude-window-timing@.timer",
                     "claude-window-timing.service"):
            check_true("{} is removed".format(name),
                       not os.path.exists(os.path.join(units, name)))
        # Without --purge the launcher stays, so re-installing needs nothing
        # put back on the PATH.
        check("bin/ still holds the launcher and nothing else",
              sorted(os.listdir(ew.BIN_DIR)), [ew.COMMAND])
        check_true("running it twice is not an error",
                   isinstance(ew.uninstall(accounts), list))

        # The things that must never be harmed.
        check_true("the user's conversation survives",
                   os.path.exists(os.path.join(mine, "a.jsonl")))
        check_true("the user's config survives",
                   os.path.exists(os.path.join(home, ".claude.json")))
        for account in accounts:
            check_true("account {}'s login is left in place".format(account.name),
                       os.path.exists(os.path.join(account.config_dir,
                                                   ".credentials.json")))
            check_true("account {}'s conversations are left in place".format(
                           account.name),
                       os.path.exists(os.path.join(account.config_dir,
                                                   "projects", "p", "c.jsonl")))
        check_true("it reports what it did", len(removed) > 4)

        # An older layout put an account in the user's own directory. Advising
        # `rm -rf` on it would be followed, and would destroy every
        # conversation they have.
        saved_user, saved_accounts = ew.USER_CONFIG_DIR, ew.ACCOUNTS_FILE
        ew.USER_CONFIG_DIR = os.path.join(home, ".claude")
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        try:
            with open(ew.ACCOUNTS_FILE, "w") as f:
                json.dump({"accounts": [
                    {"name": "1", "config_dir": os.path.join(home, ".claude")},
                    {"name": "2", "config_dir": os.path.join(home, ".claude-2")},
                ]}, f)
            buf = io.StringIO()
            out, sys.stdout = sys.stdout, buf
            try:
                ew.cli(["uninstall"])
            finally:
                sys.stdout = out
            advice = buf.getvalue()
            check_true("the user's own directory is never offered for deletion",
                       "rm -rf " + ew.USER_CONFIG_DIR + "\n" not in advice)
            check_true("the ping directory beside it still is",
                       "rm -rf " + os.path.join(home, ".claude-2") in advice)
            check_true("and it says why the other one was left alone",
                       "which is yours" in advice)
        finally:
            ew.USER_CONFIG_DIR, ew.ACCOUNTS_FILE = saved_user, saved_accounts
    finally:
        ew.UNIT_DIR, ew.BIN_DIR, ew._systemctl, ew._run, ew.HOME = saved



# ---------------------------------------------------------------------------
# Switching your own Claude Code between accounts
# ---------------------------------------------------------------------------


def _capture(fn):
    """Run fn with stdout and stderr captured. Returns (out, err, result)."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        result = fn()
    finally:
        sys.stdout, sys.stderr = saved
    return out.getvalue(), err.getvalue(), result


def _tree_snapshot(root):
    """{path: bytes} for every file under root, for before/after comparison."""
    out = {}
    for base, _, names in os.walk(root):
        for name in names:
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as f:
                    out[path] = f.read()
            except (IOError, OSError):
                pass
    return out


def _snapshot_user_files():
    """The two files a switch is allowed to rewrite, exactly as they are now."""
    out = {}
    for path in (ew.credentials_path(ew.user_login()), ew.USER_CONFIG_JSON):
        if os.path.exists(path):
            with open(path, "rb") as f:
                out[path] = f.read()
    return out


def _switch_sandbox(names=("1", "2"), signed_in_as=None, parked=(), now=None,
                    pings=True):
    """
    A home directory with ping directories, a signed-in ~/.claude, and stores.

    Returns (restore, home, accounts). `signed_in_as` is the account name the
    user's own Claude Code holds; `parked` names the accounts with a login in
    their store. Every login is given a distinct refresh token, because telling
    two *copies* of one login apart from two separate logins is the whole point
    of several checks here.
    """
    now = time.time() if now is None else now
    root = tempfile.mkdtemp()
    home = os.path.join(root, "home")
    os.makedirs(home)

    saved = (ew.HOME, ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT,
             ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.UNIT_DIR)
    ew.HOME = home
    ew.USER_CONFIG_DIR = os.path.join(home, ".claude")
    ew.USER_CONFIG_JSON = os.path.join(home, ".claude.json")
    ew.SWITCH_ROOT = os.path.join(home, ".claude-switch")
    ew.STATE_ROOT = os.path.join(root, "state")
    ew.SCHEDULE_FILE = os.path.join(root, "schedule.json")
    # Otherwise a test would read the units of whatever machine it runs on.
    ew.UNIT_DIR = os.path.join(root, "units")
    os.makedirs(ew.UNIT_DIR)

    def restore():
        (ew.HOME, ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SWITCH_ROOT,
         ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.UNIT_DIR) = saved
        shutil.rmtree(root, ignore_errors=True)

    def write_login(directory, config_json, uuid_, email, refresh,
                    refresh_expires=None, extra=None):
        if not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        with open(os.path.join(directory, ".credentials.json"), "w") as f:
            json.dump({"claudeAiOauth": {
                "accessToken": "access-" + refresh,
                "refreshToken": refresh,
                "subscriptionType": "pro",
                "refreshTokenExpiresAt": int(
                    (refresh_expires if refresh_expires is not None
                     else now + 30 * 86400) * 1000)}}, f)
        config = {"oauthAccount": {"accountUuid": uuid_, "emailAddress": email}}
        config.update(extra or {})
        directory_of = os.path.dirname(config_json)
        if directory_of and not os.path.isdir(directory_of):
            os.makedirs(directory_of, 0o700)
        with open(config_json, "w") as f:
            json.dump(config, f)

    accounts = []
    for index, name in enumerate(names):
        account = ew.Account(name, os.path.join(home, ".claude-" + name), index,
                             "label" + name)
        accounts.append(account)
        # The ping directory's own login: always distinct from the store's. A
        # machine that does no pinging has no such directory at all.
        if pings:
            write_login(account.config_dir, account.config_json,
                        "uuid-" + name, "a{}@example.com".format(name),
                        "ping-refresh-" + name)
        if name in parked:
            store = ew.switch_store(account)
            write_login(store.config_dir, store.config_json,
                        "uuid-" + name, "a{}@example.com".format(name),
                        "parked-refresh-" + name)

    if signed_in_as is not None:
        account = ew.find_account(accounts, signed_in_as)
        write_login(ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON,
                    "uuid-" + signed_in_as,
                    "a{}@example.com".format(signed_in_as),
                    "live-refresh-" + signed_in_as,
                    extra={"projects": {"/work": {"hasTrustDialogAccepted": True}},
                           "numStartups": 7,
                           # Two of the account-scoped caches, so the test can
                           # watch them go.
                           "cachedExtraUsageDisabledReason": "out_of_credits",
                           "penguinModeOrgEnabled": True})
    return restore, home, accounts


def _creds(path):
    with open(path) as f:
        return json.load(f)["claudeAiOauth"]


def test_a_login_is_never_in_two_places_at_once():
    """
    The invariant the whole design rests on, and the one that fails silently.

    Refresh tokens rotate, so two directories holding one login take turns
    invalidating each other until one is signed out — about eight hours later,
    with nothing to connect it to the switch. The defence is that a switch
    *moves* rather than copies: the outgoing login is parked before the incoming
    one is installed, so exactly one live copy of each exists at every moment.
    """
    section("A login is never in two places at once")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        before = _creds(os.path.join(ew.USER_CONFIG_DIR, ".credentials.json"))
        code = ew.switch_account(accounts, "2")
        check("switching to a parked account succeeds", code, 0)

        live = _creds(os.path.join(ew.USER_CONFIG_DIR, ".credentials.json"))
        store1 = ew.switch_store(accounts[0])
        store2 = ew.switch_store(accounts[1])
        parked1 = _creds(ew.credentials_path(store1))

        check("the incoming login is now live", live["refreshToken"],
              "parked-refresh-2")
        check("the outgoing login was parked, not discarded",
              parked1["refreshToken"], before["refreshToken"])
        check("and the account it came from is where it was parked",
              json.load(open(store1.config_json))["oauthAccount"]["accountUuid"],
              "uuid-1")

        # The heart of it: no refresh token appears in two live places.
        holders = {}
        for label, path in (("live", ew.credentials_path(ew.user_login())),
                            ("store 1", ew.credentials_path(store1)),
                            ("store 2", ew.credentials_path(store2)),
                            ("ping 1", ew.credentials_path(accounts[0])),
                            ("ping 2", ew.credentials_path(accounts[1]))):
            if os.path.exists(path):
                holders.setdefault(_creds(path)["refreshToken"], []).append(label)
        duplicated = {token: where for token, where in holders.items()
                      if len(where) > 1}
        check("no login is held in two places after a switch", duplicated, {})

        # Switching back must park the other one, symmetrically.
        ew.switch_account(accounts, "1")
        live = _creds(ew.credentials_path(ew.user_login()))
        check("switching back restores the parked login",
              live["refreshToken"], before["refreshToken"])
        check("and parks the one that was live",
              _creds(ew.credentials_path(store2))["refreshToken"],
              "parked-refresh-2")
    finally:
        restore()


def test_an_interrupted_switch_never_leaves_a_login_in_two_places():
    """
    The ordering that makes the invariant survive a crash, not just a clean run.

    Parking the outgoing login *before* installing the incoming one would leave
    a moment when one grant sits in two directories. A crash there leaves it
    there for good: two refreshers, and one signed out about eight hours later
    with nothing recording why. So the outgoing login is held in memory until
    the install has landed, and the backup taken first is what covers the rest.
    """
    section("An interrupted switch leaves nothing duplicated")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    real_park = ew.park_login
    try:
        ew.park_login = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("power cut"))
        try:
            _capture(lambda: ew.switch_account(accounts, "2", sign_in=False))
        except RuntimeError:
            pass                      # exactly the crash being modelled

        holders = {}
        for label, login in (("live", ew.user_login()),
                             ("store 1", ew.switch_store(accounts[0])),
                             ("store 2", ew.switch_store(accounts[1])),
                             ("ping 1", accounts[0]), ("ping 2", accounts[1])):
            grant = ew.login_fingerprint(login)
            if grant:
                holders.setdefault(grant, []).append(label)
        check("no login is in two places after the crash",
              {g: w for g, w in holders.items() if len(w) > 1}, {})

        backups = os.path.join(ew.SWITCH_ROOT, ".backups")
        stamp = sorted(os.listdir(backups))[-1]
        saved = json.load(open(os.path.join(backups, stamp, "credentials.json")))
        check("and the login that was interrupted is recoverable from the backup",
              saved["claudeAiOauth"]["refreshToken"], "live-refresh-1")
    finally:
        ew.park_login = real_park
        restore()

    # And doctor names the state if it is ever reached another way.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[0])
        ew.secure_dir(store.config_dir)          # 0700, or the mode check fires too
        shutil.copyfile(ew.credentials_path(ew.user_login()),
                        ew.credentials_path(store))
        findings = ew.switch_findings(accounts)
        errors = [f for f in findings if f.level == "error"]
        check("a store holding the live login is an error", len(errors), 1)
        check_true("that says what happens and when",
                   "eight hours" in errors[0].hint)
    finally:
        restore()


def test_a_spent_account_is_never_recommended():
    """
    One decision, used by every command that has an opinion: `which`, `status`,
    `status --json` and `switch` all rank through `choose_account`. What it must
    never do is prefer an account that cannot serve a request, however soon its
    window turns over -- "back first" is not the same as "usable".

    The gap this covers: a limit reported fully spent with no reset time beside
    it read as *usable*, which is exactly the shape a refusal takes, because a
    429 carries no headers at all.
    """
    section("A spent account is never recommended")

    root = tempfile.mkdtemp()
    saved = ew.STATE_ROOT
    try:
        ew.STATE_ROOT = root
        now = time.time()
        spent = ew.Account("1", os.path.join(root, "c1"), 0, "spent")
        usable = ew.Account("2", os.path.join(root, "c2"), 1, "usable")
        for a in (spent, usable):
            a.ensure_state_dir()

        # The spent one resets much sooner -- the tempting, wrong answer.
        states = {
            "1": {"last_run": now - 60, "available_at": now - 60,
                  "rate_limits": {"five_hour": {"used_percentage": 100,
                                                "resets_at": now + 300}}},
            "2": {"last_run": now - 60, "available_at": now - 60,
                  "rate_limits": {"five_hour": {"used_percentage": 10,
                                                "resets_at": now + 4 * 3600}}},
        }
        accounts = [spent, usable]
        chosen, reason = ew.choose_account(accounts, states, now)
        check("the usable account wins despite resetting far later",
              chosen.name, "2")
        check("tier outranks urgency in the sort key",
              ew.rank_account(spent, states["1"], now)[0], ew.WAITING)

        # The same, with no reset time at all -- the shape of a refusal.
        states["1"]["rate_limits"] = {"five_hour": {"used_percentage": 100}}
        av = ew.account_availability(spent, states["1"], now)
        check("spent with no reset time is still not usable",
              ew.TIER_NAMES[av.tier], "waiting")
        # It is not unknowable: a spent 5-hour limit cannot outlast five hours,
        # so the answer is a bound, offered as one rather than as a promise.
        check("the return time is bounded by the limit's own length",
              round(av.until - now), ew.WINDOW_HOURS * 3600)
        check("and marked as a bound, not an observation", av.exact, False)
        check_true("which the wording says out loud",
                   "no later than" in
                   ew.describe_availability(states["1"], av, now))
        check("it is still not recommended",
              ew.choose_account(accounts, states, now)[0].name, "2")

        # The same rule for a reading that arrived from another machine. This
        # branch had a latent NameError: no test reached it, because a
        # published WAITING entry normally carries the reset time the pinging
        # machine observed.
        sched = os.path.join(root, "schedule.json")
        saved_sched = ew.SCHEDULE_FILE
        try:
            ew.SCHEDULE_FILE = sched
            with open(sched, "w") as f:
                json.dump({"window_hours": 5, "written_at": now, "accounts": [
                    {"name": "1", "account_uuid": "u1", "usable_now": False,
                     "tier": "waiting", "unusable_until": None,
                     "unusable_because": "its 5-hour limit is spent",
                     "last_run": now - 60, "expires_at": now + 900,
                     "window_phase": 0},
                    {"name": "2", "account_uuid": "u2", "usable_now": True,
                     "tier": "usable", "last_run": now - 60,
                     "expires_at": now + 3600, "window_phase": 0}]}, f)
            for a in accounts:
                shutil.rmtree(a.state_dir, ignore_errors=True)
            view = ew.schedule_view(accounts)
            check_true("a published schedule is read at all", view is not None)
            pub_known, pub_states, pub_avail, _ = view
            check("a published WAITING entry with no time is bounded, not unknown",
                  round(pub_avail["1"].until - now), ew.WINDOW_HOURS * 3600)
            check("and marked a bound", pub_avail["1"].exact, False)
            check("it is still not the recommendation",
                  ew.choose_account(pub_known, pub_states, now,
                                    pub_avail)[0].name, "2")
        finally:
            ew.SCHEDULE_FILE = saved_sched
            for a in accounts:
                a.ensure_state_dir()
            for name, state in states.items():
                ew.write_state(ew.find_account(accounts, name), state)

        # Every command that has an opinion goes through the same function.
        # status_json reads state from disk rather than taking it, so put the
        # fixture where it will look.
        for name, state in states.items():
            ew.write_state(ew.find_account(accounts, name), state)
        for command, fn in (("which", lambda: ew.which(accounts, states)),
                            ("status_json", lambda: ew.status_json(accounts))):
            out, _, _ = _capture(fn)
            check_true("{} points at the usable account".format(command),
                       '"account": "2"' in out or "Use account 2" in out)

        # But the rotation keeps its slot: the usual cause is the 5-hour limit,
        # which clears at this account's own boundary. Dropping it would
        # re-space every other account around a hole that closes by itself.
        mixed = {"last_run": now - 60, "available_at": now - 60,
                 "rate_limits": {"five_hour": {"used_percentage": 40,
                                               "resets_at": now + 900},
                                 "seven_day": {"used_percentage": 100}}}
        av = ew.account_availability(spent, mixed, now)
        check("a weekly limit with no reset still makes it unusable",
              ew.TIER_NAMES[av.tier], "waiting")
        check("bounded by the weekly window rather than the 5-hour one",
              round(av.until - now), 7 * 86400)
        check("and known to be a bound", av.exact, False)
        # A bound that outlasts this account's own window does drop it from
        # the rotation -- correctly: a spent weekly limit really can outlast
        # the boundary, which is the case that reasoning was written for.
        check("so it does not hold a slot it cannot fill",
              ew.participation(spent, mixed, now)[0], False)
    finally:
        ew.STATE_ROOT = saved
        shutil.rmtree(root, ignore_errors=True)


def test_a_live_reading_beats_a_cached_one():
    """
    Every figure the tool shows comes from the statusLine of the last ping, so
    it is up to one interval old -- and the likeliest thing to have moved it
    since is the reader's own work, which is exactly what the recommendation is
    about to be made against. An account that has just been spent still read as
    usable, and `which` recommended it.

    Nothing here touches the network: the transport is replaced.
    """
    section("A live reading beats a cached one")

    restore, home, accounts = _switch_sandbox(signed_in_as="1")
    real_open = ew.urllib.request.urlopen
    try:
        now = time.time()
        for account in accounts:
            account.ensure_state_dir()
            ew.write_state(account, {
                "last_run": now - 1500,
                "rate_limits": {"five_hour": {"used_percentage": 12,
                                              "resets_at": now + 3600}}})
        check("the cached figure is what it was told",
              (ew.read_state(accounts[0])["rate_limits"]["five_hour"]
               ["used_percentage"]), 12)

        class Response(object):
            headers = {"anthropic-ratelimit-unified-5h-utilization": "0.97",
                       "anthropic-ratelimit-unified-5h-reset": str(int(now + 900)),
                       "anthropic-ratelimit-unified-7d-utilization": "0.40",
                       "anthropic-ratelimit-unified-7d-reset": str(int(now + 86400))}
            def __enter__(self): return self
            def __exit__(self, *a): return False
        ew.urllib.request.urlopen = lambda *a, **k: Response()

        limits, problem = ew.read_live_limits(accounts[0])
        check("a good reading reports no problem", problem, "")
        check("a live reading converts the fraction to a percentage",
              limits["five_hour"]["used_percentage"], 97)
        check("and carries the reset time", limits["five_hour"]["resets_at"],
              int(now + 900))
        check("the weekly limit comes too",
              limits["seven_day"]["used_percentage"], 40)

        ew.refresh_limits(accounts)
        state = ew.read_state(accounts[0])
        check("it replaces the cached figure",
              state["rate_limits"]["five_hour"]["used_percentage"], 97)
        check("and records where it came from", state["limits_source"], "live")
        check_true("and when", state.get("limits_read_at", 0) >= now)

        # An account that refuses is spent, whatever the last ping believed.
        def refuse(*a, **k):
            raise ew.urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
        ew.urllib.request.urlopen = refuse
        check("a refusal reads as spent",
              ew.read_live_limits(accounts[0])[0]["five_hour"]["used_percentage"],
              100)

        # A refusal that carries the headers says *which* limit refused, and
        # that is worth having: recorded as the 5-hour one, a spent weekly
        # limit would read as back in five hours rather than in four days --
        # and `which` would recommend it the moment the wrong clock ran out.
        def refuse_naming_the_limit(*a, **k):
            raise ew.urllib.error.HTTPError(
                "u", 429, "Too Many Requests",
                {"anthropic-ratelimit-unified-5h-utilization": "0.30",
                 "anthropic-ratelimit-unified-7d-utilization": "1.0",
                 "anthropic-ratelimit-unified-7d-reset":
                     str(int(now) + 4 * 86400)},
                None)
        ew.urllib.request.urlopen = refuse_naming_the_limit
        refused, problem = ew.read_live_limits(accounts[0])
        check("a refusal that names the limit is believed over the guess",
              refused["seven_day"]["used_percentage"], 100)
        check("and nothing is invented about the limit that did not refuse",
              refused["five_hour"]["used_percentage"], 30)
        blocked = ew.account_availability(
            accounts[0], {"rate_limits": refused, "available_at": now}, now)
        check("so the account is out until the weekly limit returns, not for 5 hours",
              blocked.until, int(now) + 4 * 86400)
        check("and that is an observed time, not a bound", blocked.exact, True)

        # Headers that claim nothing is spent do not outrank the refusal
        # itself: a request really was turned away.
        def refuse_saying_nothing(*a, **k):
            raise ew.urllib.error.HTTPError(
                "u", 429, "Too Many Requests",
                {"anthropic-ratelimit-unified-5h-utilization": "0.20"}, None)
        ew.urllib.request.urlopen = refuse_saying_nothing
        check("a refusal is spent even where its headers disagree",
              ew.read_live_limits(accounts[0])[0]["five_hour"]["used_percentage"],
              100)

        # Anything else leaves the cached figures alone rather than guessing.
        def broken(*a, **k):
            raise ew.urllib.error.URLError("no route to host")
        ew.urllib.request.urlopen = broken
        limits, problem = ew.read_live_limits(accounts[0])
        check("an unreachable network reads as nothing at all", limits, {})
        check_true("and says so rather than failing quietly",
                   "could not reach Claude" in problem)
        before = ew.read_state(accounts[1])["rate_limits"]
        ew.refresh_limits([accounts[1]])
        check("and leaves what was already known untouched",
              ew.read_state(accounts[1])["rate_limits"], before)

        # A live reading is only taken where it cannot start a window. Any
        # billed request starts one if none is running, and choosing that
        # moment is the whole job of the anchoring machinery -- a status
        # command must not move it.
        gone = {"last_run": now - 60,
                "rate_limits": {"five_hour": {"used_percentage": 20,
                                              "resets_at": now - 60}}}
        check("an account between windows is not probed",
              ew.probe_is_safe(gone, now), False)
        running = {"last_run": now - 60,
                   "rate_limits": {"five_hour": {"used_percentage": 20,
                                                 "resets_at": now + 900}}}
        check("one with a window running is", ew.probe_is_safe(running, now), True)
        check("and one that has never reported is not",
              ew.probe_is_safe({}, now), False)

        ew.write_state(accounts[0], gone)
        ew.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("probed an account that would have started a window"))
        ew.refresh_limits([accounts[0]])
        check("so refresh_limits skips it entirely",
              ew.read_state(accounts[0])["rate_limits"]["five_hour"]
              ["used_percentage"], 20)

        # A failure is announced, written down, and reported by doctor -- never
        # a silent fall back to figures half an hour old.
        ew.write_state(accounts[0], running)
        def broken_probe(*a, **k):
            raise ew.urllib.error.HTTPError("u", 404, "Not Found", {},
                                            io.BytesIO(
                                                ("model " + ew.LIVE_MODEL +
                                                 " not found").encode()))
        ew.urllib.request.urlopen = broken_probe
        out, err, _ = _capture(lambda: ew.refresh_limits([accounts[0]]))
        check_true("a retired probe model is named on sight",
                   "was rejected" in err and ew.LIVE_MODEL in err)
        check_true("and written to the account's log",
                   "live reading failed" in open(accounts[0].log_file).read())
        recorded = ew.read_state(accounts[0]).get("live_problem", "")
        check_true("and recorded so it outlives the moment",
                   "was rejected" in recorded)

        # A machine that only switches never takes a reading. It has no ping
        # directory to read a login from, and it is not the authority on these
        # accounts either -- `which` there answers from the schedule the
        # pinging machine published.
        saved_accounts = ew.ACCOUNTS_FILE
        try:
            ew.ACCOUNTS_FILE = os.path.join(home, "accounts.json")
            with open(ew.ACCOUNTS_FILE, "w") as f:
                json.dump({"pings": False, "accounts": [
                    {"name": a.name, "config_dir": a.config_dir}
                    for a in accounts]}, f)
            ew.write_state(accounts[0], running)
            ew.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("a switch-only machine asked Claude for a reading"))
            check("a machine that does not ping reads nothing",
                  ew.refresh_limits(accounts), {})
        finally:
            ew.ACCOUNTS_FILE = saved_accounts

        # No token, no request: an unconfigured account must not be asked about.
        os.remove(ew.credentials_path(accounts[1]))
        ew.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("asked Claude about an account with no login"))
        limits, problem = ew.read_live_limits(accounts[1])
        check("an account with no login is never asked about", limits, {})
        check_true("and the reason names the directory",
                   accounts[1].config_dir in problem)
    finally:
        ew.urllib.request.urlopen = real_open
        restore()


def test_the_schedule_is_treated_as_input_not_configuration():
    """
    `schedule.json` is copied between machines, so it arrives from elsewhere.
    `parse_accounts` validates names before they reach a path; nothing
    validated these, and a switch target taken from the file supplied both its
    store path and the identity used to check it.
    """
    section("The schedule is input, not configuration")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",),
                                              pings=False)
    try:
        now = time.time()
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": now, "accounts": [
                {"name": "../../../tmp/elsewhere", "account_uuid": "u",
                 "config_dir": "/tmp/anywhere", "usable_now": True,
                 "tier": "usable", "last_run": now - 60,
                 "expires_at": now + 60, "window_phase": 0},
                {"name": "2", "account_uuid": "uuid-2", "usable_now": True,
                 "tier": "usable", "last_run": now - 60,
                 "expires_at": now + 900, "window_phase": 0}]}, f)

        view = ew.schedule_view(accounts)
        names = [a.name for a in view[0]]
        check("a name that is not a name is dropped", names, ["2"])
        check("and no config_dir is taken from the file",
              [a.config_dir for a in view[0]],
              [ew.ping_config_dir("2")])

        # A target the local accounts.json does not configure is refused
        # outright rather than trusted from the file.
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": now, "accounts": [
                {"name": "9", "account_uuid": "u9", "usable_now": True,
                 "tier": "usable", "last_run": now - 60,
                 "expires_at": now + 60, "window_phase": 0}]}, f)
        raised = None
        try:
            _capture(lambda: ew.switch_account(accounts, None, sign_in=False))
        except ew.ConfigError as exc:
            raised = str(exc)
        check_true("an unconfigured account cannot be switched to", raised)
    finally:
        restore()


def test_the_refusals_fire_where_the_readme_says_to_switch():
    """
    Two of the five advertised refusals compared the parked login against the
    *ping* directory. On a --no-pings machine -- the configuration the README
    recommends for every laptop -- there is no ping directory, so both checks
    silently passed. And the copied-login check never looked at the login that
    is live right now, though `doctor` did.
    """
    section("The refusals fire on switch-only machines")

    # Wrong account parked, on a machine with no ping directories at all.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",),
                                              pings=False)
    try:
        now = time.time()
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": now, "accounts": [
                {"name": "1", "account_uuid": "uuid-1", "usable_now": True,
                 "tier": "usable", "last_run": now - 60, "expires_at": now + 60,
                 "window_phase": 0},
                {"name": "2", "account_uuid": "uuid-2", "usable_now": True,
                 "tier": "usable", "last_run": now - 60, "expires_at": now + 60,
                 "window_phase": 0}]}, f)
        store = ew.switch_store(accounts[1])
        config = json.load(open(store.config_json))
        config["oauthAccount"] = {"accountUuid": "uuid-stranger",
                                  "emailAddress": "stranger@example.com"}
        with open(store.config_json, "w") as f:
            json.dump(config, f)

        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("a store signed in as the wrong account is refused here too", code, 1)
        check_true("naming who it actually is", "stranger@example.com" in err)
    finally:
        restore()

    # A store that is a copy of the login live in ~/.claude right now.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[1])
        shutil.copyfile(ew.credentials_path(ew.user_login()),
                        ew.credentials_path(store))
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("a copy of the live login is refused by switch, not just doctor",
              code, 1)
        check_true("with the eight hours spelled out", "eight hours" in err)
    finally:
        restore()


def test_switching_refuses_where_it_cannot_work():
    """
    macOS keeps the credential in the Keychain, so a switch there writes a file
    Claude Code does not read -- and takes the store's only copy of that login
    on the way, leaving the user on the account they started with and one
    browser sign-in worse off. The README always said unsupported; nothing
    enforced it, and --no-pings installs cleanly there because it needs neither
    systemd nor a timer.
    """
    section("Switching refuses where it cannot work")

    check("nothing blocks it on Linux", ew.platform_blocker(), None)

    saved = sys.platform
    try:
        sys.platform = "darwin"
        blocker = ew.platform_blocker()
        check_true("but macOS is refused", blocker is not None)
        check_true("naming the platform", "darwin" in blocker.message)
        check_true("and saying why the file would not be read",
                   "Keychain" in blocker.hint)

        restore, home, accounts = _switch_sandbox(signed_in_as="1",
                                                  parked=("2",))
        try:
            before = _snapshot_user_files()
            out, err, code = _capture(lambda: ew.switch_account(
                accounts, "2", sign_in=False))
            check("the switch refuses rather than consuming the login", code, 1)
            check("nothing was written", _snapshot_user_files(), before)
            check("and the parked login is still parked",
                  os.path.exists(ew.credentials_path(
                      ew.switch_store(accounts[1]))), True)
        finally:
            restore()
    finally:
        sys.platform = saved


def test_an_override_is_reported_before_anything_reassuring():
    """
    `ANTHROPIC_API_KEY` decides which account is billed whatever this command
    does. The early "you are already signed in as that account" return skipped
    every blocker, so it reassured the user while their requests went to a
    pay-as-you-go account.
    """
    section("An override outranks every reassurance")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    saved = os.environ.get("ANTHROPIC_API_KEY")
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-test"
        out, err, code = _capture(lambda: ew.switch_account(accounts, "1",
                                                            sign_in=False))
        check("switching to the account already in use still refuses", code, 1)
        check_true("naming the variable", "ANTHROPIC_API_KEY" in err)
        check("and does not claim you are already on it",
              "already signed in" in out, False)
    finally:
        if saved is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = saved
        restore()


def test_doctor_notices_a_timer_running_the_wrong_script():
    """
    `is-enabled` and the next elapse stay true when the script a unit names has
    moved or been deleted, so an install can be enabled, scheduled, and failing
    every time. Renaming the checkout does it; so does a second clone taking
    over the shared template.
    """
    section("Doctor notices a timer running the wrong script")

    restore, home, accounts = _switch_sandbox(signed_in_as="1")
    try:
        check("nothing to say when no units are installed",
              ew.unit_target_findings(), [])

        unit = os.path.join(ew.UNIT_DIR, "claude-window-timing@.service")
        with open(unit, "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python3 "
                    "/gone/claude_window_timing.py ping %i\n")
        findings = ew.unit_target_findings()
        check("a unit running a deleted script is an error",
              [f.level for f in findings], ["error"])
        check_true("saying every ping is failing",
                   "Every ping is failing" in findings[0].hint)

        other = os.path.join(home, "other_checkout.py")
        open(other, "w").close()
        with open(unit, "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python3 {} ping %i\n".format(other))
        findings = ew.unit_target_findings()
        check("a unit owned by another checkout is a warning",
              [f.level for f in findings], ["warning"])
        check_true("explaining what taking it over would do",
                   "take them over" in findings[0].hint)

        with open(unit, "w") as f:
            f.write("[Service]\nExecStart=/usr/bin/python3 {} ping %i\n".format(
                os.path.abspath(ew.__file__)))
        check("a unit pointing here is fine", ew.unit_target_findings(), [])
    finally:
        restore()


def test_a_directory_the_wizard_asked_for_is_not_left_wide():
    """
    The sign-ins setup prints are run by the user, and Claude Code creates a
    config directory with whatever umask it is handed — 0775 on an ordinary
    machine. So the directory the tool just told somebody to put a login in
    ends up writable by their group, and `doctor` greets a brand-new install
    with a warning about a directory the install itself asked for. Seen on a
    fresh clone: two of them, one per account.

    A directory you can write is a file you can replace, whatever mode the
    file has, so this is worth more than a diagnostic.
    """
    section("A directory the wizard asked for is never left wide")
    restore, home, accounts = _switch_sandbox(names=("1", "2"), parked=("1",))
    try:
        store = ew.switch_store(accounts[0])
        # As `claude` would have left them, having created them itself.
        for directory in (ew.SWITCH_ROOT, store.config_dir,
                          accounts[0].config_dir):
            os.chmod(directory, 0o775)
        check("the fixture starts as loose as a real one",
              ew._permissions(store.config_dir), 0o775)

        changed = ew.tighten_login_dirs(accounts, pings=True)
        check("every directory holding a login is narrowed",
              ew._permissions(store.config_dir), 0o700)
        check("including the root they sit in",
              ew._permissions(ew.SWITCH_ROOT), 0o700)
        check("and the ping directories, which hold one too",
              ew._permissions(accounts[0].config_dir), 0o700)
        check_true("each one is reported rather than done silently",
                   store.config_dir in changed and ew.SWITCH_ROOT in changed)

        check("a second run has nothing left to do",
              ew.tighten_login_dirs(accounts, pings=True), [])
        check("and doctor stops warning about them",
              [f for f in ew.switch_findings(accounts)
               if "others can reach" in f.message], [])

        # A store nobody has signed into is not created here. It appearing
        # would tell `status` that switching is set up when it is not.
        missing = ew.switch_store(accounts[1]).config_dir
        check("a store that does not exist is not conjured up",
              os.path.exists(missing), False)

        # On a machine that does not ping, the ping directories are not this
        # machine's business at all.
        os.chmod(accounts[0].config_dir, 0o775)
        ew.tighten_login_dirs(accounts, pings=False)
        check("a switch-only machine leaves the ping directories alone",
              ew._permissions(accounts[0].config_dir), 0o775)
    finally:
        restore()


def test_a_credential_directory_is_never_left_wide():
    """
    os.makedirs applies its mode to the leaf only, so every intermediate landed
    at 0775. The credential inside was 0600 and substitutable anyway: directory
    write permission decides who can replace a file.
    """
    section("Credential directories are never left wide")

    root = tempfile.mkdtemp()
    try:
        deep = os.path.join(root, "switch", "sub")
        os.makedirs(deep)                       # default mode, as before
        os.chmod(os.path.dirname(deep), 0o775)
        os.chmod(deep, 0o775)
        ew.secure_dir(os.path.dirname(deep))
        ew.secure_dir(deep)
        check("an existing wide directory is tightened",
              [oct(stat.S_IMODE(os.stat(d).st_mode))
               for d in (os.path.dirname(deep), deep)], ["0o700", "0o700"])

        fresh = os.path.join(root, "fresh")
        ew.secure_dir(fresh)
        check("and a new one is created tight",
              oct(stat.S_IMODE(os.stat(fresh).st_mode)), "0o700")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_mutation_path_is_hardened():
    """
    The invariant used to be enforced by ordering alone, and ordering is what a
    crash, a Ctrl-C, a second terminal or a live session removes. These are the
    defences that do not depend on getting there first.
    """
    section("The mutation path is hardened")

    # -- every refusal precedes every write ---------------------------------
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        before = open(ew.credentials_path(ew.user_login())).read()
        with open(ew.USER_CONFIG_JSON, "w") as f:
            f.write('{"projects": {"broken')
        raised = None
        try:
            ew.install_login(ew.switch_store(accounts[1]))
        except ew.ConfigError as exc:
            raised = str(exc)
        check_true("install_login refuses an unreadable config", raised)
        check("and the credential was NOT swapped first",
              open(ew.credentials_path(ew.user_login())).read(), before)
        check("the parked login is still in its store",
              os.path.exists(ew.credentials_path(ew.switch_store(accounts[1]))),
              True)
    finally:
        restore()

    # -- two switches cannot run at once ------------------------------------
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        held = ew.acquire_switch_lock()
        check_true("a lock can be taken", held is not None)
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("a second switch refuses while one is running", code, 1)
        check_true("and says why", "Another switch is already running" in err)
        check("nothing was moved", ew.account_identity(ew.user_login())["email"],
              "a1@example.com")
        os.close(held)
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("once released it proceeds", code, 0)
    finally:
        restore()

    # -- a login that cannot be parked is kept where nothing deletes it -----
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        config = json.load(open(ew.USER_CONFIG_JSON))
        config["oauthAccount"] = {"accountUuid": "uuid-stranger",
                                  "emailAddress": "who@example.com"}
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump(config, f)
        stranded = open(ew.credentials_path(ew.user_login())).read()

        out, _, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                          sign_in=False))
        check("the switch happens", code, 0)
        check_true("and says where the unparkable login is kept",
                   "never pruned" in out)

        orphans = os.path.join(ew.SWITCH_ROOT, ".orphaned")
        kept = sorted(os.listdir(orphans))
        check("it is on disk", len(kept), 1)
        check("byte for byte",
              open(os.path.join(orphans, kept[0], ".credentials.json")).read(),
              stranded)

        # The half the suite never joined: pruning must not reach it.
        backups = os.path.join(ew.SWITCH_ROOT, ".backups")
        for n in range(ew.SWITCH_BACKUPS_KEPT + 5):
            os.makedirs(os.path.join(backups, "20200101-0000%02d" % n),
                        exist_ok=True)
        ew._prune_backups()
        check("backups are pruned",
              len(os.listdir(backups)) <= ew.SWITCH_BACKUPS_KEPT, True)
        check("the stranded login is untouched by pruning",
              sorted(os.listdir(orphans)), kept)
    finally:
        restore()

    # -- a credential is never on disk at a wider mode, even briefly --------
    root = tempfile.mkdtemp()
    saved_umask = os.umask(0o022)
    try:
        path = os.path.join(root, ".credentials.json")
        ew._write_atomically(path, '{"claudeAiOauth": {}}')
        check("credentials are created at 0600, not widened afterwards",
              oct(stat.S_IMODE(os.stat(path).st_mode)), "0o600")
    finally:
        os.umask(saved_umask)
        shutil.rmtree(root, ignore_errors=True)


def test_a_failure_mid_switch_is_a_sentence_not_a_traceback():
    """
    Two files are rewritten here. A full disk or a permission that changed
    underneath would otherwise surface as a stack trace at the one moment
    somebody most needs a plain sentence -- half way through, possibly with the
    credential already replaced. Which half happened decides what they should
    do, so it has to be said.
    """
    section("A failure mid-switch is a sentence")

    # Failing to install: nothing has moved yet.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    real_install = ew.install_login
    try:
        ew.install_login = lambda store: (_ for _ in ()).throw(
            IOError("No space left on device"))
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("it reports a failure rather than raising", code, 1)
        check_true("naming the cause", "No space left on device" in err)
        check_true("and saying nothing was moved", "no login has been moved" in err)
        check_true("with the backup pointed at", ".backups" in err)
        check("the parked login is still parked",
              ew.account_identity(ew.switch_store(accounts[1]))["has_token"], True)
    finally:
        ew.install_login = real_install
        restore()

    # Failing to park: the switch happened, the old login is in the backup only.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    real_park = ew.park_login
    try:
        ew.park_login = lambda *a, **k: (_ for _ in ()).throw(
            OSError("Permission denied"))
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("it reports a failure rather than raising", code, 1)
        check_true("saying the switch did happen", "Switched to account" in err)
        check_true("that the old login is only in the backup",
                   "only in the backup" in err)
        check_true("and where to put it back",
                   ew.switch_store(accounts[0]).config_dir in err)
        check("the switch really did land",
              ew.account_identity(ew.user_login())["email"], "a2@example.com")
    finally:
        ew.park_login = real_park
        restore()


def test_the_token_and_the_identity_move_together():
    """
    The token decides billing; oauthAccount decides what Claude Code says you
    are. Nothing in Claude Code reconciles them, and a session run with the two
    disagreeing rewrites the per-account caches under the token's account — so
    one account's extra-usage state ends up filed under another's name.
    """
    section("The token and the identity move together")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        ew.switch_account(accounts, "2")
        config = json.load(open(ew.USER_CONFIG_JSON))

        check("the identity followed the token",
              config["oauthAccount"]["accountUuid"], "uuid-2")
        check("and so did the email it displays",
              config["oauthAccount"]["emailAddress"], "a2@example.com")
        check("the outgoing account's billing cache was dropped",
              "cachedExtraUsageDisabledReason" in config, False)
        check("and so was its organisation flag",
              "penguinModeOrgEnabled" in config, False)

        check("everything that is not the account's was left alone",
              config.get("projects"), {"/work": {"hasTrustDialogAccepted": True}})
        check("including unrelated counters", config.get("numStartups"), 7)

        check("every account-scoped key is one Claude Code refetches",
              [k for k in ew.ACCOUNT_SCOPED_KEYS if k in config], [])
    finally:
        restore()


def test_switching_refuses_what_cannot_possibly_work():
    """
    An error here means the switch could not achieve anything, not that it is
    unwise — which is why there is no --force to type past. In every one of
    these the user's files must come out untouched.
    """
    section("Switching refuses what cannot work")

    def refuses(label, expect, **kw):
        restore, home, accounts = _switch_sandbox(**kw)
        try:
            before = _snapshot_user_files()
            out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
            check("{}: refused".format(label), code, 1)
            check_true("{}: says why ({})".format(label, expect),
                       expect in (out + err))
            check("{}: changed nothing".format(label),
                  _snapshot_user_files(), before)
        finally:
            restore()

    refuses("no login parked", "No login is parked", signed_in_as="1", parked=())

    # An expired parked login.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[1])
        creds = json.load(open(ew.credentials_path(store)))
        creds["claudeAiOauth"]["refreshTokenExpiresAt"] = int(
            (time.time() - 86400) * 1000)
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        before = _snapshot_user_files()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("an expired parked login is refused", code, 1)
        check_true("and names expiry as the reason", "expired" in (out + err))
        check("with the user's files untouched", _snapshot_user_files(), before)
    finally:
        restore()

    # A store holding a copy of the login the pings use: the mistake that looks
    # like it works and signs you out later.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[1])
        shutil.copyfile(ew.credentials_path(accounts[1]),
                        ew.credentials_path(store))
        before = _snapshot_user_files()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("a parked login copied from the ping directory is refused", code, 1)
        check_true("and says they would sign each other out",
                   "signed out" in (out + err) or "rotate" in (out + err))
        check("with the user's files untouched", _snapshot_user_files(), before)
    finally:
        restore()

    # A store signed in as somebody else entirely.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[1])
        config = json.load(open(store.config_json))
        config["oauthAccount"] = {"accountUuid": "uuid-stranger",
                                  "emailAddress": "someone@else.com"}
        with open(store.config_json, "w") as f:
            json.dump(config, f)
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("a store signed in as the wrong account is refused", code, 1)
        check_true("and names who it actually is",
                   "someone@else.com" in (out + err))
    finally:
        restore()

    # The worst case this command has: ~/.claude.json present but unparseable.
    # _read_json answers "missing" and "unreadable" identically, so merging
    # into the result and writing it out would replace the user's projects,
    # trust decisions and settings with a file holding one key.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        with open(ew.USER_CONFIG_JSON, "w") as f:
            f.write('{"projects": {"/work": {"hasTrustDialog')   # truncated
        damaged = open(ew.USER_CONFIG_JSON).read()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("an unparseable config stops the switch", code, 1)
        check_true("saying what would have been lost",
                   "projects" in (out + err) and "settings" in (out + err))
        check("and the file is left exactly as it was",
              open(ew.USER_CONFIG_JSON).read(), damaged)
        check("the credential was not swapped either",
              ew.account_identity(ew.user_login())["has_token"] and
              json.load(open(ew.credentials_path(ew.user_login())))
              ["claudeAiOauth"]["refreshToken"], "live-refresh-1")

        # And the write itself refuses, so no other caller can reach it.
        raised = None
        try:
            ew.install_login(ew.switch_store(accounts[1]))
        except ew.ConfigError as exc:
            raised = str(exc)
        check_true("install_login refuses independently of the blocker",
                   raised and "cannot be parsed" in raised)
        check("still untouched", open(ew.USER_CONFIG_JSON).read(), damaged)
    finally:
        restore()

    # A store with a credential but no identity: switching would leave
    # ~/.claude.json naming the account being left while the other is billed.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        store = ew.switch_store(accounts[1])
        with open(store.config_json, "w") as f:
            json.dump({"numStartups": 1}, f)
        before = _snapshot_user_files()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("a parked login with no identity is refused", code, 1)
        check_true("and says the identity is what is missing",
                   "does not say which account" in (out + err))
        check("with the user's files untouched", _snapshot_user_files(), before)
    finally:
        restore()

    # Anything that outranks the saved login makes the whole operation a no-op.
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN"):
        restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
        os.environ[var] = "x"
        try:
            before = _snapshot_user_files()
            out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
            check("{} set: refused".format(var), code, 1)
            check_true("{} set: names the variable".format(var), var in err)
            check("{} set: changed nothing".format(var),
                  _snapshot_user_files(), before)
        finally:
            del os.environ[var]
            restore()

    # settings.json is checked too, because its env block overwrites the
    # process environment rather than deferring to it.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        os.makedirs(ew.USER_CONFIG_DIR, exist_ok=True)
        with open(os.path.join(ew.USER_CONFIG_DIR, "settings.json"), "w") as f:
            json.dump({"env": {"ANTHROPIC_API_KEY": "sk-x"}}, f)
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("an override in settings.json is refused too", code, 1)
        check_true("and points at the file", "settings.json" in err)
    finally:
        restore()


def test_switching_says_what_it_will_and_will_not_fix():
    """
    The things that are true, worth saying, and not reasons to stop: a spent
    window, a symlink that Claude Code was going to destroy anyway, and sessions
    that will move underneath the user without telling them.
    """
    section("Switching says what it will and will not fix")

    # A spent window is a warning, never a refusal: it is instantly reversible
    # and the user may well know something this tool does not.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        accounts[1].ensure_state_dir()
        ew.write_state(accounts[1], {
            "last_run": time.time() - 60,
            "rate_limits": {"five_hour": {"used_percentage": 100,
                                          "resets_at": time.time() + 3600}}})
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("a spent window does not block the switch", code, 0)
        check_true("but it is said out loud", "not usable yet" in out)
        check_true("and the switch still happened", "Switched your Claude Code" in out)
    finally:
        restore()

    # A symlink at the credentials path is replaced, and the user is told why
    # that is not this tool's doing.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        target = ew.credentials_path(ew.user_login())
        elsewhere = os.path.join(home, "elsewhere.json")
        shutil.move(target, elsewhere)
        os.symlink(elsewhere, target)
        keep = open(elsewhere).read()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("switching over a symlink works", code, 0)
        check_true("and says the link is being replaced", "symlink" in out)
        check("the link is gone, as Claude Code would have left it too",
              os.path.islink(target), False)
        check("and the file it pointed at is untouched",
              open(elsewhere).read(), keep)
    finally:
        restore()

    # What a switch says about sessions already open. Measured behaviour, and
    # not what was assumed: they follow it completely -- credentials are
    # re-read per request, and /status and /usage read the identity when run
    # rather than from the startup snapshot. Telling people to restart sent
    # them to do something with no effect.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    real_scan, real_env = ew.running_claude_sessions, os.environ.get("CLAUDECODE")
    try:
        ew.running_claude_sessions = lambda: [4242]
        os.environ.pop("CLAUDECODE", None)
        out, _, _ = _capture(lambda: ew.switch_account(accounts, "2",
                                                       sign_in=False))
        check_true("it says open sessions follow the switch",
                   "moves to this account on its next request" in out)
        check_true("and that their own status agrees",
                   "/status and /usage there report the new one" in out)
        check("it no longer tells anyone to restart",
              "estart" in out, False)
        check("and does not claim to be typing in one",
              "typing in" in out, False)
    finally:
        ew.running_claude_sessions = real_scan
        if real_env is not None:
            os.environ["CLAUDECODE"] = real_env
        restore()

    # Run from inside Claude Code, the most common case, it says so.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        ew.running_claude_sessions = lambda: [4242]
        os.environ["CLAUDECODE"] = "1"
        out, _, _ = _capture(lambda: ew.switch_account(accounts, "2",
                                                       sign_in=False))
        check_true("it names the session the command was run from",
                   "including the one you are typing in" in out)
    finally:
        ew.running_claude_sessions = real_scan
        if real_env is None:
            os.environ.pop("CLAUDECODE", None)
        else:
            os.environ["CLAUDECODE"] = real_env
        restore()

    # Nothing to do is not an error.
    restore, home, accounts = _switch_sandbox(signed_in_as="2", parked=("2",))
    try:
        before = _snapshot_user_files()
        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("switching to the account already in use is a no-op", code, 0)
        check_true("and says so plainly", "already signed in" in out)
        check("without rewriting anything", _snapshot_user_files(), before)
    finally:
        restore()

    # One account is not a thing to switch between.
    restore, home, accounts = _switch_sandbox(names=("1",), signed_in_as="1")
    try:
        out, err, code = _capture(lambda: ew.switch_account(accounts, None))
        check("one configured account refuses with a usage code", code, 2)
        check_true("and says why", "nothing to switch between" in err)
    finally:
        restore()


def test_switching_with_no_account_named_follows_which():
    """
    The default has to be the useful one: the reason to switch is almost always
    "this one is spent, give me the one that is not", and that is exactly the
    question `which` already answers.
    """
    section("Switching with no account named follows `which`")

    restore, home, accounts = _switch_sandbox(signed_in_as="1",
                                              parked=("1", "2"))
    try:
        now = time.time()
        for account, resets in ((accounts[0], now + 4 * 3600),
                                (accounts[1], now + 900)):
            account.ensure_state_dir()
            ew.write_state(account, {
                "last_run": now - 60,
                "rate_limits": {"five_hour": {"used_percentage": 10,
                                              "resets_at": resets}}})
        chosen, _ = ew.choose_account(accounts)
        check("`which` prefers the window that expires first", chosen.name, "2")

        out, err, code = _capture(lambda: ew.switch_account(accounts, None))
        check("and a bare switch goes there", code, 0)
        check("leaving the user signed in as it",
              ew.current_account(accounts).name, "2")
    finally:
        restore()


def test_the_user_is_told_which_account_they_are_on():
    """
    Nothing about switching appears until it has been set up: an install that
    only pings must read exactly as it did before this feature existed.
    """
    section("Saying which account the user is on")

    restore, home, accounts = _switch_sandbox(signed_in_as="1")
    try:
        for account in accounts:
            account.ensure_state_dir()
            ew.write_state(account, {"last_run": time.time() - 60,
                                     "rate_limits": {"five_hour": {
                                         "used_percentage": 10,
                                         "resets_at": time.time() + 3600}}})
        out, _, _ = _capture(lambda: ew.status(accounts))
        check("status says nothing about switching before it is set up",
              "Your Claude Code" in out, False)
        out, _, _ = _capture(lambda: ew.which(accounts))
        check("and neither does which", "switch" in out, False)
    finally:
        restore()

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        # Account 2's window expires first, so it is the one to spend while the
        # user is signed in as the other -- the case the suggestion exists for.
        now = time.time()
        for account, resets in ((accounts[0], now + 4 * 3600),
                                (accounts[1], now + 900)):
            account.ensure_state_dir()
            ew.write_state(account, {"last_run": now - 60,
                                     "rate_limits": {"five_hour": {
                                         "used_percentage": 10,
                                         "resets_at": resets}}})
        out, _, _ = _capture(lambda: ew.status(accounts))
        check_true("once set up, status names the account in use",
                   "Your Claude Code" in out and "label1" in out)
        check_true("and offers to move it to the one worth spending",
                   "switch" in out and "label2" in out)
        out, _, _ = _capture(lambda: ew.which(accounts))
        check_true("and which offers the command to move it",
                   "{} switch 2".format(ew.COMMAND) in out)
    finally:
        restore()

    # Being on the best of a bad lot is not being on the one to spend. The
    # headline has just said nothing can be used, and this line has to agree
    # with it rather than say the reassuring thing.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        now = time.time()
        for account in accounts:
            account.ensure_state_dir()
            ew.write_state(account, {"last_run": now - 60,
                                     "rate_limits": {"five_hour": {
                                         "used_percentage": 100,
                                         "resets_at": now + 900}}})
        out, _, _ = _capture(lambda: ew.status(accounts))
        check_true("it still says which account they are on",
                   "Your Claude Code  : account 1" in out)
        check("but does not call a spent one the account to spend",
              "the one to spend" in out, False)
        check_true("and the headline says as much",
                   "No account is usable yet" in out)
    finally:
        restore()

    # An account this tool has never heard of is a state worth naming, not a
    # crash: it is exactly what happens after someone logs in by hand.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        config = json.load(open(ew.USER_CONFIG_JSON))
        config["oauthAccount"] = {"accountUuid": "uuid-stranger",
                                  "emailAddress": "who@example.com"}
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump(config, f)
        check("an unknown login is reported as unknown",
              ew.current_account(accounts), None)
        out, _, _ = _capture(lambda: ew.status(accounts))
        check_true("and status says so rather than guessing",
                   "does not know" in out)
    finally:
        restore()


def test_a_switch_backs_up_what_it_replaces():
    """
    These two files are the difference between a signed-in Claude Code and a
    browser login. This is the only command here that rewrites them.
    """
    section("A switch backs up what it replaces")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        before_creds = open(ew.credentials_path(ew.user_login())).read()
        before_config = open(ew.USER_CONFIG_JSON).read()
        ew.switch_account(accounts, "2")

        backups = os.path.join(ew.SWITCH_ROOT, ".backups")
        stamps = sorted(os.listdir(backups))
        check("one backup was taken", len(stamps), 1)
        saved = os.path.join(backups, stamps[0])
        check("the credentials it replaced are recoverable",
              open(os.path.join(saved, "credentials.json")).read(), before_creds)
        check("and so is the config", open(os.path.join(saved, "claude.json")).read(),
              before_config)
        check("the backup is not world-readable",
              oct(stat.S_IMODE(os.stat(
                  os.path.join(saved, "credentials.json")).st_mode)), "0o600")

        # And they do not pile up for ever.
        for n in range(ew.SWITCH_BACKUPS_KEPT + 3):
            os.makedirs(os.path.join(backups, "20200101-0000%02d" % n),
                        exist_ok=True)
        ew._prune_backups()
        check("old backups are pruned to the limit",
              len(os.listdir(backups)) <= ew.SWITCH_BACKUPS_KEPT, True)
    finally:
        restore()


def test_an_unknown_login_is_backed_up_rather_than_lost():
    """
    Someone who signed in by hand has a login this tool cannot name. Refusing
    would strand them; parking it under a name that is not theirs would be a
    lie. It goes to the backup, and the switch says so.
    """
    section("An unknown login is backed up rather than lost")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        config = json.load(open(ew.USER_CONFIG_JSON))
        config["oauthAccount"] = {"accountUuid": "uuid-stranger",
                                  "emailAddress": "who@example.com"}
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump(config, f)
        keep = open(ew.credentials_path(ew.user_login())).read()

        out, err, code = _capture(lambda: ew.switch_account(accounts, "2"))
        check("the switch still happens", code, 0)
        check_true("and says the old login was not parked",
                   "not parked" in out and "who@example.com" in out)

        backups = os.path.join(ew.SWITCH_ROOT, ".backups")
        stamp = sorted(os.listdir(backups))[0]
        check("the stranded login is in the backup",
              open(os.path.join(backups, stamp, "credentials.json")).read(), keep)
        check("and was not filed under an account it does not belong to",
              os.path.exists(ew.credentials_path(ew.switch_store(accounts[0]))),
              False)
    finally:
        restore()


def test_doctor_notices_a_store_going_stale():
    """
    Both failures here are invisible until the day you need to switch: a parked
    login that quietly expired, and one that is a copy of a login something else
    is already refreshing.
    """
    section("Doctor notices a store going stale")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        check("a healthy store produces no findings",
              ew.switch_findings(accounts), [])

        # The state every completed switch leaves behind, and the one this
        # check got wrong first time: the store of the account you are signed
        # in as is empty *because* its login is live in ~/.claude. Calling that
        # a gap is advice to go and make a second login nobody needs.
        os.makedirs(ew.switch_store(accounts[0]).config_dir, exist_ok=True)
        check("an empty store for the account in use is not a finding",
              ew.switch_findings(accounts), [])

        # For any other account, an empty store really is a gap: there is
        # nothing to switch to.
        config = json.load(open(ew.USER_CONFIG_JSON))
        config["oauthAccount"] = {"accountUuid": "uuid-2",
                                  "emailAddress": "a2@example.com"}
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump(config, f)
        findings = ew.switch_findings(accounts)
        check("but an empty store for one you are not on is",
              [f.level for f in findings], ["warning"])
        check_true("naming the account you cannot reach",
                   "label1" in findings[0].message)
        with open(ew.USER_CONFIG_JSON, "w") as f:
            json.dump({"oauthAccount": {"accountUuid": "uuid-1",
                                        "emailAddress": "a1@example.com"}}, f)
        shutil.rmtree(ew.switch_store(accounts[0]).config_dir)

        store = ew.switch_store(accounts[1])
        creds = json.load(open(ew.credentials_path(store)))
        creds["claudeAiOauth"]["refreshTokenExpiresAt"] = int(
            (time.time() - 3600) * 1000)
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        findings = ew.switch_findings(accounts)
        check("an expired parked login is an error",
              [f.level for f in findings], ["error"])
        check_true("naming the account", "label2" in findings[0].message)

        creds["claudeAiOauth"]["refreshTokenExpiresAt"] = int(
            (time.time() + 2 * 86400) * 1000)
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        findings = ew.switch_findings(accounts)
        check("one about to expire is a warning", [f.level for f in findings],
              ["warning"])

        # A login can be narrowed to a subset of its scopes on refresh and can
        # never be widened again -- the token endpoint refuses outright. So a
        # reduced one has to be noticed here rather than mid-switch, and the
        # advice has to be "replace it", not "fix it".
        creds["claudeAiOauth"]["refreshTokenExpiresAt"] = int(
            (time.time() + 30 * 86400) * 1000)
        creds["claudeAiOauth"]["scopes"] = list(ew.FULL_LOGIN_SCOPES)
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        check("a full-scope login is not a finding", ew.switch_findings(accounts), [])

        creds["claudeAiOauth"]["scopes"] = ["user:inference"]
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        findings = ew.switch_findings(accounts)
        check("a narrowed one is a warning", [f.level for f in findings],
              ["warning"])
        check_true("naming what it lost",
                   "user:profile" in findings[0].message)
        check_true("and saying it can only be replaced",
                   "cannot be widened" in findings[0].hint)

        # Older credential files record no scopes at all; silence beats a
        # warning invented from missing data.
        del creds["claudeAiOauth"]["scopes"]
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)
        check("a file that records no scopes says nothing",
              ew.switch_findings(accounts), [])
        creds["claudeAiOauth"]["scopes"] = list(ew.FULL_LOGIN_SCOPES)
        with open(ew.credentials_path(store), "w") as f:
            json.dump(creds, f)

        shutil.copyfile(ew.credentials_path(accounts[1]),
                        ew.credentials_path(store))
        levels = [f.level for f in ew.switch_findings(accounts)]
        check("a copied login is an error", levels, ["error"])
    finally:
        restore()

    # And nothing at all is said when the feature is not in use.
    restore, home, accounts = _switch_sandbox(signed_in_as="1")
    try:
        check("silent when switching was never set up",
              ew.switch_findings(accounts), [])
    finally:
        restore()


def test_only_one_function_writes_to_the_users_own_files():
    """
    The invariant, tightened for the one feature allowed to break it.

    Before switching existed, nothing here wrote to ~/.claude at all. Now
    exactly one function does, and this test fails if a second one ever starts
    — which is the failure that would otherwise be found by a user whose
    conversations went missing.
    """
    section("Only one function writes to the user's own files")

    here = os.path.dirname(os.path.abspath(__file__))
    source = open(os.path.join(here, "claude_window_timing.py")).read()
    tree = ast.parse(source)

    writes = {"_write_atomically", "copyfile", "copy", "copy2", "copytree",
              "replace", "remove", "unlink", "rmdir", "rmtree", "symlink",
              "rename", "makedirs", "chmod", "write_text"}
    writers = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or not call.args:
                continue
            name = (call.func.attr if isinstance(call.func, ast.Attribute)
                    else getattr(call.func, "id", ""))
            writing = name in writes or (name == "open" and len(call.args) > 1)
            if not writing:
                continue
            target = ast.dump(call.args[0])
            if "USER_CONFIG" in target or "user_login" in target:
                writers.add(node.name)

    check("exactly one function writes to the user's own files",
          sorted(writers), ["install_login"])

    # Static analysis stops at a variable, so the same claim is made again from
    # the outside: a switch may touch those two files and nothing else under
    # the user's directory.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",))
    try:
        os.makedirs(os.path.join(ew.USER_CONFIG_DIR, "projects", "mine"))
        with open(os.path.join(ew.USER_CONFIG_DIR, "projects", "mine", "a.jsonl"),
                  "w") as f:
            f.write('{"type":"user"}\n')
        with open(os.path.join(ew.USER_CONFIG_DIR, "settings.json"), "w") as f:
            json.dump({"theme": "dark"}, f)

        before = _tree_snapshot(ew.USER_CONFIG_DIR)
        ew.switch_account(accounts, "2")
        after = _tree_snapshot(ew.USER_CONFIG_DIR)

        changed = sorted(k for k in set(before) | set(after)
                         if before.get(k) != after.get(k))
        check("a switch changes exactly one file inside ~/.claude",
              changed, [os.path.join(ew.USER_CONFIG_DIR, ".credentials.json")])
        check("the user's conversation is untouched",
              after[os.path.join(ew.USER_CONFIG_DIR, "projects", "mine",
                                 "a.jsonl")], b'{"type":"user"}\n')
        check("and so are their settings",
              json.loads(after[os.path.join(ew.USER_CONFIG_DIR,
                                            "settings.json")].decode()),
              {"theme": "dark"})
        check("the installed credential is not world-readable",
              oct(stat.S_IMODE(os.stat(
                  ew.credentials_path(ew.user_login())).st_mode)), "0o600")
    finally:
        restore()


def test_running_sessions_are_looked_for_without_counting_our_own_pings():
    """
    A ping runs the same binary every half hour, so counting them would make the
    warning permanent and therefore worthless.
    """
    section("Running sessions exclude our own pings")

    found = ew.running_claude_sessions()
    check_true("the scan returns a list of pids", isinstance(found, list))
    check_true("all of them are integers",
               all(isinstance(pid, int) for pid in found))
    # A ping process is marked in its environment; that marker is what excludes
    # it, so the constant must keep existing for the exclusion to mean anything.
    check_true("pings are identifiable in the environment",
               ew.PING_MARKER_ENV in ew.build_claude_env(
                   ew.Account("1", "/tmp/nonexistent", 0)))


def test_a_machine_that_does_not_ping_is_never_told_to_ping():
    """
    The decision `--no-pings` records is the README's central one: the pings
    belong on ONE machine, because a second doubles what those accounts consume
    and buys nothing. Every command that answered as though this were the
    pinging machine quietly argued with it.

    Walked here as a new install on a second machine is walked: nothing pinged,
    no schedule copied across yet, and somebody typing the commands the tool
    itself lists.
    """
    section("A machine that does not ping is never told to ping")
    root = tempfile.mkdtemp()
    saved = (ew.ACCOUNTS_FILE, ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.UNIT_DIR,
             ew.SWITCH_ROOT, ew.ALIGNMENT_FILE, ew._systemctl, ew._run)

    class Ok(object):
        returncode = 0
        stdout = ""

    try:
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        ew.STATE_ROOT = os.path.join(root, "state")
        ew.SCHEDULE_FILE = os.path.join(root, "schedule.json")
        ew.UNIT_DIR = os.path.join(root, "units")
        ew.SWITCH_ROOT = os.path.join(root, "switch")
        ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
        ew._systemctl = lambda *a: Ok()
        ew._run = lambda cmd: Ok()
        os.makedirs(ew.UNIT_DIR)
        accounts = [ew.Account("1", os.path.join(root, "cfg-1"), 0),
                    ew.Account("2", os.path.join(root, "cfg-2"), 1)]
        ew._write_accounts_file(accounts, pings=False)
        check("the file records the decision", ew.pings_here(), False)

        # -- the recommendation ------------------------------------------
        _, reason = ew.choose_account(accounts, {"1": {}, "2": {}}, time.time())
        check_true("`which` sends them to the file, not to a ping",
                   "copy schedule.json" in reason and "run a ping" not in reason)

        said, _, _ = _capture(lambda: ew.status(accounts))
        check_true("and so does status", "copy schedule.json here" in said)
        # Spacing is worked out from what the pings observe. Here it is
        # arithmetic over an empty state that ends in advice to go and ping.
        check("no spacing report where nothing is spaced", "Spacing" in said, False)

        # -- the commands that only mean something where pings run --------
        for command, expected in (("ping", "sending one by hand"),
                                  ("realign", "no schedule of its own"),
                                  ("init", "no checkpoint to build")):
            out, err, code = _capture(lambda: ew.cli([command]))
            check("`{}` is refused rather than attempted".format(command),
                  code, 2)
            check_true("saying why: {}".format(expected), expected in err)
            check_true("and how to change the decision, if that is the mistake",
                       "./install.sh --pings" in err)

        out, err, code = _capture(lambda: ew.cli(["log"]))
        check("`log` explains where the log is", code, 1)
        check_true("which is the machine that pings",
                   "this machine does not ping" in err)

        # -- and `check` stops asking for ping directories ----------------
        out, _, code = _capture(lambda: ew.cli(["check"]))
        check_true("`check` says nothing about ping directories",
                   "does not exist" not in out and ".claude-1" not in out)
        check_true("but does say what this machine is missing",
                   "no logins parked" in out and "schedule.json" in out)

        # -- the discovery line names only what works here ----------------
        said, _, _ = _capture(lambda: ew.cli([]))
        listed = said.split("Other commands:")[1]
        check_true("realign and log are not offered", "realign" not in listed
                   and "log" not in listed)
        check_true("switch and which are", "switch" in listed
                   and "which" in listed)
    finally:
        (ew.ACCOUNTS_FILE, ew.STATE_ROOT, ew.SCHEDULE_FILE, ew.UNIT_DIR,
         ew.SWITCH_ROOT, ew.ALIGNMENT_FILE, ew._systemctl, ew._run) = saved
        shutil.rmtree(root, ignore_errors=True)


def test_a_machine_can_be_told_it_does_not_ping():
    """
    The pings belong on one machine; switching belongs on all of them. That
    fact has to be recorded rather than inferred, because everything downstream
    turns on it — a switch-only machine has no ping directories, no checkpoints
    and no timers, and a `doctor` that called all three broken would bury the
    one finding that matters there.
    """
    section("A machine can be told it does not ping")

    root = tempfile.mkdtemp()
    saved = ew.ACCOUNTS_FILE
    try:
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"accounts": [{"name": "1"}, {"name": "2"}]}, f)
        check("an ordinary accounts.json means this machine pings",
              ew.pings_here(), True)

        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"pings": False,
                       "accounts": [{"name": "1"}, {"name": "2"}]}, f)
        check("the flag turns it off", ew.pings_here(), False)
        check("and the accounts still parse",
              [a.name for a in ew.load_accounts(ew.ACCOUNTS_FILE)], ["1", "2"])

        os.remove(ew.ACCOUNTS_FILE)
        check("no accounts.json at all still means pinging", ew.pings_here(), True)
    finally:
        ew.ACCOUNTS_FILE = saved
        shutil.rmtree(root, ignore_errors=True)


def test_the_wizard_asks_for_each_sign_in_once_and_no_more():
    """
    Each directory that refreshes a token needs its own sign-in, and the count
    is the thing people judge this feature by. Two rules keep it as low as it
    can honestly be: anything already signed in is skipped, so re-running to
    add an account asks only for the new one; and the account you are already
    signed in as needs no store, because that login is *moved* into its store
    on the first switch away rather than copied into it now.
    """
    section("The wizard asks for each sign-in once")

    restore, home, accounts = _switch_sandbox(signed_in_as="1")
    try:
        needed = ew.sign_ins_needed(accounts, pings=True)
        where = [d for _what, _a, d in needed]
        check("the ping directories are already signed in, so are not asked for",
              [d for d in where if ".claude-" in d and "switch" not in d], [])
        check("only the store of the account not in use is asked for",
              where, [ew.switch_store(accounts[1]).config_dir])

        # Now park it, as a completed sign-in would.
        store = ew.switch_store(accounts[1])
        os.makedirs(store.config_dir, exist_ok=True)
        with open(ew.credentials_path(store), "w") as f:
            json.dump({"claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}}, f)
        check("re-running then asks for nothing",
              ew.sign_ins_needed(accounts, pings=True), [])
    finally:
        restore()

    # A machine that does not ping never asks for a ping directory. It also
    # cannot recognise the account it is already signed in as until the
    # schedule is copied across, so before that it asks for one store too many
    # -- which is exactly why setup says so out loud.
    restore, home, accounts = _switch_sandbox(signed_in_as="1", pings=False)
    try:
        where = [d for _w, _a, d in ew.sign_ins_needed(accounts, pings=False)]
        check("with no schedule it asks for every store and no ping directory",
              where, [ew.switch_store(a).config_dir for a in accounts])

        now = time.time()
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": now, "accounts": [
                {"name": "1", "account_uuid": "uuid-1", "last_run": now - 60,
                 "usable_now": True, "tier": "usable", "window_phase": 0}]}, f)
        where = [d for _w, _a, d in ew.sign_ins_needed(accounts, pings=False)]
        check("with the schedule copied across, one fewer",
              where, [ew.switch_store(accounts[1]).config_dir])
    finally:
        restore()

    # Grouped by account, not by kind. Many people sign in through Google SSO,
    # where re-authorising the same identity is a click and *changing* identity
    # is the expensive part -- so listing every ping directory and then every
    # store would walk a three-account install through A, B, C, B, C: four
    # identity switches where two would do.
    restore, home, accounts = _switch_sandbox(names=("1", "2", "3"), pings=False)
    try:
        order = [a.name for _what, a, _d in ew.sign_ins_needed(accounts,
                                                               pings=True)]
        check("each account's sign-ins are adjacent",
              order, ["1", "1", "2", "2", "3", "3"])
        check("which is one identity change per account, not one per directory",
              len([i for i in range(1, len(order)) if order[i] != order[i - 1]]),
              len(accounts) - 1)
    finally:
        restore()

    # Nobody signed in to Claude Code at all -- a machine where none of this
    # has ever run. Nothing can be adopted, so the count is the full 2N with
    # the pings and N without, and both have to be reachable: needing an
    # existing login before you can set up a login would be a circle.
    restore, home, accounts = _switch_sandbox(pings=False)
    try:
        check("nothing is signed in, so nothing is recognised",
              ew.current_account(accounts), None)
        check("without the pings that is one sign-in per account",
              len(ew.sign_ins_needed(accounts, pings=False)), len(accounts))
    finally:
        restore()

    restore, home, accounts = _switch_sandbox(pings=False)
    try:
        # Ping directories absent as well: the state of a brand new machine.
        needed = ew.sign_ins_needed(accounts, pings=True)
        check("with the pings it is two per account", len(needed),
              2 * len(accounts))
        check("still grouped, so the browser changes identity twice not four "
              "times", [a.name for _w, a, _d in needed], ["1", "1", "2", "2"])
    finally:
        restore()


def test_the_installer_insists_on_claude_code_in_both_modes():
    """
    Both modes need the CLI, for different reasons: the pings run it, and a
    parked login is *created* by running it and signing in. Installing without
    it would write a configuration that cannot do anything yet, so it fails
    early and says where to get it.

    systemd is different — only the pings need it, so requiring it on a machine
    that will never own a timer would turn a working setup into an error for a
    component it never uses.
    """
    section("The installer insists on Claude Code")

    here = os.path.dirname(os.path.abspath(__file__))
    home = tempfile.mkdtemp()
    try:
        for mode in ([], ["--no-pings"]):
            label = " ".join(mode) or "with the pings"
            # A PATH with python3 but no claude, and a HOME with none either.
            result = subprocess.run(
                ["bash", os.path.join(here, "install.sh")] + mode,
                cwd=here, capture_output=True, text=True,
                env={"HOME": home, "PATH": "/usr/bin:/bin"})
            check("{}: refuses without the CLI".format(label),
                  result.returncode, 1)
            check_true("{}: says what is missing".format(label),
                       "Claude Code CLI not found" in result.stderr)
            check_true("{}: and where to get it".format(label),
                       "claude.ai/download" in result.stderr)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def test_a_machine_without_systemd_can_still_install_the_switcher():
    """
    The prerequisite that has to wait for an answer.

    systemd runs the pings and nothing else, so whether it is required depends
    on a question the shell script has not asked yet. Deciding it from the
    --no-pings flag turned away a machine that had no systemd and no intention
    of pinging -- refusing an install over a component it would never use.
    """
    section("A machine without systemd can still install the switcher")

    real = ew._command_exists
    try:
        ew._command_exists = lambda name: name not in ("systemctl", "systemd-run")

        check("nothing stops a machine that will not ping",
              ew.systemd_blockers(pings=False), [])

        blockers = ew.systemd_blockers(pings=True)
        check("but pinging without systemd is an error",
              [f.level for f in blockers], ["error"])
        check_true("that offers the install which would work",
                   "--no-pings" in blockers[0].hint)

        # systemd present but systemd-run missing: the pings still work, only
        # the boundary correction is lost, so it must not stop the install.
        ew._command_exists = lambda name: name != "systemd-run"
        blockers = ew.systemd_blockers(pings=True)
        check("a missing systemd-run is only a warning",
              [f.level for f in blockers], ["warning"])
        check_true("and says what still works",
                   "still run" in blockers[0].hint)

        ew._command_exists = lambda name: True
        check("a complete machine has nothing to report",
              ew.systemd_blockers(pings=True), [])
    finally:
        ew._command_exists = real

    # End to end: the wizard stops before writing anything.
    root = tempfile.mkdtemp()
    saved = (ew.ACCOUNTS_FILE, ew._command_exists)
    try:
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        ew._command_exists = lambda name: name != "systemctl"
        out, _, code = _capture(
            lambda: ew.setup(argv_accounts=2, pings=True, assume_yes=True))
        check("choosing the pings without systemd stops the wizard", code, 1)
        check("and it wrote no configuration on the way out",
              os.path.exists(ew.ACCOUNTS_FILE), False)
        check_true("having said which install would work", "--no-pings" in out)
    finally:
        (ew.ACCOUNTS_FILE, ew._command_exists) = saved
        shutil.rmtree(root, ignore_errors=True)


def test_the_first_switch_on_a_machine_that_has_never_run_claude_code():
    """
    Someone can install this before ever signing in to Claude Code — indeed
    that is the ordinary case on a new laptop, where the store sign-in *is*
    their first login. The first switch then has nothing to park and nothing to
    back up, and has to create both files from nothing rather than assume they
    are there.
    """
    section("The first switch on a machine that has never run Claude Code")

    restore, home, accounts = _switch_sandbox(parked=("2",), pings=False)
    try:
        check("~/.claude does not exist yet",
              os.path.exists(ew.USER_CONFIG_DIR), False)
        check("nor does ~/.claude.json",
              os.path.exists(ew.USER_CONFIG_JSON), False)

        out, err, code = _capture(lambda: ew.switch_account(accounts, "2",
                                                            sign_in=False))
        check("the first switch succeeds", code, 0)
        check("it is signed in as the account switched to",
              ew.account_identity(ew.user_login())["email"], "a2@example.com")
        check("which is recognised from then on",
              ew.current_account(accounts).name, "2")
        check("and the store is empty, as after any switch",
              os.path.exists(ew.credentials_path(ew.switch_store(accounts[1]))),
              False)
        check("nothing claims to have parked a login that was never there",
              "Parked" in out, False)
        # Nor to have found one and declined to park it: there was none.
        check("nor to have found a login here at all",
              "The login that was here" in out, False)
    finally:
        restore()


def test_a_switch_only_machine_knows_which_account_it_is_on():
    """
    A second machine has no ping directories and nothing parked until its first
    switch, so it can see no identities of its own at all. The pinging machine
    already publishes each account's UUID in schedule.json — the file the
    README has people copy across — and without consulting it, the first switch
    on a laptop would fail to park a login it could not name.
    """
    section("A switch-only machine knows which account it is on")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",),
                                              pings=False)
    try:
        check("with nothing published, the account is unrecognised",
              ew.current_account(accounts), None)

        now = time.time()
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": now, "accounts": [
                {"name": "1", "label": "label1", "account_uuid": "uuid-1",
                 "usable_now": True, "tier": "usable", "last_run": now - 60,
                 "expires_at": now + 4 * 3600, "window_phase": 0,
                 "used_percentage": 5},
                {"name": "2", "label": "label2", "account_uuid": "uuid-2",
                 "usable_now": True, "tier": "usable", "last_run": now - 60,
                 "expires_at": now + 900, "window_phase": 0,
                 "used_percentage": 5}]}, f)

        found = ew.current_account(accounts)
        check("the published schedule names it", found and found.name, "1")

        # And a bare switch must choose from those readings rather than from
        # the nothing this machine knows on its own.
        out, err, code = _capture(lambda: ew.switch_account(accounts, None,
                                                            sign_in=False))
        check("a bare switch succeeds on a machine that pings nothing", code, 0)
        check("and goes to the account whose window expires first",
              ew.current_account(accounts).name, "2")
    finally:
        restore()


def test_doctor_on_a_switch_only_machine_reports_only_what_applies():
    """
    Every ping-shaped check would fail here and none of them would mean
    anything. What is worth saying is narrower: the parked logins, and whether
    there is a schedule to read.
    """
    section("Doctor on a switch-only machine")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",),
                                              pings=False)
    saved = ew.ACCOUNTS_FILE
    try:
        ew.ACCOUNTS_FILE = os.path.join(home, "accounts.json")
        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"pings": False, "accounts": [{"name": "1"},
                                                    {"name": "2"}]}, f)
        out, _, code = _capture(lambda: ew.doctor(accounts))
        check_true("it says nothing about checkpoints",
                   "checkpoint" not in out.lower())
        check_true("nothing about timers", "timer" not in out.lower())
        check_true("and nothing about ping directories not being signed in",
                   "not signed in" not in out.lower())
        check_true("but it does ask for the schedule it needs",
                   "schedule.json" in out)

        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": time.time(),
                       "accounts": [{"name": "1", "account_uuid": "uuid-1"}]}, f)
        out, _, code = _capture(lambda: ew.doctor(accounts))
        check("with a schedule and a parked login, it is happy", code, 0)
        check_true("and says so", "Everything checks out" in out)
    finally:
        ew.ACCOUNTS_FILE = saved
        restore()


def test_setup_can_install_the_switcher_without_the_pings():
    """
    The whole point of the second machine: no timers, no checkpoints, and no
    quota spent — while `switch` still works.
    """
    section("Setup without the pings")

    calls_seen = []

    def after(home, repo, calls):
        calls_seen.extend(calls)

    code, home, repo, calls = _clean_install(
        "", setup_kwargs={"argv_accounts": 2, "pings": False,
                          "assume_yes": True}, after=after)
    check("setup succeeds without pinging", code, 0)

    document = json.load(open(os.path.join(repo, "accounts.json")))
    check("the mode is recorded for every later command",
          document.get("pings"), False)
    check("the accounts are still written",
          [a["name"] for a in document["accounts"]], ["1", "2"])

    check("no timer was created",
          [c for c in calls if any("timer" in str(part) for part in c)], [])
    check("no checkpoint was built",
          os.path.exists(os.path.join(repo, "state", "1", "session_id.txt")),
          False)
    check_true("the launcher is still written",
               os.path.exists(os.path.join(repo, "bin", "claude-window")))
    # The sandbox pre-creates the ping directories to stand in for a sign-in,
    # so their absence is not the thing to check. What setup would have added
    # is the working directory a ping runs from, and it did not.
    check("nothing prepared a directory for pinging out of",
          os.path.isdir(os.path.join(home, ".claude-1", "pingcwd")), False)


def test_turning_the_pings_off_actually_turns_them_off():
    """
    The failure this prevents was made for real while building it: answering
    "this machine does not ping" recorded the answer and left the timers
    running, so the machine reported one thing and did another -- and quietly
    went on consuming the quota the question exists to save.
    """
    section("Turning the pings off turns them off")

    def after(home, repo, calls):
        del calls[:]                      # only what the second run does
        saved = ew.ACCOUNTS_FILE
        try:
            ew.setup(argv_accounts=2, pings=False, assume_yes=True)
        finally:
            ew.ACCOUNTS_FILE = saved

    code, home, repo, calls = _clean_install("2\ny\n\ny\n", after=after)
    check("the first install succeeded", code, 0)

    document = json.load(open(os.path.join(repo, "accounts.json")))
    check("the machine now records that it does not ping",
          document.get("pings"), False)
    disabled = [c for c in calls if "disable" in c]
    check("and every timer was disabled", len(disabled), 2)
    check("the unit files are gone",
          [f for f in os.listdir(os.path.join(home, ".config", "systemd", "user"))
           if f.startswith("claude-window-timing")], [])
    check_true("the checkpoints are kept, so turning it back on is cheap",
               os.path.exists(os.path.join(repo, "state", "1", "session_id.txt")))


def test_doctor_catches_a_machine_pinging_when_it_says_it_does_not():
    """The same mismatch, if it is ever reached by another route."""
    section("Doctor catches pinging that should have stopped")

    restore, home, accounts = _switch_sandbox(signed_in_as="1", parked=("2",),
                                              pings=False)
    saved = ew.ACCOUNTS_FILE
    try:
        ew.ACCOUNTS_FILE = os.path.join(home, "accounts.json")
        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"pings": False, "accounts": [{"name": "1"},
                                                    {"name": "2"}]}, f)
        with open(ew.SCHEDULE_FILE, "w") as f:
            json.dump({"window_hours": 5, "written_at": time.time(),
                       "accounts": [{"name": "1", "account_uuid": "uuid-1"}]}, f)
        out, _, code = _capture(lambda: ew.doctor(accounts))
        check("a clean switch-only machine is happy", code, 0)

        open(os.path.join(ew.UNIT_DIR, accounts[1].timer_unit), "w").close()
        out, _, code = _capture(lambda: ew.doctor(accounts))
        check("a leftover timer is an error", code, 1)
        check_true("that says what it costs",
                   "doubles what those accounts consume" in out)
        check_true("and how to fix it either way",
                   "--no-pings" in out and "--pings" in out)
    finally:
        ew.ACCOUNTS_FILE = saved
        restore()


def test_setup_names_an_account_it_is_about_to_drop():
    """
    Answering "1" where "2" was meant is an easy slip, and the layout shows
    only what survives it -- so the wizard would ask "Go ahead?" about a
    configuration quietly missing an account. Everything except the label
    survives, which is worth saying too: it makes the mistake cheap to undo.
    """
    section("Setup names an account it is about to drop")

    root = tempfile.mkdtemp()
    saved = (ew.ACCOUNTS_FILE, ew._command_exists, sys.stdin)
    try:
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        ew._command_exists = lambda name: True
        with open(ew.ACCOUNTS_FILE, "w") as f:
            json.dump({"accounts": [
                {"name": "1", "config_dir": "~/.claude-1", "label": "personal"},
                {"name": "2", "config_dir": "~/.claude-2", "label": "work"}]}, f)

        sys.stdin = io.StringIO("n\n")          # decline at "Go ahead?"
        out, _, code = _capture(
            lambda: ew.setup(argv_accounts=1, pings=True))
        check("declining changes nothing", code, 0)
        check_true("it says an account is being dropped", "Dropping" in out)
        check_true("and which one, by the name the user gave it",
                   "2 (work)" in out)
        check_true("it says the timer stops", "timer stops" in out)
        check_true("and that everything else survives",
                   "left exactly as" in out and "label" in out)
        check("the configuration was not touched, since it was declined",
              [a["name"] for a in json.load(open(ew.ACCOUNTS_FILE))["accounts"]],
              ["1", "2"])

        # Nothing to say when the count is unchanged.
        sys.stdin = io.StringIO("n\n")
        out, _, _ = _capture(lambda: ew.setup(argv_accounts=2, pings=True))
        check("no warning when no account is dropped", "Dropping" in out, False)
    finally:
        (ew.ACCOUNTS_FILE, ew._command_exists, sys.stdin) = saved
        shutil.rmtree(root, ignore_errors=True)


def test_setup_says_the_pings_belong_on_one_machine():
    """
    The one thing someone installing this on their third laptop needs to be
    told, at the moment they are deciding.
    """
    section("Setup says where the pings belong")

    buf = io.StringIO()
    out, sys.stdout = sys.stdout, buf
    held, sys.stdin = sys.stdin, io.StringIO("2\nn\ny\n\n")
    saved = (ew.HOME, ew.ACCOUNTS_FILE, ew.SWITCH_ROOT, ew.STATE_ROOT,
             ew.BIN_DIR, ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON,
             ew.SCHEDULE_FILE, ew.UNIT_DIR, ew._systemctl)
    root = tempfile.mkdtemp()
    try:
        ew.HOME = os.path.join(root, "home")
        # Answering "n" here tears down any timers it finds. Without these two
        # that teardown runs against the machine the tests are running on --
        # which is exactly how this test disabled a live install once.
        ew.UNIT_DIR = os.path.join(root, "units")
        ew._systemctl = lambda *a: type("Ok", (), {"returncode": 0,
                                                   "stdout": ""})()
        ew.ACCOUNTS_FILE = os.path.join(root, "accounts.json")
        ew.SWITCH_ROOT = os.path.join(ew.HOME, ".claude-switch")
        ew.STATE_ROOT = os.path.join(root, "state")
        ew.BIN_DIR = os.path.join(root, "bin")
        ew.USER_CONFIG_DIR = os.path.join(ew.HOME, ".claude")
        ew.USER_CONFIG_JSON = os.path.join(ew.HOME, ".claude.json")
        ew.SCHEDULE_FILE = os.path.join(root, "schedule.json")
        os.makedirs(ew.HOME)
        ew.setup()
    finally:
        sys.stdout, sys.stdin = out, held
        (ew.HOME, ew.ACCOUNTS_FILE, ew.SWITCH_ROOT, ew.STATE_ROOT, ew.BIN_DIR,
         ew.USER_CONFIG_DIR, ew.USER_CONFIG_JSON, ew.SCHEDULE_FILE,
         ew.UNIT_DIR, ew._systemctl) = saved
        shutil.rmtree(root, ignore_errors=True)

    said = buf.getvalue()
    check_true("it says the pings belong on one machine", "ONE machine" in said)
    check_true("and why a second one buys nothing",
               "doubles" in said and "buys nothing" in said)
    check_true("it says the other machines can still switch",
               "still switch accounts" in said)
    check_true("answering n explains what this machine is for",
               "does not ping" in said)
    check_true("and points at the file it needs",
               "schedule.json" in said)


def main():
    # Every path the tool reads or writes is redirected into one disposable
    # directory before a single test runs.
    #
    # Individual tests redirect what they need, but a test that forgets used to
    # fall through to the real machine: reading the developer's accounts.json,
    # their installed units, their ~/.claude. That is wrong twice over. It can
    # touch a working install, and it makes the suite's result depend on the
    # machine it runs on -- six tests here once failed because the accounts.json
    # sitting beside them had been edited, which tells a contributor nothing
    # about the code they just changed.
    #
    # Defaulting everything to a sandbox means forgetting is safe. A test that
    # wants the real thing has to say so.
    suite_root = tempfile.mkdtemp(prefix="window-timing-tests-")
    home = os.path.join(suite_root, "home")
    # Including HOME itself. Anything derived from it -- a ping directory, a
    # switch store, the bin directories a launcher can be linked into -- then
    # lands inside the sandbox rather than in the home of whoever ran this.
    ew.HOME = home
    ew.STATE_ROOT = os.path.join(suite_root, "state")
    ew.ACCOUNTS_FILE = os.path.join(suite_root, "accounts.json")
    ew.SCHEDULE_FILE = os.path.join(suite_root, "schedule.json")
    ew.BIN_DIR = os.path.join(suite_root, "bin")
    ew.UNIT_DIR = os.path.join(home, ".config", "systemd", "user")
    ew.USER_CONFIG_DIR = os.path.join(home, ".claude")
    ew.USER_CONFIG_JSON = os.path.join(home, ".claude.json")
    ew.SWITCH_ROOT = os.path.join(home, ".claude-switch")
    os.makedirs(ew.UNIT_DIR)
    os.makedirs(ew.USER_CONFIG_DIR)

    # No test may reach the network. A live reading is a real request against a
    # real account, so a test that made one would spend the developer's quota
    # and pass or fail on whether their wifi was up. Anything that wants to
    # exercise the live path replaces this locally.
    ew.urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(
        ew.urllib.error.URLError("the test suite does not have a network"))

    for test in (test_refusal_text, test_next_window_start, test_guard_rails,
                 test_anchor_scheduling,
                 test_the_anchor_is_booked_in_the_zone_systemd_reads,
                 test_statusline_parsing,
                 test_an_impossible_usage_report_is_ignored,
                 test_a_checkpoint_from_another_directory_is_rebuilt,
                 test_schedule_carries_account_identity,
                 test_the_status_line_writes_only_where_it_was_told,
                 test_resume_baseline_race, test_without_systemd, test_formatting,
                 test_usage_line, test_init_refuses_to_checkpoint_a_refusal,
                 test_pty_drain_tolerates_a_departed_child,
                 test_run_interactive_survives_an_immediate_exit,
                 test_account_paths, test_the_accounts_file_itself,
                 test_accounts_file, test_claude_env,
                 test_the_command_surface,
                 test_every_command_describes_what_it_actually_does,
                 test_every_command_the_output_suggests_can_be_typed,
                 test_every_command_routes_to_the_thing_it_names,
                 test_looking_at_the_schedule_does_not_change_it,
                 test_the_shell_scripts_call_commands_that_exist,
                 test_doctor_spots_residue_from_an_earlier_install,
                 test_uninstall_removes_the_units_and_nothing_else,
                 test_two_accounts_stay_out_of_each_others_files,
                 test_validation_catches_the_expensive_mistakes,
                 test_stale_reset_rolls_forward,
                 test_choosing_between_accounts,
                 test_an_unusable_login_is_never_recommended,
                 test_schedule_is_publishable_for_other_machines,
                 test_a_second_machine_answers_from_the_schedule,
                 test_an_account_the_schedule_has_never_heard_of,
                 test_a_login_that_stopped_working_is_reported,
                 test_what_it_says_in_every_state_it_can_be_in,
                 test_the_age_of_the_figures_is_never_overstated,
                 test_the_status_display_shows_the_unusual_parts,
                 test_spacing_optimiser,
                 test_phase_is_lost_only_when_pings_cannot_get_through,
                 test_correction_policy,
                 test_realign_is_the_only_way_a_long_hold_happens,
                 test_the_json_report_is_a_contract,
                 test_what_a_ping_records_from_how_it_went,
                 test_a_hold_suppresses_the_ping_and_nothing_else,
                 test_an_unusable_account_is_still_pinged,
                 test_the_log_reads_in_the_order_it_was_written,
                 test_setup_lays_accounts_out_sensibly,
                 test_setup_and_init_refuse_the_obviously_wrong,
                 test_the_first_run_screen_says_the_things_that_stop_people,
                 test_the_command_can_be_typed_after_installing,
                 test_the_pings_are_told_to_outlive_the_login,
                 test_the_launcher_can_reach_the_user_manager,
                 test_the_timer_is_told_where_the_cli_is,
                 test_a_timer_that_will_never_fire_again_is_noticed,
                 test_nothing_touches_the_users_own_directory,
                 test_a_clean_install_from_nothing,
                 test_install_uninstall_purge_and_install_again,
                 test_three_accounts_install_and_space_correctly,
                 test_removing_an_account_stops_its_timer,
                 test_a_clean_install_refuses_a_refused_first_message,
                 test_doctor_notices_a_deployment_going_wrong,
                 test_a_login_is_never_in_two_places_at_once,
                 test_an_interrupted_switch_never_leaves_a_login_in_two_places,
                 test_a_spent_account_is_never_recommended,
                 test_a_live_reading_beats_a_cached_one,
                 test_the_schedule_is_treated_as_input_not_configuration,
                 test_the_refusals_fire_where_the_readme_says_to_switch,
                 test_switching_refuses_where_it_cannot_work,
                 test_an_override_is_reported_before_anything_reassuring,
                 test_doctor_notices_a_timer_running_the_wrong_script,
                 test_a_directory_the_wizard_asked_for_is_not_left_wide,
                 test_a_credential_directory_is_never_left_wide,
                 test_the_mutation_path_is_hardened,
                 test_a_failure_mid_switch_is_a_sentence_not_a_traceback,
                 test_the_token_and_the_identity_move_together,
                 test_switching_refuses_what_cannot_possibly_work,
                 test_switching_says_what_it_will_and_will_not_fix,
                 test_switching_with_no_account_named_follows_which,
                 test_the_user_is_told_which_account_they_are_on,
                 test_a_switch_backs_up_what_it_replaces,
                 test_an_unknown_login_is_backed_up_rather_than_lost,
                 test_doctor_notices_a_store_going_stale,
                 test_only_one_function_writes_to_the_users_own_files,
                 test_running_sessions_are_looked_for_without_counting_our_own_pings,
                 test_a_machine_that_does_not_ping_is_never_told_to_ping,
                 test_a_machine_can_be_told_it_does_not_ping,
                 test_the_wizard_asks_for_each_sign_in_once_and_no_more,
                 test_the_installer_insists_on_claude_code_in_both_modes,
                 test_a_machine_without_systemd_can_still_install_the_switcher,
                 test_the_first_switch_on_a_machine_that_has_never_run_claude_code,
                 test_a_switch_only_machine_knows_which_account_it_is_on,
                 test_doctor_on_a_switch_only_machine_reports_only_what_applies,
                 test_setup_can_install_the_switcher_without_the_pings,
                 test_turning_the_pings_off_actually_turns_them_off,
                 test_doctor_catches_a_machine_pinging_when_it_says_it_does_not,
                 test_setup_names_an_account_it_is_about_to_drop,
                 test_setup_says_the_pings_belong_on_one_machine):
        test()
    # Nothing armed on the way out, whatever a test did or failed to do.
    for name in (TEST_PREFIX, TEST_PREFIX + "-a", TEST_PREFIX + "-b"):
        ew.cancel_anchor(ew.Account(name, "/tmp/nonexistent", 0))

    print("\n{}".format("-" * 60))
    if FAILURES:
        print("{} FAILED:".format(len(FAILURES)))
        for name in FAILURES:
            print("  - {}".format(name))
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
