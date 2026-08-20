"""
Claude Code Early Window
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
run `python3 claude_early_window.py <command>` directly.
"""

import argparse
import collections
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
        return "claude-early-window@{}.service".format(self.name)

    @property
    def timer_unit(self):
        return "claude-early-window@{}.timer".format(self.name)

    @property
    def anchor_unit(self):
        # Deliberately not templated: this is created on demand by systemd-run as
        # a transient unit, and a transient name containing "@" reads as an
        # instance of a template that does not exist.
        return "claude-early-window-anchor-{}".format(self.name)

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
        if not os.path.isdir(self.state_dir):
            os.makedirs(self.state_dir, 0o700)

    def __repr__(self):
        return "<Account {} at {}>".format(self.name, self.config_dir)


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
        tmp = account.config_json + ".tmp"
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
# Upgrading from the single-account layout
# ---------------------------------------------------------------------------
#
# The repository *is* the deployment: the units run the checked-out script in
# place, so a `git pull` swaps the code under a running service with no restart
# and no chance to say anything first. An upgrade that expected the user to move
# files by hand would therefore find its state already gone — the next ping would
# see no checkpoint, build a fresh one, and restart the window phase it had spent
# days getting right.
#
# So this runs itself, before anything reads state, and it moves rather than
# copies: leaving both copies would make it ambiguous which one is authoritative.

_LEGACY_FILES = (
    ("early_window_session_id.txt",       "session_id.txt"),
    ("early_window_checkpoint.jsonl.bak", "checkpoint.jsonl.bak"),
    ("early_window_state.json",           "state.json"),
    ("early_window_statusline.jsonl",     "statusline.jsonl"),
    ("claude_early_window.log",           "ping.log"),
)


def migrate_legacy_state(account):
    """
    Move a pre-multi-account install's files into the first account's state dir.

    Returns what moved. Does nothing if the account already has a checkpoint —
    the existing one always wins, so this can never overwrite a working setup,
    and running it repeatedly is safe.
    """
    if os.path.exists(account.session_id_file):
        return []
    moved = []
    for old_name, new_name in _LEGACY_FILES:
        old = os.path.join(SCRIPT_DIR, old_name)
        if not os.path.exists(old):
            continue
        account.ensure_state_dir()
        shutil.move(old, os.path.join(account.state_dir, new_name))
        moved.append(old_name)
    return moved


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

# Ranking tiers, best first.
USABLE, WAITING, UNKNOWN, NEEDS_ACTION = range(4)

# tier: one of the above. until: when it returns, when that is knowable.
# note: why, in words a user can act on — never consulted for the ranking.
Availability = collections.namedtuple("Availability", "tier until note")

# How a tier travels to another machine. Names rather than the integers, because
# the integers are an implementation detail and a published file outlives one.
TIER_NAMES = {USABLE: "usable", WAITING: "waiting", UNKNOWN: "unknown",
              NEEDS_ACTION: "needs_action"}
