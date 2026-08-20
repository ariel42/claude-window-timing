"""
Claude Code Window Timing
Keeps a Claude Code usage window rolling in the background so that you start work
inside a fresh, almost-untouched window. Requires Python 3.6+, Linux, and the
Claude Code CLI.

Supports several Claude subscriptions at once. Accounts are configured in
accounts.json (see accounts.example.json); with no such file the tool runs a
single account against its own ping directory, ~/.claude-1.

Usage:
  ./install.sh                 # the setup wizard, safe to re-run
  claude-window                # what every account is doing
  claude-window which          # which account to use right now
  claude-window switch 2       # point your own Claude Code at one of them
  claude-window ping 2         # send one ping; this is what the timer runs
  claude-window --help         # everything else

Setup writes `claude-window` into the repository's bin/ directory; until then,
run `python3 claude_window_timing.py <command>` directly.
"""

import argparse
import collections
import fcntl
import hashlib
import json
import os
import pty
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Paths — all derived from the script's own location, no hardcoded user paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HOME       = os.path.expanduser("~")
USER       = os.environ.get("USER") or os.environ.get("LOGNAME") or "user"

# Claude CLI: prefer PATH lookup, fall back to ~/.local/bin/claude
CLAUDE_PATH = shutil.which("claude") or os.path.join(HOME, ".local", "bin", "claude")

ACCOUNTS_FILE = os.path.join(SCRIPT_DIR, "accounts.json")
STATE_ROOT    = os.path.join(SCRIPT_DIR, "state")

# Where Claude Code keeps *the user's* data. This tool never reads, writes or
# links anything here — it is not ours. `doctor` looks at whose login it holds,
# to warn when the user is signed in as an account nobody is pinging, and that is
# the only contact of any kind.
USER_CONFIG_DIR  = os.path.join(HOME, ".claude")
USER_CONFIG_JSON = os.path.join(HOME, ".claude.json")


def ping_config_dir(name):
    """
    Where account `name`'s ping directory lives: ~/.claude-<name>.

    Beside the user's own directory rather than hidden under ~/.local/share:
    someone looking for where their Claude accounts are looks next to
    ~/.claude, and Claude Code itself ignores XDG.
    """
    return os.path.join(HOME, ".claude-{}".format(name))

# What this tool is called on the command line. Every message that suggests a
# next step spells it out in full, because a hint the reader cannot paste is
# worse than no hint.
COMMAND = "claude-window"

LOG_RETENTION_HOURS = 48

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
#
# INTERVAL_MIN is the single source of truth for the ping cadence:
# install_units() reads it from here when it writes the systemd timer, and
# install.sh reads nothing — it checks prerequisites and execs `setup`. 30
# divides the 5-hour window evenly, so consecutive windows sit back-to-back, and
# it stays well under the ~1-hour prompt-cache TTL so every ping is a
# (rate-limit-exempt) cache read.
INTERVAL_MIN = 30

# A window reset reported as 17:00:00 is pinged at 17:00:30. Firing *early* is the
# only real failure mode — the ping would land inside the old window, be wasted,
# and leave us waiting another full interval — so we deliberately aim late. The
# ping itself needs ~8s of CLI startup before it reaches the API, so the request
# actually arrives around +38s. That lateness is harmless; earliness is not.
RESET_GUARD_SEC = 30

# Accounts are spaced 5/N hours apart, and for N=2 that is an exact multiple of
# the 30-minute interval — so without this every account would ping in the same
# second, forever. Each account adds index * PING_STAGGER_SEC to its guard, which
# shifts its whole grid by a constant and is therefore absorbed into its measured
# phase. Staggering the *cadence* instead does not work: the anchor re-anchors
# each series to its own window boundary and pulls any cadence offset back out.
PING_STAGGER_SEC = 60

WINDOW_HOURS = 5

# How long a ping waits on its Claude subprocess. Named rather than inline so a
# test can shorten them: with a stand-in CLI the fixed startup pause is otherwise
# most of the suite's runtime.
STARTUP_WAIT_SEC = 8
COMPLETION_TIMEOUT_SEC = 60
STATUSLINE_WAIT_SEC = 10

# Give up on re-anchoring after this many consecutive anchors that still came back
# rate-limited — if the reset time we parsed were wrong, this stops a hot loop.
MAX_ANCHOR_STREAK = 3


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
#
# Everything the tool does is per-account: a config directory (which is the only
# thing that makes one Claude login distinct from another), a checkpoint
# conversation, state, a log, and a pair of systemd units.
#
# One ping process drives one account, but `status` and `which` have to reason
# about all of them at once, so an account is passed around as an object rather
# than kept in module-level globals. That is the whole reason for this class.

# Account names appear in systemd unit names and in paths, so keep them boring.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# Fields an accounts.json entry may carry; anything else is a typo.
_ACCOUNT_KEYS = frozenset(("name", "config_dir", "label"))


def _project_slug(path):
    """
    Claude Code's directory name for a working directory.

    Both separators and dots become dashes: /home/u/.claude-1/pingcwd is stored
    as -home-u--claude-1-pingcwd. Replacing only "/" is right for a path with no
    dots in it and silently wrong for one that has them — the transcript is then
    written somewhere this never looks, every run reports "no assistant turn
    recorded", and the account looks broken while the requests are being billed.
    """
    return path.replace("/", "-").replace(".", "-")


class Account(object):
    """One Claude login, and everywhere its files and units live."""

    def __init__(self, name, config_dir=None, index=0, label=""):
        self.name       = name
        self.index      = index
        self.label      = label
        self.config_dir = os.path.abspath(
            os.path.expanduser(config_dir or ping_config_dir(name)))

        self.state_dir         = os.path.join(STATE_ROOT, name)
        self.session_id_file   = os.path.join(self.state_dir, "session_id.txt")
        self.checkpoint_backup = os.path.join(self.state_dir, "checkpoint.jsonl.bak")
        # Which working directory the checkpoint was built for. Claude Code
        # registers a session against the directory it was created in and will
        # not resume it from anywhere else, so a checkpoint made under an older
        # layout has to be rebuilt rather than moved.
        self.checkpoint_cwd_file = os.path.join(self.state_dir, "checkpoint_cwd.txt")
        self.state_file        = os.path.join(self.state_dir, "state.json")
        self.statusline_file   = os.path.join(self.state_dir, "statusline.jsonl")
        # Per-account rather than one shared log: rotate_log() rewrites the whole
        # file, which is not safe against a second account rotating at the same
        # moment. `claude-window log` merges them for display when that is wanted.
        self.log_file          = os.path.join(self.state_dir, "ping.log")

    # -- Claude Code's own layout --------------------------------------------

    @property
    def ping_cwd(self):
        """
        The directory a ping runs in: <config dir>/pingcwd, empty and permanent.

        Not this checkout, which is what earlier versions used. Claude Code puts
        the working directory's branch, working-tree status and recent commits
        into the *cached* part of every prompt, so a ping that runs inside a git
        repository loses its cache every time that repository changes — and the
        repository this tool ships from is one its own author commits to. A week
        of logs bore that out: a 100% cache hit rate except during the three
        hours when commits were landing here, and every miss in that stretch.

        Empty and outside any repository, there is nothing left to change.
        """
        return os.path.join(self.config_dir, "pingcwd")

    @property
    def session_dir(self):
        """
        Where Claude Code stores this account's transcripts for our working
        directory: <config dir>/projects/<cwd with "/" replaced by "-">.

        Deriving this from the account's config dir rather than ~/.claude is what
        lets a second account exist at all — otherwise every account would look
        for its checkpoint in the first account's tree.
        """
        return os.path.join(self.config_dir, "projects",
                            _project_slug(self.ping_cwd))

    # -- systemd --------------------------------------------------------------

    @property
    def service_unit(self):
        return "claude-window-timing@{}.service".format(self.name)

    @property
    def timer_unit(self):
        return "claude-window-timing@{}.timer".format(self.name)

    @property
    def anchor_unit(self):
        # Deliberately not templated: this is created on demand by systemd-run as
        # a transient unit, and a transient name containing "@" reads as an
        # instance of a template that does not exist.
        return "claude-window-timing-anchor-{}".format(self.name)

    # -- timing ---------------------------------------------------------------

    @property
    def guard_sec(self):
        return RESET_GUARD_SEC + self.index * PING_STAGGER_SEC

    # -- Claude Code's own config file ----------------------------------------

    @property
    def config_json(self):
        """
        This account's `.claude.json`.

        Claude Code resolves it as `join(CLAUDE_CONFIG_DIR || homedir(),
        ".claude.json")` — beside the config directory, not inside it. Every ping
        sets the variable, so each account gets its own here and the user's
        ~/.claude.json is never involved.
        """
        return os.path.join(self.config_dir, ".claude.json")

    # -- misc -----------------------------------------------------------------

    @property
    def display(self):
        return "{} ({})".format(self.name, self.label) if self.label else self.name

    def ensure_state_dir(self):
        secure_dir(STATE_ROOT)
        secure_dir(self.state_dir)

    def __repr__(self):
        return "<Account {} at {}>".format(self.name, self.config_dir)


def _temp_name(path):
    """
    A scratch name beside `path` that no other process can be writing.

    Every atomic write here is tmp-then-rename, and two of them shared one
    scratch name: schedule.json is republished after every ping and
    alignment.json after every one that spaces, so two accounts finishing
    together -- a slow ping, a hand-run one beside the timer's -- wrote the
    same file at the same time and renamed whatever the interleaving produced
    into place.
    """
    return "{}.tmp.{}".format(path, os.getpid())


def _read_json(path):
    """Parse a JSON file, treating "missing" and "unreadable" as empty."""
    try:
        with open(path) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


# The whole of what a ping directory needs in its .claude.json. Proven
# sufficient: a directory holding only credentials and these three keys ran a
# complete session with no prompt of any kind. Written by us rather than copied
# from the user, which is what removes any need to touch ~/.claude.json at all.
def ping_config(cwd):
    return {
        "hasCompletedOnboarding": True,
        "hasCompletedClaudeInChromeOnboarding": True,
        "projects": {cwd: {"hasTrustDialogAccepted": True}},
    }


def ensure_ping_config(account):
    """
    Make sure this ping directory's config lets a ping run unattended.

    Merged rather than overwritten: Claude Code keeps caches and counters in the
    same file and there is no reason to discard them. Only the keys a ping
    depends on are asserted. Returns True if anything changed.
    """
    if not os.path.isdir(account.config_dir):
        os.makedirs(account.config_dir, 0o700)
    if not os.path.isdir(account.ping_cwd):
        os.makedirs(account.ping_cwd, 0o700)
    config = _read_json(account.config_json)
    changed = False
    for key, value in ping_config(account.ping_cwd).items():
        if key == "projects":
            entry = config.setdefault("projects", {}).setdefault(account.ping_cwd, {})
            if not entry.get("hasTrustDialogAccepted"):
                entry["hasTrustDialogAccepted"] = True
                changed = True
        elif config.get(key) != value:
            config[key] = value
            changed = True
    if changed:
        tmp = _temp_name(account.config_json)
        with open(tmp, "w") as f:
            json.dump(config, f, indent=2, sort_keys=True)
        os.replace(tmp, account.config_json)
    return changed


class ConfigError(Exception):
    """accounts.json says something that cannot be acted on."""


def parse_accounts(data):
    """
    Turn parsed accounts.json into a list of Accounts, or raise ConfigError.

    Validation is strict and early because every mistake here is one that would
    otherwise show up much later as a puzzle: two accounts sharing a config dir
    are the same login wearing two hats, and a name with a slash or a space in it
    produces a systemd unit that cannot be started.
    """
    if not isinstance(data, dict):
        raise ConfigError("accounts.json must contain a JSON object")

    entries = data.get("accounts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError("accounts.json needs a non-empty \"accounts\" list")

    accounts, seen_names, seen_dirs = [], {}, {}
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError("account #{} must be an object".format(index + 1))

        # Reject unknown keys rather than ignoring them. A typo like "configdir"
        # would otherwise leave the account silently pointing at the default
        # directory, which is the one mistake that turns two subscriptions back
        # into one. Anything starting with "_" is treated as a comment.
        unknown = sorted(k for k in entry
                         if k not in _ACCOUNT_KEYS and not k.startswith("_"))
        if unknown:
            raise ConfigError(
                "account #{} has unrecognised field(s) {} — valid fields are: "
                "{}".format(index + 1, ", ".join(repr(k) for k in unknown),
                            ", ".join(sorted(_ACCOUNT_KEYS))))

        config_dir = entry.get("config_dir")
        if config_dir is not None and not isinstance(config_dir, str):
            raise ConfigError(
                "account #{}: config_dir must be a string, not {}".format(
                    index + 1, type(config_dir).__name__))

        name = entry.get("name")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ConfigError(
                "account #{} has an invalid name {!r}: use letters, digits, "
                "\"-\" or \"_\", starting with a letter or digit".format(
                    index + 1, name))
        if name in seen_names:
            raise ConfigError("two accounts are both named {!r}".format(name))
        seen_names[name] = True

        account = Account(name, config_dir, index, entry.get("label") or "")
        if account.config_dir in seen_dirs:
            raise ConfigError(
                "accounts {!r} and {!r} share the config directory {} — they "
                "would be the same Claude login".format(
                    seen_dirs[account.config_dir], name, account.config_dir))
        seen_dirs[account.config_dir] = name
        accounts.append(account)

    return accounts


def default_accounts():
    """The implied configuration when there is no accounts.json: one account."""
    return [Account("1", ping_config_dir("1"), 0)]


def load_accounts(path=None):
    """
    Every configured account, in slot order. Falls back to a single default
    account so that the tool works before it has ever been configured.
    """
    path = path or ACCOUNTS_FILE
    if not os.path.exists(path):
        return default_accounts()
    try:
        with open(path) as f:
            data = json.load(f)
    except ValueError as e:
        raise ConfigError("{} is not valid JSON: {}".format(path, e))
    except (IOError, OSError) as e:
        raise ConfigError("cannot read {}: {}".format(path, e))
    return parse_accounts(data)


def pings_here(path=None):
    """
    Whether this machine runs the pings. From accounts.json; defaults to yes.

    Persisted rather than inferred, because everything downstream turns on it.
    A machine that only switches accounts has no ping directories, no
    checkpoints and no timers, and a `doctor` that reported all three as broken
    would be worse than no `doctor` at all. Absent, the answer is yes: that is
    what every install predating the flag was, and what the common case still
    is.
    """
    path = path or ACCOUNTS_FILE
    value = _read_json(path).get("pings", True)
    return value is not False


def find_account(accounts, name):
    """The named account, or raise ConfigError naming the ones that do exist."""
    for account in accounts:
        if account.name == name:
            return account
    raise ConfigError("no account named {!r} — configured accounts are: {}".format(
        name, ", ".join(a.name for a in accounts) or "(none)"))


# ---------------------------------------------------------------------------
# Which account to use right now
# ---------------------------------------------------------------------------
#
# Two questions, in this order:
#
#   Can this account serve a request at all?   -> availability
#   How soon does its current window expire?   -> urgency
#
# Urgency decides only between accounts that pass the first test, because a
# window you cannot spend is not worth spending: naming the account that expires
# soonest is exactly the wrong answer when Claude is going to refuse it.
#
# Availability is read off evidence, and four kinds of evidence can say no:
#
#   * a ping was refused, and the refusal said when to try again;
#   * a limit is *reported* as fully spent — the 5-hour one or the weekly one;
#   * the last few pings produced no answer at all;
#   * the account's own files rule out any request succeeding — not signed in,
#     no paid subscription, a sign-in that has already expired.
#
# The second is the subtle one, and it is why a ping getting through is not
# proof of anything. A ping replays a cached conversation, and cache reads are
# not deducted from the rate limit, so a ping can sail through an account whose
# limit is spent and whose next *real* request would be refused. Believing the
# ping over the reported percentage would recommend precisely the account that
# cannot be used. When several of these apply the account is back only when the
# last of them clears, so they are combined by taking the latest.
#
# What separates them is how they end, and that ordering is the whole ranking: a
# spent limit names the moment it returns, a silent ping does not, and a lapsed
# subscription does not return at all until the user does something. Prefer, in
# order: usable now, back at a known time, cannot tell, needs you.
#
# Given a choice of usable accounts, spend the most perishable one first. Quota
# does not carry over — whatever is left when a window ends is simply gone — so
# using the account that expires soonest is the classic perishable-stock rule.
# With windows spaced evenly it has a simpler form: use whichever account did
# *not* most recently refill.

# Pings that fail for reasons that say nothing about the account (no network, a
# crashed CLI) should not condemn it. Enough of them in a row should.
UNHEALTHY_AFTER = 3

# A limit reported at or above this is spent. Claude reports whole percents and
# refuses real work at 100, while the ping — a cache read — may still be served.
LIMIT_SPENT_PCT = 100

# How old the readings can get before `which` says so. Three missed pings is past
# coincidence: by then the answer is being computed from history, not from facts.
STALE_AFTER_SEC = UNHEALTHY_AFTER * INTERVAL_MIN * 60

# The two limits Claude reports, and what to call them. Availability consults
# both; `status` and the log print both.
_LIMIT_NAMES = (("five_hour", "5-hour"), ("seven_day", "weekly"))

# How long each limit's window is, for bounding a spent limit whose reset time
# was never recorded -- which is what a refusal looks like, since a 429 carries
# no headers at all.
_LIMIT_LENGTHS = {"five_hour": WINDOW_HOURS * 3600, "seven_day": 7 * 86400}

# Ranking tiers, best first.
USABLE, WAITING, UNKNOWN, NEEDS_ACTION = range(4)

# tier: one of the above. until: when it returns, when that is knowable.
# note: why, in words a user can act on — never consulted for the ranking.
# `exact` says whether `until` is an observed reset time or the longest the
# limit could possibly last. A spent limit always returns eventually -- a
# 5-hour window cannot outlast five hours -- so "we do not know the reset time"
# is never a reason to answer "we do not know when", only a reason to say
# "no later than".
Availability = collections.namedtuple("Availability", "tier until note exact")

# How a tier travels to another machine. Names rather than the integers, because
# the integers are an implementation detail and a published file outlives one.
TIER_NAMES = {USABLE: "usable", WAITING: "waiting", UNKNOWN: "unknown",
              NEEDS_ACTION: "needs_action"}
TIERS_BY_NAME = {name: tier for tier, name in TIER_NAMES.items()}

# What is known about an account configured here that the published schedule
# does not mention -- the copy predates it being added, or the machine running
# the pings does not have it. Nothing on this machine can answer for it: there
# are no readings, and on a machine that only switches there is no ping
# directory to look in either. So it is "cannot tell", never "fine", and never
# a KeyError in the middle of `status`.
UNPUBLISHED = Availability(
    UNKNOWN, None, "the machine that pings has not published this account", True)

SCHEDULE_FILE = os.path.join(SCRIPT_DIR, "schedule.json")


