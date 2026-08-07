"""
Tests for claude_early_window.

Pure-logic tests only: nothing here contacts Claude, spawns a session, or spends
any of your usage window. The pieces that decide *when* to ping are the ones worth
testing, because getting them wrong is silent — the tool keeps running and just
drifts, or anchors to the wrong moment.

    python3 test_early_window.py
"""

import json
import os
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
    ew.cancel_anchor()

    def attempt(boundary, horizon=ew.FIVE_HOUR_HORIZON, label="5-hour window",
                limited=False, state=None):
        state = {} if state is None else state
        ew.maybe_schedule_anchor(boundary, horizon, label, state, limited)
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
                   lambda: attempt(now + 3 * 86400, ew.WEEKLY_HORIZON, "weekly limit")))
    check_true("the same target judged as a 5-hour one is rejected as nonsense",
               "out of range" in _log_of(
                   lambda: attempt(now + 3 * 86400, ew.FIVE_HOUR_HORIZON, "5-hour window")))


def _log_of(fn):
    """Capture what a call writes to the log, so log-only decisions are testable."""
    before = os.path.getsize(ew.LOG_FILE) if os.path.exists(ew.LOG_FILE) else 0
    fn()
    if not os.path.exists(ew.LOG_FILE):
        return ""
    with open(ew.LOG_FILE) as f:
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
    ew.cancel_anchor()

    state = {}
    ew.maybe_schedule_anchor(now + 1500, ew.FIVE_HOUR_HORIZON, "5-hour window", state, False)
    first = ew.anchor_pending()
    check_true("a target within one interval is scheduled", bool(first))
    check_true("the anchor is placed %ds after the boundary, never before"
               % ew.RESET_GUARD_SEC,
               abs(state["anchor_target"] - (now + 1500 + ew.RESET_GUARD_SEC)) < 2)

    ew.maybe_schedule_anchor(now + 1200, ew.FIVE_HOUR_HORIZON, "5-hour window", {}, False)
    check_true("re-scheduling replaces the pending anchor", ew.anchor_pending() != first)
    check("exactly one anchor timer exists, never a stack", _anchor_timer_count(), 1)

    ew.cancel_anchor()
    check("cancelling leaves nothing pending", ew.anchor_pending(), "")
    check("cancelling removes the unit", _anchor_timer_count(), 0)


def _have_systemd():
    return ew._systemctl("--version").returncode == 0


def _anchor_timer_count():
    out = ew._systemctl("list-timers", "--all", "--no-pager").stdout or ""
    return sum(1 for line in out.splitlines() if ew.ANCHOR_UNIT + ".timer" in line)


# ---------------------------------------------------------------------------
# Reading the statusLine capture
# ---------------------------------------------------------------------------

def test_statusline_parsing():
    section("statusLine capture")
    original = ew.STATUSLINE_FILE
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    ew.STATUSLINE_FILE = tmp.name
    try:
        tmp.write(json.dumps({"cost": {"total_api_duration_ms": 6962}}) + "\n")
        tmp.write(json.dumps({"cost": {"total_api_duration_ms": 8212},
                              "rate_limits": {"five_hour": {"resets_at": 111,
                                                            "used_percentage": 50}}}) + "\n")
        tmp.write('{"cost": {"total_api_dur')   # a half-written final line
        tmp.flush()

        check("a half-written line is skipped, not misread",
              len(ew.statusline_records()), 2)
        check("api duration takes the highest reported", ew.statusline_api_ms(), 8212)
        check("limits take the most recent report carrying them",
              ew.read_statusline_limits(),
              {"five_hour": {"resets_at": 111, "used_percentage": 50}})

        os.remove(tmp.name)
        check("a missing capture file reads as no records", ew.statusline_records(), [])
        check("api duration with no records is 0", ew.statusline_api_ms(), 0)
        check("limits with no records is empty", ew.read_statusline_limits(), {})
    finally:
        ew.STATUSLINE_FILE = original
        if os.path.exists(tmp.name):
            os.remove(tmp.name)