TIERS_BY_NAME = {name: tier for tier, name in TIER_NAMES.items()}

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
    Every reported limit that is fully spent, as (name, when it resets).

    A reset already in the past is not evidence of anything: it describes a
    window that has since rolled over, so the percentage recorded beside it
    belongs to a limit that has already refilled.
    """
    limits = state.get("rate_limits") or {}
    spent = []
    for key, name in _LIMIT_NAMES:
        window = limits.get(key) or {}
        resets = window.get("resets_at")
        if (resets and resets > now
                and (window.get("used_percentage") or 0) >= LIMIT_SPENT_PCT):
            spent.append((name, resets))
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
        return Availability(NEEDS_ACTION, None, stuck)

    blockers = []

    # A refusal is Claude's own answer about this account, and it carries the
    # moment it stops applying. It never says which limit refused, and nothing
    # here needs to know.
    available_at = state.get("available_at")
    if available_at and available_at > now:
        blockers.append((available_at, "Claude refused the last ping"))

    for name, resets in spent_limits(state, now):
        blockers.append((resets, "its {} limit is spent".format(name)))

    if blockers:
        until, note = max(blockers)         # back only when the last one clears
        return Availability(WAITING, until, note)

    if state.get("consecutive_failures", 0) >= UNHEALTHY_AFTER:
        return Availability(UNKNOWN, None, "its pings keep failing — cannot tell")

    return Availability(USABLE, None, "")


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
        return best, "{}; back {} (in {})".format(
            chosen.note, fmt_time(chosen.until), fmt_delta(chosen.until - now))

    expiry = next_expiry(state, now)
    if expiry == float("inf"):
        return best, "no window information yet — run a ping first"
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
        return "unusable until {} — {}".format(fmt_time(avail.until), avail.note)
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
            "unusable_because": usable.note or None,
            "used_percentage": five.get("used_percentage"),
            "last_run": state.get("last_run"),
        })

    document = {"written_at": now, "window_hours": WINDOW_HOURS,
                "accounts": entry}
    tmp = SCHEDULE_FILE + ".tmp"
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

    published, states, avail = [], {}, {}
    for index, entry in enumerate(document["accounts"]):
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        account = Account(str(entry["name"]), entry.get("config_dir"), index,
                          entry.get("label") or "")
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
        avail[account.name] = Availability(
            tier, entry.get("unusable_until"),
            entry.get("unusable_because") or "")

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
    if avail.tier == WAITING and avail.until > boundary:
        return False, "{}, which outlasts its current window".format(avail.note)

    # Proof of life, and the reason a phase can be believed at all. A phase says
    # where this account's boundary falls; that claim is only as good as the last
    # ping that got an answer, because once a boundary has passed with nothing
    # getting through, the next window began at some unobserved moment and the
    # recorded phase is fiction. `available_at` is written on every ping that
    # succeeded, so a value older than a whole window means exactly that.
    proven = state.get("available_at")
    if not proven or proven <= now - WINDOW_HOURS * 3600:
        return False, "no ping has got through for a whole window"

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
        tmp = ALIGNMENT_FILE + ".tmp"
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
                lines.append("  account {:<10} is not: {}".format(
                    account.display, why))
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
            lines.append("  account {:<10} not holding a window right now — "
                         "{}".format(account.display,
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
        lines.append("  account {:<10} next window starts {}{}".format(
            account.display, fmt_time(boundary), note))

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
PING_MARKER_ENV = "CLAUDE_EARLY_WINDOW_PING"


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

def log(account, msg):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {msg}"
    account.ensure_state_dir()
    with open(account.log_file, "a") as f:
        f.write(line + "\n")
    print(line)


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
    tmp = account.state_file + ".tmp"
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
    Why `new` cannot be believed given `previous`, or None if it can.

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
    """
    if not previous:
        return None
    for key, name in _LIMIT_NAMES:
        was = (previous.get(key) or {}).get("resets_at")
        now_says = (new.get(key) or {}).get("resets_at")
        if not was or not now_says:
            continue
        if now_says > was and was > now + ROLLOVER_SLACK_SEC:
            return ("{} claims to reset at {} but the reset already known, {}, "
                    "has not passed yet".format(name, fmt_time(now_says),
                                                fmt_time(was)))
    return None


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


def schedule_anchor(account, target_epoch):
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
    cancel_anchor(account)
    stamp = datetime.fromtimestamp(target_epoch).strftime("%Y-%m-%d %H:%M:%S")
    result = _run(
        ["systemd-run", "--user", "--collect",
         "--unit", account.anchor_unit,
         "--description",
         "Claude Early Window — window-boundary anchor for account "
         + account.name,
         "--on-calendar", stamp,
         "--timer-property=AccuracySec=1s",
         "systemctl", "--user", "start", "--no-block", account.service_unit])
    if result.returncode != 0:
        log(account, "WARNING: could not schedule anchor: {}".format(
            (result.stdout or "").strip()))
        return False
    log(account, "Anchor scheduled for {} (in {})".format(
        stamp, fmt_delta(target_epoch - time.time())))
    return True


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
        log(account, f"ERROR: Claude CLI not found at {CLAUDE_PATH}. "
            "Install Claude Code from https://claude.ai/download")
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
# Early-window run (called by the systemd timer on each interval)
# ---------------------------------------------------------------------------