def next_expiry(state, now):
    """
    When this account's current window ends, rolled forward if the reading is old.

    A missed ping leaves a reset time in the past. Windows tile back to back, so
    the honest correction is to add whole windows until it is in the future
    rather than to distrust the reading — it is still the right phase.
    """
    resets = ((state.get("rate_limits") or {}).get("five_hour") or {}).get("resets_at")
    if not resets:
        return float("inf")
    window = WINDOW_HOURS * 3600
    if resets <= now:
        resets += window * (int((now - resets) // window) + 1)
    return resets


def spent_limits(state, now):
    """
    Every reported limit that is fully spent.

    Each entry is (name, when it comes back, whether that time was observed);
    an unobserved one is the longest the limit could possibly run, offered as
    an upper bound rather than as a fact.

    A reset already in the past is not evidence of anything: it describes a
    window that has since rolled over, so the percentage recorded beside it
    belongs to a limit that has already refilled.
    """
    limits = state.get("rate_limits") or {}
    spent = []
    for key, name in _LIMIT_NAMES:
        window = limits.get(key) or {}
        resets = window.get("resets_at")
        if (window.get("used_percentage") or 0) < LIMIT_SPENT_PCT:
            continue
        if resets is None:
            # Spent, with no reset time recorded -- what a refusal looks like,
            # since a 429 carries no headers. The limit still turns over: this
            # window began at some unobserved moment and cannot run longer than
            # the limit's own length, so the latest it can end is that far from
            # now. An upper bound, offered as one.
            spent.append((name, now + _LIMIT_LENGTHS[key], False))
        elif resets > now:
            spent.append((name, resets, True))
    return spent


def identity_blocker(account):
    """
    What this account's own files say makes every request fail, or "".

    File reads only — the same two JSON files `doctor` reads, and no subprocess —
    because this sits behind `which`, which has to answer instantly and must
    never spend anything.

    Absent evidence is not bad evidence. A machine holding only a copy of the
    schedule has no config directory to read, and condemning an account there
    would be worse than missing a fault: the fault is visible on the machine
    that pings, and `doctor` is where it gets diagnosed.
    """
    if not os.path.isdir(account.config_dir):
        return ""
    identity = account_identity(account)
    if not identity["has_token"] and not identity["account_uuid"]:
        return "not signed in"
    subscription = (identity["subscription"] or "").lower()
    if subscription in ("free", "none"):
        return "no paid subscription ({})".format(identity["subscription"])
    expires = identity["refresh_expires_at"]
    if expires and expires <= time.time():
        return "its sign-in has expired"
    return ""


def account_availability(account, state, now):
    """
    Whether this account can be used right now — and if not, until when and why.

    Deliberately pessimistic where the evidence disagrees: an account counts as
    usable only when nothing known about it says otherwise.
    """
    # First, because it is the only thing here that is true *now* rather than as
    # of the last ping — and because waiting will not fix it.
    stuck = identity_blocker(account)
    if stuck:
        return Availability(NEEDS_ACTION, None, stuck, True)

    blockers = []

    # A refusal is Claude's own answer about this account, and it carries the
    # moment it stops applying. It never says which limit refused, and nothing
    # here needs to know.
    available_at = state.get("available_at")
    if available_at and available_at > now:
        blockers.append((available_at, "Claude refused the last ping", True))

    for name, resets, exact in spent_limits(state, now):
        blockers.append((resets, "its {} limit is spent".format(name), exact))

    if blockers:
        until, note, exact = max(blockers)   # back when the last one clears
        return Availability(WAITING, until, note, exact)

    if state.get("consecutive_failures", 0) >= UNHEALTHY_AFTER:
        return Availability(UNKNOWN, None, "its pings keep failing", True)

    return Availability(USABLE, None, "", True)


def availabilities(accounts, states, now):
    return {a.name: account_availability(a, states[a.name], now) for a in accounts}


def rank_account(account, state, now, availability=None):
    """
    Sort key for choosing an account: lower is better.

    Availability first, urgency second, and inside each tier the tie-break is
    whatever a user would ask next — soonest back, most recently alive, and
    failing all else the order they configured the accounts in.
    """
    avail = availability or account_availability(account, state, now)
    if avail.tier == USABLE:
        return (USABLE, next_expiry(state, now), account.index)  # most perishable
    if avail.tier == WAITING:
        return (WAITING, avail.until, account.index)             # soonest back
    if avail.tier == UNKNOWN:
        return (UNKNOWN, -(state.get("last_run") or 0.0), account.index)
    return (NEEDS_ACTION, 0.0, account.index)


def choose_account(accounts, states=None, now=None, avail=None):
    """
    The account to use right now, and why, as (account, reason).

    This is advice. Nothing acts on it: the tool does not choose an account for
    anyone, it only says which one it would pick.
    """
    now = time.time() if now is None else now
    states = {a.name: read_state(a) for a in accounts} if states is None \
        else states
    avail = availabilities(accounts, states, now) if avail is None else avail

    best = min(accounts,
               key=lambda a: rank_account(a, states[a.name], now, avail[a.name]))
    state, chosen = states[best.name], avail[best.name]
    only = len(accounts) == 1

    if chosen.tier == NEEDS_ACTION:
        return best, "{}; run `{} doctor`".format(chosen.note, COMMAND)
    if chosen.tier == UNKNOWN:
        return best, chosen.note
    if chosen.tier == WAITING:
        return best, "{}; back {}{} (in {})".format(
            chosen.note, "" if chosen.exact else "no later than ",
            fmt_time(chosen.until), fmt_delta(chosen.until - now))

    expiry = next_expiry(state, now)
    if expiry == float("inf"):
        return best, ("no window information yet — copy schedule.json here "
                      "from the machine that runs the pings"
                      if not pings_here() else
                      "no window information yet — run a ping first")
    if only:
        return best, "the only account; its window ends {}".format(fmt_time(expiry))
    # "ends first" is a claim about the accounts it was chosen over, so it has to
    # be false when there was nothing to choose between: another account's window
    # may well end sooner and simply be unusable.
    if sum(1 for a in accounts if avail[a.name].tier == USABLE) == 1:
        return best, "the only account usable right now; its window ends {} (in {})".format(
            fmt_time(expiry), fmt_delta(expiry - now))
    return best, "its window ends first, {} (in {})".format(
        fmt_time(expiry), fmt_delta(expiry - now))


def headline(chosen, avail, count):
    """
    The first line `which` and `status` print: what to do, before the why.

    Separate from the reason because "Use account 2" is a lie when nothing can
    be used, and a recommendation nobody can act on should not be phrased as an
    instruction.
    """
    if avail.tier == USABLE:
        return "Use account {}".format(chosen.display)
    if avail.tier == UNKNOWN:
        return "Nothing is known to be usable — try account {}".format(
            chosen.display)
    if avail.tier == NEEDS_ACTION:
        return ("Account {} cannot be used".format(chosen.display) if count == 1
                else "No account can be used — start with {}".format(
                    chosen.display))
    return ("Account {} is not usable yet".format(chosen.display) if count == 1
            else "No account is usable yet — {} is next".format(chosen.display))


def describe_availability(state, avail, now):
    """One line per account, for the list `which` prints under its answer."""
    if avail.tier == USABLE:
        expiry = next_expiry(state, now)
        if expiry == float("inf"):
            return "no window information yet"
        return "usable — window ends in {}".format(fmt_delta(expiry - now))
    if avail.tier == WAITING:
        return "unusable until {}{} — {}".format(
            "" if avail.exact else "no later than ",
            fmt_time(avail.until), avail.note)
    if avail.tier == UNKNOWN:
        # Not "unusable": nothing here knows that. The headline above offers
        # this account as a guess, and a list calling it unusable in the same
        # breath contradicts it.
        return "cannot tell — {}".format(avail.note)
    return "unusable — {}".format(avail.note)


def publish_schedule(accounts):
    """
    Write what other machines need in order to choose an account themselves.

    Only the machine running the pings can observe any of this, but selection
    needs nothing live: a window's *phase* is a constant, so a laptop with a copy
    of this file can work out which account is most perishable with arithmetic
    alone — no network call, no daemon, and nothing to go stale but the phase,
    which changes only when the schedule is realigned.
    """
    now = time.time()
    window = WINDOW_HOURS * 3600
    entry = []
    for account in accounts:
        state = read_state(account)
        expiry = next_expiry(state, now)
        five = ((state.get("rate_limits") or {}).get("five_hour") or {})
        # The verdict travels with the numbers. Only this machine can see a
        # lapsed subscription or a spent weekly limit, so a laptop working from
        # a copy would otherwise have to re-derive what it cannot observe.
        usable = account_availability(account, state, now)
        entry.append({
            "name": account.name,
            "label": account.label,
            "config_dir": account.config_dir,
            # Which account this actually is, not merely which slot it occupies.
            # A copy of this file on another machine keys on `name` and
            # `config_dir`, both of which are positional: if that machine's
            # directories were created in a different order, or one was signed
            # in again as someone else, the advice would name the wrong account
            # with nothing to notice it by. The uuid is the only field that
            # travels. Email is deliberately left out — this file gets copied
            # between machines, and a uuid identifies without disclosing.
            "account_uuid": account_identity(account).get("account_uuid"),
            "window_phase": (expiry % window) if expiry != float("inf") else None,
            "expires_at": expiry if expiry != float("inf") else None,
            "available_at": state.get("available_at"),
            "usable_now": usable.tier == USABLE,
            "tier": TIER_NAMES[usable.tier],
            "unusable_until": usable.until,
            # Whether that time was observed, or is the longest the limit could
            # possibly run. It has to travel with the time it qualifies, or the
            # machine reading this file repeats a bound as though it were a
            # fact.
            "unusable_until_exact": usable.exact,
            "unusable_because": usable.note or None,
            "used_percentage": five.get("used_percentage"),
            "last_run": state.get("last_run"),
        })

    document = {"written_at": now, "window_hours": WINDOW_HOURS,
                "accounts": entry}
    tmp = _temp_name(SCHEDULE_FILE)
    with open(tmp, "w") as f:
        json.dump(document, f, indent=2, sort_keys=True)
    os.replace(tmp, SCHEDULE_FILE)


def read_schedule(path=None):
    """The published schedule as written, or {} if there is not a usable one."""
    document = _read_json(path or SCHEDULE_FILE)
    if not isinstance(document, dict) or not document.get("accounts"):
        return {}
    return document


def schedule_view(accounts):
    """
    Accounts and readings taken from a published schedule, or None.

    This is what makes a second machine useful without running anything: copy
    schedule.json in beside the script and `which` answers from it, with no
    network call and nothing installed. Only the pinging machine can observe any
    of this, so the file carries the verdicts as well as the numbers.

    A stale copy still answers correctly. Windows tile back to back, so an
    expiry that has passed is rolled forward by whole windows into the one
    running now — what was published is really the *phase*, and a phase does not
    move between windows. The availability travelling beside it is a snapshot
    and does age, which is why `which` says how old the file is.

    Returns None whenever this machine has readings of its own: the machine
    doing the pinging is the authority on itself, and this is a fallback rather
    than a second opinion.
    """
    if any(read_state(account).get("last_run") for account in accounts):
        return None
    document = read_schedule()
    if not document:
        return None

    published, states, avail, now = [], {}, {}, time.time()
    for index, entry in enumerate(document["accounts"]):
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        # This file arrives from another machine -- the README says to copy it
        # across -- so it is input, not configuration. `parse_accounts` applies
        # this rule to accounts.json and nothing applied it here, which let a
        # name like "../../elsewhere" out of SWITCH_ROOT and a config_dir point
        # anywhere at all. The name is checked; the directory is ignored
        # entirely, because a path from another machine cannot mean anything
        # useful on this one.
        name = str(entry["name"])
        if not _NAME_RE.match(name):
            continue
        account = Account(name, None, index, str(entry.get("label") or "")[:40])
        published.append(account)
        states[account.name] = {
            "last_run": entry.get("last_run"),
            "rate_limits": {"five_hour": {
                "resets_at": entry.get("expires_at"),
                "used_percentage": entry.get("used_percentage")}},
        }
        # Fall back on `usable_now` if the file predates the named tier, so an
        # older copy degrades to a coarser answer rather than to no answer.
        tier = TIERS_BY_NAME.get(entry.get("tier"))
        if tier is None:
            tier = USABLE if entry.get("usable_now") else WAITING
        # A WAITING entry that arrives with no time at all is bounded here the
        # same way a local one would be. One that arrives with a time says
        # beside it whether that time was observed; a file written before that
        # flag existed only ever carried observed times, so its absence reads
        # as observed.
        until = entry.get("unusable_until")
        exact = entry.get("unusable_until_exact", True) is not False
        if tier == WAITING and not until:
            until, exact = now + WINDOW_HOURS * 3600, False
        avail[account.name] = Availability(
            tier, until, entry.get("unusable_because") or "", exact)

    if not published:
        return None
    return published, states, avail, document.get("written_at")


# ---------------------------------------------------------------------------
# Keeping the windows evenly spaced
# ---------------------------------------------------------------------------
#
# N accounts are worth most when their windows are spread evenly — 5/N hours
# apart. No arrangement creates capacity: the expected number of refills in any
# stretch of time is the same however the windows sit. What even spacing changes
# is the *shape* of the supply, and since unused quota expires at every reset,
# shape is worth real money. It also halves the worst-case wait for a fresh
# window, from 5 hours to 5/N.
#
# N is not the number of accounts configured. It is the number that will start a
# window at their next boundary, recomputed from observation on every ping —
# see participation(). An account that supplies nothing for the next few days
# still costs a slot if it is counted, and the slot is a stretch of the day with
# no window arriving in it.
#
# One constraint governs everything here:
#
#     A window starts on the first ping *after* the previous one ends, so a
#     phase can only ever be delayed, never advanced.
#
# Every correction therefore costs a deliberate gap with no window running, and
# the job is to find the cheapest set of delays that lands the accounts evenly
# spaced. Note that only *relative* offsets matter — the whole arrangement may
# rotate freely — which is what makes common drift free to ignore.

# Ignore errors smaller than this. Each window's phase creeps forward by roughly
# the CLI's startup time, and that creep is near-identical across accounts, so
# it cancels out of the relative offsets. Chasing it would mean paying real dead
# time to correct noise.
ALIGN_DEADBAND_SEC = 5 * 60

# Correct silently up to here. Beyond it, say what it would cost and wait to be
# told: a multi-hour hold that nobody asked for is not something a tool other
# people install should do on its own.
AUTO_CORRECT_MAX_SEC = 45 * 60

ALIGNMENT_FILE = os.path.join(STATE_ROOT, "alignment.json")


def window_phase(state, now):
    """Where this account's boundary falls within the 5-hour cycle, or None."""
    expiry = next_expiry(state, now)
    if expiry == float("inf"):
        return None
    return expiry % (WINDOW_HOURS * 3600)


def participation(account, state, now):
    """
    Does this account hold a place in the rotation, and if not, why not?

    This is what decides N in the 5/N spacing, and the question it asks is
    narrower than "is this account any good": **will it start a window at its own
    next boundary?** One test answers it —

        it must be able to serve a request no later than the moment its current
        window ends.

    Everything follows from that, with no knowledge of which limit is
    responsible. Draining the 5-hour quota does *not* cost an account its place:
    it becomes usable again at exactly the boundary, the ping 30 seconds later
    gets through, and the next window starts on time. A spent weekly limit, a
    lapsed subscription and a revoked login all do cost it, for one reason — the
    boundary passes with nothing getting through, so no new window begins, and a
    slot reserved for a window that never starts is a hole in the rotation.

    Counting a dead account is not neutral. Three accounts with one dead are
    spaced 1h40m apart instead of 2h30m, so the two that still supply windows are
    bunched into a third of the day for no reason at all.

    Returns (participating, reason) — the reason exists because "why is account 3
    not in the plan" is the first thing anyone asks of the spacing report.
    """
    boundary = next_expiry(state, now)
    if boundary == float("inf"):
        return False, "it has not reported a window yet"

    avail = account_availability(account, state, now)
    if avail.tier == NEEDS_ACTION:
        # Nothing it does on its own brings this back, so no future boundary of
        # its own is worth reserving a slot for.
        return False, avail.note
    if avail.tier == WAITING and avail.until is not None \
            and avail.until > boundary:
        return False, "{}, which outlasts its current window".format(avail.note)
    # An unknown return time deliberately does *not* drop it. The usual cause is
    # a refusal carrying no reset header, and a refusal is almost always the
    # 5-hour limit -- which clears at exactly this account's boundary, so its
    # slot is still worth holding. Dropping it would re-space every other
    # account around a hole that closes by itself, and re-spacing costs hours
    # of dead window. Being pessimistic is right for "which account should I
    # spend"; it is wrong here.

    # Proof of life, and the reason a phase can be believed at all. A phase says
    # where this account's boundary falls; that claim is only as good as the last
    # ping that got an answer, because once a boundary has passed with nothing
    # getting through, the next window began at some unobserved moment and the
    # recorded phase is fiction. `available_at` is written on every ping that
    # succeeded, so a value older than a whole window means exactly that.
    proven = state.get("available_at")
    if not proven:
        # Never, rather than not lately. On a fresh install this is the whole
        # story, and "for a whole window" invites somebody to wait one out.
        # The pointer to `doctor` is added only once the pings have actually
        # been failing: an account whose first ping is still minutes away is
        # not a fault, and sending somebody to a command with nothing to say
        # is how a diagnostic loses its authority.
        if state.get("consecutive_failures", 0) >= UNHEALTHY_AFTER:
            return False, "no ping has ever got through — see `{} doctor`".format(
                COMMAND)
        return False, "no ping has got through yet"
    if proven <= now - WINDOW_HOURS * 3600:
        return False, "nothing has got through since {}".format(
            fmt_time(proven))

    return True, ""


def is_participating(account, state, now):
    return participation(account, state, now)[0]


def plan_alignment(phases, window=None):
    """
    The cheapest set of forward-only delays that spaces these phases evenly.

    `phases` maps name -> phase in seconds within the window. Returns
    (delays, total) where delays maps name -> seconds to hold that account back.

    Because targets are evenly spaced, sliding a single global offset covers
    every way of assigning accounts to slots, and the optimum always leaves at
    least one account untouched — so it is enough to try the offset that zeroes
    each account in turn. That is N candidates, each costing N to evaluate.

    Worth noticing what falls out: correcting one account that has drifted late
    is usually done by delaying *the others* a little, not by dragging the late
    one all the way around.
    """
    window = window or WINDOW_HOURS * 3600
    names = sorted(phases, key=lambda n: (phases[n], n))
    count = len(names)
    if count < 2:
        return {name: 0.0 for name in names}, 0.0

    spacing = float(window) / count
    best_delays, best_total = None, None
    for zeroed in range(count):
        offset = (phases[names[zeroed]] - zeroed * spacing) % window
        delays, total = {}, 0.0
        for position, name in enumerate(names):
            delay = ((offset + position * spacing) - phases[name]) % window
            delays[name] = delay
            total += delay
        if best_total is None or total < best_total - 1e-9:
            best_delays, best_total = delays, total
    return best_delays, best_total


def read_alignment():
    try:
        with open(ALIGNMENT_FILE) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def write_alignment(data):
    """
    Record the shared alignment view. Failing to is survivable, and must be.

    This runs *after* the ping, from the same stretch of code that still has to
    write the account's state and book the boundary anchor. Spacing is advisory;
    those two are not. So a full disk here costs a slightly stale spacing view
    rather than a permanently late schedule.
    """
    try:
        if not os.path.isdir(STATE_ROOT):
            os.makedirs(STATE_ROOT, 0o700)
        tmp = _temp_name(ALIGNMENT_FILE)
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp, ALIGNMENT_FILE)
    except (IOError, OSError) as e:
        sys.stderr.write("Could not record the spacing view: {}\n".format(e))


def alignment_plan(accounts, states, now, record=True):
    """
    What the spacing should be, and what it would cost to get there.

    Returns (delays, total, participants, settled). With `record` false it
    reports without writing anything, which is what the commands that only
    *look* — status, doctor — must do: noting a change in the participating set
    starts the settling clock, and a diagnostic should never quietly move the
    schedule it is reporting on.

    `settled` is False while the set of participating accounts is still new:
    an account dropping out changes the ideal spacing for everyone, and acting
    on that immediately would mean paying for a re-space twice if it comes back
    shortly. So the set has to hold steady for a full window before it moves the
    target. An account nobody has ever seen is exempt — it cannot be the one
    coming back — which is what keeps a fresh install from waiting on itself.
    """
    window = WINDOW_HOURS * 3600
    participants = sorted(a.name for a in accounts
                          if is_participating(a, states[a.name], now))

    alignment = read_alignment()
    previous = alignment.get("participants")
    # An install that predates this key has still seen whatever it currently
    # records as participating, and treating those as new would grant exactly
    # the wrong account a free re-space the first time one drops out and
    # returns.
    ever = set(alignment.get("ever") or alignment.get("participants") or ())
    if previous != participants and record:
        # A *first* sighting is not a change. There is no earlier arrangement to
        # thrash against and nothing has been paid for one yet, so waiting would
        # only mean a freshly installed setup sitting visibly misaligned for a
        # whole window with nothing to show for the patience.
        #
        # Nor is an account joining that has never been seen before. What this
        # waits out is an account dropping out and coming back — a limit spent
        # on Friday buying a re-space that Saturday buys back — and an account
        # with no history cannot be doing that. Without this a two-account
        # install would restart its own clock: the accounts are pinged a minute
        # apart, so the set is observed as {1} and then {1,2}, and the second
        # sighting would read as thrash on a setup minutes old.
        joined = set(participants) - set(previous or ())
        first_look = previous is None or (
            joined and not set(previous) - set(participants)
            and not joined & ever)
        alignment["participants"] = participants
        alignment["ever"] = sorted(ever | set(participants))
        alignment["participants_since"] = now - window if first_look else now
        write_alignment(alignment)
    settled = now - alignment.get("participants_since", now) >= window

    phases = {}
    for account in accounts:
        if account.name in participants:
            phase = window_phase(states[account.name], now)
            if phase is not None:
                phases[account.name] = phase

    delays, total = plan_alignment(phases, window)
    return delays, total, participants, settled


def describe_alignment(accounts, states, now, suggest_realign=True,
                       note_waiting=True, record=False):
    """
    A human summary of the spacing and what correcting it would cost.

    `suggest_realign` is off when `realign` is itself the caller, which is
    already showing the fuller version of that advice. `note_waiting` is off
    when the caller is about to correct the spacing regardless — an explicit
    request overrides the wait, and saying both would contradict itself.
    """
    delays, total, participants, settled = alignment_plan(accounts, states, now,
                                                         record=record)
    lines = []
    window = WINDOW_HOURS * 3600
    # Measured rather than assumed: an account labelled "1 (personal)" is wider
    # than the fixed column this used to have, so it pushed its own line out of
    # line with every other one.
    width = max([len(a.display) for a in accounts] or [0])
    if len(participants) < 2:
        # A brand-new install is not a fault, and must not read like one: no
        # account has a window yet because nothing has been pinged yet.
        if not participants and all(next_expiry(states[a.name], now) == float("inf")
                                    for a in accounts):
            lines.append("No account has been pinged yet — the spacing is "
                         "worked out from the first ping onwards.")
            return lines, delays, total, settled

        lines.append("{} — nothing to space.".format(
            "No account is holding a window" if not participants
            else "Only one account is holding a window"))
        for account in accounts:
            holding, why = participation(account, states[account.name], now)
            if not holding:
                lines.append("  account {:<{}} is not: {}".format(
                    account.display, width, why))
        return lines, delays, total, settled

    # The target is 5/N over the accounts that will actually start a window, not
    # over the accounts that exist. Say so when those differ, because otherwise
    # the spacing looks wrong to anyone counting their subscriptions.
    if len(participants) < len(accounts):
        lines.append("Windows should sit {} apart — {} of {} accounts are "
                     "holding a window.".format(
                         fmt_delta(window / float(len(participants))),
                         len(participants), len(accounts)))
    else:
        lines.append("Windows should sit {} apart.".format(
            fmt_delta(window / float(len(participants)))))

    # A hold that has been booked but not yet served leaves the phases exactly
    # where they were, so the arithmetic still reports the full error. Saying
    # only that would read as though nothing had been done.
    booked = False
    for account in accounts:
        if account.name not in delays:
            lines.append("  account {:<{}} not holding a window right now — "
                         "{}".format(account.display, width,
                                     participation(account, states[account.name],
                                                   now)[1]))
            continue
        delay = delays[account.name]
        boundary = next_expiry(states[account.name], now)
        hold = states[account.name].get("hold") or {}
        if hold.get("until", 0) > now:
            booked = True
            note = "   already held back to {}".format(fmt_time(hold["until"]))
        elif delay < ALIGN_DEADBAND_SEC:
            note = ""
        else:
            note = "   hold {} to line up".format(fmt_delta(delay))
        lines.append("  account {:<{}} next window starts {}{}".format(
            account.display, width, fmt_time(boundary), note))

    if total < ALIGN_DEADBAND_SEC:
        lines.append("Spacing is correct.")
    elif booked:
        lines.append("A correction is already booked. The spacing will be right "
                     "once those windows have started.")
    elif not settled:
        if note_waiting:
            lines.append("Spacing is off, but the set of active accounts changed "
                         "recently — waiting for it to settle before correcting.")
    elif total > AUTO_CORRECT_MAX_SEC:
        # The flag suppresses the sentence, not the branch: dropping into the
        # "small enough" case for a correction this size would contradict the
        # very next line `realign` prints.
        if suggest_realign:
            lines.append("Correcting this means {} with no window running on "
                         "the accounts being held, which is too much to do "
                         "unasked. See what it involves with:  {} "
                         "realign".format(fmt_delta(total), COMMAND))
    else:
        lines.append("Small enough to fix without asking — the ping at the next "
                     "window boundary will do it.")
    return lines, delays, total, settled


def apply_alignment(account, accounts, states, state, now, boundary):
    """
    How long this account should hold its next window back, in seconds.

    `boundary` is the moment the next window could otherwise start, and is passed
    in rather than recomputed: the caller derives it from *both* limits, while
    the spacing arithmetic only ever reasons about the 5-hour cycle. Those two
    coincide for a healthy account and diverge when a weekly limit is in the way,
    so mixing them would place a hold relative to the wrong instant.
    """
    if not boundary or len(accounts) < 2:
        return 0.0

    # A hold already booked for this account is the answer to this question,
    # and it was reached once already. The phases do not move until it has been
    # served, so recomputing here would find the same error every half hour and
    # either re-book the identical hold or — after `realign --confirm` — go on
    # telling the user to confirm a correction they have just confirmed. What
    # the anchor needs is the moment that hold ends, expressed the way the
    # caller wants it: as an amount to add to the boundary.
    booked = state.get("hold")
    if booked and booked.get("until", 0) > now:
        return max(0.0, booked["until"] - boundary)

    delays, total, participants, settled = alignment_plan(accounts, states, now)
    delay = delays.get(account.name, 0.0)

    if delay < ALIGN_DEADBAND_SEC:
        return 0.0
    if not settled:
        log(account, "Spacing is out by {} but the active accounts changed "
                     "recently — leaving it alone until that settles.".format(
                         fmt_delta(delay)))
        return 0.0

    if total > AUTO_CORRECT_MAX_SEC:
        # Said, not stored. `realign` prices the correction from live state when
        # it is asked to, because a plan recorded half a day ago describes
        # windows that have since moved on.
        log(account, "Spacing is out by {} in total, which needs a hold of {} "
                     "on this account. That is too long to do unasked — run "
                     "`{} realign --confirm` to apply it.".format(
                         fmt_delta(total), fmt_delta(delay), COMMAND))
        return 0.0

    state["hold"] = {"from": boundary, "until": boundary + delay,
                     "reason": "spacing this account {} later".format(
                         fmt_delta(delay))}
    log(account, "Holding the next window back by {} so the accounts stay "
                 "{} apart: it will start at {}.".format(
                     fmt_delta(delay),
                     fmt_delta(WINDOW_HOURS * 3600 / float(len(participants))),
                     fmt_time(boundary + delay)))
    return delay


def active_hold(state, now):
    """The hold currently suppressing pings for this account, or None."""
    hold = state.get("hold")
    if not hold:
        return None
    if hold.get("from", 0) <= now < hold.get("until", 0):
        return hold
    return None


# ---------------------------------------------------------------------------
# Checking a multi-account setup makes sense
# ---------------------------------------------------------------------------
#
# Most ways of getting this wrong keep working — they just quietly stop being
# worth anything. Logging in to the same account twice is the clearest example:
# every timer runs, every ping succeeds, and the second subscription buys
# nothing. So the checks that matter run before setup finishes and again from
# `doctor`, and they name the consequence rather than the symptom.

Finding = collections.namedtuple("Finding", "level message hint")

REFRESH_WARNING_DAYS = 7

# Markers left by file-sync tools in the directories they manage. A ping
# directory holds a login, and Claude Code rewrites its token in place on every
# refresh — so two machines syncing one directory take turns invalidating each
# other's token, and the symptom is an account that mysteriously logs itself
# out.
_SYNC_MARKERS = (".stfolder", ".stignore", ".dropbox", ".dropbox.cache",
                 ".syncthing", ".nextcloudsync.log", ".csync_journal.db")


def _epoch_seconds(value):
    """Claude Code writes some timestamps in milliseconds; normalise to seconds."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return value / 1000.0 if value > 1e11 else float(value)


def account_identity(account):
    """
    Who an account is, from Claude Code's own files. Empty when never logged in.

    Two files are involved: the identity lives in .claude.json's oauthAccount,
    while the token and plan live in .credentials.json inside the config
    directory. On macOS the latter is in Keychain instead, so a missing file
    means "cannot tell", never "not logged in".
    """
    oauth = (_read_json(account.config_json).get("oauthAccount") or {})
    creds = _read_json(os.path.join(account.config_dir, ".credentials.json"))
    claude_ai = creds.get("claudeAiOauth") or {}
    return {
        "account_uuid": oauth.get("accountUuid"),
        "email": oauth.get("emailAddress"),
        "organization_uuid": (oauth.get("organizationUuid")
                              or creds.get("organizationUuid")),
        "subscription": claude_ai.get("subscriptionType"),
        "tier": (claude_ai.get("rateLimitTier")
                 or oauth.get("organizationRateLimitTier")),
        "refresh_expires_at": _epoch_seconds(claude_ai.get("refreshTokenExpiresAt")),
        "has_token": bool(claude_ai.get("accessToken")),
    }


def sign_in_command(account):
    """How to sign in to this account's ping directory."""
    return "CLAUDE_CONFIG_DIR={} claude".format(account.config_dir)


def _sync_marker(path):
    """The first file-sync marker found at or above path, or ''."""
    seen = set()
    while path and path not in seen and path != os.path.dirname(path):
        seen.add(path)
        for marker in _SYNC_MARKERS:
            if os.path.exists(os.path.join(path, marker)):
                return os.path.join(path, marker)
        if path == HOME:
            break
        path = os.path.dirname(path)
    return ""


def validate_accounts(accounts):
    """
    Everything worth saying about a configured set of accounts, worst first.

    Returns Findings rather than printing, so setup, `doctor` and the tests can
    each present them their own way.
    """
    findings = []
    identities = {}

    for account in accounts:
        identity = identities[account.name] = account_identity(account)

        if not os.path.isdir(account.config_dir):
            findings.append(Finding(
                "error",
                "Account {}: {} does not exist".format(
                    account.name, account.config_dir),
                "Create it and sign in with: {}".format(sign_in_command(account))))
            continue

        if not identity["has_token"] and not identity["account_uuid"]:
            findings.append(Finding(
                "error",
                "Account {} is not signed in".format(account.name),
                "Sign in with: {}   then /login (this sends no message, so it "
                "starts no usage window)".format(sign_in_command(account))))

        subscription = (identity["subscription"] or "").lower()
        if subscription and subscription in ("free", "none"):
            findings.append(Finding(
                "error",
                "Account {} is on the {} plan".format(
                    account.name, identity["subscription"]),
                "This tool only does anything useful for a paid subscription."))

        expires = identity["refresh_expires_at"]
        if expires and expires - time.time() < REFRESH_WARNING_DAYS * 86400:
            findings.append(Finding(
                "warning",
                "Account {}'s login expires {}".format(
                    account.name, fmt_time(expires)),
                "Sign in again before then; an expired login looks exactly like "
                "the tool having stopped working."))

        mode = _permissions(account.config_dir)
        if mode is not None and mode & 0o077:
            findings.append(Finding(
                "warning",
                "{} is readable by other users (mode {:o})".format(
                    account.config_dir, mode),
                "It holds an access token: chmod 700 {}".format(
                    account.config_dir)))

        marker = _sync_marker(account.config_dir)
        if marker:
            findings.append(Finding(
                "warning",
                "Account {}'s config directory is inside a synced folder "
                "({})".format(account.name, marker),
                "It holds a login, and a token refreshed on one machine will "
                "be overwritten by the copy from another. Move it outside the "
                "synced folder, or exclude it."))

    # The mistake that leaves everything apparently working and worth nothing.
    by_uuid = {}
    for account in accounts:
        uuid_ = (identities[account.name]["account_uuid"]
                 or identities[account.name]["email"])
        if not uuid_:
            continue
        if uuid_ in by_uuid:
            findings.append(Finding(
                "error",
                "Accounts {} and {} are the same Claude account ({})".format(
                    by_uuid[uuid_], account.name,
                    identities[account.name]["email"] or uuid_),
                "Two logins to one account share one usage window, so the "
                "second subscription buys nothing. Sign one of them in as a "
                "different account."))
        else:
            by_uuid[uuid_] = account.name

    tiers = set(identities[a.name]["tier"] for a in accounts
                if identities[a.name]["tier"])
    if len(tiers) > 1:
        findings.append(Finding(
            "warning",
            "The accounts are on different plans ({})".format(
                ", ".join(sorted(tiers))),
            "Windows are spaced evenly, which assumes each account supplies a "
            "similar amount. Uneven plans still work; the spacing is just no "
            "longer optimal."))

    order = {"error": 0, "warning": 1}
    return sorted(findings, key=lambda f: order.get(f.level, 2))


def _permissions(path):
    try:
        return os.stat(path).st_mode & 0o777
    except OSError:
        return None


# Set on every ping so anything watching `claude` invocations can tell a ping
# from a person. Nothing here needs it — CLAUDE_CONFIG_DIR already pins the
# account — but a marker is clearer than inferring intent from a path.
PING_MARKER_ENV = "CLAUDE_WINDOW_TIMING_PING"


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


def build_claude_env(account):
    """
    Build a clean, minimal environment for the Claude subprocess.

    CLAUDE_CONFIG_DIR is always set, and always to this account's own ping
    directory. Nothing here can reach the user's ~/.claude.
    """
    env = {
        "HOME":  HOME,
        "USER":  USER,
        "TERM":  "xterm-256color",
        "SHELL": "/bin/bash",
        "PATH":  os.path.join(HOME, ".local", "bin")
                 + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        PING_MARKER_ENV: "1",
        "CLAUDE_CONFIG_DIR": account.config_dir,
        # A ping's reply is thrown away, but thinking tokens are billed as
        # output — and output is the expensive half. Measured on a real
        # account: 'bye' with thinking costs 37 output tokens, without it 4.
        # Wording the prompt to demand brevity does not help and made it
        # worse (41 tokens), because the instruction is itself something to
        # think about. Turning thinking off is the whole saving.
        "MAX_THINKING_TOKENS": "0",
        # Nothing here should upgrade the CLI behind the user's back: an
        # update changes the tool schemas that sit at the front of every
        # cached prompt prefix, so a background ping could silently make
        # every session the user has open expensive to resume.
        "DISABLE_AUTOUPDATER": "1",
    }
    for key in _ENV_PASSTHROUGH:
        if key in os.environ:
            env[key] = os.environ[key]
    return env


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log_quietly(account, msg):
    """
    Append to the account's log without printing.

    `log` prints as well, which is right during a ping -- the run's output *is*
    the log -- and wrong anywhere a command's stdout means something. A warning
    written this way from a read-only command corrupted `status --json`, which
    is a documented machine-readable contract; the test for that contract is
    what caught it.
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = "[{}] {}".format(timestamp, msg)
    account.ensure_state_dir()
    with open(account.log_file, "a") as f:
        f.write(line + "\n")
    return line


def log(account, msg):
    print(log_quietly(account, msg))


def rotate_log(account):
    if not os.path.exists(account.log_file):
        return
    cutoff = datetime.now() - timedelta(hours=LOG_RETENTION_HOURS)
    with open(account.log_file, "r") as f:
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
    with open(account.log_file, "w") as f:
        f.writelines(kept)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def read_state(account):
    try:
        with open(account.state_file) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def write_state(account, state):
    account.ensure_state_dir()
    tmp = _temp_name(account.state_file)
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, account.state_file)


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

def statusline_settings(account):
    """
    Inline --settings JSON that points Claude's statusLine back at this script.

    What travels is the **file to write**, not the account to look up. Claude
    Code spawns this command itself, so the child starts from nothing: no
    inherited environment worth trusting, and — because it re-imports this
    module — a fresh set of module-level paths pointing at whatever checkout
    the script lives in. Handing it a name to resolve made the readings land
    wherever *that* copy's accounts.json said account "2" lives, which is not
    necessarily the account being pinged, or even the same install. Handing it
    a path cannot go anywhere else.
    """
    command = " ".join(shlex.quote(part) for part in (
        sys.executable or "/usr/bin/python3",
        os.path.abspath(__file__),
        "capture-statusline", os.path.abspath(account.statusline_file),
    ))
    return json.dumps({
        "statusLine": {"type": "command", "command": command, "padding": 0}
    })


def capture_statusline(path):
    """
    Append the statusLine payload to `path`, one JSON object per line.

    Claude Code invokes this repeatedly during a session and only the later
    invocations carry rate_limits, so we append rather than overwrite and pick the
    freshest usable record afterwards. Prints nothing: the status line stays blank.
    This must stay silent and side-effect-free — it runs inside the Claude UI loop.
    """
    try:
        raw = sys.stdin.read()
        json.loads(raw)  # validate before storing
        directory = os.path.dirname(path)
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, 0o700)
        with open(path, "a") as f:
            f.write(raw.replace("\n", " ") + "\n")
    except Exception:
        pass


def statusline_records(account):
    """
    Every complete statusLine payload captured so far, oldest first.

    A half-written final line is skipped rather than treated as data — the file is
    read while Claude Code is still appending to it, so "no valid JSON yet" is an
    ordinary state, not an error.
    """
    records = []
    try:
        with open(account.statusline_file) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    except (IOError, OSError):
        pass
    return records


def statusline_api_ms(account, records=None):
    """
    Highest total_api_duration_ms reported by the statusLine so far.

    Claude Code only writes the session JSONL on exit, so a run cannot watch that
    file to know when the reply landed. The statusLine can: this counter stays at
    its starting value until the API call completes, then jumps. That is a far
    better completion signal than guessing from when the screen stops changing.
    """
    if records is None:
        records = statusline_records(account)
    return max([(r.get("cost") or {}).get("total_api_duration_ms") or 0
                for r in records] or [0])


# Where a live reading comes from. The statusLine figures everywhere else are
# captured during a ping, so they are up to one interval old -- and the thing
# most likely to have moved them since is the user's own work, which is exactly
# what the recommendation is about to be made against. So `which` and `status`
# take one by default. It costs one very small request against each account it
# asks about; `--no-live` skips it, and the bare `claude-window` never takes
# one, because the command people type idly must stay free.
LIVE_URL = "https://api.anthropic.com/v1/messages"
LIVE_MODEL = "claude-haiku-4-5-20251001"
OAUTH_BETA = "oauth-2025-04-20"


def _limits_from_headers(headers):
    """
    The rate-limit figures a response carries, in the statusLine's own shape.

    Anything missing or unparseable is left out rather than guessed at, so an
    answer that carries only half the picture contributes only that half.
    """
    limits = {}
    for key, prefix in (("five_hour", "5h"), ("seven_day", "7d")):
        used = headers.get(
            "anthropic-ratelimit-unified-{}-utilization".format(prefix))
        resets = headers.get(
            "anthropic-ratelimit-unified-{}-reset".format(prefix))
        window = {}
        if used is not None:
            try:
                window["used_percentage"] = round(float(used) * 100)
            except (TypeError, ValueError):
                pass
        if resets is not None:
            try:
                window["resets_at"] = int(resets)
            except (TypeError, ValueError):
                pass
        if window:
            limits[key] = window
    return limits


def read_live_limits(account):
    """
    Ask Claude what this account's limits are *now*, as (limits, problem).

    The rate-limit headers ride on a successful response, so this has to be a
    real request: the smallest one that can be made, one token in and one out,
    on the cheapest model. A refusal is an answer too -- an account that refuses
    is spent, whatever the last ping said.

    `limits` has the same shape the statusLine produces, so every caller
    downstream is unchanged, and is {} when nothing could be read; `problem` is
    a sentence saying why, or "".
    """
    creds = (_read_json(credentials_path(account)).get("claudeAiOauth") or {})
    token = creds.get("accessToken")
    if not token:
        return {}, "there is no login in {}".format(account.config_dir)
    body = json.dumps({"model": LIVE_MODEL, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "."}]}).encode()
    request = urllib.request.Request(LIVE_URL, data=body, headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": "{}/{}".format(COMMAND, "live")})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            headers = response.headers
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # Refused, which is itself the reading: whatever the last ping
            # believed, a real request is being turned away right now.
            #
            # A refusal may or may not carry the rate-limit headers. Where it
            # does they are worth having, because they name *which* limit
            # refused and when it comes back -- and a weekly limit reported as
            # the 5-hour one would have the account read as usable again five
            # hours from now, days early. Where it does not, all that is known
            # is that something is spent, and the 5-hour limit is the one that
            # nearly always is.
            refused = _limits_from_headers(e.headers or {})
            if not any((window.get("used_percentage") or 0) >= LIMIT_SPENT_PCT
                       for window in refused.values()):
                refused["five_hour"] = dict(refused.get("five_hour") or {},
                                            used_percentage=LIMIT_SPENT_PCT)
            return refused, ""
        detail = ""
        try:
            detail = e.read()[:200].decode("utf8", "replace")
        except Exception:
            pass
        if e.code in (400, 404) and LIVE_MODEL in detail:
            # The one failure this design knows it is exposed to: the probe
            # names a model, and model names are retired. Say so in as many
            # words, because the alternative is a tool that quietly goes back
            # to reporting half-hour-old figures as if they were current.
            return {}, ("the model this probe uses ({}) was rejected — it has "
                        "probably been retired, and LIVE_MODEL needs "
                        "updating [HTTP {}]".format(LIVE_MODEL, e.code))
        if e.code in (401, 403):
            # Worth the extra sentence: the ordinary cause is not a login gone
            # bad but an access token that expired between pings -- they last
            # about eight hours, and a machine that was asleep can wake with a
            # stale one. The next ping renews it. `doctor` asks the CLI itself
            # whether the login still works, which is the question this cannot
            # answer.
            return {}, ("this account's saved token was rejected [HTTP {}] — "
                        "usually one that expired between pings, which the "
                        "next ping renews".format(e.code))
        return {}, "Claude answered HTTP {}{}".format(
            e.code, ": " + detail if detail else "")
    except (urllib.error.URLError, OSError) as e:
        return {}, "could not reach Claude: {}".format(e)

    limits = _limits_from_headers(headers)
    if not limits:
        return {}, ("Claude answered, but sent no rate-limit headers — the "
                    "reading cannot be taken this way any more")
    return limits, ""


def probe_is_safe(state, now):
    """
    Whether asking this account for a reading can be done without side effects.

    Any billed request starts a 5-hour window if none is running -- so probing
    an account between windows would start one, at a moment nothing chose. That
    is precisely what the anchoring machinery exists to decide, and a status
    command has no business moving it. Where a window is already running the
    probe cannot start anything; it just reports.

    An account whose window has ended keeps its cached figure, which is honest:
    the figure is stale, the age is on screen, and the next ping refreshes it
    within the interval anyway.

    The reset time is read raw rather than through `next_expiry`, which rolls a
    stale reading forward on the assumption that windows tile back to back.
    That assumption is the tool's *intent* and the right one for judging phase,
    but here it would answer "a window is running" for an account that has not
    been heard from in days -- which is exactly the account a probe would start
    one on.
    """
    recorded = ((state.get("rate_limits") or {}).get("five_hour")
                or {}).get("resets_at")
    return bool(recorded) and recorded > now


def refresh_limits(accounts):
    """
    Take a live reading for every account it is safe to ask. Returns what it got.

    Written back into state so that the next command -- and the next ping's
    plausibility check -- start from the same picture the user was just shown.

    Nothing is read on a machine that does not ping. A reading is taken with
    the ping directory's own login, and there is no such directory there; that
    machine is not the authority on these accounts either, since `which` and
    `switch` both answer from the schedule the pinging machine published.
    """
    taken, now = {}, time.time()
    if not pings_here():
        return taken
    for account in accounts:
        state = read_state(account)
        if not probe_is_safe(state, now):
            continue
        limits, problem = read_live_limits(account)
        if problem:
            # Loud on the way past, and recorded so `doctor` keeps saying it
            # after the moment has scrolled by. A live reading that silently
            # falls back to half-hour-old figures is the failure this whole
            # option exists to prevent.
            sys.stderr.write("WARNING: no live reading for account {}: {}\n"
                             .format(account.display, problem))
            log_quietly(account, "WARNING: live reading failed: {}".format(problem))
            state["live_problem"] = problem
            state["live_problem_at"] = time.time()
            account.ensure_state_dir()
            write_state(account, state)
            continue
        state.pop("live_problem", None)
        state.pop("live_problem_at", None)
        taken[account.name] = limits
        merged = dict(state.get("rate_limits") or {})
        for key, window in limits.items():
            merged[key] = dict(merged.get(key) or {}, **window)
        state["rate_limits"] = merged
        state["limits_source"] = "live"
        state["limits_read_at"] = time.time()
        account.ensure_state_dir()
        write_state(account, state)
    return taken


def read_statusline_limits(account):
    """Return the most recent {'five_hour': {...}, 'seven_day': {...}} seen, or {}."""
    latest = {}
    for record in statusline_records(account):
        limits = record.get("rate_limits")
        if isinstance(limits, dict) and limits:
            latest = limits
    return latest


# How far into the future a previously-known reset must still be before a
# claimed rollover is treated as impossible. Small, because the only thing being
# guarded against is a reading that contradicts arithmetic, not one that is
# merely surprising.
ROLLOVER_SLACK_SEC = 60


def implausible_limits(new, previous, now):
    """
    Which of these readings cannot be believed, as {limit key: why}.

    A window cannot begin again before the end of the one before it. So a
    reading that reports a *later* reset time while the reset we already knew
    about is still in the future is describing something that cannot have
    happened, and is discarded rather than acted on.

    This is not hypothetical. A run once reported "5-hour 3%, resets in exactly
    4h00m00s, weekly 20%, resets in exactly 72h00m00s" — round numbers measured
    from the moment of the run, i.e. placeholders rather than observations —
    fourteen minutes before the same account correctly reported 99% with its
    real reset time. Believed, it moved the account's boundary three hours late
    and produced a recommendation to hold an account back for two hours.

    Per limit, because the two arrive together but are independent
    observations. Judged as one reading, an impossible weekly reset threw away
    a perfectly good 5-hour one — and a refused ping needs that 5-hour figure
    more than at any other moment, since it is the only thing that says when
    the account comes back.
    """
    problems = {}
    if not previous:
        return problems
    for key, name in _LIMIT_NAMES:
        was = (previous.get(key) or {}).get("resets_at")
        now_says = (new.get(key) or {}).get("resets_at")
        if not was or not now_says:
            continue
        if now_says > was and was > now + ROLLOVER_SLACK_SEC:
            problems[key] = (
                "{} claims to reset at {} but the reset already known, {}, "
                "has not passed yet".format(name, fmt_time(now_says),
                                            fmt_time(was)))
    return problems


def fmt_pct(used):
    return "?%" if used is None else "{}%".format(used)


def format_usage(account, limits=None):
    """
    One line summarising both limits: how much is used, and when each resets.

    Logged on every ping. It costs nothing extra — the figures already arrive with
    the statusLine report the run needs anyway — and it turns the log into a
    record of usage over time rather than only a record of pings.
    """
    if limits is None:
        limits = read_statusline_limits(account)
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


def next_window_start(limits, refusal_text, was_limited, now=None):
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

    # Each candidate is bounded by the length of the limit it came from: a
    # 5-hour window cannot reset 21 hours from now, and a reading that says so
    # is a misparse or a placeholder rather than a very long window. Dropped
    # here, one at a time, rather than at the anchor: `max` runs first, so one
    # nonsense candidate used to swallow every sane one beside it and leave a
    # refused account marked unusable for a day.
    now = time.time() if now is None else now
    sane = [c for c in candidates if c[0] <= now + c[1]]
    if not sane:
        return None, None, ""
    return max(sane, key=lambda c: c[0])


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


# What `systemctl --user is-system-running` answers when there is a manager to
# answer. Anything else -- "Failed to connect to bus", or nothing at all -- is
# not a state, it is the absence of an answer.
_MANAGER_STATES = ("running", "degraded", "initializing", "starting",
                   "stopping", "maintenance", "offline", "unknown")


def systemd_reachable():
    """
    Whether this process can reach the user's systemd manager.

    `systemctl --user` finds the bus through XDG_RUNTIME_DIR. A login session
    has it; cron, a container exec and `ssh host command` on some
    distributions do not -- and without it every query fails in exactly the way
    a missing timer does. Reported per account, that reads as "the timer for
    account 1 is not enabled", twice, with a fix that fails the same way: a
    diagnostic crying wolf about the one thing it is supposed to be trusted on.
    """
    return (_systemctl("is-system-running").stdout or "").strip().lower() \
        in _MANAGER_STATES


def cancel_anchor(account):
    """Clear any pending anchor so a new one can take its place."""
    for unit in (account.anchor_unit + ".timer", account.anchor_unit + ".service"):
        _systemctl("stop", unit)
    _systemctl("reset-failed",
               account.anchor_unit + ".timer", account.anchor_unit + ".service")


def anchor_pending(account):
    """Return the pending anchor's scheduled time as a string, or '' if none."""
    result = _systemctl("show", account.anchor_unit + ".timer",
                        "--property=NextElapseUSecRealtime", "--value")
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    return value if value and value not in ("n/a", "0") else ""


def _system_local(epoch):
    """
    `epoch` as wall clock in the *system's* timezone, whatever TZ this process
    was handed.

    systemd reads an OnCalendar stamp in the system's zone, not in the
    environment of whoever wrote it, so the two have to agree. They do when TZ
    is unset, which is how the timer runs a ping. They do not for somebody who
    runs a command by hand from a shell with TZ set to somewhere else, and the
    anchor is then booked hours from where it was meant to be -- forward, and
    the ping happens at the wrong moment, or backward, where an OnCalendar time
    in the past simply never fires. Either way the one correction the schedule
    relies on is silently absent.

    Everything this tool *prints* stays in the reader's TZ, which is theirs to
    set. This is not printed: it is an instruction to systemd.
    """
    saved = os.environ.pop("TZ", None)
    try:
        time.tzset()
        return datetime.fromtimestamp(epoch)
    finally:
        if saved is not None:
            os.environ["TZ"] = saved
        time.tzset()


def schedule_anchor(account, target_epoch):
    """
    Create a transient one-shot timer that starts the ping service at target_epoch.

    OnCalendar (wall clock) rather than OnActiveSec (monotonic) is deliberate:
    monotonic timers do not advance while the machine is suspended, which is one
    of the very situations this is meant to recover from.

    The moment is given in UTC, because a local wall clock is not a reliable
    way to name an instant twice a year. In the autumn hour that happens twice,
    systemd takes the first of the two and the ping lands inside the window it
    was meant to end; in the spring hour that never happens at all,
    `systemd-analyze` answers "Next elapse: never" and the correction simply
    does not occur -- on a machine whose whole schedule depends on it. Older
    systemd may not accept a timezone on a calendar spec, so the local form is
    kept as a fallback rather than as the default.

    The anchor starts the ping service with --no-block so it finishes immediately
    instead of waiting for the ping to return. Otherwise the anchor unit would
    still be active *during* the run it triggered, and that run deciding it needs
    another anchor would end up trying to cancel its own parent.
    """
    cancel_anchor(account)
    stamps = (datetime.fromtimestamp(target_epoch, timezone.utc).strftime(
                  "%Y-%m-%d %H:%M:%S UTC"),
              _system_local(target_epoch).strftime("%Y-%m-%d %H:%M:%S"))
    result = None
    for stamp in stamps:
        result = _run(
            ["systemd-run", "--user", "--collect",
             "--unit", account.anchor_unit,
             "--description",
             "Claude Window Timing — window-boundary anchor for account "
             + account.name,
             "--on-calendar", stamp,
             "--timer-property=AccuracySec=1s",
             "systemctl", "--user", "start", "--no-block",
             account.service_unit])
        if result.returncode == 0:
            log(account, "Anchor scheduled for {} (in {})".format(
                stamp, fmt_delta(target_epoch - time.time())))
            return True
    log(account, "WARNING: could not schedule anchor: {}".format(
        (result.stdout or "").strip() if result else "no answer"))
    return False


def maybe_schedule_anchor(account, boundary, horizon, label, state, was_limited):
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
        log(account, "Reset time {} is out of range for the {} — ignoring.".format(
            fmt_time(boundary), label))
        return

    streak = state.get("anchor_streak", 0)
    if was_limited and streak >= MAX_ANCHOR_STREAK:
        log(account,
            "Anchored {} times in a row and still rate-limited — no more anchors "
            "until a ping succeeds (ordinary pings carry on).".format(streak))
        return

    target = boundary + account.guard_sec
    if target - now > INTERVAL_MIN * 60:
        log(account,
            "Next ping can start a window at {} (in {}, set by the {}) — "
            "more than one interval away, no anchor needed yet.".format(
                fmt_time(boundary), fmt_delta(boundary - now), label))
        return

    log(account,
        "Next ping can start a window at {} (in {}, set by the {}) — "
        "within one interval.".format(
            fmt_time(boundary), fmt_delta(boundary - now), label))
    if schedule_anchor(account, target):
        state["anchor_target"] = target
        state["anchor_label"] = label
        state["anchor_streak"] = streak + 1 if was_limited else 1


# ---------------------------------------------------------------------------
# Checkpoint backup / restore
# ---------------------------------------------------------------------------

def _session_file(account, session_id):
    return os.path.join(account.session_dir, f"{session_id}.jsonl")


def _assistant_entries(account, session_id):
    """Return the (deduplicated) assistant-turn objects recorded in the session file."""
    path = _session_file(account, session_id)
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


def _discard_session(account, session_id):
    """Remove a session file from a failed setup attempt so a retry starts clean."""
    try:
        os.remove(_session_file(account, session_id))
    except OSError:
        pass


def backup_checkpoint(account, session_id):
    account.ensure_state_dir()
    shutil.copy2(_session_file(account, session_id), account.checkpoint_backup)
    log(account, "Checkpoint backed up ({} bytes)".format(
        os.path.getsize(account.checkpoint_backup)))


def ensure_ping_cwd(account):
    """
    Create the account's ping working directory if absent, and return it.

    Called from every place that spawns Claude, because a working directory that
    does not exist turns an ordinary run into "could not run the CLI" — which
    reads as a broken login rather than a missing directory.
    """
    try:
        if not os.path.isdir(account.ping_cwd):
            os.makedirs(account.ping_cwd, 0o700)
    except OSError:
        # Fall back to somewhere that certainly exists rather than fail the run.
        return account.config_dir if os.path.isdir(account.config_dir) else HOME
    return account.ping_cwd


def checkpoint_is_for_this_cwd(account):
    """
    Whether the stored checkpoint can actually be resumed where pings now run.

    Claude Code resolves `--resume <id>` through a per-account registry that
    records the directory a session was created in; resuming from anywhere else
    answers "No conversation found". A checkpoint from an install that pinged
    somewhere else is therefore unusable, and unusable in a way that looks like
    a working install until the first ping fails.

    An absent marker means the checkpoint predates this file, which is exactly
    the case that needs rebuilding.
    """
    if not os.path.exists(account.checkpoint_backup):
        return True          # nothing to be wrong about yet
    return _read_text(account.checkpoint_cwd_file).strip() == account.ping_cwd


def restore_checkpoint(account, session_id):
    # The session directory belongs to Claude Code and may not exist yet on a
    # freshly created account, so make it rather than assume it.
    if not os.path.isdir(account.session_dir):
        os.makedirs(account.session_dir, 0o700)

    # Every transcript line records the directory the session ran in, and Claude
    # Code will not resume a session whose recorded directory is not the one it
    # is being resumed from — it belongs to a different project. Rewriting the
    # field on restore is what lets the working directory move at all, and makes
    # a checkpoint taken under an older layout usable under a newer one without
    # asking the user to re-initialise (which would cost a window).
    written = 0
    with open(account.checkpoint_backup) as src, \
            open(_session_file(account, session_id), "w") as dst:
        for line in src:
            stripped = line.strip()
            if stripped:
                try:
                    entry = json.loads(stripped)
                except ValueError:
                    dst.write(line)
                    continue
                if isinstance(entry, dict) and entry.get("cwd") is not None:
                    entry["cwd"] = account.ping_cwd
                    written += 1
                dst.write(json.dumps(entry) + "\n")
            else:
                dst.write(line)
    log(account, "Checkpoint restored to frozen 'hi' state"
                 + (" ({} line(s) repointed at {})".format(written, account.ping_cwd)
                    if written else ""))


# ---------------------------------------------------------------------------
# Interactive Claude session (PTY)
# ---------------------------------------------------------------------------
#
# One rule governs everything below: **once the prompt has been sent, nothing in
# the teardown may abort the run.** By that point the ping has already reached
# Claude and started a window; if the run dies afterwards it never writes its
# state or books the boundary anchor, so a crash that looks harmless costs a
# permanently late schedule. Both PTY helpers exist to keep that promise.


def _drain(fd, size=65536):
    """
    Read whatever is waiting on the PTY master; return b"" when there is nothing.

    Two conditions are ordinary here and neither is a failure:

      * `BlockingIOError` — the fd is non-blocking and no output is pending.
      * `OSError` with EIO — the child closed the slave side. On Linux a master
        whose slave is gone reports EIO rather than EOF, and the child exiting is
        exactly what the post-`/exit` drain is waiting for.

    `BlockingIOError` is a subclass of `OSError`, so one clause covers both. This
    used to be `except BlockingIOError` alone, which let the EIO escape and abort
    the run after a successful ping — measured at 5 of 98 runs over 48 hours.
    """
    try:
        return os.read(fd, size)
    except OSError:
        return b""


def _send(fd, data):
    """
    Write to the PTY master, reporting rather than raising if the child is gone.

    Returns True if the bytes were handed over. A dead child does not reliably
    fail this call — the master accepts writes into its buffer even after the
    slave closes — so a True result means "sent", not "received". The guard is
    here because a raising teardown would break the rule stated above.
    """
    try:
        os.write(fd, data)
        return True
    except OSError:
        return False


def run_interactive(account, extra_args, prompt_text, session_id,
                    startup_wait=None, completion_timeout=None,
                    statusline_wait=None):
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
        log(account, "ERROR: Claude CLI not found at {}. If Claude Code is "
                     "installed but lives somewhere only your shell knows "
                     "about — an nvm or npm install does — re-run "
                     "./install.sh from that shell so the timer is told where."
                     .format(CLAUDE_PATH))
        return {"completed": False, "limited": False, "text": ""}

    # Start each run with a clean statusLine capture so stale readings from the
    # previous run can never be mistaken for this one's.
    account.ensure_state_dir()
    try:
        os.remove(account.statusline_file)
    except OSError:
        pass

    startup_wait = STARTUP_WAIT_SEC if startup_wait is None else startup_wait
    completion_timeout = (COMPLETION_TIMEOUT_SEC if completion_timeout is None
                          else completion_timeout)
    statusline_wait = (STATUSLINE_WAIT_SEC if statusline_wait is None
                       else statusline_wait)

    master_fd, slave_fd = pty.openpty()
    cmd = [CLAUDE_PATH,
           "--tools", "",
           # A background ping has no business driving a browser, and without
           # this an account that has never answered the "Claude in Chrome
           # detected" prompt sits on it forever with nobody to press a key.
           # That prompt appeared in 2.1.x, which is the general lesson: a new
           # first-run question can block the ping at any release, so the ping
           # asks for as little of Claude Code as it can.
           "--no-chrome",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
           "--settings", statusline_settings(account),
           "--model", "haiku", "--effort", "low"] + extra_args

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=ensure_ping_cwd(account),
            preexec_fn=os.setsid,
            env=build_claude_env(account),
        )
    except Exception as e:
        log(account, f"ERROR: Failed to spawn Claude: {e}")
        os.close(master_fd)
        os.close(slave_fd)
        return {"completed": False, "limited": False, "text": ""}

    os.close(slave_fd)
    os.set_blocking(master_fd, False)

    time.sleep(startup_wait)
    _drain(master_fd, 8192)  # drain startup output

    # Wait for one complete statusLine report before taking the baseline. On a
    # --resume the very first report already carries the API duration inherited
    # from the restored session, so a baseline taken before it arrives would be 0
    # and that inherited figure would instantly look like our own reply landing.
    # Waiting for a parsed record — not merely for the file to exist, which happens
    # a moment earlier — closes that race. If no report ever comes (no
    # subscription, so no statusLine data) the baseline stays 0, the counter stays
    # 0, and completion falls through to the PTY heuristic below.
    statusline_deadline = time.time() + statusline_wait
    while time.time() < statusline_deadline and not statusline_records(account):
        time.sleep(0.25)
        _drain(master_fd)

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
    baseline = len(_assistant_entries(account, session_id))
    api_baseline = statusline_api_ms(account)
    log(account, f"Sending: '{prompt_text}'")
    sent = _send(master_fd, (prompt_text + "\r").encode())
    if not sent:
        log(account,
            "ERROR: could not send the prompt — the Claude process is already gone.")

    min_settle = 10.0
    idle_threshold = 5.0
    settle_after_reply = 2.0
    start = time.time()
    deadline = start + completion_timeout
    last_activity = start
    saw_output = False
    while sent and time.time() < deadline:
        time.sleep(0.5)
        chunk = _drain(master_fd)
        if chunk:
            saw_output = True
            last_activity = time.time()

        if statusline_api_ms(account) > api_baseline:
            # Reply confirmed. Give the UI a moment to finish rendering before
            # /exit, so the transcript is written with the turn complete.
            drain_until = time.time() + settle_after_reply
            while time.time() < drain_until:
                time.sleep(0.25)
                _drain(master_fd)
            break

        if (not chunk and saw_output
                and (time.time() - start) >= min_settle
                and (time.time() - last_activity) >= idle_threshold):
            break  # output settled — assume the turn finished

    _send(master_fd, b"/exit\r")

    # Keep draining while it shuts down: Claude Code emits a burst of terminal
    # escape sequences on exit, and a full PTY buffer would block it from exiting
    # cleanly — which is exactly when the session file would not get written.
    for _ in range(16):
        if proc.poll() is not None:
            break
        time.sleep(0.5)
        _drain(master_fd)

    if proc.poll() is None:
        log(account, "Process still running — sending SIGTERM")
        proc.terminate()
        proc.wait()

    try:
        os.close(master_fd)
    except OSError:
        pass

    # Now that the process has exited, the session file is flushed: verify a new
    # assistant turn was recorded and log its token usage (cache read vs write).
    entries = _assistant_entries(account, session_id)
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
        log(account,
            "Turn confirmed{}: cache_read={} cache_write={} in={} out={}".format(
            " [RATE-LIMITED]" if limited else "",
            usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0),
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0)))
        if limited:
            log(account, "Refusal: {}".format(text.strip()))
    else:
        log(account,
            "WARNING: no new assistant turn recorded — the ping may not have counted.")

    usage = format_usage(account)
    if usage:
        log(account, usage)

    log(account, f"Exited with code: {proc.returncode}")
    return {"completed": completed, "limited": limited, "text": text}


