"""
Tests for claude_early_window.

By default nothing here contacts Claude, spawns a session, or spends any of your
usage window. The pieces that decide *when* to ping, *which account* to use, and
*where conversations live* are the ones worth testing, because getting them wrong
is silent — the tool keeps running and just drifts, picks the wrong account, or
strands a conversation nobody can resume.

    python3 test_early_window.py

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
import subprocess
import sys
import tempfile
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_early_window as ew


FAILURES = []


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

def test_refusal_text():
    section("Refusal text -> (reset time, which limit)")
    now = datetime(2026, 8, 6, 16, 40, 0).timestamp()
    T = lambda *a: datetime(*a).timestamp()

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

    weekly_text = "You've hit your weekly limit · resets Aug 10, 10pm (Asia/Jerusalem)"
    session_text = "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)"

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


def _have_systemd():
    """
    Whether a systemd *user* manager is actually reachable from here.

    `systemctl --version` is not the question: the binary is installed in
    plenty of places where the per-user bus is not running — a container, a
    cron job, an ssh session with no login session behind it — and it answers
    without contacting anything. Asking the manager for a property is what
    tells the two apart, and getting it wrong means this file cannot be run by
    someone who just cloned the repository.
    """
    return ew._systemctl("show", "--property=Version", "--value").returncode == 0


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
                   os.path.join("projects", ew.SCRIPT_DIR.replace("/", "-"))))

    for attr in ("state_dir", "session_id_file", "checkpoint_backup",
                 "state_file", "statusline_file", "log_file"):
        check_true("accounts do not share {}".format(attr),
                   getattr(first, attr) != getattr(second, attr))

    check("units are systemd template instances", second.service_unit,
          "claude-early-window@2.service")
    check("timers match their service", second.timer_unit,
          "claude-early-window@2.timer")
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
    script = os.path.join(here, "claude_early_window.py")
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

        # -- the pinging machine trusts itself, never the file ---------------
        mine = ew.Account("1", os.path.join(laptop, "cfg-1"), 0)
        mine.ensure_state_dir()
        ew.write_state(mine, {"last_run": now})
        check("a machine with readings of its own ignores the schedule",
              ew.schedule_view([mine]), None)
    finally:
        ew.STATE_ROOT, ew.SCHEDULE_FILE = saved


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
            control = os.path.join(root, ".fake_claude.json")
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

    # Told once is enough: a hold already booked leaves the phases where they
    # were, so recomputing would find the same error every half hour and go on
    # asking for a confirmation that has already been given.
    ew.write_alignment({"participants": [a.name, b.name], "ever": [a.name, b.name],
                        "participants_since": now - 2 * W})
    st[b.name]["hold"] = {"from": now + 600, "until": now + 600 + 2.5 * HOUR,
                          "reason": "realigning, on your say-so"}
    before = len(open(b.log_file).read())
    extra = ew.apply_alignment(b, [a, b], st, st[b.name], now, now + 600)
    check_true("a booked correction is not proposed all over again",
               "realign --confirm" not in open(b.log_file).read()[before:])
    check("and the anchor is told to wait for the end of the hold, not the "
          "boundary", round(extra / 60), 150)

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


def test_a_hold_suppresses_the_ping_and_nothing_else():
    section("A hold skips the ping, and only for timing")
    ew.STATE_ROOT = tempfile.mkdtemp()
    ew.ALIGNMENT_FILE = os.path.join(ew.STATE_ROOT, "alignment.json")
    now = time.time()
    account = temp_account()
    with open(account.session_id_file, "w") as f:
        f.write("sid")
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
                               "claude_early_window.py")).read()
    stoppers = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and getattr(inner.func, "id", "") == "_systemctl"
                    and any(getattr(a, "s", None) == "disable"
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
                     "[2026-08-13 00:01:43] Early-window run finished.",
                     ""]),
            (second, ["[2026-08-13 00:02:00] Starting early-window run"])):
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
                      "Exited with code: 0", "Early-window run finished."])
    check("and later accounts still interleave by time",
          shown[-1], "Starting early-window run")
    check("blank separators are not carried into the merged view",
          [line for line in buf.getvalue().splitlines() if not line.strip()], [])


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


def test_upgrading_keeps_the_existing_checkpoint():
    """
    The units run the checked-out script in place, so `git pull` swaps the code
    under a running service. If an upgrade looked like a fresh install it would
    build a new checkpoint and throw away a window phase that took days to settle.
    """
    section("Upgrading from the single-account layout")
    ew.STATE_ROOT = tempfile.mkdtemp()
    saved_dir, ew.SCRIPT_DIR = ew.SCRIPT_DIR, tempfile.mkdtemp()
    try:
        account = ew.Account("1", os.path.join(ew.STATE_ROOT, "cfg"), 0)
        for name, body in (("early_window_session_id.txt", "old-session"),
                           ("early_window_checkpoint.jsonl.bak", "{}\n"),
                           ("early_window_state.json", '{"boundary": 1}'),
                           ("claude_early_window.log", "[x] hello\n")):
            with open(os.path.join(ew.SCRIPT_DIR, name), "w") as f:
                f.write(body)

        moved = ew.migrate_legacy_state(account)
        check("every old file is accounted for", len(moved), 4)
        check("the checkpoint id survives untouched",
              open(account.session_id_file).read(), "old-session")
        check("and so does the schedule it had worked out",
              ew.read_state(account).get("boundary"), 1)
        check_true("the log comes along too", os.path.exists(account.log_file))
        # Moved, not copied: two copies would leave it ambiguous which is real.
        check_true("nothing is left behind at the old location",
                   not os.path.exists(os.path.join(ew.SCRIPT_DIR,
                                                   "early_window_state.json")))

        check("running it again does nothing", ew.migrate_legacy_state(account), [])

        # setup() returns before cli()'s migration runs, so it has to do its own
        # — otherwise the wizard sees "no checkpoint" and spends a ping
        # rebuilding one that already exists.
        import inspect
        body = inspect.getsource(ew.setup)
        check_true("the wizard migrates before it checks for a checkpoint",
                   body.index("migrate_legacy_state")
                   < body.index("session_id_file"))
        # An account that already has a checkpoint must never be overwritten.
        with open(os.path.join(ew.SCRIPT_DIR, "early_window_session_id.txt"), "w") as f:
            f.write("stray")
        check("a stray old file cannot displace a working checkpoint",
              ew.migrate_legacy_state(account), [])
        check("the working checkpoint is still the one in use",
              open(account.session_id_file).read(), "old-session")
    finally:
        ew.SCRIPT_DIR = saved_dir


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
        f.write("[x] Starting early-window run\n" * 5)
        f.write("[x] Early-window run finished\n" * 3)
    check("unfinished runs are counted", ew._log_run_counts(account), (5, 3))

    messages = [f.message for f in ew.validate_accounts([account])]
    check_true("an account with no login is reported",
               any("not signed in" in m for m in messages))

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
                   home=None, repo=None, after=None):
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
             ew.STATUSLINE_WAIT_SEC)
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
    ew._systemctl = lambda *a: record(("systemctl",) + a)
    ew._run = record
    # A stand-in Claude answers instantly, so the fixed pauses meant for a real
    # one are pure waiting.
    ew.STARTUP_WAIT_SEC, ew.COMPLETION_TIMEOUT_SEC, ew.STATUSLINE_WAIT_SEC = \
        0.4, 15, 4
    os.environ["HOME"] = home
    sys.stdin = io.StringIO(answers)
    if claude_control:
        with open(os.path.join(repo, "fake_claude.json"), "w") as f:
            json.dump(claude_control, f)
        os.rename(os.path.join(repo, "fake_claude.json"),
                  os.path.join(repo, ".fake_claude.json"))

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
            code = ew.setup()
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
         ew.COMPLETION_TIMEOUT_SEC, ew.STATUSLINE_WAIT_SEC) = saved
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
    source = open(os.path.join(here, "claude_early_window.py")).read()
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
    check_true("account 1's checkpoint is in its own directory",
               os.path.exists(os.path.join(home, ".claude-1", "projects",
                                           repo.replace("/", "-"),
                                           first + ".jsonl")))

    units = os.path.join(home, ".config", "systemd", "user")
    check_true("the unit template is written",
               os.path.exists(os.path.join(units, "claude-early-window@.service")))
    body = open(os.path.join(units, "claude-early-window@.service")).read()
    check_true("and runs an explicit ping subcommand", "ping %i" in body)
    check_true("the second account is staggered so they do not collide",
               os.path.exists(os.path.join(
                   units, "claude-early-window@2.timer.d", "stagger.conf")))
    stagger = open(os.path.join(
        units, "claude-early-window@2.timer.d", "stagger.conf")).read()
    check_true("and its drop-in restates the repeat interval",
               "OnUnitActiveSec" in stagger)

    enabled = [c for c in calls if c[:2] == ["systemctl", "enable"]]
    check("a timer is enabled per account", len(enabled), 2)
    check_true("both by name",
               {c[2] for c in enabled} == {"claude-early-window@1.timer",
                                           "claude-early-window@2.timer"})

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
                       and c[3] == "claude-early-window@{}.timer".format(n)
                       for c in calls) for n in ("1", "2")))
    check("no unit file is left behind",
          [n for n in os.listdir(units) if n.startswith("claude-early-window")],
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
          [n for n in os.listdir(units) if n.startswith("claude-early-window")],
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
                                           "claude-early-window@.service")))
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
          ["claude-early-window@{}.timer".format(n) for n in ("1", "2", "3")])

    units = os.path.join(home, ".config", "systemd", "user")
    check_true("the first account has no stagger drop-in",
               not os.path.isdir(os.path.join(
                   units, "claude-early-window@1.timer.d")))
    offsets = []
    for name in ("2", "3"):
        body = open(os.path.join(units, "claude-early-window@{}.timer.d".format(name),
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
                          "claude-early-window@{}.timer".format(name)), "w").close()
    os.makedirs(os.path.join(units, "claude-early-window@3.timer.d"))

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
               any("disable --now claude-early-window@3.timer" in c for c in flat))
    # A pending anchor is a transient unit with no file, so it survives a
    # disable and would restart the service it belongs to.
    check_true("and any anchor it left armed is stopped",
               any("stop claude-early-window-anchor-3.timer" in c for c in flat))
    check_true("the accounts that remain are still enabled",
               any("enable claude-early-window@1.timer" in c for c in flat)
               and any("enable claude-early-window@2.timer" in c for c in flat))
    check_true("and neither of them is disabled",
               not any("disable --now claude-early-window@1.timer" in c
                       for c in flat)
               and not any("disable --now claude-early-window@2.timer" in c
                           for c in flat))
    check_true("the removed account's stagger drop-in is gone",
               not os.path.isdir(os.path.join(
                   units, "claude-early-window@3.timer.d")))


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

    pattern = re.compile(r"claude_early_window\.py\"?\s+(--?[\w-]+|[\w-]+)")
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

        # Our own units failing is a different finding, made elsewhere; saying it
        # twice, and calling them foreign, would be worse than silence.
        ew._systemctl = stub("claude-early-window@1.timer loaded failed failed x\n"
                             "claude-early-window-anchor-2.timer loaded failed f\n")
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
        for name in ("claude-early-window@.service", "claude-early-window@.timer",
                     "claude-early-window.service"):
            open(os.path.join(units, name), "w").close()
        os.makedirs(os.path.join(units, "claude-early-window@2.timer.d"))
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
                       any("disable --now claude-early-window@{}.timer".format(name)
                           in c for c in seen))
            # A pending anchor is transient and survives a disable, so it would
            # fire afterwards and restart the service.
            check_true("account {}'s anchor is stopped".format(name),
                       any("stop claude-early-window-anchor-{}.timer".format(name)
                           in c for c in seen))
        for name in ("claude-early-window@.service", "claude-early-window@.timer",
                     "claude-early-window.service"):
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


def main():
    # Log somewhere disposable: several decisions are only visible in the log, so
    # the tests read it, and they should not scribble on the running tool's.
    ew.STATE_ROOT = tempfile.mkdtemp()

    for test in (test_refusal_text, test_next_window_start, test_guard_rails,
                 test_anchor_scheduling, test_statusline_parsing,
                 test_the_status_line_writes_only_where_it_was_told,
                 test_resume_baseline_race, test_without_systemd, test_formatting,
                 test_usage_line, test_init_refuses_to_checkpoint_a_refusal,
                 test_pty_drain_tolerates_a_departed_child,
                 test_run_interactive_survives_an_immediate_exit,
                 test_account_paths, test_accounts_file, test_claude_env,
                 test_the_command_surface,
                 test_every_command_describes_what_it_actually_does,
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
                 test_a_login_that_stopped_working_is_reported,
                 test_spacing_optimiser,
                 test_phase_is_lost_only_when_pings_cannot_get_through,
                 test_correction_policy,
                 test_a_hold_suppresses_the_ping_and_nothing_else,
                 test_an_unusable_account_is_still_pinged,
                 test_the_log_reads_in_the_order_it_was_written,
                 test_setup_lays_accounts_out_sensibly,
                 test_upgrading_keeps_the_existing_checkpoint,
                 test_a_timer_that_will_never_fire_again_is_noticed,
                 test_nothing_touches_the_users_own_directory,
                 test_a_clean_install_from_nothing,
                 test_install_uninstall_purge_and_install_again,
                 test_three_accounts_install_and_space_correctly,
                 test_removing_an_account_stops_its_timer,
                 test_a_clean_install_refuses_a_refused_first_message,
                 test_doctor_notices_a_deployment_going_wrong):
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