def ping(account, accounts=None):
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

    log(account, "Starting early-window run for account {}...".format(
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
        log(account, "WARNING: early-window run did not confirm a completed turn.")

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
        why = implausible_limits(limits, state.get("rate_limits"), time.time())
        if why:
            log(account, "Ignoring this run's usage report: {}. Keeping the "
                         "previous figures.".format(why))
            limits = state.get("rate_limits") or {}
        else:
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

    log(account, "Early-window run finished.\n")


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

    print("Claude Code Early Window — status")
    print("=" * 34)
    print()

    avail = availabilities(accounts, states, now)
    chosen, reason = choose_account(accounts, states, now, avail)
    print(headline(chosen, avail[chosen.name], len(accounts)))
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
            print("Your Claude Code  : account {} — the one to spend".format(
                mine.display))
        else:
            print("Your Claude Code  : account {}".format(mine.display))
            print("                    `{} switch` moves it to account "
                  "{}".format(COMMAND, chosen.display))

    for account in accounts:
        state = states[account.name]
        print()
        print("Account {}".format(account.display))
        print("  Config dir    : {}".format(account.config_dir))

        installed = (os.path.exists(account.session_id_file)
                     and os.path.exists(account.checkpoint_backup))
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
            if not window.get("resets_at"):
                continue
            print("  {:<14}: {} used, resets {} (in {})".format(
                "5-hour window" if key == "five_hour" else "Weekly limit",
                fmt_pct(window.get("used_percentage")),
                fmt_time(window["resets_at"]),
                fmt_delta(window["resets_at"] - now)))

        usable = avail[account.name]
        if usable.tier == WAITING:
            print("  Usable again  : {} (in {}) — {}".format(
                fmt_time(usable.until), fmt_delta(usable.until - now), usable.note))
        elif usable.tier != USABLE:
            print("  Usable        : no — {}".format(usable.note))

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
                print("  Next ping     : {}".format(" ".join(line.split()[:4])))

    if len(accounts) > 1:
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

def doctor(accounts):
    # A machine that only switches has no ping directories, no checkpoints and
    # no timers. Reporting all three as broken would bury the one finding that
    # matters there -- the state of the parked logins -- under six errors
    # describing a machine this was never meant to be.
    pings = pings_here()
    if not pings:
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
        order = {"error": 0, "warning": 1}
        return report_findings(sorted(findings, key=lambda f: order.get(f.level, 2)))

    findings = validate_accounts(accounts)
    now = time.time()

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
        if (enabled.stdout or "").strip() != "enabled":
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

    findings.extend(_user_account_findings(accounts))
    findings.extend(switch_findings(accounts))
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
    after its unit file is gone. It cannot run, but it is the first thing anyone
    diagnosing this will trip over, so say what it is rather than leave them to
    wonder.
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
                "Probably an earlier or hand-rolled version. It cannot run, but "
                "it will confuse the next person to look. Clear it with: "
                "systemctl --user reset-failed {}".format(unit)))
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
        "You are getting no early-window benefit from this tool. " + fix)]


def _log_run_counts(account):
    try:
        with open(account.log_file) as f:
            body = f.read()
    except (IOError, OSError):
        return 0, 0
    return (body.count("Starting early-window run"),
            body.count("Early-window run finished"))


def _read_text(path):
    try:
        with open(path) as f:
            return f.read()
    except (IOError, OSError):
        return ""


def which(accounts, states=None, avail=None, published_at=None):
    """
    Say which account to use, and why.

    Advice only: nothing here switches accounts, and running it can never spend
    quota. `states` and `avail` arrive filled in when the answer is coming from
    a schedule published by another machine rather than from this one's own
    state files — see `schedule_view`.
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

    # Everything above was computed from the last ping. Say so when that was long
    # enough ago to be a different story — an answer this confident should not
    # come from readings nobody has refreshed since the timer stopped.
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
# Generated by claude-early-window. Put this directory on your PATH.
exec {python} {script} "$@"
'''


def write_entry_point():
    """
    Write bin/claude-window, so the tool is a command rather than a path.

    A directory of its own rather than a symlink into ~/.local/bin: the
    launcher embeds the absolute path of this checkout, so it belongs beside
    the checkout and goes away with `uninstall --purge`. One PATH entry is the
    whole installation.
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
#     the only live one, and a switch *parks the outgoing login before installing
#     the incoming one*. That is why this is a move and not a copy, and it is the
#     single most important thing in this section.
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
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w") as f:
        f.write(body)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


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
    directory = os.path.join(SWITCH_ROOT, ".backups", stamp)
    if not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
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