# ---------------------------------------------------------------------------
# Init (one-time, called by install.sh)
# ---------------------------------------------------------------------------

def init(account):
    """
    Create the frozen checkpoint session with a single 'hi' message and back it up.
    Must be run once per account before its systemd timer starts.
    """
    if os.path.exists(account.session_id_file) and \
       os.path.exists(account.checkpoint_backup) and \
       not checkpoint_is_for_this_cwd(account):
        print("Account {}: the existing checkpoint was built for another "
              "working directory, so it cannot be resumed. Rebuilding it."
              .format(account.display))
        for path in (account.session_id_file, account.checkpoint_backup):
            try:
                os.remove(path)
            except OSError:
                pass

    if os.path.exists(account.session_id_file) and \
       os.path.exists(account.checkpoint_backup):
        print("Account {}: checkpoint already exists.".format(account.display))
        print("  Session: {}".format(_read_text(account.session_id_file).strip()))
        print("  Backup:  {} ({} bytes)".format(
            account.checkpoint_backup, os.path.getsize(account.checkpoint_backup)))
        print("  To rebuild it, delete {} and re-run ./install.sh.".format(
            account.state_dir))
        return

    account.ensure_state_dir()
    # A ping directory is ours, so we assert what a ping needs rather than
    # hoping the user's config happens to contain it.
    ensure_ping_config(account)
    for path in (account.session_id_file, account.checkpoint_backup):
        if os.path.exists(path):
            os.remove(path)          # clean up a half-finished earlier attempt

    checkpoint_id = str(uuid.uuid4())
    log(account, f"Creating checkpoint session {checkpoint_id[:8]}... with 'hi'")

    result = run_interactive(account, ["--session-id", checkpoint_id], "hi",
                             checkpoint_id)

    # The checkpoint is frozen once and replayed by every future ping, so it must
    # not be built out of a refusal. That would bake the refusal into the prompt
    # for good, and install.sh would report success over a degraded setup.
    if result["limited"]:
        log(account, "ERROR: Claude refused the first message, so there is no "
                     "reply to build the checkpoint from:")
        log(account, "  " + result["text"].strip())
        log(account, "Wait for the limit to reset, then run ./install.sh again.")
        _discard_session(account, checkpoint_id)
        sys.exit(1)

    if not result["completed"]:
        log(account, "ERROR: Failed to create checkpoint session.")
        _discard_session(account, checkpoint_id)
        sys.exit(1)

    with open(account.session_id_file, "w") as f:
        f.write(checkpoint_id)

    backup_checkpoint(account, checkpoint_id)
    with open(account.checkpoint_cwd_file, "w") as f:
        f.write(account.ping_cwd)
    log(account, f"Checkpoint ready: {checkpoint_id}")


