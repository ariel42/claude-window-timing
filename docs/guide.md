# claude-window-timing — user guide

Installing it, using it day to day, and everything worth knowing before you rely on it. For the design and the reasoning behind it, see [how it works](how-it-works.md).

- [Before you install](#before-you-install)
- [Installing](#installing)
- [Checking on it](#checking-on-it)
- [Which account to use now](#which-account-to-use-now)
- [Switching accounts](#switching-accounts)
- [When something is wrong](#when-something-is-wrong)
- [Several machines](#several-machines)
- [Commands](#commands)
- [Uninstalling](#uninstalling)
- [Files](#files)
- [Billing: subscription vs. API](#billing-subscription-vs-api)
- [What it does not do](#what-it-does-not-do)
- [Limitations and caveats](#limitations-and-caveats)

## Before you install

Two things to decide before you run anything, because both are easier to weigh now than later.

**This sends automated requests to your subscription, around the clock.** One saved line per account every 30 minutes, day and night, whether or not you are at the machine — roughly 340 requests per account per week. Anthropic's [consumer terms](https://www.anthropic.com/legal/consumer-terms) address reaching the service by automated means, and Anthropic also ships Claude Code for scripted and headless use; the pings create no capacity, exceed no limit and ask for nothing extra. Where that leaves you is a judgement this tool cannot make for you. **Read the terms and decide.** If you would rather not, there is nothing here for you, and that is a reasonable conclusion. This project is not affiliated with or endorsed by Anthropic, and running several subscriptions is a separate decision with its own considerations.

**The pings belong on exactly one machine.** A second machine pinging the same accounts doubles what they consume and buys nothing at all. Install with `./install.sh --no-pings` everywhere else — those machines can still switch accounts and read the figures. If it happens anyway, `claude-window doctor` notices and names the machine — provided the two have exchanged a `schedule.json`, which is the normal way a second machine is set up. Two installs that have never seen each other's files have nothing to compare, and nothing here can see the other one; that case is yours to keep track of.

## Installing

**Requirements**

- **Linux.** Running the pings needs systemd with a user session; a machine that only switches does not.
- **Claude Code**, installed on every machine — including the ones that only switch, and including when you only ever use it through an editor extension. The installer stops and says so if it is missing.
- **Python 3.** Standard library only; there is nothing to `pip install`.
- **One or more Claude Pro or Max subscriptions.** You do *not* need to be signed in to anything first.

```bash
git clone https://github.com/ariel42/claude-window-timing
cd claude-window-timing
./install.sh
```

The wizard asks how many accounts you have and whether this machine should run the pings, shows the directories it will create, walks you through signing in to each, creates one background conversation per account, starts a timer for each, and makes `claude-window` typeable — with one symlink into a directory your PATH already holds, so it works in the shell you installed from as well as in every new one. It asks first, and `uninstall --purge` takes it back out.

Re-run it any time — adding an account, or changing your mind about the pings, is just running it again. It asks only for the sign-ins that are still missing, so a re-run costs nothing you have already done. systemd is only needed on the machine that runs the pings, and that question is asked before it is checked, so a machine without it can still install the switcher.

For an unattended install: `./install.sh --accounts 2 -y`, and `--no-pings` on the machines that only switch.

**About the sign-ins**

- **Signing in sends no message to Claude**, so it starts no usage window. There is no good or bad moment, and nothing to time.
- **Sign in to each directory even if you already use that account elsewhere.** Each gets its own login rather than a copy of one, so a token refresh in a ping directory can never log you out of your own Claude Code.
- **How many sign-ins that is.** One per ping directory, plus one per account you want to switch to — except the account you are already signed in as, which needs none, because that login moves into its own store the first time you switch away from it. That exemption needs the tool to be able to recognise the account you are on, which it can once its ping directory is signed in or a `schedule.json` has been copied across — so on a **first** install expect two per account, and one fewer on any re-run. On a machine that only switches, it is one per account, or one fewer once the schedule is there. Two per account is the floor, not an accident: the ping directory refreshes that account's token every eight hours forever, your own Claude Code refreshes too, and rotation is strict — one login in both places means whichever refreshes second is signed out. Setup lists an account's sign-ins together so that a browser only has to change identity once per account, which is the part that actually costs time under SSO.

You never have to be awake at a particular hour. The service works out where each window sits and spaces them itself.

## Checking on it

```bash
claude-window status
```

```
Claude Code Window Timing — status
==================================

Use account 2 (work)
  its window ends first, 2026-08-11 13:00:00 IDT (in 1h28m12s)

Your Claude Code  : account 1 (personal)
                    `claude-window switch` moves it to account 2 (work)

Account 1 (personal)
  Config dir    : /home/you/.claude-1
  Checkpoint    : 6f1c47a9-2d40-4e5b-9a7c-1b3e8d05f2aa
  Last ping     : 2026-08-11 11:30:43 IDT (0h01m05s ago)
  5-hour window : 82% used, resets 2026-08-11 15:30:00 IDT (in 3h58m12s)
  Weekly limit  : 17% used, resets 2026-08-17 10:00:00 IDT (in 5d 22h28m12s)
  Next start-of-window opportunity: 2026-08-11 15:30:00 IDT (in 3h58m12s)
    set by the 5-hour window, as reported by the last ping
  Anchor        : none scheduled
  Next ping     : Tue 2026-08-11 12:00:30 IDT

Account 2 (work)
  ... the same again

Spacing
  Windows should sit 2h30m00s apart.
    account 1 (personal) next window starts 2026-08-11 15:30:00 IDT
    account 2 (work)     next window starts 2026-08-11 13:00:00 IDT
  Spacing is correct.
```

While an account is being held back to space the windows, its block says until when, and that using it before then simply opens its window early.

Every run is logged, so the log doubles as a record of your usage through the day:

```bash
claude-window log -f
```

```
[2026-08-11 11:30:43] Turn confirmed: cache_read=6864 cache_write=0 in=10 out=54
[2026-08-11 11:30:43] Usage: 5-hour 82% (resets 2026-08-11 15:30:00 IDT, in 3h59m17s) · weekly 17% (resets 2026-08-17 10:00:00 IDT, in 5d 22h29m17s)
```

## Which account to use now

```bash
claude-window which
```

```
Use account 1 (personal)
  the only account usable right now; its window ends 2026-08-13 03:30:00 IDT (in 3h41m12s)
  That is the window to spend; how you use the account is up to you.
  Point your own Claude Code at it:  claude-window switch 1

  1 (personal)  usable — 71% left, window ends in 3h41m12s   <- use this
  2 (work)      unusable until 2026-08-13 01:00:00 IDT — its 5-hour limit is spent
  3 (spare)     unusable until 2026-08-14 22:00:00 IDT — its weekly limit is spent

  Figures from a live reading, 0h00m02s ago.
```

The `switch` line appears once switching is set up, and names the account rather than making you match it up yourself. Until then `which` says nothing about switching at all.

Two questions, in that order. **Can this account serve a request at all?** and only then **how soon does its window expire?** Spending the most perishable window first is the right rule — quota does not carry over — but it is exactly the wrong answer for an account Claude is about to refuse.

How much of that window is left is shown beside it, and said again in the recommendation when there is little of it: an account with 3% remaining is still the right one to spend, since that 3% is what expires next, but being sent there without being told reads as bad advice four messages later.

So an account is skipped, and told to wait, when any of this is true: its 5-hour limit is reported spent, its weekly limit is reported spent, a ping came back refused, its sign-in has expired, or it is on no paid plan. Accounts that cannot serve are ranked by when they come back, and one that needs *you* — a lapsed subscription, a login that ran out — ranks below one that will recover on its own. An account being held back to space the windows is usable but has nothing about to expire, so it is ranked after the ones that do — and recommended when it is the only one, with a note that using it starts a fresh window now.

**How fresh those figures are.** `which` and `status` read them from Claude when you run them, because the alternative — the last ping's numbers — can be half an hour old, and the likeliest thing to have moved them since is your own work. `switch` takes one too, for the account it just moved you to, and prints what is actually left of its window.

It costs one very small request per account, and only for accounts whose window is already running. An account between windows is never probed: any billed request would *start* a window, and choosing that moment is the spacing's job — a status command has no business moving it. Those accounts keep the last ping's figure, with its age on screen. Nor is a reading taken with a login whose access token has expired: refreshing it is Claude Code's job, and it does so on its next request.

`--no-live` skips the reading — including on the bare `claude-window`, which takes one like every other human-facing form. `status --json` does not: a scripting interface is the thing something polls in a loop. Add `--live` there when you want one. A reading is reused for a minute rather than re-taken, so typing any of these repeatedly costs nothing, and putting `which` in a shell prompt or a `watch` loop cannot run up a bill. A refusal counts as an answer — an account that will not serve a request is spent, whatever the last ping believed. And if a reading cannot be taken, it says so loudly, writes it to the account's log and keeps reporting it in `doctor`, rather than quietly falling back to stale numbers.

One subtlety is worth stating, because it is the case a simpler tool gets wrong: a ping getting through is not proof that an account is usable. Pings are cache reads and are exempt from the rate limit, so one can sail through an account whose limit is spent and whose next real request would be refused. The reported percentages are believed over the ping.

## Switching accounts

`which` names the account worth spending. This points your own Claude Code at it:

```bash
claude-window switch        # the account `which` recommends
claude-window switch 2      # or a named one
```

It moves two things together — the credential, which decides what you are billed for, and the identity block, which decides what Claude Code tells you that you are. It backs up what it replaces first. **Sessions you already have open follow it**, so there is nothing to restart. Switching to an account that is being held back to space the windows opens its window on the spot.

Each account parks its login in `~/.claude-switch/<name>`, signed in once per machine. `./install.sh` walks you through it, and `switch` offers the one it needs if you skipped it — or by hand:

```bash
CLAUDE_CONFIG_DIR=~/.claude-switch/2 claude    # then /login
```

The account you are signed in as right now needs no sign-in of its own: the first switch away from it parks the login you already have.

**Give each one its own sign-in rather than a copy of an existing login.** Claude Code rotates refresh tokens strictly: the moment one holder refreshes, the token it replaced is rejected outright. A copy therefore keeps working until its own access token runs out — about eight hours — and is then signed out, far enough from the copy that nothing connects the two. Two *separate* logins to one account coexist for as long as they both live, which is why the ping directories each get their own too.

That is also why a switch **moves** a login rather than copying one: the store is a parking place and the copy in `~/.claude` is the only live one. The outgoing login is read into memory, the incoming one installed, and only then is the outgoing one written to its store — so there is no moment when one grant sits in two directories, not even if the machine dies in the middle. `doctor` says so if it ever finds one login in two places. A parked login that has already expired, found in the way of the one being parked, is moved to `~/.claude-switch/.orphaned/` rather than blocking the switch; one that is still live stops it.

Three things worth knowing before you rely on it:

- **It is one dial for the whole machine.** Credentials are re-read per request, so every session already open moves to the new account on its next turn — including the one you ran the command from. `/status` and `/usage` in those sessions report the new account too, so nothing is left disagreeing. That is convenient when you meant it and surprising when you did not: there is no per-session version of this that does not put software in the path of every request, which is a much worse trade.
- **The first request on the new account re-sends whatever you resume**, because the prompt cache belongs to the account you left. That is the same cost as signing out and back in by hand: roughly a percentage point of the new window per 27,000 tokens of conversation. Switch at a break, and start a fresh session where you can — a new conversation pays almost nothing, a resumed one pays for its whole history.
- **It refuses when it would not work.** No parked login, one that expired, one signed in as the wrong account, one that is a copy of a login something else is already refreshing, or an `ANTHROPIC_API_KEY`-style override that outranks the saved login entirely — each stops the switch and says which it was. There is no `--force`, because nothing it refuses would have worked.

## When something is wrong

```bash
claude-window doctor
```

It checks what otherwise fails silently: accounts that are secretly the same login, a lapsed subscription, a sign-in that no longer works or is about to expire, an account Claude refuses even though its login looks valid, timers that stopped, pings that keep failing (and what Claude said), runs that started but never finished, a checkpoint that no ping can resume because it belongs to an older layout, a checkout it cannot write to, an installed timer older than the current one, leftover units from an older install — and the one failure specific to this design, **your own Claude Code being signed in as an account nobody is pinging**, where every other check passes while you get no benefit at all.

Some of them exist because the thing they catch is invisible from every other screen:

- **A second machine pinging the same accounts.** The evidence is destroyed by the act — `schedule.json` is the file a second machine copies to answer `which`, and the moment it starts pinging it overwrites that copy with its own. The sighting is taken in the instant before the overwrite and kept, so it is still there when somebody looks.
- **A boundary anchor that could not be booked**, which `status` would otherwise show exactly like a healthy "none scheduled".
- **A clock that disagrees with Claude's by more than half a minute.** The pings are wall-clock times computed from this machine's clock, so a machine two minutes fast pings two minutes *before* the boundary — inside the window still running, which opens nothing. Measured for free from the `Date` header on any live reading.
- **A reset time off Anthropic's 30-minute grid**, the assumption the whole schedule rests on.
- **A timer still firing for an account that is no longer in `accounts.json`.** Losing that one file quietly halves a two-account setup, and everything runtime here is gitignored, so a `git clean -xdf` takes it.

Three more are worth naming, because each one leaves an install that looks perfect and does nothing: **a timer that cannot find the Claude CLI** (an npm or nvm install lives where only your shell knows to look, and the timer has none of your shell), **lingering being off** (a user timer belongs to your login session, so the pings stop when you log out), and **`claude-window` not being on your PATH** (every instruction here begins with it).

A ping that fails also exits non-zero, so the account's unit shows as failed in `systemctl --user --failed` and to anything you have watching systemd.

## Several machines

A usage window belongs to the **account**, server-side, not to a directory or a computer. A window started by a ping on your home server is the same window you get on your laptop. So run the service on **one** always-on machine and every other machine benefits for free; a second copy would only double the consumption for no gain.

On every other machine, install it without the pings:

```bash
./install.sh --no-pings
```

That creates no timers, builds no conversations and spends nothing. Copy `schedule.json` across from the pinging machine, and both `claude-window which` and `claude-window switch` answer from it: the pings run in one place, and every machine you actually type on follows them.

Copy `schedule.json` **before** running the wizard if you can. It is how a machine that pings nothing recognises the account you are already signed in as — and recognising it saves one browser sign-in, because that login parks itself on your first switch instead of needing one of its own. Setup says so if it cannot find the file.

`which` answers from that file alone: no timers, no logins, no network call — a machine that does not ping never reads a limit from Claude, because it has no login of its own to ask with and the pinging machine has already looked.

**The two machines do not have to number the accounts the same way.** Which account is 1 and which is 2 falls out of the order you signed in at install time, and each machine keeps its own. Every entry in the file carries the account it belongs to, so a copy is read onto *this* machine's accounts by account rather than by slot: the names on screen are the ones this machine uses, and `which` and `switch` can never mean different accounts by the same number. The file carries each window's *phase*, which does not move between windows, so even a days-old copy still names the right account — along with each account's availability, which is the part only the pinging machine can see, and which does age. `which` says how old the file is.

Answering "no pings" on a machine that has been pinging offers to stop its timers, because a second pinger doubles what those accounts consume and buys nothing. `doctor` reports the mismatch until the two agree.

## Commands

| Command | What it does |
|---|---|
| `./install.sh [--pings \| --no-pings] [--accounts N] [--yes]` | The setup wizard. Safe to re-run; asks only for what is missing. `--no-pings` sets a machine up to switch accounts without running the pings; `--pings` turns them back on. `./install.sh --help` lists them. |
| `claude-window setup` | The same wizard, once the launcher exists. Takes the same options. |
| `claude-window` | Status. The bare form never spends anything. |
| `claude-window status [--json] [--no-live] [--live]` | What every account is doing, and which to use now. `--json` reads nothing from Claude unless you add `--live`. |
| `claude-window which [--no-live] [--live]` | Just the recommendation, and what is unusable. Reads the limits from Claude unless you pass `--no-live`. |
| `claude-window switch [2] [--no-sign-in]` | Point your own Claude Code at an account. The only command that writes to `~/.claude`. |
| `claude-window doctor` | Check the setup and say what is wrong. |
| `claude-window log [2] [-f] [-n N]` | A ping log, or every account's interleaved. |
| `claude-window accounts` | List the configured accounts, who each is, and where its login lives. |
| `claude-window check` | Validate the accounts without changing anything. |
| `claude-window ping [2]` | Run one tick: ping, or hold the account back if the spacing wants it later. This is what the timer runs. |
| `claude-window init [2]` | Build one account's checkpoint. Setup does this for you. |
| `claude-window install-command` | Rewrite the `claude-window` launcher in `bin/`, and offer to put it where your shell will find it. |
| `claude-window uninstall [--purge]` | Remove the timers; with `--purge`, the generated files too. |
| `./uninstall.sh [--purge]` | The same, from the shell. |

`claude-window help <command>` explains any of them. Only `switch` changes which account you use, and only when you type it.

## Uninstalling

```bash
claude-window uninstall          # stop and remove the timers
claude-window uninstall --purge  # ...and this checkout's generated files
```

Uninstalling stops the timers and removes every unit, and by default leaves this directory's state, checkpoints and logs alone so that re-installing carries on where it left off. `--purge` removes those too, leaving the checkout as git has it, and asks first: the checkpoints cost a real message each to rebuild. Neither form ever deletes a ping directory or a parked login: those hold sign-ins you performed, and signing you out is not an uninstaller's business — a parked login is also the *only* copy of itself, so deleting one would cost a browser round-trip to recover.

To stop pinging an account but keep the others, remove it from `accounts.json` and re-run `./install.sh`; its timer is stopped and its ping directory and parked login are kept, so adding it back later is a re-run and a sign-in.

## Files

| File | Purpose |
|---|---|
| `claude_window_timing.py` | The whole tool. |
| `install.sh` / `uninstall.sh` | Prerequisite checks, then the wizard; and the teardown. |
| `test_window_timing.py` | Over 1,700 checks. `python3 test_window_timing.py`. |
| `fake_claude.py` | A stand-in CLI, so the tests never contact Claude or spend usage. |
| `accounts.example.json` | A starting point for `accounts.json`. |

Created while running (all gitignored): `accounts.json`, `state/<account>/`, `schedule.json`, `bin/claude-window`. Systemd units go to `~/.config/systemd/user/`, and — if you accept the offer — a `claude-window` symlink to `~/.local/bin`, or failing that one marked line in your shell's startup file. Both are removed by `uninstall --purge`. The account directories `~/.claude-1`, `~/.claude-2` … belong to the tool, including the empty `pingcwd` inside each that its pings run from. `~/.claude-switch/<name>` holds a parked login per account; it appears as soon as you run the sign-in the wizard prints, alongside `.backups/` and `.orphaned/`. `switch` is the only thing that ever writes to `~/.claude` or `~/.claude.json`.

The tests cover the decisions that fail silently — which reset time to believe, how to space windows for the least dead time, whether a ping can start a window at the wrong moment, which accounts are safe to recommend, and whether anything writes where it should not — along with the words each command prints in each state it can be in, because a recommendation nobody can act on is a bug too. After a switch, no login may exist in two places at once; that is checked by counting refresh tokens across every directory involved. A full install, uninstall, purge and re-install runs end to end in a sandboxed home directory. They spend no usage: a stand-in CLI plays Claude, so all of that runs with no account and no network, and nothing they do touches a running install.

## Billing: subscription vs. API

Whether usage counts against your subscription or a pay-as-you-go API account is decided by **how you are signed in**, not by which mode the CLI runs in.

- This is only useful when Claude Code is signed in to a **Pro or Max subscription**. With an API key the pings would simply be billed per token.
- If `ANTHROPIC_API_KEY` is set, the CLI prefers it and bills the API account. The tool runs Claude with a clean environment that leaves that variable out.
- Pings run in interactive mode rather than `--print`, because `claude --print` under a subscription login has a reported problem where it can be billed as API usage ([anthropics/claude-code#43333](https://github.com/anthropics/claude-code/issues/43333)).

## What it does not do

**It does not touch how you use Claude Code.** It creates its own directories — `~/.claude-1`, `~/.claude-2` — signs each in, and pings them from an empty working directory inside each. Nothing it does on a schedule reads or writes `~/.claude` or `~/.claude.json`. Your conversations, your trust decisions, your MCP servers and your settings are never touched by any of it.

**It does not switch accounts behind your back.** `claude-window which` tells you which window is most perishable and which accounts cannot be used at all; `claude-window switch` acts on that, when you type it. Nothing switches on a timer, in a hook, or in response to a limit being hit.

**It does not need you to use Claude Code on the machine running it.** A window belongs to the account, so the service can run on a home server while you work on a laptop.

## Limitations and caveats

- **Linux only, and enforced rather than merely stated.** Running the pings needs systemd; `--no-pings` does not, but switching reads the credentials file Claude Code keeps on Linux, which macOS replaces with the Keychain — so `switch` refuses there rather than consuming a parked login to no effect. Setup on macOS or Windows warns up front that `switch` will not work, `doctor` reports it instead of saying "Everything checks out", and what does still work there — `status`, `which` and `doctor` reading a copied `schedule.json` — carries on working. Switching on macOS and Windows is wanted and not yet built.
- **Having `systemctl` is not the same as having a systemd user session.** WSL without `systemd=true`, `docker exec`, `su -` and `ssh host ./install.sh` on some distributions all ship the binary and reach no user bus. Setup checks for the manager itself and refuses rather than reporting timers it did not start.
- **Best on an always-on machine.** The pings do not catch up after downtime: a machine that slept pings at the next half hour and is immediately back in step, but windows due while it slept started late or not at all, and with several subscriptions the first boundaries afterwards are spent re-spacing them.
- **Accounts must be genuinely different Claude accounts.** Signing in twice as the same one looks like it works and buys nothing; setup checks for it.
- **Do not point a ping directory at a *different* account with `/login`.** That directory's identity is how the tool knows which account it is pinging. Signing the *same* account in again is fine and is what `doctor` tells you to do when a login expires; changing which account lives there means editing `accounts.json` and re-running `./install.sh`.
- **A held account is not watched.** If you use an account while the spacing is holding it back, the tool learns of it at that account's next ping rather than at once; until then `which` still describes it as having no window running.
- **It relies on internal details of Claude Code** — where it stores sessions, and the reset times it reports — and on Anthropic's 30-minute reset grid. A future release could change any of them; the tests would notice, and `doctor` reports what it can verify.