def _prune_backups():
    """Keep the most recent SWITCH_BACKUPS_KEPT, oldest first out."""
    root = os.path.join(SWITCH_ROOT, ".backups")
    try:
        stamps = sorted(d for d in os.listdir(root)
                        if os.path.isdir(os.path.join(root, d)))
    except (IOError, OSError):
        return
    for stamp in stamps[:max(0, len(stamps) - SWITCH_BACKUPS_KEPT)]:
        shutil.rmtree(os.path.join(root, stamp), ignore_errors=True)


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
    if not os.path.isdir(store.config_dir):
        os.makedirs(store.config_dir, 0o700)
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
    with open(credentials_path(store)) as f:
        _write_atomically(credentials_path(user_login()), f.read())

    config = _read_json(USER_CONFIG_JSON)
    identity = (_read_json(store.config_json).get("oauthAccount") or {})
    if identity:
        config["oauthAccount"] = identity
    dropped = []
    for key in ACCOUNT_SCOPED_KEYS:
        if key in config:
            del config[key]
            dropped.append(key)
    _write_atomically(USER_CONFIG_JSON,
                      json.dumps(config, indent=2, sort_keys=True),
                      _existing_mode(USER_CONFIG_JSON))

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
    if not os.path.isdir(store.config_dir):
        os.makedirs(store.config_dir, 0o700)
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


def switch_blockers(accounts, account, usable=None):
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

    want = account_identity(account)["account_uuid"]
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
    if parked_grant and parked_grant == login_fingerprint(account):
        findings.append(Finding(
            "error",
            "Account {}'s parked login is the same login its pings use, not a "
            "separate one".format(account.display),
            "Refresh tokens rotate, so the two would take turns invalidating "
            "each other and one of them would be signed out. Sign in again so "
            "the store holds its own: {}".format(sign_in_command(store))))

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
            "It comes back {}. Switching now is harmless; the first request "
            "before then is simply refused.".format(fmt_time(usable.until))))
    elif usable.tier == NEEDS_ACTION:
        findings.append(Finding(
            "warning",
            "Account {} needs attention — {}".format(account.display, usable.note),
            "Run `{} doctor`. Switching to it now will not get you a working "
            "session.".format(COMMAND)))

    return findings


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
        try:
            account = find_account(accounts, chosen.name)
        except ConfigError:
            # The schedule knows an account this machine's accounts.json does
            # not. Its name is all the switch needs.
            account = chosen

    current = current_account(accounts)
    if current is not None and current.name == account.name:
        print("Your Claude Code is already signed in as account {}.".format(
            account.display))
        return 0

    # Before judging: the one missing piece a person can supply right now.
    store = switch_store(account)
    if (sign_in and not os.path.exists(credentials_path(store))
            and sys.stdin.isatty() and not ASSUME_YES):
        offer_sign_in(account, store)

    findings = switch_blockers(accounts, account, avail.get(account.name))
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

    # Read before anything is overwritten: after install_login the outgoing
    # identity is gone from ~/.claude.json, and it is the only way to name the
    # login for someone whose Claude Code was signed in by hand.
    outgoing = account_identity(user_login())["email"]

    taken = take_login()
    backup = backup_user_login()
    dropped = install_login(switch_store(account))
    parked = park_login(current, taken) if current is not None else None

    print("Switched your Claude Code to account {}.".format(account.display))
    if parked is not None:
        print("  Parked account {} in {}".format(current.display,
                                                 parked.config_dir))
    elif backup:
        # Refusing would be worse: it would leave someone stuck behind a login
        # this tool cannot name. Saying exactly where it went is enough.
        print("  The login that was here{} is not one of the configured "
              "accounts, so it was not parked.".format(
                  " ({})".format(outgoing) if outgoing else ""))
        print("  It is in the backup below, and nowhere else.")
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
    if pings:
        for account in accounts:
            if not account_identity(account)["has_token"]:
                needed.append(("pings for account {}".format(account.display),
                               account, account.config_dir))
    mine = current_account(accounts)
    for account in accounts:
        store = switch_store(account)
        if account_identity(store)["has_token"]:
            continue
        if mine is not None and mine.name == account.name:
            continue
        needed.append(("switching to account {}".format(account.display),
                       account, store.config_dir))
    return needed