# ---------------------------------------------------------------------------
# The ping run (called by the systemd timer on each interval)
# ---------------------------------------------------------------------------

def acquire_ping_lock(account):
    """
    Hold this account's ping lock, or return None if a ping is already running.

    systemd will not run two instances of one service unit at once, so the
    timer and the anchor cannot collide. A ping typed by hand can, and it lands
    on the one file a run cannot share: the checkpoint is copied over the
    session transcript at the start of every run, so a second run restoring it
    while the first is being read is how a perfectly good ping comes back as
    "no assistant turn recorded" -- and gets counted against the account.

    The loser skips rather than waits. The pings are half an hour apart and the
    schedule is repaired by the boundary anchor, so one skipped ping costs
    nothing; a queued second one would ping twice in a row for no reason.
    """
    account.ensure_state_dir()
    fd = os.open(os.path.join(account.state_dir, "ping.lock"),
                 os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        os.close(fd)
        return None
    return fd


def ping(account, accounts=None):
    lock = acquire_ping_lock(account)
    if lock is None:
        log(account, "A ping for account {} is already running — skipping this "
                     "one rather than pinging twice.".format(account.display))
        return
    try:
        _ping(account, accounts)
    finally:
        os.close(lock)          # releases the flock with it


def _ping(account, accounts=None):
    rotate_log(account)
    accounts = accounts or [account]
    states = {a.name: read_state(a) for a in accounts}
    state = states[account.name]
    now = time.time()

    # A hold is the one reason a ping is ever skipped, and it is always about
    # timing, never about whether the account looks usable. Pings to an account
    # that cannot serve anything carry on regardless: they cost nothing and they
    # are the only way to notice it coming back.
    hold = active_hold(state, now)
    if hold:
        log(account, "Holding account {} until {} — {}. Not pinging, because a "
                     "ping now would start the window at the wrong time.".format(
                         account.display, fmt_time(hold["until"]),
                         hold.get("reason", "alignment")))
        schedule_anchor(account, hold["until"] + account.guard_sec)
        write_state(account, state)
        return

    log(account, "Starting ping run for account {}...".format(
        account.display))
    ensure_ping_config(account)

    if not os.path.exists(account.session_id_file) or \
       not os.path.exists(account.checkpoint_backup):
        log(account, "ERROR: No checkpoint for account {}. Run ./install.sh to "
                     "initialise.".format(account.name))
        sys.exit(1)

    if not checkpoint_is_for_this_cwd(account):
        log(account, "ERROR: account {}'s checkpoint was built for another "
                     "working directory and cannot be resumed from {}. This "
                     "happens after an upgrade that moved where pings run. "
                     "Run ./install.sh to rebuild it — one message per account."
                     .format(account.name, account.ping_cwd))
        sys.exit(1)

    with open(account.session_id_file) as f:
        checkpoint_id = f.read().strip()

    # Restore the frozen 'hi' state so --resume always sees the same 2-message
    # context, regardless of what the previous run left behind.
    restore_checkpoint(account, checkpoint_id)

    log(account, f"Resuming checkpoint {checkpoint_id[:8]}... with 'bye'")
    result = run_interactive(account, ["--resume", checkpoint_id], "bye",
                             checkpoint_id)
    if not result["completed"]:
        log(account, "WARNING: ping run did not confirm a completed turn.")

    # Clear a hold only once it has actually been served. A hold set for a
    # future boundary — by `realign --confirm`, say — has to survive every
    # ordinary ping between now and then, or the correction is discarded by the
    # next tick and nothing says so.
    spent = state.get("hold")
    if spent and time.time() >= spent.get("until", 0):
        state.pop("hold", None)
    state["last_run"] = time.time()

    # Work out the earliest moment the next ping could start a window — which
    # depends on both the 5-hour and the weekly limit — then decide whether that
    # moment needs a one-shot anchor. A successful ping reports both limits
    # exactly via the statusLine; a refused one says so in the refusal text.
    limits = read_statusline_limits(account)
    if limits:
        names = dict(_LIMIT_NAMES)
        problems = implausible_limits(limits, state.get("rate_limits"),
                                      time.time())
        for key in sorted(problems):
            log(account, "Ignoring this run's {} figure: {}. Keeping the "
                         "previous one.".format(names[key], problems[key]))
        believed = dict((k, v) for k, v in limits.items() if k not in problems)
        if believed:
            merged = dict(state.get("rate_limits") or {})
            merged.update(believed)
            state["rate_limits"] = merged
            state["limits_source"] = "statusline"
            # When, as well as where from. Without this a live reading taken
            # earlier leaves its own timestamp behind, and every later command
            # reports figures this ping had just refreshed as half an hour old.
            state["limits_read_at"] = time.time()
        limits = state.get("rate_limits") or {}

    boundary, horizon, label = next_window_start(
        limits, result["text"], result["limited"])
    if boundary:
        state["boundary"] = boundary
        state["boundary_label"] = label
        if not limits:
            state["limits_source"] = "refusal-text"
    else:
        log(account, "No reset time reported this run — leaving the schedule "
                     "as it is.")

    # Availability is an observation, never a stored belief. A ping that got
    # through proves the account is usable right now; a refusal reports when it
    # will be. Nothing here asks *which* limit refused: a weekly limit spent, a
    # lapsed subscription and a revoked login are the same state, and all three
    # recover the same way — by an ordinary ping succeeding again.
    if result["completed"] and not result["limited"]:
        state["available_at"] = time.time()          # proven by demonstration
        state["consecutive_failures"] = 0
        state["anchor_streak"] = 0    # back to normal; forget past corrections
    elif result["limited"]:
        if boundary:
            state["available_at"] = boundary
        else:
            # Refused, and nothing in the reply says when it comes back: no
            # statusLine figures, and no reset in the text that its own limit
            # could plausibly reach. The refusal is still an answer, and
            # recorded the way a refused live reading is -- a limit spent with
            # no reset time, which reads as "back no later than" its own length
            # rather than as an account worth recommending.
            spent = dict(state.get("rate_limits") or {})
            five = dict(spent.get("five_hour") or {})
            five["used_percentage"] = LIMIT_SPENT_PCT
            # A reset already in the past would be read as a window that has
            # since rolled over, and the refusal forgotten. Rolled forward on
            # the phase this account is already believed to be on, which keeps
            # both facts: spent now, and back at the boundary it was going to
            # reach anyway. Only where nothing is known at all does it fall
            # back to the limit's own length.
            rolled = next_expiry(state, time.time())
            if rolled == float("inf"):
                five.pop("resets_at", None)
            else:
                five["resets_at"] = rolled
                # A refusal is Claude answering, which is proof this account is
                # reachable and that the phase it is on is still real. Without
                # recording that, the only account whose news is refusals reads
                # as one nothing has been heard from, and drops out of the
                # spacing it is still entitled to a place in.
                state["available_at"] = rolled
            spent["five_hour"] = five
            state["rate_limits"] = spent
            log(account, "Refused with no usable reset time — recording the "
                         "5-hour limit as spent until something says "
                         "otherwise.")
        state["consecutive_failures"] = 0            # it answered; it just said no
    else:
        # No answer at all says nothing about the account — that is a local
        # problem until it keeps happening.
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1

    # Spacing is decided from what every account is *observed* to be doing, not
    # from a schedule agreed earlier — so nothing here can fall out of date, and
    # a user who ignored the setup advice simply gets a different plan.
    states[account.name] = state
    extra = apply_alignment(account, accounts, states, state, time.time(),
                            boundary)

    # The anchor has to land on the *held* moment, and the horizon that
    # sanity-checks it has to stretch by the same amount or it would reject its
    # own target as implausible.
    maybe_schedule_anchor(account,
                          boundary + extra if boundary else boundary,
                          horizon + extra if horizon else horizon,
                          label, state, result["limited"])
    write_state(account, state)

    log(account, "Ping run finished.\n")


def realign(accounts, confirm=False):
    """Show what correcting the spacing would cost, and apply it when asked."""
    now = time.time()
    states = {a.name: read_state(a) for a in accounts}
    lines, delays, total, settled = describe_alignment(
        accounts, states, now, suggest_realign=False, note_waiting=not confirm)
    for line in lines:
        print(line)

    if total < ALIGN_DEADBAND_SEC:
        return 0
    print()
    print("Correcting this costs {} in total with no window running — the "
          "accounts being held cannot start a new window until they are back "
          "in step.".format(fmt_delta(total)))
    if confirm and not settled:
        # The wait exists to stop the tool re-spacing on its own initiative
        # while accounts come and go. An explicit request is not that.
        print("The set of active accounts changed recently, so this would not "
              "have happened by itself — but you asked, so here it is.")
    if not confirm:
        print("Re-run with --confirm to apply it.")
        return 0

    for account in accounts:
        delay = delays.get(account.name, 0.0)
        if delay < ALIGN_DEADBAND_SEC:
            continue
        state = states[account.name]
        boundary = next_expiry(state, now)
        if boundary == float("inf"):
            continue
        state["hold"] = {"from": boundary, "until": boundary + delay,
                         "reason": "realigning, on your say-so"}
        write_state(account, state)
        schedule_anchor(account, boundary + delay + account.guard_sec)
        print("  account {}: next window will start {}".format(
            account.display, fmt_time(boundary + delay)))
    return 0


# ---------------------------------------------------------------------------
# Status (for humans)
# ---------------------------------------------------------------------------

def status(accounts):
    now = time.time()
    states = {a.name: read_state(a) for a in accounts}
    pings = pings_here()

    print("Claude Code Window Timing — status")
    print("=" * 34)
    print()

    # A machine that pings nothing has no readings of its own, and answering
    # "no window information yet — run a ping first" on a machine the README
    # says must never ping is worse than useless: the remedy is one it has been
    # told not to apply. `which` already answers from the published schedule;
    # the headline here should agree with it rather than contradict it.
    view = schedule_view(accounts)
    absent = set()
    if view:
        known, states, avail, _published_at = view
        # Every line below looks both dicts up by account name, and an account
        # added here since the file was copied is in neither.
        absent = set(a.name for a in accounts if a.name not in avail)
        for account in accounts:
            states.setdefault(account.name, {})
            avail.setdefault(account.name, UNPUBLISHED)
    else:
        known = accounts
        avail = availabilities(accounts, states, now)
    chosen, reason = choose_account(known, states, now, avail)
    print(headline(chosen, avail[chosen.name], len(known)))
    print("  {}".format(reason))

    # Only once switching has been set up. Until then this tool has no business
    # having an opinion about which account the user is signed in as, and an
    # install that only pings should read exactly as it always did.
    if switching_configured(accounts):
        mine = current_account(accounts)
        print()
        if mine is None:
            print("Your Claude Code  : signed in as an account this tool does "
                  "not know")
        elif mine.name == chosen.name:
            # "The one to spend" is an endorsement, and there is nothing to
            # endorse when the account it names cannot serve a request: the
            # headline above has just said so, and agreeing with it matters
            # more than saying something reassuring.
            print("Your Claude Code  : account {}{}".format(
                mine.display, " — the one to spend"
                if avail[chosen.name].tier == USABLE else ""))
        else:
            print("Your Claude Code  : account {}".format(mine.display))
            print("                    `{} switch` moves it to account "
                  "{}".format(COMMAND, chosen.display))

    for account in accounts:
        state = states.get(account.name, {})
        print()
        print("Account {}".format(account.display))
        # Naming the ping directory on a machine that does not ping points at
        # somewhere that does not exist; the store is where its login lives.
        print("  {:<14}: {}".format(
            "Config dir" if pings else "Parked login",
            account.config_dir if pings else switch_store(account).config_dir))

        installed = (os.path.exists(account.session_id_file)
                     and os.path.exists(account.checkpoint_backup))
        if pings:
            print("  Checkpoint    : {}".format(
                _read_text(account.session_id_file).strip() if installed
                else "MISSING — run ./install.sh"))

        if state.get("last_run"):
            print("  Last ping     : {} ({} ago)".format(
                fmt_time(state["last_run"]), fmt_delta(now - state["last_run"])))
        failures = state.get("consecutive_failures", 0)
        if failures:
            print("  Failed pings  : {} in a row".format(failures))

        limits = state.get("rate_limits", {})
        for key, name in _LIMIT_NAMES:
            window = limits.get(key) or {}
            label = "5-hour window" if key == "five_hour" else "Weekly limit"
            if not window.get("resets_at"):
                # A limit can be known to be spent without anything having
                # said when it comes back -- that is what a refusal carrying
                # no figures looks like. Printing nothing there left `status`
                # silent about the very limit the line below is waiting on.
                if window.get("used_percentage") is not None:
                    print("  {:<14}: {} used, no reset time reported".format(
                        label, fmt_pct(window.get("used_percentage"))))
                continue
            print("  {:<14}: {} used, resets {} (in {})".format(
                label,
                fmt_pct(window.get("used_percentage")),
                fmt_time(window["resets_at"]),
                fmt_delta(window["resets_at"] - now)))

        usable = avail[account.name]
        if account.name in absent:
            print("  Usable        : cannot tell — {}".format(usable.note))
        elif usable.tier == WAITING:
            print("  Usable again  : {}{} (in {}) — {}".format(
                "" if usable.exact else "no later than ",
                fmt_time(usable.until), fmt_delta(usable.until - now),
                usable.note))
        elif usable.tier == UNKNOWN:
            print("  Usable        : cannot tell — {}".format(usable.note))
        elif usable.tier != USABLE:
            print("  Usable        : no — {}".format(usable.note))

        # Everything below describes this machine's own pinging. On a machine
        # that does not ping, each line is either unknowable or actively
        # misleading — a "Next ping" for a timer that does not exist.
        if pings:
            boundary = state.get("boundary")
            if boundary:
                stale = "" if boundary > now else " — passed, awaiting next ping"
                print("  Next start-of-window opportunity: {} (in {}){}".format(
                    fmt_time(boundary), fmt_delta(boundary - now), stale))
                print("    set by the {}   [via {}]".format(
                    state.get("boundary_label", "?"),
                    state.get("limits_source", "?")))
            else:
                print("  Next start-of-window opportunity: not known yet — "
                      "run one ping first")

            hold = state.get("hold")
            if hold and hold.get("until", 0) > now:
                print("  Holding       : until {} — {}".format(
                    fmt_time(hold["until"]), hold.get("reason", "alignment")))

            pending = anchor_pending(account)
            print("  Anchor        : {}".format(
                "pending — " + pending if pending else "none scheduled"))

            result = _systemctl("list-timers", "--all", account.timer_unit)
            for line in (result.stdout or "").splitlines():
                if account.timer_unit in line:
                    print("  Next ping     : {}".format(
                        " ".join(line.split()[:4])))

    # Spacing is worked out from what the pings observe, so it belongs to the
    # machine doing them. On any other it is arithmetic over an empty state
    # that ends in advice to go and ping, which is the one thing that machine
    # must not do.
    if len(accounts) > 1 and pings:
        print()
        print("Spacing")
        for line in describe_alignment(accounts, states, now)[0]:
            print("  {}".format(line))

    # A second machine has nothing of its own to report and does not want the
    # "run ./install.sh" above — it is not meant to be pinging. Say where the
    # answer it actually came for lives.
    if schedule_view(accounts):
        print()
        print("Nothing has been pinged from this machine, but a schedule "
              "published by the")
        print("machine that does is here. `{} which` answers from "
              "it.".format(COMMAND))
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------
#
# Everything validate_accounts() checks, plus the parts that only make sense once
# the tool is actually deployed. For something strangers install, being able to
# say precisely what is wrong is not an extra — most of these failures are silent,
# and "it stopped helping" is all the user would otherwise see.

def switch_only_findings(accounts):
    """
    Everything worth saying about a machine that switches but does not ping.

    Its own function because two commands need it. `doctor` has always made
    this split; `check` did not, and validated ping directories that a
    switch-only machine is not supposed to have -- telling somebody to create
    and sign into the very thing the install had just told them they did not
    need.
    """
    findings = switch_findings(accounts)
    findings.extend(_stray_unit_findings(accounts))
    if installed_units():
        findings.append(Finding(
            "error",
            "This machine is configured not to ping, but its timers are "
            "still installed",
            "It is pinging anyway, which doubles what those accounts "
            "consume for no benefit. Re-run ./install.sh --no-pings to "
            "stop them, or ./install.sh --pings if this machine should be "
            "the one doing it."))
    if not parked_logins(accounts) and not switching_configured(accounts):
        findings.append(Finding(
            "warning",
            "This machine switches accounts but has no logins parked",
            "Run ./install.sh, or sign in as you need them: `{} switch` "
            "offers the one it needs, when it needs it.".format(COMMAND)))
    if not read_schedule():
        findings.append(Finding(
            "warning",
            "No schedule.json here, so nothing knows which account to spend",
            "Copy it from the machine running the pings; `{} which` reads "
            "it and needs nothing else.".format(COMMAND)))
    return findings


def doctor(accounts):
    # A machine that only switches has no ping directories, no checkpoints and
    # no timers. Reporting all three as broken would bury the one finding that
    # matters there -- the state of the parked logins -- under six errors
    # describing a machine this was never meant to be.
    pings = pings_here()
    if not pings:
        order = {"error": 0, "warning": 1}
        return report_findings(sorted(switch_only_findings(accounts),
                                      key=lambda f: order.get(f.level, 2)))

    findings = validate_accounts(accounts)
    now = time.time()
    # Whether anything here can see systemd at all. Every timer question below
    # answers "no" when the bus is out of reach, which is a different thing
    # entirely and has a different remedy.
    reachable = systemd_reachable()
    if not reachable:
        findings.append(Finding(
            "warning",
            "Cannot reach your systemd user manager, so nothing here can say "
            "whether the timers are running",
            "`systemctl --user` finds the bus through XDG_RUNTIME_DIR, which "
            "a login session sets and cron and `ssh host <command>` may not. "
            "Everything else below is still checked. To ask about the "
            "timers:\n"
            "       XDG_RUNTIME_DIR=/run/user/{} {} doctor".format(
                os.getuid(), COMMAND)))

    for account in accounts:
        state = read_state(account)

        if not os.path.exists(account.session_id_file):
            findings.append(Finding(
                "error", "Account {} has no checkpoint".format(account.name),
                "Run ./install.sh"))
        elif not checkpoint_is_for_this_cwd(account):
            # Invisible otherwise: everything looks installed, and every ping
            # fails for a reason that is only in the log.
            findings.append(Finding(
                "error",
                "Account {}'s checkpoint was built for a different working "
                "directory, so no ping can resume it".format(account.name),
                "Run ./install.sh to rebuild it — one message per account"))

        ok, detail = account_auth_ok(account)
        if not ok:
            findings.append(Finding(
                "error", "Account {}'s login is not usable: {}".format(
                    account.name, detail),
                "Sign in again with: {}".format(sign_in_command(account))))

        enabled = _systemctl("is-enabled", account.timer_unit)
        if not reachable:
            pass                      # asked and answered above, once
        elif (enabled.stdout or "").strip() != "enabled":
            findings.append(Finding(
                "error", "The timer for account {} is not enabled".format(
                    account.name),
                "systemctl --user enable --now {}".format(account.timer_unit)))

        # A timer can be enabled, active, and still never fire again — a drop-in
        # that resets systemd's monotonic timer list does exactly that. Nothing
        # announces it: the account simply stops being pinged.
        elif not _timer_will_fire_again(account):
            findings.append(Finding(
                "error",
                "The timer for account {} is running but has nothing scheduled "
                "— it will never fire again".format(account.name),
                "Re-run ./install.sh to rewrite the timer units."))

        # The failure mode that used to be invisible: a run that dies after the
        # ping has already succeeded still leaves the ping counted, but skips the
        # state write and the boundary anchor. Comparing the two counts is the
        # cheapest way to notice it.
        started, finished = _log_run_counts(account)
        if started and finished < started:
            findings.append(Finding(
                "warning",
                "Account {}: {} of {} recent runs did not finish".format(
                    account.name, started - finished, started),
                "Each unfinished run may have skipped a boundary anchor. See "
                + account.log_file))

        problem = state.get("live_problem")
        if problem:
            findings.append(Finding(
                "warning",
                "Account {}: the last live reading failed — {}".format(
                    account.name, problem),
                "`{} which` is falling back to the figures from the last ping, "
                "which can be up to {} minutes old. Recorded {}.".format(
                    COMMAND, INTERVAL_MIN,
                    fmt_time(state.get("live_problem_at") or 0))))

        last_run = state.get("last_run")
        if last_run and now - last_run > 3 * INTERVAL_MIN * 60:
            findings.append(Finding(
                "warning", "Account {} has not pinged since {}".format(
                    account.name, fmt_time(last_run)),
                "Check: systemctl --user status {}".format(account.timer_unit)))

        # A reset time far outside the window it belongs to means the machine
        # clock and the server disagree, and every timing decision here is built
        # on comparing the two.
        resets = ((state.get("rate_limits") or {}).get("five_hour")
                  or {}).get("resets_at")
        if resets and not (now - 86400 < resets < now + FIVE_HOUR_HORIZON):
            findings.append(Finding(
                "warning", "Account {}'s reported reset time is implausible "
                           "({})".format(account.name, fmt_time(resets)),
                "The machine clock may be wrong; every schedule here depends "
                "on it."))

    findings.extend(unit_cli_findings())
    findings.extend(linger_findings(pings))
    findings.extend(_launcher_findings())
    findings.extend(_user_account_findings(accounts))
    findings.extend(switch_findings(accounts))
    findings.extend(unit_target_findings())
    if reachable:
        findings.extend(_stray_unit_findings(accounts))

    if len(accounts) > 1:
        _, total, _, settled = alignment_plan(
            accounts, {a.name: read_state(a) for a in accounts}, now,
            record=False)
        if total >= ALIGN_DEADBAND_SEC and settled:
            findings.append(Finding(
                "warning",
                "The windows are {} away from evenly spaced".format(
                    fmt_delta(total)),
                "See what correcting it would cost: {} realign".format(
                    COMMAND)))

    order = {"error": 0, "warning": 1}
    return report_findings(sorted(findings, key=lambda f: order.get(f.level, 2)))


_NO_ELAPSE = ("", "infinity", "n/a", "0")


def _timer_will_fire_again(account):
    """
    Whether systemd still has a next elapse for this account's timer.

    Both properties have to be consulted. A monotonic timer — which is what the
    ping cadence is — reports NextElapseUSecMonotonic and leaves the realtime one
    empty; a calendar timer does the opposite. Reading only one of them calls a
    perfectly healthy timer broken, and a diagnostic that cries wolf is worse
    than no diagnostic at all.

    Unknown counts as fine: without systemd there is nothing to report.
    """
    for prop in ("NextElapseUSecMonotonic", "NextElapseUSecRealtime"):
        result = _systemctl("show", account.timer_unit,
                            "--property=" + prop, "--value")
        if result.returncode != 0:
            return True
        if (result.stdout or "").strip() not in _NO_ELAPSE:
            return True
    return False


def _stray_unit_findings(accounts):
    """
    Failed units that look like this tool but are not part of it.

    An earlier version installed under another name, or a hand-rolled attempt at
    the same idea, leaves a failed entry systemd remembers indefinitely — long
    after its unit file is gone. It is the first thing anyone diagnosing this
    will trip over, so say what it is rather than leave them to wonder.

    Clearing the failure is not always the end of it: a service fails because
    something started it, and if that something is a timer nobody disabled, it
    will fail again on the next tick and go on doing so for ever. So the hint
    covers both, rather than the half that is true on the day it is read.
    """
    ours = set()
    for account in accounts:
        ours.update((account.timer_unit, account.service_unit,
                     account.anchor_unit + ".timer",
                     account.anchor_unit + ".service"))
    result = _systemctl("list-units", "--failed", "--all", "--plain",
                        "--no-legend")
    findings = []
    for line in (result.stdout or "").splitlines():
        unit = line.split()[0] if line.split() else ""
        if not unit or unit in ours:
            continue
        if "claude" in unit and "window" in unit:
            findings.append(Finding(
                "warning",
                "{} is in a failed state but is not part of this "
                "install".format(unit),
                "Probably an earlier or hand-rolled version; nothing here "
                "installed it. Clear it with `systemctl --user reset-failed "
                "{}`. If it comes back, a timer is still starting it: "
                "`systemctl --user list-timers --all` names it, and "
                "`systemctl --user disable --now <that timer>` stops it "
                "firing.".format(unit)))
    return findings


def account_auth_ok(account):
    """
    Whether this account's login actually works, according to Claude Code.

    Costs nothing: `auth status --json` reads the credentials and reports,
    without contacting the API or touching a usage window. Returns
    (ok, description) — and "cannot tell" counts as ok, since inventing a fault
    is worse than missing one.
    """
    # No credentials at all is answerable without spawning anything, and the
    # answer is not in doubt.
    if not os.path.exists(os.path.join(account.config_dir, ".credentials.json")):
        return False, "not signed in"
    try:
        result = subprocess.run(
            [CLAUDE_PATH, "auth", "status", "--json"],
            env=build_claude_env(account), cwd=ensure_ping_cwd(account),
            timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True)
    except (OSError, ValueError, subprocess.SubprocessError):
        return True, "could not run the CLI"
    try:
        report = json.loads((result.stdout or "").strip())
    except ValueError:
        return True, "no readable answer"
    if not report.get("loggedIn"):
        return False, "not signed in"
    plan = (report.get("subscriptionType") or "").lower()
    if plan in ("free", "none", ""):
        return False, "no paid subscription ({})".format(plan or "unknown")
    return True, plan


def _user_account_findings(accounts):
    """
    Warn when the user's own Claude is signed in as an account nobody pings.

    This is the one way this design can deliver nothing while reporting perfect
    health: the tool no longer owns ~/.claude, so the account the user actually
    works with can drift away from the ones being kept warm — a third account, or
    a re-login they have forgotten — and every other check would still pass.

    Read-only. Their directory is not ours to change; it is only ours to notice.
    """
    identity = (_read_json(USER_CONFIG_JSON).get("oauthAccount") or {})
    theirs = identity.get("accountUuid")
    if not theirs:
        return []                       # never signed in here, or not a user of it

    pinged = {}
    for account in accounts:
        who = account_identity(account)
        if who["account_uuid"]:
            pinged[who["account_uuid"]] = who["email"] or account.name
    if not pinged or theirs in pinged:
        return []

    if parked_logins(accounts):
        fix = ("Switch to one of them with `{} switch`, or add the account you "
               "actually use to accounts.json and re-run ./install.sh.".format(
                   COMMAND))
    else:
        fix = ("Either sign in as one of {}, or add the account you actually "
               "use to accounts.json and re-run ./install.sh.".format(
                   ", ".join(sorted(pinged.values()))))
    return [Finding(
        "warning",
        "Your own Claude Code is signed in as {}, which is not one of the "
        "accounts being pinged".format(identity.get("emailAddress") or theirs[:8]),
        "You are getting no benefit from this tool at all. " + fix)]


def unit_cli_findings():
    """
    Whether the environment the timer runs with can find the Claude CLI.

    The blind spot this closes: every other check here runs from the user's own
    shell, where `claude` is on PATH because their startup files put it there.
    The timer has none of that, and a ping that cannot find the CLI writes one
    line to a log nobody reads, leaves the run counted as finished, and answers
    every other question exactly as a healthy install does -- `doctor` said
    "Everything checks out" while not one ping in an hour had got through.
    """
    body = _read_text(os.path.join(UNIT_DIR, "claude-window-timing@.service"))
    for line in body.splitlines():
        if not line.startswith("Environment=PATH="):
            continue
        directories = line.split("=", 2)[2].split(os.pathsep)
        for directory in directories:
            candidate = os.path.join(directory, "claude")
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return []
        return [Finding(
            "error",
            "The timer cannot find the Claude CLI, so every ping is failing",
            "Its PATH is {}, and there is no `claude` in any of it — an nvm or "
            "npm install lives somewhere only your shell knows about. Re-run "
            "./install.sh from a shell where `claude` works and it will record "
            "where.".format(line.split("=", 2)[2]))]
    return []


def linger_findings(pings):
    """
    Warn when the pings stop the moment the user logs out.

    A user timer lives in the user's own systemd manager, and without lingering
    that manager is torn down with their last session. So the tool whose whole
    promise is "a window is already running when you sit down" stops at the end
    of the working day and starts again when somebody logs in -- which is the
    shape of every window it was meant to prevent.

    Nothing here can fix it: `loginctl enable-linger` needs root, and a setup
    wizard that asks for sudo is a setup wizard people stop trusting. Saying it
    plainly, every time `doctor` runs, is the whole of what this can do. Setup
    says it once, at the end, where it is easy to miss -- which is exactly how
    the machine that prompted this check ended up without it.
    """
    if not pings:
        return []
    answer = (_run(["loginctl", "show-user", USER]).stdout or "")
    if "Linger=" not in answer:
        return []                     # no loginctl, or no such user: cannot tell
    if "Linger=yes" in answer:
        return []
    return [Finding(
        "error",
        "Lingering is off, so the pings stop when you log out of this machine",
        "A user timer belongs to your login session. Until this is on, the "
        "windows are only kept rolling while you are logged in:\n"
        "       sudo loginctl enable-linger {}".format(USER))]


def _launcher_findings():
    """
    Warn when `claude-window` cannot be typed.

    Silent otherwise, and worth saying at all because every instruction this
    tool prints -- in setup, in `which`, in `status`, in these findings --
    begins with the command. An install where it is not on PATH looks complete
    and answers "command not found" to every one of them.
    """
    state, path = launcher_link()
    if state == "reachable":
        return []
    if state == "foreign":
        return [Finding(
            "warning",
            "The `{}` on your PATH belongs to a different checkout".format(
                COMMAND),
            "It is {}; this one is {}. Whichever you run decides which "
            "checkout's accounts and state you are looking at.".format(
                path, os.path.join(BIN_DIR, COMMAND)))]
    return [Finding(
        "warning",
        "`{}` is not on your PATH, so every command here has to be typed "
        "as a path".format(COMMAND),
        "Run `{} install-command`, which offers to link it somewhere your "
        "shell already looks.".format(os.path.join(BIN_DIR, COMMAND)))]


def _log_run_counts(account):
    try:
        with open(account.log_file) as f:
            body = f.read()
    except (IOError, OSError):
        return 0, 0
    return (body.count("Starting ping run"),
            body.count("Ping run finished"))


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except (IOError, OSError):
        return ""


def which(accounts, states=None, avail=None, published_at=None, live=False):
    """
    Say which account to use, and why.

    Advice only: nothing here switches accounts. `states` and `avail` arrive
    filled in when the answer is coming from a schedule published by another
    machine rather than from this one's own state files — see `schedule_view`.

    `live` says whether a live reading was taken before this ran, which decides
    only one thing: whether to offer one.
    """
    now = time.time()
    states = {a.name: read_state(a) for a in accounts} if states is None \
        else states
    avail = availabilities(accounts, states, now) if avail is None else avail
    chosen, reason = choose_account(accounts, states, now, avail)

    print(headline(chosen, avail[chosen.name], len(accounts)))
    print("  {}".format(reason))
    # Only when there is actually a window to point at. Before the first ping
    # there is nothing to spend, and saying otherwise reads as a false promise.
    if (avail[chosen.name].tier == USABLE
            and next_expiry(states[chosen.name], now) != float("inf")):
        print("  That is the window to spend; how you use the account is up "
              "to you.")

    # The one line that turns advice into something to do — and only when it is
    # actually actionable, because a recommendation to run a command that is not
    # set up is worse than no recommendation at all.
    if switching_configured(accounts):
        mine = current_account(accounts)
        if mine is None or mine.name != chosen.name:
            print("  Point your own Claude Code at it:  {} switch {}".format(
                COMMAND, chosen.name))
        else:
            print("  Your own Claude Code is already signed in as it.")

    if len(accounts) > 1:
        print()
        width = max(len(a.display) for a in accounts)
        for account in sorted(accounts, key=lambda a: rank_account(
                a, states[a.name], now, avail[a.name])):
            print("  {:<{}}  {}{}".format(
                account.display, width,
                describe_availability(states[account.name], avail[account.name], now),
                ("   <- use this" if avail[chosen.name].tier == USABLE
                 else "   <- first back") if account is chosen else ""))

    # Say how old the figures are, always. They come from the last ping unless a
    # live reading was taken, and the likeliest thing to have moved them since is
    # the reader's own work -- which is precisely what they are about to act on.
    # A stale number that looks current is worse than no number.
    #
    # Reported by the *oldest* of them, and by the weaker of the two sources: a
    # set of readings is only as current as its stalest member, and the answer
    # rests on all of them at once, since each account is kept or skipped on
    # the strength of its own. Summarising by the freshest would describe the
    # one account nobody was worried about.
    stamps = [((states[a.name].get("limits_read_at")
                or states[a.name].get("last_run") or 0), a.name)
              for a in accounts]
    stamps = [stamp for stamp in stamps if stamp[0]]
    if stamps and not published_at:
        read_at = min(stamps)[0]
        source = ("a live reading"
                  if all(states[name].get("limits_source") == "live"
                         for _, name in stamps) else "the last ping")
        # Offering a reading is only useful to someone who did not just take
        # one: `--no-live` is the way to arrive here with figures worth
        # refreshing, and pointing anybody else at the command they have this
        # second run would be a loop rather than advice.
        offer = ("" if live or now - read_at < 120 else
                 "  `{} which` reads them from Claude now.".format(COMMAND))
        print()
        print("  Figures from {}, {} ago.{}".format(
            source, fmt_delta(now - read_at), offer))

    freshest = max([states[a.name].get("last_run") or 0 for a in accounts])
    if published_at:
        # Where the answer came from matters here in a way it does not on the
        # pinging machine: nothing on this one would notice the file going
        # stale, so the age is the reader's only guard against acting on it.
        print()
        print("  From schedule.json, published {} ago by the machine running "
              "the pings.".format(fmt_delta(now - published_at)))
        print("  Window phases hold whatever the file's age; which accounts are "
              "usable is as old as the file.")
    elif freshest and now - freshest > STALE_AFTER_SEC:
        print()
        print("  These readings are {} old. If that is not expected, check the "
              "timers: {} doctor".format(fmt_delta(now - freshest), COMMAND))
    return 0


BIN_DIR = os.path.join(SCRIPT_DIR, "bin")

# The interpreter is resolved at generation time rather than hardcoded:
# /usr/bin/python3 does not exist on NixOS, on Homebrew Python installs, or in
# many containers, and the tool would install cleanly and then fail to run.
_ENTRY_POINT = '''#!/usr/bin/env bash
# Generated by claude-window-timing. Put this directory on your PATH.
# `systemctl --user` finds its bus through this, and a login session is not the
# only place this command gets run from -- cron and `ssh host <command>` have
# no session at all. Defaulted rather than overridden, so a session that set it
# somewhere else keeps its own.
export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"
exec {python} {script} "$@"
'''


def write_entry_point():
    """
    Write bin/claude-window, so the tool is a command rather than a path.

    The real file lives here, beside the checkout whose absolute path it
    embeds, and goes away with `uninstall --purge`. Whether a shell can find
    it is a separate question, answered by `launcher_link`.
    """
    if not os.path.isdir(BIN_DIR):
        os.makedirs(BIN_DIR, 0o755)
    path = os.path.join(BIN_DIR, COMMAND)
    with open(path, "w") as f:
        f.write(_ENTRY_POINT.format(
            python=shlex.quote(sys.executable or "/usr/bin/python3"),
            script=shlex.quote(os.path.abspath(__file__))))
    os.chmod(path, 0o755)
    return path


def link_dirs():
    """
    Where a launcher can be reached from without editing anybody's dotfiles.

    Both are conventional user bin directories -- ~/.local/bin is the one
    Ubuntu's own ~/.profile puts on PATH when it exists -- and a symlink in one
    of them is a file this tool created and can take back.

    Resolved against HOME when asked rather than when the module loads, so that
    a test which redirects HOME redirects this too. As a constant it named the
    developer's own ~/.local/bin, and a suite whose first promise is that it
    writes nothing outside a temporary directory would have linked into it.
    """
    return (os.path.join(HOME, ".local", "bin"), os.path.join(HOME, "bin"))


def _path_dirs():
    """The directories on PATH, absolute, in order."""
    return [os.path.abspath(os.path.expanduser(entry))
            for entry in (os.environ.get("PATH") or "").split(os.pathsep)
            if entry]


def launcher_link():
    """
    Whether `claude-window` can be typed, and what would make it so.

    Returns (state, path):

      "reachable"  a shell finds this checkout's launcher; path is where.
      "foreign"    a shell finds a *different* one; path is that one. Another
                   checkout owns the name, and quietly replacing it would take
                   its timers' command away from it.
      "linkable"   nothing on PATH yet, but a conventional bin directory is on
                   PATH; path is the link to create.
      "manual"     nothing on PATH and nowhere conventional to link from; path
                   is the directory the user has to add themselves.

    Printing an `export PATH=...` line and calling it an installation is what
    this replaces: the line dies with the shell it was printed in, and every
    instruction this tool gives afterwards begins with `claude-window`.
    """
    ours = os.path.join(os.path.abspath(BIN_DIR), COMMAND)
    on_path = _path_dirs()
    for directory in on_path:
        candidate = os.path.join(directory, COMMAND)
        if not os.path.exists(candidate):
            continue
        if os.path.realpath(candidate) == os.path.realpath(ours):
            return "reachable", candidate
        return "foreign", candidate
    for directory in link_dirs():
        if os.path.abspath(directory) in on_path:
            return "linkable", os.path.join(directory, COMMAND)
    return "manual", BIN_DIR


def link_launcher(path):
    """
    Point `path` at this checkout's launcher. Returns True if it now does.

    A symlink rather than a copy: the launcher embeds the absolute path of
    this checkout and is rewritten whenever that changes, and a copy would go
    on naming wherever the checkout used to be.

    Anything already there that is not a link of ours is left alone. Nothing
    reaches this function in that state -- a `claude-window` on PATH is
    reported as somebody else's rather than linked over -- but this is the
    line where a mistake would take away another program's command.
    """
    target = os.path.join(os.path.abspath(BIN_DIR), COMMAND)
    directory = os.path.dirname(path)
    try:
        if os.path.lexists(path):
            if not os.path.islink(path):
                sys.stderr.write(
                    "{} already exists and is not a link; leaving it "
                    "alone.\n".format(path))
                return False
            os.remove(path)
        elif not os.path.isdir(directory):
            os.makedirs(directory, 0o755)
        os.symlink(target, path)
    except OSError as e:
        sys.stderr.write("Could not link {} to {}: {}\n".format(
            path, target, e))
        return False
    return True


# The line added to a shell's startup file, and the comment that makes it
# findable again. Marked because anything written into somebody else's dotfile
# has to be removable without them reading a diff to work out which line was
# ours -- `uninstall --purge` takes it back out by this marker.
PATH_MARKER = "# Added by claude-window-timing"


def shell_rc_file():
    """
    The startup file this user's shell reads, or "" if it cannot be guessed.

    $SHELL is the login shell, which is what a *new* terminal will start --
    the thing being fixed here. bash reads ~/.bashrc for interactive shells
    and, on the distributions that matter here, sources it from ~/.profile for
    login shells too, so it is the one file that covers both. fish is left out
    deliberately: its syntax is not this line, and guessing wrong writes a
    startup file that errors on every new shell.
    """
    shell = os.path.basename(os.environ.get("SHELL") or "")
    if shell == "bash":
        return os.path.join(HOME, ".bashrc")
    if shell == "zsh":
        return os.path.join(HOME, ".zshrc")
    if shell in ("sh", "dash", "ksh"):
        return os.path.join(HOME, ".profile")
    return ""


def path_line_present(path):
    """Whether `path` already puts this checkout's bin/ on PATH."""
    body = _read_text(path)
    return bool(body) and os.path.abspath(BIN_DIR) in body


def add_path_line(path):
    """
    Append the PATH line to a shell startup file. Returns True if it is there.

    Appended, never rewritten: this is the user's file, it may be under
    version control, and the only safe edit to make to somebody else's
    configuration is one at the end that says who made it.
    """
    if path_line_present(path):
        return True
    try:
        with open(path, "a") as f:
            f.write('\n{} -- so `{}` can be typed anywhere.\n'
                    'export PATH="{}:$PATH"\n'.format(
                        PATH_MARKER, COMMAND, os.path.abspath(BIN_DIR)))
    except (IOError, OSError) as e:
        sys.stderr.write("Could not write {}: {}\n".format(path, e))
        return False
    return True


def remove_path_line(path):
    """Take our marked line back out of a startup file. True if anything went."""
    body = _read_text(path)
    if not body or PATH_MARKER not in body:
        return False
    kept, dropped, lines = [], False, body.splitlines(True)
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith(PATH_MARKER):
            # The marker and the export beneath it, and the blank line the two
            # were written after -- exactly what add_path_line wrote.
            index += 2
            dropped = True
            if kept and not kept[-1].strip():
                kept.pop()
            continue
        kept.append(line)
        index += 1
    if not dropped:
        return False
    try:
        with open(path, "w") as f:
            f.writelines(kept)
    except (IOError, OSError):
        return False
    return True


def _rehash_hint():
    """What this shell needs in order to see a command that just appeared."""
    # bash searches PATH again for a name it has never found, so a link that
    # lands in a directory already on PATH works in the shell that made it.
    # zsh keeps a table of what is in each PATH directory and needs telling.
    return ("  This shell needs `rehash` before it sees it."
            if os.path.basename(os.environ.get("SHELL") or "") == "zsh" else "")


def offer_launcher_link():
    """
    Make `claude-window` typeable -- in this shell, and in every later one.

    Asked rather than assumed: a symlink in ~/.local/bin and a line in a
    startup file are the only things this tool puts outside its own directory
    other than systemd units, and both are named out loud and taken back by
    `uninstall --purge`.

    A child process cannot change its parent's environment, so "works in this
    shell too" has exactly one honest implementation: put the launcher in a
    directory the shell is *already* searching. Where there is no such
    directory, the startup file fixes every later shell and the one line
    printed at the end fixes this one.
    """
    state, path = launcher_link()
    if state == "reachable":
        print("`{}` is on your PATH ({}).".format(COMMAND, path))
        return True

    if state == "foreign":
        print("Another `{}` is already on your PATH:".format(COMMAND))
        print("  {}".format(path))
        print("It belongs to a different checkout, so this one is left alone.")
        print("Run this checkout's copy as:  {}".format(
            os.path.join(BIN_DIR, COMMAND)))
        return False

    if state == "linkable":
        directory = os.path.dirname(path)
        print("`{}` is not on your PATH yet, but {} is.".format(
            COMMAND, directory))
        print("One link there makes it work in this shell as well as in "
              "every new one.")
        if _ask_yes("Link it into {}?".format(directory)):
            if link_launcher(path):
                print("  Linked {} -> {}".format(
                    path, os.path.join(BIN_DIR, COMMAND)))
                hint = _rehash_hint()
                if hint:
                    print(hint)
                return True

    elif state == "manual":
        rc = shell_rc_file()
        if rc and not path_line_present(rc):
            print("`{}` is not on your PATH, and nothing on your PATH is a "
                  "place this".format(COMMAND))
            print("tool should be putting files. One line in {} fixes every "
                  "new shell.".format(_tilde(rc)))
            if _ask_yes("Add it to {}?".format(_tilde(rc))) \
                    and add_path_line(rc):
                print("  Added to {}, marked so `uninstall --purge` can take "
                      "it back out.".format(_tilde(rc)))
                print("  This shell has already read that file, so for this "
                      "one only:")
                print('    export PATH="{}:$PATH"'.format(BIN_DIR))
                return True
        elif rc and path_line_present(rc):
            # The line is there and PATH still lacks it: this shell started
            # before it was added. Nothing to write, only something to run.
            print("{} already puts `{}` on the PATH of new shells.".format(
                _tilde(rc), COMMAND))
            print("This shell started before that, so for this one only:")
            print('  export PATH="{}:$PATH"'.format(BIN_DIR))
            return True

    print("Add it to your PATH to type `{}` anywhere:".format(COMMAND))
    print('  export PATH="{}:$PATH"'.format(BIN_DIR))
    print("  (in ~/.bashrc, ~/.zshrc, or wherever your shell reads it)")
    return False


def report_findings(findings):
    """Print validation findings for a human. Returns an exit code."""
    if not findings:
        print("Everything checks out.")
        return 0
    for finding in findings:
        print("{}: {}".format(finding.level.upper(), finding.message))
        if finding.hint:
            print("  -> {}".format(finding.hint))
    errors = sum(1 for f in findings if f.level == "error")
    print()
    print("{} error(s), {} warning(s).".format(errors, len(findings) - errors))
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# Switching your own Claude Code between accounts
# ---------------------------------------------------------------------------
#
# `which` says which account holds the window worth spending. This points your
# own Claude Code at it — the one part of this tool that writes to ~/.claude. It
# writes only when you run it, only to two files, and it copies both somewhere
# safe first.
#
# Three measured facts about Claude Code shape the design, and each of them rules
# out something simpler:
#
#   * Credential files are replaced by rename, never edited in place. A symlink
#     at ~/.claude/.credentials.json is therefore destroyed by the first write,
#     leaving two files that look like one and silently disagree. Nothing here
#     links; it copies.
#
#   * Refresh tokens rotate. Two holders of one login diverge the moment either
#     refreshes, and the stale one is signed out about eight hours later, from a
#     cause nobody would connect to the switch. So a login is never in two places
#     at once: each account's store is a parking place, the copy in ~/.claude is
#     the only live one. The outgoing login is read into memory, the incoming
#     one installed, and only then is the outgoing one written to its store, so
#     there is no instant at which one grant sits in two directories -- not even
#     if the machine dies between the two. That is why this is a move and not a
#     copy, and it is the single most important thing in this section.
#
#   * The bearer token decides which account is billed; the oauthAccount block in
#     ~/.claude.json decides which account Claude Code tells you that you are.
#     Nothing reconciles them, and a session run with the two disagreeing
#     rewrites the per-account caches in that file under the *token's* account —
#     mixing one account's organisation and extra-usage state into another's. So
#     the two move together here, and those caches are dropped rather than
#     carried: Claude Code refetches each on demand, so a dropped key repairs
#     itself within one session and a stale one never does.
#
# What it will not do: pick for you, run in the background, or know anything
# about your sessions. It is one dial for one machine, pulled by hand.

SWITCH_ROOT = os.path.join(HOME, ".claude-switch")

# How many previous credential backups to keep. Each is two small files; the
# point of keeping several is that the switch you need to undo is not always the
# most recent one.
SWITCH_BACKUPS_KEPT = 10

# Keys in ~/.claude.json that Claude Code fills in from the server for whichever
# account is signed in. Observed being rewritten under the token's account during
# a single four-second session, `cachedExtraUsageDisabledReason` among them —
# which is why carrying one across a switch attributes one account's billing
# state to another. Dropping them costs nothing.
ACCOUNT_SCOPED_KEYS = (
    "additionalModelCostsCache",
    "cachedExperimentData",
    "cachedExperimentFeatures",
    "cachedExtraUsageDisabledReason",
    "cachedGrowthBookFeatures",
    "cachedGrowthBookFeaturesAt",
    "clientDataCacheSlots",
    "fableOverageConsentV2",
    "hasAvailableSubscription",
    "modelAccessCache",
    "orgModelDefaultCache",
    "overageCreditGrantCache",
    "passesEligibilityCache",
    "penguinModeOrgEnabled",
    "subscriptionNoticeCount",
)

# A directory holding one Claude Code login, in the shape account_identity()
# already reads: somewhere to find .credentials.json, and a .claude.json holding
# the oauthAccount block. Accounts are that shape already; this gives it to the
# two directories that are not accounts — a switch store, and the user's own,
# where the config file sits *beside* the directory rather than inside it.
Login = collections.namedtuple("Login", "name config_dir config_json")


def switch_store(account):
    """Where this account's login is parked: ~/.claude-switch/<name>."""
    directory = os.path.join(SWITCH_ROOT, account.name)
    return Login(account.name, directory,
                 os.path.join(directory, ".claude.json"))


def user_login():
    """The user's own Claude Code: ~/.claude, with ~/.claude.json beside it."""
    return Login("your own Claude Code", USER_CONFIG_DIR, USER_CONFIG_JSON)


def credentials_path(login):
    return os.path.join(login.config_dir, ".credentials.json")


def readable_json(path):
    """
    True when `path` is absent, or present and parseable.

    `_read_json` deliberately answers "missing" and "unreadable" the same way,
    which is right for reading and dangerous for writing: a config that failed
    to parse comes back as {}, and merging into {} and writing it out replaces
    the file with whatever few keys were merged. For ~/.claude.json that is the
    user's projects, trust decisions and settings, gone silently. Anything that
    rewrites a file it first read has to ask this instead.
    """
    if not os.path.exists(path):
        return True
    try:
        with open(path) as f:
            json.load(f)
        return True
    except (IOError, OSError, ValueError):
        return False


def login_fingerprint(login):
    """
    A stable, non-secret identifier for the login stored here, or "".

    It hashes the refresh token, which names the *grant* rather than the account.
    That is exactly the distinction worth drawing: two directories holding the
    same grant are one login copied twice, and one of them is going to be signed
    out. Two separate logins to one account have different grants and coexist
    indefinitely.
    """
    creds = (_read_json(credentials_path(login)).get("claudeAiOauth") or {})
    token = creds.get("refreshToken") or ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


# What a full claude.ai sign-in grants. A credential can be narrowed to a
# subset on refresh -- and never widened back, which the token endpoint refuses
# outright -- so a parked login carrying less than this is one somebody has to
# replace rather than repair.
FULL_LOGIN_SCOPES = ("user:file_upload", "user:inference", "user:mcp_servers",
                     "user:profile", "user:sessions:claude_code")


def login_scopes(login):
    """What this stored login is allowed to do, as recorded beside it."""
    creds = (_read_json(credentials_path(login)).get("claudeAiOauth") or {})
    scopes = creds.get("scopes")
    return tuple(sorted(scopes)) if isinstance(scopes, list) else ()


def parked_logins(accounts):
    """{account name: Login} for every account with a login parked."""
    return {a.name: switch_store(a) for a in accounts
            if os.path.exists(credentials_path(switch_store(a)))}


def switching_configured(accounts):
    """
    Whether the user has set any of this up.

    Nothing about switching is printed until they have, so an install that only
    pings looks exactly as it did before this existed.
    """
    return any(os.path.isdir(switch_store(a).config_dir) for a in accounts)


def current_account(accounts):
    """
    Which configured account the user's own Claude Code is signed in as, or None.

    Matched on the account UUID rather than on any file, because that is what
    survives a re-login: the identity is the account, not the directory it came
    from. Both the ping directory and the parked login are consulted, so this
    answers correctly before anything has ever been parked.
    """
    mine = account_identity(user_login())["account_uuid"]
    if not mine:
        return None
    for account in accounts:
        for login in (account, switch_store(account)):
            if account_identity(login)["account_uuid"] == mine:
                return account
    # A machine that pings nothing has no ping directories to compare against,
    # and nothing parked until the first switch — so on a fresh secondary
    # machine the two loops above can see no identities at all. The pinging
    # machine already publishes each account's UUID in schedule.json, which is
    # the file the README has people copy across, so use it rather than telling
    # somebody their own account is unrecognised.
    for entry in (read_schedule() or {}).get("accounts", []):
        if not isinstance(entry, dict) or entry.get("account_uuid") != mine:
            continue
        for account in accounts:
            if account.name == str(entry.get("name")):
                return account
    return None


def running_claude_sessions():
    """
    PIDs of this user's running Claude Code processes, pings excluded.

    Worth knowing before a switch rather than after: credentials are re-read per
    request, so a session that is running when you switch moves to the new
    account on its very next turn — while still displaying the old one, because
    that is read once at startup. Pings are excluded because they run constantly
    and are the one kind of session a switch is not about to surprise.
    """
    try:
        entries = os.listdir("/proc")
    except (IOError, OSError):
        return []                        # not Linux, or no /proc: say nothing
    mine, found = os.getuid(), []
    for entry in entries:
        if not entry.isdigit():
            continue
        base = os.path.join("/proc", entry)
        try:
            if os.stat(base).st_uid != mine:
                continue
            exe = os.path.realpath(os.path.join(base, "exe"))
            # Two shapes in the wild: the editor extension ships a binary called
            # `claude`, the native install a versioned one under .../versions/.
            if os.path.basename(exe) != "claude" and "/claude/versions/" not in exe:
                continue
            with open(os.path.join(base, "environ"), "rb") as f:
                if PING_MARKER_ENV.encode("utf-8") in f.read():
                    continue
        except (IOError, OSError):
            continue                     # exited while we looked, or not ours
        found.append(int(entry))
    return sorted(found)


_ACCOUNT_OVERRIDE_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                          "CLAUDE_CODE_OAUTH_TOKEN")