def test_resume_baseline_race():
    """
    The bug this guards against: on a --resume, the first statusLine report already
    carries the API duration inherited from the restored session. A baseline taken
    before that report arrives would be 0, the inherited figure would immediately
    look like our own reply landing, and the run would exit before Claude answered.
    """
    section("The resume baseline must not be taken before the first report")
    original = ew.STATUSLINE_FILE
    tmp = tempfile.mkdtemp()
    ew.STATUSLINE_FILE = os.path.join(tmp, "statusline.jsonl")
    try:
        check("before any report there is nothing to take a baseline from",
              ew.statusline_records(), [])

        # File exists but is still empty — the moment the old check was fooled by.
        open(ew.STATUSLINE_FILE, "w").close()
        check_true("an empty capture file does not count as a report",
                   not ew.statusline_records())

        with open(ew.STATUSLINE_FILE, "a") as f:
            f.write(json.dumps({"cost": {"total_api_duration_ms": 6962}}) + "\n")
        baseline = ew.statusline_api_ms()
        check("the baseline is the inherited figure, not 0", baseline, 6962)
        check_true("the inherited figure alone does not look like a fresh reply",
                   not ew.statusline_api_ms() > baseline)

        with open(ew.STATUSLINE_FILE, "a") as f:
            f.write(json.dumps({"cost": {"total_api_duration_ms": 8212}}) + "\n")
        check_true("only a genuine increase counts as the reply landing",
                   ew.statusline_api_ms() > baseline)
    finally:
        ew.STATUSLINE_FILE = original


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

    subprocess.run = missing
    try:
        state = {}
        ew.maybe_schedule_anchor(time.time() + 600, ew.FIVE_HOUR_HORIZON,
                                 "5-hour window", state, False)
        check_true("scheduling an anchor does not raise", True)
        check_true("no anchor is recorded that was never created",
                   "anchor_target" not in state)
        check("no anchor is reported as pending", ew.anchor_pending(), "")
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

    line = ew.format_usage({"five_hour": {"used_percentage": 95, "resets_at": five},
                            "seven_day": {"used_percentage": 87, "resets_at": week}})
    check_true("both limits appear", "5-hour" in line and "weekly" in line)
    check_true("each limit's percentage is shown", "95%" in line and "87%" in line)
    check_true("each reset time is shown", line.count("resets") == 2)
    check_true("the line is a single line", "\n" not in line)

    check("only one limit reported -> only that one is shown",
          "weekly" in ew.format_usage(
              {"seven_day": {"used_percentage": 20, "resets_at": week}}), True)
    check("nothing reported -> nothing logged", ew.format_usage({}), "")
    check("a limit with no figures at all is skipped",
          ew.format_usage({"five_hour": {}}), "")


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
    original_run, original_ids = ew.run_interactive, (ew.SESSION_ID_FILE,
                                                      ew.CHECKPOINT_BACKUP,
                                                      ew.LOG_FILE)
    tmp = tempfile.mkdtemp()
    ew.SESSION_ID_FILE = os.path.join(tmp, "session_id.txt")
    ew.CHECKPOINT_BACKUP = os.path.join(tmp, "checkpoint.jsonl.bak")
    ew.LOG_FILE = os.path.join(tmp, "test.log")
    refusal = "You've hit your session limit · resets 9:30pm (Asia/Jerusalem)"
    ew.run_interactive = lambda *a, **k: {"completed": True, "limited": True,
                                          "text": refusal}
    try:
        try:
            ew.init()
            check("setup exits rather than continuing", "returned normally", "SystemExit")
        except SystemExit as exc:
            check("setup exits non-zero", exc.code, 1)
        check_true("no checkpoint id is written",
                   not os.path.exists(ew.SESSION_ID_FILE))
        check_true("no checkpoint backup is written",
                   not os.path.exists(ew.CHECKPOINT_BACKUP))
        with open(ew.LOG_FILE) as f:
            logged = f.read()
        check_true("the refusal is reported to the user", refusal in logged)
    finally:
        ew.run_interactive = original_run
        ew.SESSION_ID_FILE, ew.CHECKPOINT_BACKUP, ew.LOG_FILE = original_ids


# ---------------------------------------------------------------------------

def main():
    # Log somewhere disposable: several decisions are only visible in the log, so
    # the tests read it, and they should not scribble on the running tool's.
    log_dir = tempfile.mkdtemp()
    ew.LOG_FILE = os.path.join(log_dir, "test.log")

    for test in (test_refusal_text, test_next_window_start, test_guard_rails,
                 test_anchor_scheduling, test_statusline_parsing,
                 test_resume_baseline_race, test_without_systemd, test_formatting,
                 test_usage_line, test_init_refuses_to_checkpoint_a_refusal):
        test()
    ew.cancel_anchor()
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