def setup(argv_accounts=None, pings=None, assume_yes=False):
    """The whole first-run experience. Safe to re-run at any time."""
    global ASSUME_YES
    ASSUME_YES = assume_yes
    print("Claude Code Early Window — setup")
    print("=" * 32)
    print()

    try:
        existing = load_accounts()
    except ConfigError:
        existing = default_accounts()
    configured = os.path.exists(ACCOUNTS_FILE)

    if configured:
        print("Currently configured:")
        for account in existing:
            print("  account {:<10} {}".format(account.display, account.config_dir))
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
        print("Beyond three accounts this stops being the cheaper option: four")
        print("Pro subscriptions cost about the same as one Max plan, which")
        print("gives you a single pool instead of four you cannot combine.")
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

    print()
    print("Layout:")
    for account in accounts:
        print("  account {:<10} {}".format(
            account.name,
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

    # Must happen before the checkpoint check below, or an upgrade would look
    # like a fresh install and spend a ping rebuilding what it already has.
    moved = migrate_legacy_state(accounts[0])
    if moved:
        print("Moved {} from the previous single-account layout into {}".format(
            ", ".join(moved), accounts[0].state_dir))

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
        for what, _account, directory in needed:
            print()
            print("  For {}:".format(what))
            print("    CLAUDE_CONFIG_DIR={} claude    then /login".format(
                directory))
        print()
        print("That is {} browser sign-in{}, once on this machine.".format(
            len(needed), "" if len(needed) == 1 else "s"))
        print("Any you skip can be done later; `{} switch` offers the one it"
              .format(COMMAND))
        print("needs, when it needs it.")
        print()
        _ask("Press Enter when you are done.")

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
    write_entry_point()
    print("Wrote {}".format(os.path.join(BIN_DIR, COMMAND)))
    print("  put it on your PATH with:  "
          "export PATH=\"{}:$PATH\"".format(BIN_DIR))

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
    """Keep the accounts already configured, add or drop to reach `count`."""
    accounts = []
    for index in range(count):
        if index < len(existing):
            source = existing[index]
            accounts.append(Account(source.name, source.config_dir, index,
                                    source.label))
            continue
        name = str(index + 1)
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
    tmp = ACCOUNTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(document, f, indent=2)
        f.write("\n")
    os.replace(tmp, ACCOUNTS_FILE)


def _tilde(path):
    return "~" + path[len(HOME):] if path.startswith(HOME + os.sep) else path


UNIT_DIR = os.path.join(HOME, ".config", "systemd", "user")

_SERVICE_UNIT = """[Unit]
Description=Claude Code Early Window — ping for account %i

[Service]
Type=oneshot
WorkingDirectory={script_dir}
ExecStart={python} {script} ping %i
# PATH so the script can locate the claude CLI; HOME comes from the user manager.
Environment=PATH={home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

[Install]
WantedBy=default.target
"""

_TIMER_UNIT = """[Unit]
Description=Claude Code Early Window — ping account %i every {interval} minutes

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


def installed_instances():
    """
    Account names that currently have a timer enabled.

    Read from timers.target.wants rather than asked of systemd: `enable` works
    by putting a symlink there, so it is the authoritative record and it still
    answers when the user manager is not reachable.
    """
    wants = os.path.join(UNIT_DIR, "timers.target.wants")
    pattern = re.compile(r"^claude-early-window@(.+)\.timer$")
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
    for legacy in ("claude-early-window.timer", "claude-early-window.service"):
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
        _systemctl("disable", "--now", "claude-early-window@{}.timer".format(name))
        _systemctl("stop", "claude-early-window-anchor-{}.timer".format(name))
        _systemctl("stop", "claude-early-window-anchor-{}.service".format(name))
        _systemctl("reset-failed",
                   "claude-early-window@{}.service".format(name),
                   "claude-early-window-anchor-{}.timer".format(name))
        drop_in = os.path.join(UNIT_DIR, "claude-early-window@{}.timer.d".format(name))
        if os.path.isdir(drop_in):
            shutil.rmtree(drop_in)
        print("Stopped the timer for account {}, which is no longer "
              "configured".format(name))

    script = os.path.abspath(__file__)
    with open(os.path.join(UNIT_DIR, "claude-early-window@.service"), "w") as f:
        f.write(_SERVICE_UNIT.format(script_dir=SCRIPT_DIR, script=script,
                                     python=sys.executable or "/usr/bin/python3",
                                     home=HOME))
    with open(os.path.join(UNIT_DIR, "claude-early-window@.timer"), "w") as f:
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
    `claude-early-window@2.timer` is never a file on disk and checking for one
    finds nothing on a machine that is pinging perfectly well.
    """
    try:
        return sorted(f for f in os.listdir(UNIT_DIR)
                      if f.startswith("claude-early-window"))
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

    for name in ("claude-early-window@.service", "claude-early-window@.timer",
                 "claude-early-window.service", "claude-early-window.timer"):
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

    status = add("status", "What every account is doing, and which to use now.")
    status.add_argument("--json", action="store_true",
                        help="report it as JSON instead, for scripting")

    add("which", "Say which account to use right now, and why.")

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


def _selected(accounts, name):
    return find_account(accounts, name) if name else accounts[0]


def show_log(accounts, name, lines, follow):
    """Show one account's log, or every account's interleaved."""
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
        sys.stderr.write("No logs yet — run `claude-window ping` first.\n")
        return 1
    # Timestamp first, then each file's own order. Sorting whole lines would
    # alphabetise everything that shares a second — and a run writes several
    # lines a second, so "Early-window run finished" would print before the
    # "Exited with code" it followed.
    entries.sort(key=lambda entry: (entry[0], entry[1], entry[2]))
    width = max(len(a.name) for a in chosen)
    for _, name_, _, line in entries[-lines:]:
        print("{:<{}}  {}".format(name_, width, line) if len(chosen) > 1 else line)
    return 0


def status_json(accounts):
    now = time.time()
    states = {a.name: read_state(a) for a in accounts}
    avail = availabilities(accounts, states, now)
    chosen, reason = choose_account(accounts, states, now, avail)
    mine = current_account(accounts)
    document = {
        "generated_at": now,
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
        # Before anything reads state: an upgrade must not look like a fresh
        # install, or it would throw away a checkpoint that still works.
        moved = migrate_legacy_state(accounts[0])
        if moved:
            sys.stderr.write(
                "Moved {} from the previous single-account layout into "
                "{}\n".format(", ".join(moved), accounts[0].state_dir))

        if command == "status":
            # getattr, because the bare invocation has no status subparser and
            # therefore none of its options.
            if getattr(args, "json", False):
                return status_json(accounts)
            code = status(accounts)
            if args.command is None:
                # Someone who typed the bare command has been shown one view
                # of a tool with a dozen, and nothing on screen suggests the
                # rest exist. Naming a few beats pointing at `help`, which is
                # only useful to someone who already suspects there is more.
                print()
                print("Other commands: which, doctor, log, realign, setup — "
                      "run `{} help` for all of them.".format(COMMAND))
            return code
        if command == "which":
            # A machine that pings nothing has no state of its own; a copy of
            # the pinging machine's schedule.json is all it needs to answer.
            view = schedule_view(accounts)
            return which(*view) if view else which(accounts)
        if command == "switch":
            return switch_account(accounts, args.account, sign_in=args.sign_in)
        if command == "log":
            return show_log(accounts, args.account, args.lines, args.follow)
        if command == "realign":
            return realign(accounts, confirm=args.confirm)
        if command == "doctor":
            return doctor(accounts)
        if command == "check":
            return report_findings(validate_accounts(accounts))
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
            path = write_entry_point()
            print("Wrote {}".format(path))
            print("Put it on your PATH:  export PATH=\"{}:$PATH\"".format(
                BIN_DIR))
            return 0
        if command == "init":
            init(_selected(accounts, args.account))
            return 0
        if command == "ping":
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