def account_overrides():
    """
    Settings that would make a switch pointless, as (where, variable) pairs.

    All three take precedence over the saved login, so with one of them set the
    switch would rewrite a file nothing reads. Two are worse than ineffective:
    ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN move requests on to pay-as-you-go
    billing, which is the opposite of what someone reaching for a subscription
    switcher wants. settings.json is checked as well as the environment because
    its `env` block overwrites the environment rather than deferring to it.
    """
    found = []
    for var in _ACCOUNT_OVERRIDE_VARS:
        if os.environ.get(var):
            found.append(("the environment", var))
    settings = os.path.join(USER_CONFIG_DIR, "settings.json")
    env = (_read_json(settings).get("env") or {})
    for var in _ACCOUNT_OVERRIDE_VARS:
        if env.get(var):
            found.append((settings, var))
    return found


def _write_atomically(path, body, mode=0o600):
    """
    Write via a temp file and a rename, the way Claude Code writes these files.

    Anything less leaves a window in which a crash puts half a credential in
    ~/.claude, which reads to Claude Code as a broken login and costs a browser
    sign-in to repair.
    """
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    tmp = _temp_name(path)
    # Created at its final mode rather than widened-then-narrowed: opening with
    # the default and calling chmod afterwards leaves a real window in which a
    # credential sits on disk at 0644, and these directories are shared.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            # rename is atomic, but only about which name points at which
            # inode. Without this the rename can be durable while the bytes
            # behind it are not, and a power cut leaves an empty credential
            # file -- which reads to Claude Code as a broken login.
            os.fsync(f.fileno())
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    _fsync_directory(directory)


def secure_dir(path, mode=0o700):
    """
    Create `path` if absent, and make sure it is no wider than `mode`.

    `os.makedirs(p, 0o700)` applies that mode to the **leaf only**; every
    intermediate it creates gets `0o777 & ~umask`, which is 0775 on an ordinary
    machine. That is how a directory holding parked logins ended up
    group-writable while the credential inside it was correctly 0600 -- and a
    directory you can write is a file you can replace, whatever mode the file
    has. Called for the root and the child separately, because there is no
    version of makedirs that gets this right.
    """
    if not os.path.isdir(path):
        os.makedirs(path, mode)
    try:
        current = stat.S_IMODE(os.stat(path).st_mode)
        if current & ~mode:
            os.chmod(path, current & mode)
    except OSError:
        pass
    return path


def _fsync_directory(path):
    """
    Make a rename durable, not just atomic.

    Renaming into place survives a crash only once the directory entry itself
    has reached the disk. Best effort: a filesystem that refuses the open is
    not a reason to fail a switch that has otherwise succeeded.
    """
    try:
        fd = os.open(path or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _existing_mode(path, fallback=0o600):
    try:
        return stat.S_IMODE(os.stat(path).st_mode)
    except (IOError, OSError):
        return fallback


def backup_user_login():
    """
    Copy ~/.claude/.credentials.json and ~/.claude.json somewhere safe.

    Taken before every switch. These two files are the difference between a
    signed-in Claude Code and a browser login, and this is the only command here
    that rewrites them.
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    secure_dir(SWITCH_ROOT)
    secure_dir(os.path.join(SWITCH_ROOT, ".backups"))
    directory = secure_dir(os.path.join(SWITCH_ROOT, ".backups", stamp))
    saved = []
    for source, name in ((credentials_path(user_login()), "credentials.json"),
                         (USER_CONFIG_JSON, "claude.json")):
        if os.path.exists(source):
            target = os.path.join(directory, name)
            shutil.copyfile(source, target)
            os.chmod(target, 0o600)
            saved.append(target)
    if not saved:
        os.rmdir(directory)              # nothing to say sorry about later
        return None
    _prune_backups()
    return directory


def orphan_login(taken):
    """
    Store a login that belongs to no configured account, permanently.

    The alternative was the rolling backup, and the switch said so out loud --
    "it is in the backup below, and nowhere else" -- while `_prune_backups`
    deleted it ten switches later. A copy described as the only one must not sit
    in a directory whose whole job is to throw things away, so this one is never
    pruned. It costs a few hundred bytes and saves a browser sign-in for
    somebody who had signed in by hand.
    """
    credential, identity = taken
    if not credential:
        return None
    secure_dir(SWITCH_ROOT)
    secure_dir(os.path.join(SWITCH_ROOT, ".orphaned"))
    directory = secure_dir(os.path.join(
        SWITCH_ROOT, ".orphaned", datetime.now().strftime("%Y%m%d-%H%M%S")))
    _write_atomically(os.path.join(directory, ".credentials.json"), credential)
    if identity:
        _write_atomically(os.path.join(directory, ".claude.json"),
                          json.dumps({"oauthAccount": identity}, indent=2,
                                     sort_keys=True))
    return directory


def _prune_backups():
    """
    Keep the most recent SWITCH_BACKUPS_KEPT, oldest first out.

    Only ever touches .backups. A login that could not be parked is written to
    .orphaned instead, precisely so that it is out of this function's reach.
    """
    root = os.path.join(SWITCH_ROOT, ".backups")
    try:
        stamps = sorted(d for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d)))
    except (IOError, OSError):
        return
    for stamp in stamps[:max(0, len(stamps) - SWITCH_BACKUPS_KEPT)]:
        shutil.rmtree(os.path.join(root, stamp), ignore_errors=True)


def acquire_switch_lock():
    """
    Hold an exclusive lock for the length of a switch, or return None.

    Two switches running at once interleave two read-modify-write pairs over
    the same two files, and the loser does not merely end up stale: its login
    is gone, because each run removes the store credential it installed and
    each backup captured the same pre-state. No ordering survives that, so
    they are serialised instead of being made clever.

    The lock lives beside the stores rather than in /tmp, so it shares their
    lifetime and their permissions.
    """
    secure_dir(SWITCH_ROOT)
    fd = os.open(os.path.join(SWITCH_ROOT, ".lock"),
                 os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        os.close(fd)
        return None
    return fd


def take_login():
    """
    Read the login now in ~/.claude, to be parked once it has been replaced.

    Read rather than copied, because *when* it reaches the store decides
    whether the invariant can be broken at all. Writing it to the store first
    leaves a moment — between that write and the install — when one grant sits
    in two directories, and a crash inside that moment leaves it there: two
    refreshers, and one of them signed out about eight hours later with nothing
    on the machine recording why. Holding it in memory until the install has
    landed removes the moment; the backup taken beforehand covers a crash in
    what is left.
    """
    path = credentials_path(user_login())
    credential = None
    if os.path.exists(path):
        with open(path) as f:
            credential = f.read()
    return credential, (_read_json(USER_CONFIG_JSON).get("oauthAccount") or {})


def park_login(account, taken):
    """
    Write a login previously read by take_login() into `account`'s store.

    Called only once ~/.claude has been overwritten, so the store is left
    holding the only copy — which is the whole invariant, and the reason token
    rotation never signs anybody out here.
    """
    credential, identity = taken
    store = switch_store(account)
    secure_dir(SWITCH_ROOT)
    secure_dir(store.config_dir)
    if credential is not None:
        _write_atomically(credentials_path(store), credential)
    if identity:
        config = _read_json(store.config_json)
        config["oauthAccount"] = identity
        _write_atomically(store.config_json,
                          json.dumps(config, indent=2, sort_keys=True),
                          _existing_mode(store.config_json))
    return store


def install_login(store):
    """
    Point ~/.claude at the login parked in `store` — token and identity together.

    The credential is **taken out** of the store, not copied from it. Leaving a
    copy behind would put two holders on one grant, which is the exact thing
    this design exists to prevent: whichever one refreshed first would strip the
    other of a working refresh token, and a switch back weeks later would install
    a credential that fails and takes the user's login with it. The store keeps
    its .claude.json, so the directory still knows whose login it parks; the
    token itself lives in exactly one place, and after this that place is
    ~/.claude.

    Removal happens only once the install has landed, so a failure part-way
    leaves the login where it was rather than nowhere.

    Returns the account-scoped keys that were dropped. They are dropped rather
    than replaced because Claude Code refills each from the server on demand: a
    key removed here is repaired within one session, while a key carried over
    from the other account is never repaired at all.
    """
    # Everything that can refuse happens before anything is written. The first
    # version of this guard sat one statement further down, after the credential
    # had already been swapped -- so reaching it caused exactly the damage it
    # exists to prevent, and left the user on a token whose identity had not
    # been updated to match.
    if not readable_json(USER_CONFIG_JSON):
        raise ConfigError(
            "{} cannot be parsed; refusing to overwrite it".format(
                USER_CONFIG_JSON))
    with open(credentials_path(store)) as f:
        incoming = f.read()

    config = _read_json(USER_CONFIG_JSON)
    identity = (_read_json(store.config_json).get("oauthAccount") or {})
    if identity:
        config["oauthAccount"] = identity
    dropped = []
    for key in ACCOUNT_SCOPED_KEYS:
        if key in config:
            del config[key]
            dropped.append(key)
    # Serialised first, so the only work left between the two writes is the
    # writes themselves. A crash between them leaves the credential swapped and
    # the identity stale, which `doctor` names and a re-run repairs; anything
    # slower in the gap widens the window for a live Claude Code session to
    # rewrite the file underneath us.
    body = json.dumps(config, indent=2, sort_keys=True)
    mode = _existing_mode(USER_CONFIG_JSON)

    _write_atomically(credentials_path(user_login()), incoming)
    _write_atomically(USER_CONFIG_JSON, body, mode)

    try:
        os.remove(credentials_path(store))
    except OSError:
        pass
    return dropped


def prepare_store(store):
    """
    Give a store directory the little `.claude.json` a sign-in needs.

    The same three keys a ping directory gets, for the same reason: a fresh
    directory otherwise opens on onboarding and a trust prompt, and somebody
    part-way through a switch should meet `/login` and nothing else. Merged
    rather than overwritten, so re-running cannot discard an identity already
    there.
    """
    secure_dir(SWITCH_ROOT)
    secure_dir(store.config_dir)
    config = _read_json(store.config_json)
    for key, value in ping_config(store.config_dir).items():
        if key == "projects":
            entry = config.setdefault("projects", {}).setdefault(
                store.config_dir, {})
            entry["hasTrustDialogAccepted"] = True
        else:
            config.setdefault(key, value)
    _write_atomically(store.config_json,
                      json.dumps(config, indent=2, sort_keys=True),
                      _existing_mode(store.config_json))


def offer_sign_in(account, store):
    """
    Offer to run the one browser sign-in this switch is missing. True if done.

    Printing a command and stopping is the wrong answer here: this is exactly
    where somebody is when they discover they need it, and the alternative is
    that they go and copy a credential from somewhere, which is the one mistake
    that costs a login. Declining is free — the blocker below prints the
    command anyway.
    """
    print("No login is parked for account {}.".format(account.display))
    print()
    print("Switching to it needs one browser sign-in, once on this machine.")
    print("Give it its own sign-in rather than copying an existing one:")
    print("refresh tokens rotate, so one login living in two directories has")
    print("about eight hours before one of the two is signed out.")
    print()
    if not _ask_yes("Sign in to account {} now?".format(account.display)):
        return False

    prepare_store(store)
    print()
    print("Starting Claude Code in {}".format(store.config_dir))
    print("Run /login, then /exit once it says you are signed in.")
    print()
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = store.config_dir
    # The same three that would silently outrank the login being created.
    for var in _ACCOUNT_OVERRIDE_VARS:
        env.pop(var, None)
    try:
        subprocess.call([CLAUDE_PATH], env=env, cwd=store.config_dir)
    except OSError as e:
        print("Could not start Claude Code: {}".format(e))
        return False
    print()
    return bool(account_identity(store)["has_token"])


def published_uuid(account):
    """
    This account's UUID as the pinging machine published it, or "".

    A machine that only switches has no ping directory, so the checks that
    compare a parked login against "who this account is" had nothing to compare
    against and silently passed -- on exactly the machines the README tells
    people to use for switching. The schedule carries the UUID already.
    """
    for entry in (read_schedule() or {}).get("accounts", []):
        if isinstance(entry, dict) and str(entry.get("name")) == account.name:
            return entry.get("account_uuid") or ""
    return ""


def switch_blockers(account, usable=None):
    """
    Everything that should stop or interrupt a switch to `account`, worst first.

    Findings, in the shape `doctor` and `check` already use. An **error** means
    the switch cannot achieve anything — no login to install, one that is expired
    or belongs to somebody else, or a setting that overrides it — so the switch
    does not happen. A **warning** means it will work and there is something
    worth knowing. Nothing here is merely unwise-but-blocked, which is why there
    is no --force to type.
    """
    findings = []
    store = switch_store(account)

    for where, var in account_overrides():
        findings.append(Finding(
            "error",
            "{} is set in {}".format(var, where),
            "It takes precedence over the saved login, so switching would "
            "change nothing"
            + ("." if var == "CLAUDE_CODE_OAUTH_TOKEN"
               else " — and it bills a pay-as-you-go account rather than a "
                    "subscription.")))

    if not os.path.exists(credentials_path(store)):
        findings.append(Finding(
            "error",
            "No login is parked for account {}".format(account.display),
            "Sign in once, in a browser, and it stays parked:\n"
            "       {}\n"
            "     then /login. Give it its own sign-in rather than copying one: "
            "two holders of a single login sign each other out.".format(
                sign_in_command(store))))
        return findings          # nothing further can be said about an absent login

    identity = account_identity(store)
    if not identity["has_token"]:
        findings.append(Finding(
            "error",
            "Account {}'s parked login has no access token".format(account.display),
            "Sign in again: {}".format(sign_in_command(store))))
    expires = identity["refresh_expires_at"]
    if expires and expires <= time.time():
        findings.append(Finding(
            "error",
            "Account {}'s parked login expired {}".format(
                account.display, fmt_time(expires)),
            "Sign in again: {}".format(sign_in_command(store))))

    want = account_identity(account)["account_uuid"] or published_uuid(account)
    got = identity["account_uuid"]
    if not got:
        # Without it the identity in ~/.claude.json would keep naming the old
        # account while the new one is billed — the mixture this whole section
        # exists to avoid, and one nothing downstream would notice.
        findings.append(Finding(
            "error",
            "The login parked for account {} does not say which account it "
            "is".format(account.display),
            "Its .claude.json has no oauthAccount block, so switching would "
            "leave Claude Code naming the account you left. Sign in again: "
            "{}".format(sign_in_command(store))))
    elif want and want != got:
        findings.append(Finding(
            "error",
            "The login parked for account {} is signed in as {}, which is not "
            "the account this slot pings".format(
                account.display, identity["email"] or got[:8]),
            "Sign in as the right account: {}".format(sign_in_command(store))))

    parked_grant = login_fingerprint(store)
    # Against the live login as well as the ping directory. `doctor` already
    # checked both; the switch checked only one, which left the copy it warns
    # about -- "the unexplained logout eight hours later" -- reachable by the
    # command that is supposed to refuse it.
    if parked_grant and parked_grant == login_fingerprint(user_login()):
        findings.append(Finding(
            "error",
            "Account {}'s parked login is the same one your Claude Code is "
            "using right now".format(account.display),
            "Installing it would put one grant in two places, and about eight "
            "hours later one of them would be signed out. Sign in again so the "
            "store holds its own: {}".format(sign_in_command(store))))
    if parked_grant and parked_grant == login_fingerprint(account):
        findings.append(Finding(
            "error",
            "Account {}'s parked login is the same login its pings use, not a "
            "separate one".format(account.display),
            "Refresh tokens rotate, so the two would take turns invalidating "
            "each other and one of them would be signed out. Sign in again so "
            "the store holds its own: {}".format(sign_in_command(store))))

    if not readable_json(USER_CONFIG_JSON):
        findings.append(Finding(
            "error",
            "{} exists but cannot be parsed".format(USER_CONFIG_JSON),
            "Switching rewrites that file, and rewriting one it cannot read "
            "would replace your projects, trust decisions and settings with "
            "almost nothing. Repair or move it first — Claude Code will "
            "rebuild what it needs."))

    target = credentials_path(user_login())
    if os.path.islink(target):
        findings.append(Finding(
            "warning",
            "{} is a symlink; switching replaces it with a real file".format(target),
            "Claude Code would have replaced it on its next token refresh "
            "anyway — it writes these files by rename — so nothing is lost that "
            "was not already going to be."))

    if usable is None:
        usable = account_availability(account, read_state(account), time.time())
    if usable.tier == WAITING:
        findings.append(Finding(
            "warning",
            "Account {} is not usable yet — {}".format(account.display, usable.note),
            "It comes back {}{}. Switching now is harmless; the first "
            "request before then is simply refused.".format(
                "" if usable.exact else "no later than ",
                fmt_time(usable.until))))
    elif usable.tier == NEEDS_ACTION:
        findings.append(Finding(
            "warning",
            "Account {} needs attention — {}".format(account.display, usable.note),
            "Run `{} doctor`. Switching to it now will not get you a working "
            "session.".format(COMMAND)))

    return findings


def platform_blocker():
    """
    Why this cannot work here, or None.

    On macOS the credential lives in the Keychain, not in a file. A switch
    would write a file Claude Code ignores, and take the store's only copy of
    that login on the way -- leaving the user on the account they started with
    and one browser sign-in worse off. The README has always said macOS is
    unsupported; nothing enforced it, and `--no-pings` installs cleanly there
    because it needs neither systemd nor a timer.
    """
    if sys.platform.startswith("linux"):
        return None
    return Finding(
        "error",
        "Switching accounts is only supported on Linux (this is {})".format(
            sys.platform),
        "Claude Code keeps its credential in the Keychain here, not in a file, "
        "so a switch would move a file it does not read — and consume the "
        "parked login doing it.")


def switch_account(accounts, name=None, sign_in=True):
    """
    Point the user's own Claude Code at one account. Returns an exit code.

    With no account named it follows `which`, which is the useful default: the
    reason to switch is almost always "this one is spent, give me the one that
    is not". Naming an account overrides that without argument.

    `sign_in` is what makes the first switch to an account survivable rather
    than a dead end; set it False for anything unattended, where a prompt would
    hang and a spawned Claude Code would never be answered.
    """
    if len(accounts) < 2:
        sys.stderr.write(
            "Only one account is configured, so there is nothing to switch "
            "between.\nAdd another to accounts.json and re-run ./install.sh.\n")
        return 2

    # Where the readings come from. On the pinging machine that is its own
    # state; on any other it is the schedule that machine published, which is
    # the same fallback `which` uses -- otherwise a bare `switch` on a laptop
    # would choose from no information at all and pick the first account every
    # time.
    view = schedule_view(accounts)
    if view:
        known, states, avail, _ = view
    else:
        known = accounts
        states = {a.name: read_state(a) for a in accounts}
        avail = availabilities(known, states, time.time())

    if name:
        account = find_account(accounts, name)
    else:
        chosen, _ = choose_account(known, states, time.time(), avail)
        # Deliberately no fallback to the published account. A schedule naming
        # an account this machine does not configure is either stale or came
        # from somewhere it should not have, and switching to it would take its
        # store path and its identity from the same untrusted file.
        account = find_account(accounts, chosen.name)

    # Overrides first, before anything reassuring can be said. They decide
    # which account is billed regardless of what is on disk, so "you are
    # already signed in as that account" is a false comfort while one is set --
    # and with ANTHROPIC_API_KEY it is not even the same subscription.
    unsupported = platform_blocker()
    if unsupported is not None:
        sys.stderr.write("ERROR: {}\n  -> {}\n".format(
            unsupported.message, unsupported.hint))
        return 1

    overrides = account_overrides()
    if overrides:
        for where, var in overrides:
            sys.stderr.write(
                "ERROR: {} is set in {}\n  -> It decides which account is "
                "billed, whatever this command does.\n".format(var, where))
        sys.stderr.write("\nNothing was changed.\n")
        return 1

    current = current_account(accounts)
    if current is not None and current.name == account.name:
        print("Your Claude Code is already signed in as account {}.".format(
            account.display))
        return 0

    store = switch_store(account)
    findings = switch_blockers(account, avail.get(account.name))

    # The sign-in is offered only once everything else has passed. Offering it
    # first meant a full browser round-trip could end in "Nothing was changed"
    # because of a condition that was already knowable.
    if (sign_in and not os.path.exists(credentials_path(store))
            and not [f for f in findings if f.level == "error"
                     and "No login is parked" not in f.message]
            and sys.stdin.isatty() and not ASSUME_YES):
        offer_sign_in(account, store)
        findings = switch_blockers(account, avail.get(account.name))
    errors = [f for f in findings if f.level == "error"]
    for finding in findings:
        stream = sys.stderr if finding.level == "error" else sys.stdout
        stream.write("{}: {}\n".format(finding.level.upper(), finding.message))
        if finding.hint:
            stream.write("  -> {}\n".format(finding.hint))
    if errors:
        sys.stderr.write("\nNothing was changed.\n")
        return 1
    if findings:
        print()

    lock = acquire_switch_lock()
    if lock is None:
        sys.stderr.write(
            "Another switch is already running on this machine.\n"
            "  Two at once would leave one of the logins on no disk at all, "
            "so this one stops.\n  Try again once it has finished.\n")
        return 1

    try:
        return _perform_switch(account, current, states)
    finally:
        os.close(lock)          # releases the flock with it


def _perform_switch(account, current, states):
    """The half of `switch_account` that writes, run under the lock."""
    # Read before anything is overwritten: after install_login the outgoing
    # identity is gone from ~/.claude.json, and it is the only way to name the
    # login for someone whose Claude Code was signed in by hand.
    outgoing = account_identity(user_login())["email"]

    # Reading and copying come before anything is written, so a failure here
    # costs nothing -- but it still arrives as a traceback unless it is caught,
    # and a traceback from a command that touches ~/.claude reads as though it
    # had got half way. It has not.
    try:
        taken = take_login()
        backup = backup_user_login()
    except (IOError, OSError) as e:
        sys.stderr.write(
            "Could not read the login in {}: {}\n"
            "  Nothing was changed.\n".format(USER_CONFIG_DIR, e))
        return 1
    except KeyboardInterrupt:
        sys.stderr.write("\nInterrupted. Nothing was changed.\n")
        return 1

    # From here two files are being rewritten. A disk that filled up, a
    # permission that changed underneath, or a Ctrl-C would otherwise surface
    # as a traceback at the one moment a person most needs a sentence:
    # mid-switch, possibly with the credential already replaced. Say which half
    # happened and where the copy is.
    where = ("\n  Your previous credentials are in {}.".format(backup)
             if backup else "")
    try:
        dropped = install_login(switch_store(account))
    except KeyboardInterrupt:
        sys.stderr.write(
            "\nInterrupted while installing account {}'s login.{}\n"
            "  Run `{} status` to see which account you are on, then switch "
            "again.\n".format(account.display, where, COMMAND))
        return 1
    except (IOError, OSError) as e:
        sys.stderr.write(
            "Could not install account {}'s login: {}\n"
            "  Nothing was parked, so no login has been moved.{}\n"
            "  Run `{} status` to see which account you are on.\n".format(
                account.display, e, where, COMMAND))
        return 1
    try:
        parked = park_login(current, taken) if current is not None else None
    except KeyboardInterrupt:
        sys.stderr.write(
            "\nSwitched to account {}, but was interrupted before the login "
            "it replaced could be parked.{}\n"
            "  That login is in the backup and nowhere else.\n".format(
                account.display, where))
        return 1
    except (IOError, OSError) as e:
        sys.stderr.write(
            "Switched to account {}, but could not park the login it "
            "replaced: {}\n"
            "  That login now exists only in the backup.{}\n"
            "  Copy it back into {} before switching away again.\n".format(
                account.display, e, where,
                switch_store(current).config_dir if current else "its store"))
        return 1

    print("Switched your Claude Code to account {}.".format(account.display))
    if parked is not None:
        print("  Parked account {} in {}".format(current.display,
                                                 parked.config_dir))
    elif any(taken):
        # Refusing would be worse: it would leave someone stuck behind a login
        # this tool cannot name. Saying exactly where it went is enough.
        orphan = orphan_login(taken)
        print("  The login that was here{} is not one of the configured "
              "accounts, so it was not parked.".format(
                  " ({})".format(outgoing) if outgoing else ""))
        if orphan:
            print("  It is kept in {}, which is never pruned.".format(orphan))
    # And where `taken` holds nothing at all, nothing is said: this is the
    # first switch on a machine that had never signed in to Claude Code, and
    # describing the login that was not here reads as though one had been
    # lost.
    if backup:
        print("  Previous credentials backed up to {}".format(backup))
    if dropped:
        print("  Dropped {} cached account setting{} that belonged to the old "
              "account; Claude Code refetches them.".format(
                  len(dropped), "" if len(dropped) == 1 else "s"))

    print()
    # Measured rather than assumed, and not what was assumed first: a session
    # that was already open follows the switch completely. Credentials are
    # re-read per request, and /status and /usage read the identity when you
    # run them rather than from the snapshot taken at startup -- so both the
    # requests and the display move. Nothing needs restarting, and saying it
    # did sent people to do something with no effect.
    sessions = running_claude_sessions()
    inside = " — including the one you are typing in" if os.environ.get(
        "CLAUDECODE") else ""
    if len(sessions) == 1:
        print("  One Claude Code session is already running{}. It moves to "
              "this account on its next request; /status and /usage there "
              "report the new one.".format(inside))
    elif sessions:
        print("  {} Claude Code sessions are already running{}. They move to "
              "this account on their next request; /status and /usage there "
              "report the new one.".format(len(sessions), inside))

    expiry = next_expiry(states.get(account.name) or read_state(account),
                         time.time())
    if expiry != float("inf"):
        print("  Account {}'s window ends {} (in {}).".format(
            account.name, fmt_time(expiry), fmt_delta(expiry - time.time())))
    # The one cost worth naming, because it is payable and avoidable in the
    # same breath: the prompt cache belongs to the account you left, so the
    # first request on this one re-sends whatever it resumes.
    print("  A fresh session here costs almost nothing; resuming a long one "
          "pays for its whole history again.")
    return 0


def unit_target_findings():
    """
    Units that no longer point at the checkout they are being run from.

    `is-enabled` and the next elapse both stay true when the script a unit
    names has moved or been deleted, so a machine can be enabled, scheduled,
    and failing every single time. That happens when the checkout is renamed or
    moved, and when a second clone rewrites the shared template out from under
    the first. `checkpoint_is_for_this_cwd` exists for exactly this shape of
    failure -- everything looks installed and every run dies for a reason only
    the log knows -- and the same reasoning was never applied to the units.
    """
    findings, me = [], os.path.abspath(__file__)
    for name in installed_units():
        if not name.endswith(".service"):
            continue
        for line in _read_text(os.path.join(UNIT_DIR, name)).splitlines():
            if not line.startswith("ExecStart="):
                continue
            target = next((part for part in shlex.split(line[10:])
                           if part.endswith(".py")), "")
            if not target:
                continue
            if not os.path.exists(target):
                findings.append(Finding(
                    "error",
                    "The installed timer runs {}, which does not exist".format(
                        target),
                    "Every ping is failing. Re-run ./install.sh from the "
                    "checkout you want it to use."))
            elif os.path.abspath(target) != me:
                findings.append(Finding(
                    "warning",
                    "The installed timer runs {}, not this checkout".format(
                        target),
                    "Another copy of this repository owns the timers. Running "
                    "./install.sh here would take them over; uninstalling here "
                    "would stop them there."))
    return findings


def switch_findings(accounts):
    """
    What `doctor` should say about the switch stores, or [] if unconfigured.

    Only the failures that are invisible otherwise: a parked login that has
    quietly expired, and one that is a copy of a login something else is already
    refreshing.
    """
    if not switching_configured(accounts):
        return []
    findings = []
    # An empty store is only a gap when that account's login is somewhere else
    # entirely. For the account you are signed in as it is the healthy state --
    # its login is live in ~/.claude, which is the whole point of the store
    # being a parking place and never a copy. Warning there would be advice to
    # go and create a second login nobody needs.
    live = current_account(accounts)
    for account in accounts:
        store = switch_store(account)
        if not os.path.isdir(store.config_dir):
            continue
        if not os.path.exists(credentials_path(store)):
            if live is None or live.name != account.name:
                findings.append(Finding(
                    "warning",
                    "Account {} has a switch directory but no login parked in "
                    "it".format(account.display),
                    "You cannot switch to it until there is one: {}".format(
                        sign_in_command(store))))
            continue
        identity = account_identity(store)
        expires = identity["refresh_expires_at"]
        if expires and expires <= time.time():
            findings.append(Finding(
                "error",
                "Account {}'s parked login expired {}".format(
                    account.display, fmt_time(expires)),
                "You cannot switch to it until you sign in again: {}".format(
                    sign_in_command(store))))
        elif expires and expires - time.time() < REFRESH_WARNING_DAYS * 86400:
            findings.append(Finding(
                "warning",
                "Account {}'s parked login expires {}".format(
                    account.display, fmt_time(expires)),
                "Switch to it before then and it renews itself; leave it and it "
                "needs a browser sign-in."))
        mode = _permissions(store.config_dir)
        if mode is not None and mode & 0o077:
            findings.append(Finding(
                "warning",
                "Account {}'s parked login is in a directory others can reach "
                "({:o})".format(account.display, mode),
                "A directory that can be written is a file that can be "
                "replaced, whatever mode the file has. Tighten it: chmod 700 "
                "{}".format(store.config_dir)))
        marker = _sync_marker(store.config_dir)
        if marker:
            findings.append(Finding(
                "warning",
                "Account {}'s parked login is inside a synced folder "
                "({})".format(account.display, marker),
                "Syncing it puts one login on two machines, and refresh tokens "
                "rotate — within about eight hours one of them is signed out. "
                "Exclude {} from the sync.".format(SWITCH_ROOT)))

        scopes = login_scopes(store)
        missing = [s for s in FULL_LOGIN_SCOPES if scopes and s not in scopes]
        if missing:
            findings.append(Finding(
                "warning",
                "Account {}'s parked login is missing {}".format(
                    account.display, ", ".join(missing)),
                "It can still answer, but not everything Claude Code does — "
                "and a narrowed sign-in cannot be widened again, only "
                "replaced. Sign in once more: {}".format(
                    sign_in_command(store))))

        parked_grant = login_fingerprint(store)
        if parked_grant and parked_grant == login_fingerprint(user_login()):
            findings.append(Finding(
                "error",
                "Account {}'s parked login is the same one your Claude Code is "
                "using right now".format(account.display),
                "Two directories are refreshing one login, and in about eight "
                "hours whichever refreshes second is signed out. Nothing here "
                "creates this state, so it is either a switch that was "
                "interrupted or a credentials file copied by hand: delete "
                "{} and sign in again there.".format(
                    credentials_path(store))))
        if parked_grant and parked_grant == login_fingerprint(account):
            findings.append(Finding(
                "error",
                "Account {}'s parked login is a copy of the one its pings "
                "use".format(account.display),
                "Refresh tokens rotate, so these two will take turns "
                "invalidating each other until one is signed out. Give the "
                "store its own sign-in: {}".format(sign_in_command(store))))
    return findings


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
#
# One pass, plain questions, and nothing irreversible without saying so first.
# Two things matter more than they look:
#
#   * Signing in starts no usage window, because it sends no message. Saying so
#     removes the main reason people put this off until "a good moment" — there
#     isn't one, and waiting for it is the actual mistake.
#   * Nothing here asks the user to be awake at a particular hour. Alignment is
#     the runtime's job; the worst that comes of ignoring the advice is that the
#     tool spends a little longer getting the spacing right.

# Set by `setup --yes`. A module global rather than an argument threaded through
# every call, because the questions are asked from several places and a flag
# that reached only some of them would hang the unattended install on the one
# it missed.
ASSUME_YES = False


def _ask(prompt, default=""):
    if ASSUME_YES:
        # Echoed, so an unattended transcript still shows what was decided.
        print("{} {}".format(prompt, default))
        return default
    try:
        answer = input("{} ".format(prompt)).strip()
    except EOFError:
        return default
    return answer or default


def _ask_yes(prompt, default=True):
    suffix = "[Y/n]" if default else "[y/N]"
    answer = _ask("{} {}".format(prompt, suffix)).lower()
    if not answer:
        return default
    return answer.startswith("y")


def _command_exists(name):
    """Whether `name` is on PATH. A seam, so the tests can pretend it is not."""
    return bool(shutil.which(name))


def systemd_blockers(pings):
    """
    What stops this machine running the pings, as Findings. Empty if nothing.

    Checked here rather than in install.sh because the answer depends on a
    question install.sh has not asked yet. Deciding it from the --no-pings flag
    alone turned a machine without systemd away even when its owner was about
    to say they did not want the pings — a prerequisite refusing an install
    that would never have used it.
    """
    if not pings:
        return []
    findings = []
    if not _command_exists("systemctl"):
        findings.append(Finding(
            "error",
            "Running the pings needs systemd, and systemctl was not found",
            "This machine can still switch accounts, which needs no timers: "
            "re-run with ./install.sh --no-pings"))
    elif not _command_exists("systemd-run"):
        # Not fatal: the pings still run on their fixed cadence. What is lost
        # is the correction that puts a schedule back on its window boundary.
        findings.append(Finding(
            "warning",
            "systemd-run was not found, so boundary anchoring and spacing "
            "will be disabled",
            "The pings still run every {} minutes; they just cannot correct "
            "their phase after a missed one.".format(INTERVAL_MIN)))
    return findings


def sign_ins_needed(accounts, pings):
    """
    Every directory on this machine still needing a browser sign-in.

    Returns (what it is for, account, directory) triples. Two rules decide the
    list, and both come from measurement rather than taste:

      * Each directory that refreshes a token needs its **own** sign-in.
        Refresh tokens rotate strictly — the token a refresh replaces is
        rejected from that moment — so one login copied into two directories
        has about eight hours before whichever refreshed last strips the other,
        and the loser's next refresh empties its credentials file.

      * The account you are already signed in as needs no store sign-in. Its
        login is *moved* into the store the first time you switch away, which
        keeps the one-live-copy rule intact. Copying it there now would break
        exactly that rule.

    Anything already signed in is skipped, so re-running to add an account asks
    only for the new one.
    """
    needed = []
    mine = current_account(accounts)
    # Grouped by account, not by kind. Where an account needs both, the two
    # sign-ins are the same Claude account and land next to each other, so an
    # SSO user picks that identity in the browser once and does both. Listing
    # every ping directory and then every store would walk them through
    # A, B, C, B, C -- four identity switches in the browser for three
    # accounts, which is the whole friction.
    for account in accounts:
        if pings and not account_identity(account)["has_token"]:
            needed.append(("its pings", account, account.config_dir))
        store = switch_store(account)
        if (not account_identity(store)["has_token"]
                and (mine is None or mine.name != account.name)):
            needed.append(("switching to it", account, store.config_dir))
    return needed


def tighten_login_dirs(accounts, pings):
    """
    Narrow every directory here that holds a login. Returns the ones changed.

    The sign-ins this wizard prints are run by the user, and Claude Code
    creates a config directory with whatever umask it is handed -- 0775 on an
    ordinary machine. So a directory the tool has just told somebody to sign
    into ends up writable by their group, and `doctor` opens a brand-new
    install with a warning about a directory the install asked for.

    Narrowed rather than reported, and on every run rather than only the run
    that created them, so that re-running setup repairs one that was loosened.
    Nothing is created here: a store that does not exist is a sign-in nobody
    has done, and `switch` offers that when it needs it.
    """
    changed = []
    for directory in ([SWITCH_ROOT]
                      + [switch_store(a).config_dir for a in accounts]
                      + ([a.config_dir for a in accounts] if pings else [])):
        if not os.path.isdir(directory):
            continue
        mode = _permissions(directory)
        if mode is not None and mode & 0o077:
            secure_dir(directory)
            changed.append(directory)
    return changed


def setup(argv_accounts=None, pings=None, assume_yes=False):
    """The whole first-run experience. Safe to re-run at any time."""
    global ASSUME_YES
    ASSUME_YES = assume_yes
    print("Claude Code Window Timing — setup")
    print("=" * 33)
    print()

    try:
        existing = load_accounts()
    except ConfigError:
        existing = default_accounts()
    configured = os.path.exists(ACCOUNTS_FILE)

    if configured:
        print("Currently configured:")
        width = max([len(a.display) for a in existing] or [0])
        for account in existing:
            print("  account {:<{}} {}".format(account.display, width,
                                               account.config_dir))
        print()

    count = argv_accounts
    if count is None:
        answer = _ask("How many Claude accounts do you want to use?",
                      str(len(existing) if configured else 1))
        try:
            count = int(answer)
        except ValueError:
            print("That is not a number.")
            return 2
    if count < 1:
        print("You need at least one account.")
        return 2
    if count > 4:
        print()
        print("Beyond three accounts this stops being the cheaper option:")
        print("four Pro subscriptions cost about the same as one Max plan,")
        print("which gives you a single pool instead of four you cannot")
        print("combine — and this many logins to keep alive besides.")
        if not _ask_yes("Continue with {} anyway?".format(count), default=False):
            return 0

    accounts = _plan_accounts(existing if configured else [], count)

    if pings is None:
        print()
        print("The pings belong on ONE machine. A usage window belongs to the")
        print("account, server-side, so a second machine pinging the same")
        print("accounts doubles what they consume and buys nothing at all.")
        print()
        print("Answer n on every other machine. It can still switch accounts,")
        print("which needs no timers and spends nothing.")
        print()
        pings = _ask_yes("Run the pings from this machine?", default=pings_here())

    blockers = systemd_blockers(pings)
    if blockers:
        print()
        report_findings(blockers)
        if any(f.level == "error" for f in blockers):
            return 1

    # Answering "1" where "2" was meant is a plausible slip, and the layout
    # below shows only what survives it -- so without this the wizard asks
    # "Go ahead?" about a configuration quietly missing an account, and the
    # label is the one part that cannot be put back by re-running.
    dropped = [a for a in existing
               if a.name not in set(b.name for b in accounts)]
    if dropped:
        print()
        print("Dropping {} from the configuration:".format(
            "an account" if len(dropped) == 1 else "accounts"))
        width = max(len(a.display) for a in dropped)
        for account in dropped:
            print("  account {:<{}} {}".format(account.display, width,
                                               account.config_dir))
        print("Its timer stops." if len(dropped) == 1 else "Their timers stop.")
        print("The directory, login, checkpoint and logs are left exactly as")
        print("they are, so adding it back later costs nothing but the label.")

    print()
    print("Layout:")
    width = max(len(a.name) for a in accounts)
    for account in accounts:
        print("  account {:<{}} {}".format(
            account.name, width,
            account.config_dir if pings else switch_store(account).config_dir))
    if pings:
        print()
        print("Parked logins for switching go in {}/<account>.".format(
            _tilde(SWITCH_ROOT)))
    else:
        print()
        print("No timers on this machine: it switches accounts, it does not")
        print("ping. Copy schedule.json here from the machine that does, and")
        print("`{} which` will answer from it.".format(COMMAND))
    if pings and count > 1:
        print()
        print("A fresh window will arrive every {}, instead of every {}."
              .format(fmt_delta(WINDOW_HOURS * 3600 / float(count)),
                      fmt_delta(WINDOW_HOURS * 3600)))
    print()
    if not _ask_yes("Go ahead?"):
        return 0

    _write_accounts_file(accounts, pings=pings)
    print("Wrote {}".format(ACCOUNTS_FILE))

    # ── Signing in ──────────────────────────────────────────────────────────
    if pings:
        for account in accounts:
            ensure_ping_config(account)

    needed = sign_ins_needed(accounts, pings)
    if needed:
        mine = current_account(accounts)
        print()
        print("Sign in to each of these directories. They belong to this tool —")
        print("they are not where you work, and nothing you do in Claude Code")
        print("touches them. Signing in sends no message, so it starts no usage")
        print("window and there is no wrong time to do it.")
        print()
        print("Sign in to each separately, even for the same account: each")
        print("gets its own login rather than a copy of one, so a token refresh")
        print("here can never log you out over there. Refresh tokens rotate, so")
        print("a copy would have about eight hours before one of the two was")
        print("signed out; two separate logins last as long as they both live.")
        if mine is not None:
            print()
            print("Account {} needs no store of its own: you are signed in as it,"
                  .format(mine.display))
            print("and that login moves into its store the first time you switch")
            print("away from it.")
        elif not pings:
            # Worth a sign-in, so worth saying before they start: without the
            # published schedule this machine cannot tell which account it is
            # already signed in as, and asks for a store it does not need.
            print()
            print("None of these is the account you are already signed in as —")
            print("this machine cannot tell which that is. Copy schedule.json")
            print("here from the machine running the pings and re-run setup,")
            print("and it will recognise it and ask for one sign-in fewer.")
        grouped = [(a, [(what, d) for what, acc, d in needed
                        if acc.name == a.name]) for a in accounts]
        grouped = [(a, rows) for a, rows in grouped if rows]
        if any(len(rows) > 1 for _a, rows in grouped):
            print()
            print("Where an account needs two, they are listed together on")
            print("purpose: both are the same Claude account, so do them one")
            print("after the other and your browser only changes identity once.")
        for account, rows in grouped:
            print()
            print("  Account {}".format(account.display))
            for what, directory in rows:
                print("    CLAUDE_CONFIG_DIR={} claude".format(directory))
                print("      then /login — for {}".format(what))
        print()
        print("That is {} browser sign-in{}, once on this machine.".format(
            len(needed), "" if len(needed) == 1 else "s"))
        print("Any you skip can be done later; `{} switch` offers the one it"
              .format(COMMAND))
        print("needs, when it needs it.")
        print()
        _ask("Press Enter when you are done.")

    for directory in tighten_login_dirs(accounts, pings):
        print("Tightened {} to 700 — it holds a login.".format(_tilde(directory)))

    # ── Checking ────────────────────────────────────────────────────────────
    print()
    print()
    findings = validate_accounts(accounts) if pings else []
    if findings:
        report_findings(findings)
        if any(f.level == "error" for f in findings):
            print()
            print("Setup stopped. Fix the errors above and run it again.")
            return 1
        print()
        if not _ask_yes("Continue despite the warnings?"):
            return 0

    # ── Checkpoints ─────────────────────────────────────────────────────────
    built = []
    for account in (accounts if pings else []):
        if os.path.exists(account.session_id_file) and \
           os.path.exists(account.checkpoint_backup) and \
           checkpoint_is_for_this_cwd(account):
            continue
        print()
        if os.path.exists(account.checkpoint_backup):
            # An upgrade moved where pings run, so the existing conversation is
            # registered against a directory it will never be resumed from.
            print("Rebuilding account {}'s background conversation — the old "
                  "one belongs to a working directory this no longer uses..."
                  .format(account.display))
        else:
            print("Creating account {}'s background conversation...".format(
                account.display))
        init(account)
        built.append(account)

    # ── Timers, then the launcher ───────────────────────────────────────────
    if pings:
        print()
        install_units(accounts)
    else:
        # Somebody who just said this machine does not ping, on a machine that
        # has been pinging, means it. Leaving the timers running would make the
        # recorded answer a lie and quietly double what the accounts consume --
        # the exact waste the question exists to prevent.
        if installed_units():
            print()
            print("This machine is currently pinging, which is not what you")
            print("just asked for. Leaving the timers running would double what")
            print("these accounts consume for no benefit, so they should stop —")
            print("the checkpoints and logs stay either way, so turning it back")
            print("on later picks up where it left off.")
            print()
            if _ask_yes("Stop the timers on this machine?"):
                for item in uninstall(accounts):
                    print("  removed {}".format(item))
            else:
                print()
                print("Left running. `{} doctor` will keep reporting this as an"
                      .format(COMMAND))
                print("error until the two agree.")
    print()
    print("Wrote {}".format(write_entry_point()))
    offer_launcher_link()

    # ── What happens next ───────────────────────────────────────────────────
    print()
    print("Done.")
    print()
    if not pings:
        print("This machine switches accounts; it does not ping. Copy")
        print("schedule.json here from the machine that does, and both of")
        print("these answer from it:")
        print()
        print("  {} which       which account to spend right now".format(COMMAND))
        print("  {} switch      point your own Claude Code at it".format(COMMAND))
        print()
        print("Sessions already open follow a switch on their next request,")
        print("so there is nothing to restart.")
        return 0
    if len(accounts) > 1:
        print("The first ping for each account runs within a minute. From there")
        print("the service works out where each window sits, and holds an")
        print("account back when that is what it takes to space them evenly.")
        print()
        # Only worth saying while the spacing is still being established. On a
        # re-run of a working setup the windows are already where they should
        # be, and telling someone not to use their accounts for no reason is
        # how a tool gets uninstalled.
        if built:
            print("Accounts starting together are the one case it will not fix")
            print("on its own: lining them up costs hours with no window")
            print("running, which is not something to do unasked. Once every")
            print("account has pinged, `{} realign` prices it.".format(COMMAND))
            print()
            print("For the quickest result, avoid using the accounts other than")
            print("{} for the next few hours. If you do use them, nothing "
                  "breaks —".format(accounts[0].display))
            print("the service re-plans from wherever things actually end up.")
            print()
        print("  {} status      what each account is doing".format(COMMAND))
        print("  {} which       which one to use right now".format(COMMAND))
        print("  {} realign     what evening out the spacing would "
              "cost".format(COMMAND))
        print()
        # Worth one line here rather than none: the whole point of knowing
        # which account to spend is being able to go and spend it, and nobody
        # reads the help for a command they do not know exists.
        print("`{} switch` can point your own Claude Code at whichever "
              "account".format(COMMAND))
        print("that is. It needs one browser sign-in per account, per machine;")
        print("`{} help switch` explains it.".format(COMMAND))
    return 0


def _plan_accounts(existing, count):
    """
    Keep the accounts already configured, add or drop to reach `count`.

    A new slot takes its position as its name, unless an account being kept
    already answers to it: somebody whose accounts.json holds one account
    called "2" would otherwise be handed a second one called "2", and every
    run after that would refuse to read the file it had just been given.
    """
    accounts = []
    for index in range(count):
        if index < len(existing):
            source = existing[index]
            accounts.append(Account(source.name, source.config_dir, index,
                                    source.label))
            continue
        number, taken = index + 1, set(a.name for a in accounts)
        while str(number) in taken:
            number += 1
        name = str(number)
        accounts.append(Account(name, ping_config_dir(name), index))
    return accounts


def _write_accounts_file(accounts, pings=True):
    document = {"accounts": [
        dict([("name", a.name), ("config_dir", _tilde(a.config_dir))]
             + ([("label", a.label)] if a.label else []))
        for a in accounts]}
    # Only written when it is false, so an ordinary install's accounts.json is
    # exactly what it always was and nobody has to wonder what a new key means.
    if not pings:
        document["pings"] = False
    tmp = _temp_name(ACCOUNTS_FILE)
    with open(tmp, "w") as f:
        json.dump(document, f, indent=2)
        f.write("\n")
    os.replace(tmp, ACCOUNTS_FILE)


def _tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + os.sep) else path


UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")

# What this tool's units are called. One generation: this project has never
# been released under another name, so there is nothing older to recognise.
UNIT_PREFIX = "claude-window-timing"

_SERVICE_UNIT = """[Unit]
Description=Claude Code Window Timing — ping for account %i

[Service]
Type=oneshot
WorkingDirectory={script_dir}
ExecStart={python} {script} ping %i
# PATH so the script can locate the claude CLI; HOME comes from the user manager.
Environment=PATH={path}

[Install]
WantedBy=default.target
"""

_TIMER_UNIT = """[Unit]
Description=Claude Code Window Timing — ping account %i every {interval} minutes

[Timer]
# OnActiveSec fires shortly after the timer starts; OnUnitActiveSec then repeats
# every interval after the service was last activated. "Last activated" is what
# makes the boundary anchor work: when the one-shot anchor starts this same
# service, the repeating series re-anchors off that run, so a single correction
# puts every later ping back on the window boundary.
OnActiveSec=1min
OnUnitActiveSec={interval}min
# systemd's default accuracy is 1 minute, which would let each ping land up to a
# minute late and quietly stretch the cadence. Tighten it so the interval holds.
AccuracySec=1s
Persistent=true

[Install]
WantedBy=timers.target
"""


def unit_path():
    """
    The PATH a ping runs with: the usual directories, plus the CLI's own.

    A timer inherits none of the shell setup that makes `claude` findable. An
    npm or nvm install puts it under ~/.nvm/versions/node/<version>/bin, which
    only ~/.bashrc adds to PATH -- so setup, run from that shell, finds the CLI
    and builds the checkpoint, and then every ping for ever after cannot find
    it. The install looks perfect and nothing works, which is the failure this
    tool exists to notice rather than to have.

    Resolved here, in the environment the user is installing from, where the
    CLI has just been proven to exist. Its *directory* rather than the binary
    alone, because an npm-installed launcher can begin `#!/usr/bin/env node`
    and needs node beside it.
    """
    entries = [os.path.join(HOME, ".local", "bin"), "/usr/local/sbin",
               "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    directory = os.path.dirname(os.path.abspath(CLAUDE_PATH))
    if directory and directory not in entries:
        entries.insert(0, directory)
    return os.pathsep.join(entries)


def installed_instances():
    """
    Account names that currently have a timer enabled.

    Read from timers.target.wants rather than asked of systemd: `enable` works
    by putting a symlink there, so it is the authoritative record and it still
    answers when the user manager is not reachable.
    """
    wants = os.path.join(UNIT_DIR, "timers.target.wants")
    pattern = re.compile(r"^claude-window-timing@(.+)\.timer$")
    try:
        entries = os.listdir(wants)
    except (IOError, OSError):
        return set()
    return {m.group(1) for m in (pattern.match(e) for e in entries) if m}


def install_units(accounts):
    """Write the templated units and start one timer per account."""
    if not os.path.isdir(UNIT_DIR):
        os.makedirs(UNIT_DIR)

    # An upgrade from the single-account layout: leaving these enabled would run
    # a second, unaccounted-for ping series against the default account.
    for legacy in ("claude-window-timing.timer", "claude-window-timing.service"):
        path = os.path.join(UNIT_DIR, legacy)
        if os.path.exists(path):
            _systemctl("disable", "--now", legacy)
            os.remove(path)
            print("Removed the old single-account unit {}".format(legacy))

    # An account that has been removed keeps its timer otherwise: it fires every
    # interval, fails because the account no longer exists, and goes on doing so
    # forever. Nothing else would ever clean it up, since the tool no longer
    # knows that account is something it should think about.
    for name in sorted(installed_instances()):
        if name in {a.name for a in accounts}:
            continue
        _systemctl("disable", "--now", "claude-window-timing@{}.timer".format(name))
        _systemctl("stop", "claude-window-timing-anchor-{}.timer".format(name))
        _systemctl("stop", "claude-window-timing-anchor-{}.service".format(name))
        _systemctl("reset-failed",
                   "claude-window-timing@{}.service".format(name),
                   "claude-window-timing-anchor-{}.timer".format(name))
        drop_in = os.path.join(UNIT_DIR, "claude-window-timing@{}.timer.d".format(name))
        if os.path.isdir(drop_in):
            shutil.rmtree(drop_in)
        print("Stopped the timer for account {}, which is no longer "
              "configured".format(name))

    script = os.path.abspath(__file__)
    with open(os.path.join(UNIT_DIR, "claude-window-timing@.service"), "w") as f:
        f.write(_SERVICE_UNIT.format(script_dir=SCRIPT_DIR, script=script,
                                     python=sys.executable or "/usr/bin/python3",
                                     path=unit_path()))
    with open(os.path.join(UNIT_DIR, "claude-window-timing@.timer"), "w") as f:
        f.write(_TIMER_UNIT.format(interval=INTERVAL_MIN))

    # Offset each account's first firing. OnUnitActiveSec measures from the last
    # activation, so shifting the first one shifts that account's whole grid for
    # good — without this, timers started together stay in lockstep forever and
    # every account spawns a Claude process in the same second. The guard in
    # Account.guard_sec only offsets *anchored* pings, which do not happen until
    # a window boundary comes round.
    for account in accounts:
        drop_in = os.path.join(UNIT_DIR, account.timer_unit + ".d")
        if account.index:
            if not os.path.isdir(drop_in):
                os.makedirs(drop_in)
            with open(os.path.join(drop_in, "stagger.conf"), "w") as f:
                # Two systemd subtleties, both of which bite silently:
                #
                #   * OnActiveSec is a list, not a scalar. Assigning it in a
                #     drop-in *adds* to what the template set, so the account
                #     would fire at both times and collide anyway.
                #   * Assigning the empty string to any monotonic timer option
                #     resets *all* of them — so clearing OnActiveSec also
                #     discards the template's OnUnitActiveSec, and the timer
                #     fires once and then never again. It has to be restated.
                f.write("[Timer]\nOnActiveSec=\nOnActiveSec={}s\n"
                        "OnUnitActiveSec={}min\n".format(
                            60 + account.index * PING_STAGGER_SEC, INTERVAL_MIN))
        elif os.path.isdir(drop_in):
            shutil.rmtree(drop_in)          # account order may have changed

    _systemctl("daemon-reload")
    for account in accounts:
        _systemctl("enable", account.timer_unit)
        _systemctl("restart", account.timer_unit)
        print("Timer running for account {} — every {} minutes{}".format(
            account.display, INTERVAL_MIN,
            "" if not account.index else ", offset {}s so the accounts do not "
            "ping at the same moment".format(account.index * PING_STAGGER_SEC)))

    if "Linger=yes" not in (_run(["loginctl", "show-user", USER]).stdout or ""):
        print()
        print("To keep the timers running while you are logged out:")
        print("  sudo loginctl enable-linger {}".format(USER))


def installed_units():
    """
    Any unit file this tool has put in place, by name.

    Looked up by prefix rather than by asking whether account N's timer exists:
    the timers are one systemd *template* plus a drop-in per account, so
    `claude-window-timing@2.timer` is never a file on disk and checking for one
    finds nothing on a machine that is pinging perfectly well.
    """
    try:
        return sorted(f for f in os.listdir(UNIT_DIR)
                      if f.startswith(UNIT_PREFIX))
    except (IOError, OSError):
        return []


def uninstall(accounts, purge=False):
    """
    Remove everything this tool installed, and nothing else.

    Two rules. The user's own `~/.claude`, `~/.claude.json` and conversations are
    never touched — they were never ours, and uninstalling is not the moment to
    start. And directories holding a login the user performed are left in place
    rather than deleted: the ping directories, and any parked login under
    `~/.claude-switch`. Signing somebody out is not an uninstaller's business,
    and a parked login is the only copy of itself.

    `purge` additionally removes what this tool *generated* inside its own
    directory — state, logs, the published schedule, the account list and the
    launcher — so that what is left is the checkout as git has it. It stops at
    exactly the same line: nothing outside this directory except the units, and
    never a directory holding a login.
    """
    removed = []

    for account in accounts:
        _systemctl("disable", "--now", account.timer_unit)
        _systemctl("stop", account.anchor_unit + ".timer")
        _systemctl("stop", account.anchor_unit + ".service")
        _systemctl("reset-failed", account.service_unit,
                   account.anchor_unit + ".timer")
        removed.append("timer for account " + account.name)

    for name in ("claude-window-timing@.service", "claude-window-timing@.timer",
                 "claude-window-timing.service", "claude-window-timing.timer"):
        path = os.path.join(UNIT_DIR, name)
        if os.path.exists(path):
            os.remove(path)
            removed.append(name)
    for account in accounts:
        drop_in = os.path.join(UNIT_DIR, account.timer_unit + ".d")
        if os.path.isdir(drop_in):
            shutil.rmtree(drop_in)
    _systemctl("daemon-reload")

    if purge:
        # Generated files only, each one re-created by the next install. The
        # ping directories are deliberately not in this list: they hold logins,
        # and a flag called --purge is not consent to sign anybody out.
        # The link is removed before the launcher it points at, and only
        # when it is ours: a `claude-window` on PATH belonging to another
        # checkout is that checkout's, and purging here would take away the
        # command its timers were installed with.
        state, link = launcher_link()
        if state == "reachable" and os.path.islink(link):
            try:
                os.remove(link)
                removed.append(link)
            except OSError:
                pass
        rc = shell_rc_file()
        if rc and remove_path_line(rc):
            removed.append("the PATH line in " + _tilde(rc))
        for path in (STATE_ROOT, SCHEDULE_FILE, ACCOUNTS_FILE,
                     os.path.join(BIN_DIR, COMMAND), BIN_DIR):
            if os.path.isdir(path):
                if path == BIN_DIR and os.listdir(path):
                    continue            # something else lives there; leave it
                shutil.rmtree(path)
            elif os.path.exists(path):
                os.remove(path)
            else:
                continue
            removed.append(os.path.relpath(path, SCRIPT_DIR))

    return removed


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------
#
# Flat verbs with the account as a positional argument, in the shape of
# systemctl and git rather than `docker <noun> <verb>`. Noun-verb grouping earns
# its keep when there is a real matrix of resources; here there is one resource —
# accounts — and a handful of global actions, so grouping would only invent
# structure and lengthen every command. Docker itself keeps `ps`, `run` and
# `images` flat for exactly that reason.
#
# Conventions, all of them load-bearing for something other tools will call:
#
#   * stdout carries data, stderr carries commentary, so `which` stays pipeable.
#   * machine-readable output only ever on request (--json), never by default.
#   * exit codes: 0 fine, 1 something is wrong, 2 the command was misused.
#   * the bare command reports status. Sending a ping costs real quota and
#     changes when a window starts, so it must always be asked for by name.

DESCRIPTION = "Keep Claude Code usage windows rolling, across one or more accounts."


def build_parser():
    parser = argparse.ArgumentParser(
        prog=COMMAND, description=DESCRIPTION,
        # The usage line is written out rather than generated. argparse's own
        # version, `claude-window [-h] <command> ...`, describes the top-level
        # grammar — but it reads as the invocation template, so it puts -h
        # *before* the command while every other line of help puts it after.
        # One screen telling you two different things is worse than either.
        usage="%(prog)s [<command>] [options]\n"
              "       %(prog)s help [<command>]",
        epilog="With no command at all, reports status.\n"
               "Run `{0} help realign`, or any other command, to see what it "
               "does and takes.".format(COMMAND),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # `prog` must be given explicitly. Without it argparse derives each
    # subparser's program name from the parent's *usage* string, so a custom one
    # — especially a multi-line one — is inherited verbatim as a prefix and every
    # subcommand's help comes out mangled.
    sub = parser.add_subparsers(dest="command", metavar="<command>",
                                prog=COMMAND)

    def add(name, help_text, account="no"):
        child = sub.add_parser(name, help=help_text, description=help_text)
        if account == "optional":
            child.add_argument("account", nargs="?", metavar="ACCOUNT",
                               help="which account (default: the first)")
        elif account == "required":
            child.add_argument("account", metavar="ACCOUNT",
                               help="an account name from `{} accounts`".format(
                                   COMMAND))
        return child

    def add_live(command, default_note):
        """
        The `--live` / `--no-live` pair, defaulting to neither.

        Both are needed, and the default has to be undecidable from the flags
        alone: `status --json` is the interface something polls, and a reading
        per account per poll is a bill nobody meant to run up — so it does not
        take one unless asked. `--live` is how it is asked.
        """
        group = command.add_mutually_exclusive_group()
        group.add_argument("--no-live", dest="live", action="store_false",
                           default=None,
                           help="use the last ping's figures instead of "
                                "reading the limits from Claude now")
        group.add_argument("--live", dest="live", action="store_true",
                           default=None,
                           help="read the limits from Claude now ({})".format(
                               default_note))

    status = add("status", "What every account is doing, and which to use now.")
    status.add_argument("--json", action="store_true",
                        help="report it as JSON instead, for scripting")
    add_live(status, "the default, except with --json")

    which_ = add("which", "Say which account to use right now, and why.")
    add_live(which_, "the default")

    switching = sub.add_parser(
        "switch",
        help="Point your own Claude Code at one account's login.",
        description="Point your own Claude Code at one account's login. This is "
                    "the only command that writes to ~/.claude, and it backs up "
                    "what it replaces. Sessions already open follow it.")
    switching.add_argument("account", nargs="?", metavar="ACCOUNT",
                           help="which account (default: the one `{} which` "
                                "recommends)".format(COMMAND))
    switching.add_argument("--no-sign-in", dest="sign_in", action="store_false",
                           help="never offer to sign in, even when no login is "
                                "parked — for scripts, where a prompt would "
                                "hang")

    add("ping", "Send one ping. Spends a little quota, and starts a new window "
                "if the last one has ended. This is what the timer runs.",
        account="optional")

    log = sub.add_parser("log", help="Show a ping log.",
                         description="Show a ping log. With no account named, "
                                     "every account's is shown together, "
                                     "oldest first.")
    log.add_argument("account", nargs="?", metavar="ACCOUNT",
                     help="which account (default: all of them, interleaved)")
    log.add_argument("-f", "--follow", action="store_true",
                     help="keep watching for new lines")
    log.add_argument("-n", "--lines", type=int, default=40,
                     metavar="N", help="how many lines to show (default 40)")

    realign = add("realign", "Show how far the windows are from evenly spaced.")
    realign.add_argument("--confirm", action="store_true",
                         help="apply the correction rather than describing it")

    add("doctor", "Check a deployed setup and say what is wrong.")
    setup_ = add("setup", "Create the ping directories, sign them in, and start "
                          "their timers. Re-runnable.")
    setup_.add_argument("--accounts", type=int, metavar="N",
                        help="how many accounts, instead of being asked")
    pinging = setup_.add_mutually_exclusive_group()
    pinging.add_argument("--no-pings", dest="pings", action="store_false",
                         default=None,
                         help="this machine only switches accounts; the pings "
                              "run elsewhere. No timers, no checkpoints, and "
                              "no quota spent here")
    pinging.add_argument("--pings", dest="pings", action="store_true",
                         default=None,
                         help="run the pings here (the default), rather than "
                              "being asked")
    setup_.add_argument("-y", "--yes", action="store_true",
                        help="take every default rather than prompting")
    add("check", "Validate the account configuration without changing anything.")
    add("accounts", "List the configured accounts.")

    add("install-command", "Rewrite the {} launcher in bin/.".format(COMMAND))
    uninstall_ = add("uninstall",
                     "Remove the timers and anything this tool added. Leaves "
                     "your own ~/.claude and every conversation alone.")
    uninstall_.add_argument("--purge", action="store_true",
                            help="also delete this directory's generated files "
                                 "(state, logs, accounts.json, schedule.json, "
                                 "bin/) — never a ping directory")

    add("init", "Build one account's checkpoint. Setup does this for you.",
        account="optional")

    # Claude Code runs this itself as the ping's status line; it is not something
    # a person ever types. Omitting help= entirely is what keeps it out of the
    # command list — argparse only lists subparsers that were given one, and
    # help=SUPPRESS would print the literal string instead.
    capture = sub.add_parser("capture-statusline")
    capture.add_argument("path", metavar="FILE")

    return parser


def known_commands(parser):
    names = set()
    for action in parser._actions:
        names.update(getattr(action, "choices", None) or {})
    return names


def normalise_help(argv, commands):
    """
    Accept the ways people actually ask for help on one command.

    The usage line reads `claude-window [-h] <command> ...`, so `-h which` is a
    perfectly reasonable thing to type. argparse answers it by printing the
    general help and silently discarding the word, which from the other side
    looks like nothing happened at all — the user asked a fair question and got
    no answer and no error.

    `help which` is the git habit and just as fair. Both are rewritten into the
    form argparse understands, rather than corrected at the person typing.
    """
    if not argv or argv[0] not in ("-h", "--help", "help"):
        return argv
    for word in argv[1:]:
        if word in commands:
            return [word, "--help"]
    return ["--help"]


def not_a_pinging_machine(clause):
    """
    Refuse an action that only makes sense where the pings run. Exit code 2.

    This machine was configured with `--no-pings`, which is a decision somebody
    made and the README leans on: a second machine pinging the same accounts
    doubles what they consume and buys nothing at all. Acting on it anyway
    because a command was typed would undo that quietly.
    """
    sys.stderr.write(
        "This machine is configured not to ping, {}.\n"
        "  The pings belong on one machine; a second one doubles what these "
        "accounts consume.\n"
        "  If this should be that machine, re-run:  ./install.sh --pings\n"
        .format(clause))
    return 2


def _selected(accounts, name):
    return find_account(accounts, name) if name else accounts[0]


def show_log(accounts, name, lines, follow):
    """Show one account's log, or every account's interleaved."""
    # A count below zero would slice from the *front* — `-n -5` printing the
    # oldest five lines of a log people read to see what just happened.
    lines = max(0, lines)
    chosen = [_selected(accounts, name)] if name else accounts
    if len(chosen) == 1 and follow:
        account = chosen[0]
        if not os.path.exists(account.log_file):
            sys.stderr.write("No log yet for account {}.\n".format(account.name))
            return 1
        try:
            subprocess.call(["tail", "-n", str(lines), "-f", account.log_file])
        except KeyboardInterrupt:
            pass
        except OSError as e:
            # Following is the one thing here that shells out. A container
            # without coreutils is a strange place to run this, but answering
            # with a traceback is stranger.
            sys.stderr.write("Could not run `tail` to follow the log: {}\n"
                             "  The log is at {}\n".format(e, account.log_file))
            return 1
        return 0

    if follow:
        sys.stderr.write("Following needs one account: try `claude-window log "
                         "{} -f`.\n".format(chosen[0].name))
        return 2

    # Merged view: the logs are per-account (rotation rewrites the whole file, so
    # one shared log could not be written safely by two processes), but reading
    # them back together is exactly what you want when comparing accounts.
    entries = []
    for account in chosen:
        try:
            with open(account.log_file) as f:
                for position, line in enumerate(f):
                    if not line.strip():
                        continue        # run separators; nothing to interleave
                    entries.append((line[:20], account.name, position,
                                    line.rstrip("\n")))
        except (IOError, OSError):
            continue
    if not entries:
        if not pings_here():
            sys.stderr.write(
                "No logs here: this machine does not ping.\n"
                "  Every ping, and every log line, is on the machine that "
                "does.\n")
            return 1
        sys.stderr.write("No logs yet — run `claude-window ping` first.\n")
        return 1
    # Timestamp first, then each file's own order. Sorting whole lines would
    # alphabetise everything that shares a second — and a run writes several
    # lines a second, so "Ping run finished" would print before the
    # "Exited with code" it followed.
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    width = max(len(a.name) for a in chosen)
    for _, name_, _, line in (entries[-lines:] if lines else []):
        print("{:<{}}  {}".format(name_, width, line) if len(chosen) > 1 else line)
    return 0


def status_json(accounts):
    now = time.time()
    states = {a.name: read_state(a) for a in accounts}
    # The same fallback the human `status` uses. Without it a script on a
    # machine that pings nothing would read every account as usable with no
    # window information -- confidently, and wrongly, which is the one thing a
    # machine-readable answer must not do.
    view = schedule_view(accounts)
    if view:
        known, published, avail, published_at = view
        states.update(published)
        for account in accounts:
            states.setdefault(account.name, {})
            avail.setdefault(account.name, UNPUBLISHED)
    else:
        known, published_at = accounts, None
        avail = availabilities(accounts, states, now)
    chosen, reason = choose_account(known, states, now, avail)
    # Reported as this machine configures it. With a view, `chosen` was built
    # from the published file and carries the *other* machine's paths, and
    # `use.account` naming something absent from `accounts` below would make
    # the two halves of one document disagree.
    chosen = next((a for a in accounts if a.name == chosen.name), chosen)
    mine = current_account(accounts)
    document = {
        "generated_at": now,
        # When the machine running the pings wrote the file these figures came
        # from, or null when they are this machine's own.
        "published_at": published_at,
        "use": {"account": chosen.name, "reason": reason,
                "config_dir": chosen.config_dir},
        # Always present, unlike the human output, which stays silent until
        # switching is set up: a shape that appears and disappears is not a
        # contract anybody can write against.
        "switching": {"configured": switching_configured(accounts),
                      "current_account": mine.name if mine else None},
        "accounts": [],
    }
    for account in accounts:
        state = states[account.name]
        expiry = next_expiry(state, now)
        usable = avail[account.name]
        document["accounts"].append({
            "name": account.name,
            "label": account.label,
            "config_dir": account.config_dir,
            "usable_now": usable.tier == USABLE,
            "tier": TIER_NAMES[usable.tier],
            "unusable_until": usable.until,
            # False where that time is an upper bound rather than a reset time
            # anybody observed — a limit reported spent without one cannot run
            # longer than its own length, and that is what is being reported.
            "unusable_until_exact": usable.exact,
            "unusable_because": usable.note or None,
            "last_run": state.get("last_run"),
            "available_at": state.get("available_at"),
            "consecutive_failures": state.get("consecutive_failures", 0),
            "expires_at": None if expiry == float("inf") else expiry,
            "rate_limits": state.get("rate_limits", {}),
            "hold": state.get("hold"),
            "boundary": state.get("boundary"),
            "boundary_label": state.get("boundary_label"),
            "limits_source": state.get("limits_source"),
            # How old the figures above are, for a caller that has to decide
            # whether to trust them — the same question the human output
            # answers in words at the bottom of `which`.
            "limits_read_at": (state.get("limits_read_at")
                               or state.get("last_run")),
            "checkpoint": _read_text(account.session_id_file).strip() or None,
        })
    json.dump(document, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def cli(argv=None):
    """
    Returns a process exit code: 0 fine, 1 something is wrong, 2 misused.

    With no command at all this reports status. It deliberately does *not* ping:
    a ping spends quota and decides when a window starts, so it has to be asked
    for by name.
    """
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    args = parser.parse_args(normalise_help(argv, known_commands(parser)))
    command = args.command or "status"

    # Must come first and stay silent: this runs inside Claude Code's UI loop,
    # which displays anything reaching stdout as the status line.
    if command == "capture-statusline":
        try:
            capture_statusline(args.path)
        except Exception:
            pass
        return 0

    # Setup writes the account list, so it must not require a valid one first.
    if command == "setup":
        return setup(argv_accounts=args.accounts, pings=args.pings,
                     assume_yes=args.yes)

    try:
        accounts = load_accounts()
        if command == "status":
            # getattr, because the bare invocation has no status subparser and
            # therefore none of its options.
            live = getattr(args, "live", None)
            if live is None:
                # Neither flag given. A person gets a reading; the bare
                # `claude-window` does not, because it is what people type
                # idly and it stays free; and `--json` does not, because a
                # scripting interface is the thing something polls in a loop.
                live = args.command is not None and not getattr(args, "json",
                                                                False)
            if live:
                refresh_limits(accounts)
            if getattr(args, "json", False):
                return status_json(accounts)
            code = status(accounts)
            if args.command is None:
                # Someone who typed the bare command has been shown one view
                # of a tool with a dozen, and nothing on screen suggests the
                # rest exist. Naming a few beats pointing at `help`, which is
                # only useful to someone who already suspects there is more.
                print()
                print("Other commands: {} — run `{} help` for all of "
                      "them.".format(
                          "which, switch, doctor, log, realign, setup"
                          if pings_here() else "which, switch, doctor, setup",
                          COMMAND))
            return code
        if command == "which":
            # A machine that pings nothing has no state of its own; a copy of
            # the pinging machine's schedule.json is all it needs to answer.
            # Asked first, because a reading taken for an answer that comes out
            # of a file is a request spent on nothing.
            view = schedule_view(accounts)
            live = getattr(args, "live", None)
            live = True if live is None else live
            if not view and live:
                refresh_limits(accounts)
            return which(*view) if view else which(accounts, live=live)
        if command == "switch":
            return switch_account(accounts, args.account, sign_in=args.sign_in)
        if command == "log":
            return show_log(accounts, args.account, args.lines, args.follow)
        if command == "realign":
            if not pings_here():
                return not_a_pinging_machine("so there is no schedule of its "
                                             "own to realign")
            return realign(accounts, confirm=args.confirm)
        if command == "doctor":
            return doctor(accounts)
        if command == "check":
            # The same split `doctor` makes. On a machine that does not ping,
            # "Account 1: ~/.claude-1 does not exist -- create it and sign in"
            # is an instruction to build the very thing it was told not to.
            return report_findings(validate_accounts(accounts)
                                   if pings_here()
                                   else switch_only_findings(accounts))
        if command == "accounts":
            for account in accounts:
                print(account.name)
            return 0
        if command == "uninstall":
            for item in uninstall(accounts, purge=args.purge):
                print("  removed {}".format(item))
            print()
            if not args.purge:
                print("Kept: this directory's state/, accounts.json, "
                      "schedule.json and bin/ — re-installing picks them up "
                      "where they left off.")
                print("Remove them too with:  {} uninstall --purge".format(
                    COMMAND))
                print()
            print("Your ~/.claude, ~/.claude.json and every conversation are "
                  "untouched.")
            # Parked logins are sign-ins the user performed, exactly like a ping
            # directory's, so an uninstaller has no business removing them —
            # and deleting the only live copy of a login is unrecoverable
            # without a browser.
            if switching_configured(accounts):
                print("Parked logins in {} are left alone; delete them "
                      "yourself if you want them gone.".format(SWITCH_ROOT))
            disposable = [a for a in accounts
                          if os.path.realpath(a.config_dir)
                          != os.path.realpath(USER_CONFIG_DIR)]
            if disposable:
                print("The ping directories are left in place; delete them "
                      "yourself if you want them gone:")
                for account in disposable:
                    print("  rm -rf {}".format(account.config_dir))
            # Never, under any circumstances, suggest deleting the directory the
            # user works in. An older layout put an account there, and that is
            # exactly when this advice would be followed and be catastrophic.
            kept = [a for a in accounts if a not in disposable]
            if kept:
                print("Account {} lives in your own {}, which is yours and is "
                      "left completely alone.".format(
                          ", ".join(a.name for a in kept), USER_CONFIG_DIR))
            return 0
        if command == "install-command":
            print("Wrote {}".format(write_entry_point()))
            offer_launcher_link()
            return 0
        if command == "init":
            if not pings_here():
                return not_a_pinging_machine("so it has no checkpoint to "
                                             "build")
            init(_selected(accounts, args.account))
            return 0
        if command == "ping":
            if not pings_here():
                return not_a_pinging_machine("and sending one by hand is "
                                             "exactly what that decision was "
                                             "about")
            ping(_selected(accounts, args.account), accounts)
            publish_schedule(accounts)
            return 0
    except ConfigError as e:
        sys.stderr.write("Configuration error: {}\n".format(e))
        return 2

    sys.stderr.write("Unknown command: {}\n".format(command))
    return 2


if __name__ == "__main__":
    sys.exit(cli())
