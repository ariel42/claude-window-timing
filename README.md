# claude-early-window

**Your Claude Code usage window starts when you send your first message — so if you start work at 9am, you wait until 2pm for a fresh one. This starts it for you at dawn.**

A small background service that keeps your 5-hour windows rolling around the clock. You sit down to an almost-untouched window, and the next one arrives sooner. With more than one Claude subscription, it spaces their windows evenly through the day so a fresh one is never far away, and tells you which account to spend next.

It does this without touching your Claude Code: no wrapper, no proxy, no shared config directory, nothing intercepted. Nothing in the background goes near `~/.claude`. One command you run by hand, `claude-window switch`, points your own Claude Code at whichever account still has quota. About 4,400 lines of Python standard library and a systemd timer — no dependencies, no daemon, and no network calls of its own.

---

## The problem

Claude Code gives you a **5-hour usage window**. The clock does not start at a fixed time of day — it starts the moment you send your first message.

Begin work at 9:00 with no earlier usage, and your window runs 9:00–14:00. Burn through it by 11:30 and you wait two and a half hours doing nothing. The window started late because *you* did, and the rest of your day inherits that: every window that day is anchored to the moment you happened to sit down.

The cost is not theoretical. It is the difference between a limit you hit at 16:00 and one you hit at 13:00, every day, for the same money.

Plenty of people work around it by hand: fire a throwaway "hi" at Claude early in the morning so the window starts then, not when they actually sit down. That works, and it is exactly as reliable as remembering to do it before coffee.

## What this does

It sends that message for you, every 30 minutes, all day and all night.

Each one is tiny — a single "bye" to a saved one-line conversation — and **they cost as close to nothing as makes no difference**, because Claude serves them from its prompt cache and [cache reads are not deducted from your rate limit](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). A week of logs: every ping served from cache, apart from a run of misses traced to the one thing that can spoil it — see [where the pings run](#where-the-pings-run).

Same morning, with it running:

| | Without | With |
|---|---|---|
| Window starts | 9:00 — when you sit down | 6:40 — a background ping started it |
| Window ends | 14:00 | 11:40 |
| Left when you start at 9:00 | all of it | almost all of it |
| Wait for a **fresh** window | 5 hours | 2 hours 40 minutes |

Same capacity, less waiting. Where the window happens to sit when you arrive is luck — sometimes it just reset, sometimes it is about to. Averaged over many days that is **about 2.5 hours of waiting instead of a flat 5**.

## What makes it different

There are three familiar ways to attack this, and this tool is none of them.

**A cron job that sends a message.** The obvious version, and the one most people reach for. It knows nothing about where your window boundary actually is, so it drifts: miss one ping — a suspended laptop, a dropped network, a limit you spent yourself — and the next window starts late, and *every* window after it inherits that late start, with nothing to pull it back. The natural way to write one is `claude --print`, which under a subscription login has a [reported problem where it can be billed as API usage](https://github.com/anthropics/claude-code/issues/43333). And nothing about it is arranged around the prompt cache, so it pays for pings that could have been free.

**A usage monitor.** Tells you how much of your window is left, which is worth knowing and completely orthogonal: it observes the window, it does not start one earlier.

**A wrapper, proxy or router that switches accounts for you.** These sit in front of the CLI and multiplex your requests. They work, at the price of putting a third party in the path of every request you make, and of a config directory that is no longer just yours. `claude-window switch` is not one of these: it hands Claude Code a different login and gets out of the way, so there is nothing left running to fail, and a failure while switching costs a backup file rather than your session.

What this one does instead:

- **It aims at the window boundary, not at the clock.** Claude Code reports exactly when your current window ends. The tool reads that, books a ping for 30 seconds after it, and so starts the next window the instant the last one closes. One correction repairs a schedule that a missed ping knocked out of step — this is the part that makes it hold up over weeks instead of days. ([Staying on schedule](#staying-on-schedule).)
- **The pings are engineered to be free.** Every ping replays one identical saved conversation from a directory whose contents never change, so Claude serves it from cache, and 30 minutes sits comfortably inside the ~1-hour cache lifetime while dividing 5 hours evenly. All three facts are load-bearing; none is a coincidence ([why 30 minutes](#why-30-minutes), [where the pings run](#where-the-pings-run)).
- **It runs several subscriptions as one supply.** Windows spaced 5/N hours apart, kept spaced automatically, and a straight answer to "which account should I use right now" that skips any account that cannot serve a request. ([More than one subscription](#more-than-one-subscription).)
- **It stays out of your Claude Code.** Its own directories, its own logins, its own conversations. Nothing that runs on a timer ever writes to `~/.claude` or `~/.claude.json`. The one thing that does is `claude-window switch`, only when you run it, to two files, after copying both somewhere safe — and a test names the single function allowed to write there and fails the day a second one appears.
- **It refuses to bill you by surprise.** Pings run in interactive mode rather than `--print`, and `ANTHROPIC_API_KEY` is stripped from the environment so a ping can never land on a pay-as-you-go account.
- **It says when it is broken.** Most failures here are silent — a timer that will never fire again, a login that expired, two accounts that are secretly the same account. `claude-window doctor` names them.
- **It is small enough to read.** Standard library only, one file, no service to trust and nothing running unless a timer fires. Every design decision that could fail quietly is written down at the point in the source where it applies.

## More than one subscription

If one subscription is not enough, the usual answer is a second. Two accounts give you twice the quota — but left alone their windows drift into whatever arrangement chance produces, and that arrangement matters more than it looks.

This spaces them evenly: two accounts land **2h30m apart**, three land 1h40m apart. In general, N accounts sit 5/N hours apart.

### Why even spacing is worth having

No arrangement creates capacity. Whatever your windows are doing, you get the same number of refills per day. What changes is the **shape** of the supply — and because unused quota expires the moment a window ends, shape is worth real money.

- **A fresh window always arrives within 5/N hours** instead of up to 5. With two accounts the worst case halves and the average wait drops from 2.5 hours to about 1.25.
- **Less quota expires unused.** When windows reset together, all your capacity shares one deadline and whatever you could not burn is gone. Staggered, the deadlines are spread out.

That holds for everyone, not just heavy users: your own throughput caps how much extra quota is worth to you, so a steadier supply is never worse than a lumpy one of the same size.

### Which account to use now

```bash
claude-window which
```

```
Use account 1 (personal)
  the only account usable right now; its window ends 2026-08-13 03:52:35 (in 4h00m00s)
  That is the window to spend; how you use the account is up to you.

  1 (personal)  usable — window ends in 4h00m00s   <- use this
  2 (work)      unusable until 2026-08-13 01:22:35 — its 5-hour limit is spent
  3 (spare)     unusable until 2026-08-14 23:52:35 — its weekly limit is spent
```

Two questions, in that order. **Can this account serve a request at all?** and only then **how soon does its window expire?** Spending the most perishable window first is the right rule — quota does not carry over — but it is exactly the wrong answer for an account Claude is about to refuse.

So an account is skipped, and told to wait, when any of this is true: its 5-hour limit is reported spent, its weekly limit is reported spent, a ping came back refused, its sign-in has expired, or it is on no paid plan. Accounts that cannot serve are ranked by when they come back, and one that needs *you* — a lapsed subscription, a login that ran out — ranks below one that will recover on its own.

One subtlety is worth stating, because it is the case a simpler tool gets wrong: a ping getting through is not proof that an account is usable. Pings are cache reads and are exempt from the rate limit, so one can sail through an account whose limit is spent and whose next real request would be refused. The reported percentages are believed over the ping.

### Switching to it

`which` names the account worth spending. This points your own Claude Code at it:

```bash
claude-window switch        # the account `which` recommends
claude-window switch 2      # or a named one
```

It moves two things together — the credential, which decides what you are billed for, and the identity block, which decides what Claude Code tells you that you are. It backs up what it replaces first. **Sessions you already have open follow it**, so there is nothing to restart.

Each account parks its login in `~/.claude-switch/<name>`, signed in once per machine. `./install.sh` walks you through it, and `switch` offers the one it needs if you skipped it — or by hand:

```bash
CLAUDE_CONFIG_DIR=~/.claude-switch/2 claude    # then /login
```

The account you are signed in as right now needs no sign-in of its own: the first switch away from it parks the login you already have.

**Give each one its own sign-in rather than a copy of an existing login.** Claude Code rotates refresh tokens strictly: the moment one holder refreshes, the token it replaced is rejected outright. A copy therefore keeps working until its own access token runs out — about eight hours — and is then signed out, far enough from the copy that nothing connects the two. Two *separate* logins to one account coexist for as long as they both live, which is why the ping directories each get their own too.

That is also why a switch **moves** a login rather than copying one: the store is a parking place, the copy in `~/.claude` is the only live one, and switching parks the outgoing login before installing the incoming one. `doctor` says so if it ever finds one login in two places.

Three things worth knowing before you rely on it:

- **It is one dial for the whole machine.** Credentials are re-read per request, so every session already open moves to the new account on its next turn — including the one you ran the command from. `/status` and `/usage` in those sessions report the new account too, so nothing is left disagreeing. That is convenient when you meant it and surprising when you did not: there is no per-session version of this that does not put software in the path of every request, which is a much worse trade.
- **The first request on the new account re-sends whatever you resume**, because the prompt cache belongs to the account you left. That is the same cost as signing out and back in by hand: roughly a percentage point of the new window per 27,000 tokens of conversation. Switch at a break, and start a fresh session where you can — a new conversation pays almost nothing, a resumed one pays for its whole history.
- **It refuses when it would not work.** No parked login, one that expired, one signed in as the wrong account, one that is a copy of a login something else is already refreshing, or an `ANTHROPIC_API_KEY`-style override that outranks the saved login entirely — each stops the switch and says which it was. There is no `--force`, because nothing it refuses would have worked.

## What it does *not* do

Worth being plain about, because it is unusual for a tool like this:

**It does not touch how you use Claude Code.** It creates its own directories — `~/.claude-1`, `~/.claude-2` — signs each in, and pings them from an empty working directory inside each. Nothing it does on a schedule reads or writes `~/.claude` or `~/.claude.json`. Your conversations, your trust decisions, your MCP servers and your settings are never touched by any of it.

**It does not switch accounts behind your back.** `claude-window which` tells you which window is most perishable and which accounts cannot be used at all; `claude-window switch` acts on that, when you type it. Nothing switches on a timer, in a hook, or in response to a limit being hit ([switching to it](#switching-to-it)).

**It does not need you to use Claude Code on the machine running it.** A usage window belongs to the *account*, server-side — not to a directory or a computer. A window started by a ping on your home server is the same window you get on your laptop. Run the service in one place and every machine benefits.

## Quick start

```bash
git clone https://github.com/ariel42/claude-early-window
cd claude-early-window
./install.sh
```

Claude Code itself must already be installed — on every machine, including the ones that only switch, and including when you only ever use it through the editor extension. The installer stops and says so if it is missing. You do **not** need to be signed in to anything first.

systemd is only needed on the machine that runs the pings, and that question is asked before it is checked, so a machine without it can still install the switcher.

The wizard asks how many accounts you have and whether this machine should run the pings, shows the directories it will create, walks you through signing in to each, creates one background conversation per account, and starts a timer for each. Re-run it any time — adding an account, or changing your mind about the pings, is just running it again. It asks only for the sign-ins that are still missing, so a re-run costs nothing you have already done.

For an unattended install: `./install.sh --accounts 2 -y`, and `--no-pings` on the machines that only switch.

Three things worth knowing:

- **Signing in sends no message to Claude**, so it starts no usage window. There is no good or bad moment, and nothing to time.
- **Sign in to each directory even if you already use that account elsewhere.** Each gets its own login rather than a copy of one, so a token refresh in a ping directory can never log you out of your own Claude Code.
- **How many sign-ins that is.** One per ping directory, plus one per account you want to switch to — except the account you are already signed in as, which needs none, because that login moves into its own store the first time you switch away from it. On a machine running the pings for two accounts, that is three; on a machine that only switches, one. If you have never signed in to Claude Code on this machine there is nothing to adopt, so it is two per account and one per account respectively — and that works: nothing here needs you to be signed in before you start. Two per account is the floor, not an accident: the ping directory refreshes that account's token every eight hours forever, your own Claude Code refreshes too, and rotation is strict — one login in both places means whichever refreshes second is signed out. Setup lists an account's sign-ins together so that a browser only has to change identity once per account, which is the part that actually costs time under SSO.

You never have to be awake at a particular hour. The service works out where each window sits and spaces them itself, holding an account back when that is what it takes.

## Checking on it

```bash
claude-window status
```

```
Claude Code Early Window — status
==================================

Use account 2 (work)
  its window ends first, 2026-08-11 13:00:00 (in 1h21m29s)

Account 1 (personal)
  Config dir    : /home/you/.claude-1
  Checkpoint    : 6f1c47a9-2d40-4e5b-9a7c-1b3e8d05f2aa
  Last ping     : 2026-08-11 11:37:26 (0h01m05s ago)
  5-hour window : 82% used, resets 2026-08-11 15:30:00 (in 3h51m29s)
  Weekly limit  : 17% used, resets 2026-08-17 10:00:00 (in 142h21m29s)
  Next start-of-window opportunity: 2026-08-11 15:30:00 (in 3h51m29s)
    set by the 5-hour window   [via statusline]
  Anchor        : none scheduled
  Next ping     : Tue 2026-08-11 12:07:13 IDT

Account 2 (work)
  ... the same again

Spacing
  Windows should sit 2h30m00s apart.
    account 1 (personal) next window starts 2026-08-11 15:30:00
    account 2 (work)   next window starts 2026-08-11 13:00:00
  Spacing is correct.
```

When something is wrong rather than merely worth knowing:

```bash
claude-window doctor
```

It checks what otherwise fails silently: accounts that are secretly the same login, a lapsed subscription, a sign-in that no longer works or is about to expire, timers that stopped, runs that started but never finished, a checkpoint that no ping can resume because it belongs to an older layout, a machine clock that disagrees with Claude's, leftover units from an older install — and the one failure specific to this design, **your own Claude Code being signed in as an account nobody is pinging**, where every other check passes while you get no benefit at all.

Every run is logged, so the log doubles as a record of your usage through the day:

```
[2026-08-11 11:37:26] Turn confirmed: cache_read=6864 cache_write=0 in=10 out=54
[2026-08-11 11:37:26] Usage: 5-hour 82% (resets 15:30:00, in 3h51m) · weekly 17% (...)
```

## Staying on schedule

This is what makes it reliable over weeks rather than days.

A window lasts 5 hours and the pings are 30 minutes apart, so a ping lands exactly on the moment each window ends and immediately starts the next. As long as that keeps happening, everything stays lined up on its own.

**Sometimes a ping does not happen.** The laptop slept, the network dropped, or you used the window up yourself and Claude refused the ping until your limit reset. When the ping at the *end* of a window is missed, the next window starts late — and stays late, because every window after it is measured from that late start.

**The fix.** Claude Code reports exactly when your current window ends. Shortly before it does, the tool books one extra ping for **30 seconds after** that moment. Because the regular 30-minute rhythm restarts from whenever the last ping happened, everything after it comes back into step. One correction and the schedule is repaired.

**Why 30 seconds late rather than exactly on time?** Early and late are not equally bad. A ping a moment *early* finds the old window still running, achieves nothing, and waits another 30 minutes. A ping a moment *late* starts the new window a few seconds late. So it deliberately aims late.

### Keeping several accounts spaced

The same mechanism does the spacing, under one constraint:

> A window starts on the first ping *after* the previous one ends, so a schedule can only ever be pushed **later**, never earlier.

Every correction therefore costs a stretch with no window running, and the tool finds the cheapest way to get everything evenly spaced. That leads somewhere slightly counterintuitive: when one account drifts 20 minutes late, it is cheaper to hold the *others* back 20 minutes than to drag the late one nearly all the way around the clock.

Corrections worth minutes simply happen. Corrections worth hours — two accounts restarting together after a long outage is the realistic case — are costed and left for you to approve:

```bash
claude-window realign            # what it would cost
claude-window realign --confirm  # do it
```

A tool other people install should not make an account unavailable for two hours on its own initiative.

### Who counts as N

The target is 5/N, and N is not how many subscriptions you own. It is how many will **start a window at their next boundary** — recomputed from observation on every ping. One test decides it: *can this account serve a request no later than the moment its current window ends?*

- **Out of 5-hour quota — still counts.** It becomes usable again at exactly its boundary, the ping 30 seconds later gets through, and its next window starts on time. Nothing was lost, so nothing needs re-spacing.
- **Weekly limit spent, subscription lapsed, sign-in expired, or silent for a whole window — does not count.** Its boundary passes with nothing getting through, so no window begins, and a slot held for a window that never starts is a hole in the rotation.

The difference is not cosmetic. Three accounts with one out of action, counted as three, are spaced 1h40m apart — which bunches the two that still supply windows into a third of the day and leaves the rest of it empty. Counted properly they sit 2h30m apart and cover it:

```
Spacing
  Windows should sit 2h30m00s apart — 2 of 3 accounts are holding a window.
    account 1 (personal) next window starts 2026-08-13 01:52:35
    account 2 (work)   next window starts 2026-08-13 04:22:35
    account 3 (spare)  not holding a window right now — its weekly limit is spent,
                       which outlasts its current window
  Spacing is correct.
```

**An account that is out of the rotation is still pinged**, on the same 30-minute schedule, and that is deliberate: an ordinary ping getting through is the only thing that ever notices an account coming back — a weekly limit resetting, a renewed subscription, a fresh login, a plan upgrade. It rejoins on the spot, and nothing is ever required of you. Because a change in the set moves the target for everyone, the set then has to hold steady for a full window before the tool acts on it — otherwise a limit spent on Friday afternoon would buy a re-space and Saturday morning would buy it back.

### Both limits have a say

Claude has a 5-hour limit *and* a separate weekly one. A ping must satisfy **both**, so the tool aims at whichever frees up last.

Beyond that, the *scheduler* never asks which limit is in the way. For deciding when the next window can start, an account is either able to serve a ping or it is not, and a spent weekly limit, a lapsed subscription, a revoked sign-in and a dead network are the same state. They also recover the same way: the ordinary pings never stop, so the first one that succeeds puts the account straight back into rotation. **Nothing is ever required of you** — including when you upgrade a plan, which is noticed within half an hour like anything else.

(The one place the distinction *is* drawn is the advice above about which account to use, where "back in 40 minutes" and "needs you to renew a subscription" are worth telling apart.)

## Several machines

A usage window belongs to the **account**, so run the service on **one** always-on machine and every other machine benefits for free. A second copy would only double the consumption for no gain.

On every other machine, install it without the pings:

```bash
./install.sh --no-pings
```

That creates no timers, builds no conversations and spends nothing. Copy `schedule.json` across from the pinging machine, and both `claude-window which` and `claude-window switch` answer from it: the pings run in one place, and every machine you actually type on follows them.

Copy `schedule.json` **before** running the wizard if you can. It is how a machine that pings nothing recognises the account you are already signed in as — and recognising it saves one browser sign-in, because that login parks itself on your first switch instead of needing one of its own. Setup says so if it cannot find the file.

`which` answers from that file alone: no timers, no logins, no network call. The file carries each window's *phase*, which does not move between windows, so even a days-old copy still names the right account — along with each account's availability, which is the part only the pinging machine can see, and which does age. `which` says how old the file is.

Answering "no pings" on a machine that has been pinging offers to stop its timers, because a second pinger doubles what those accounts consume and buys nothing. `doctor` reports the mismatch until the two agree.

## Commands

| Command | What it does |
|---|---|
| `./install.sh [--no-pings] [--accounts N] [-y]` | The setup wizard. Safe to re-run; asks only for what is missing. `--no-pings` sets a machine up to switch accounts without running the pings. |
| `claude-window` | Status. Typing it can never spend quota. |
| `claude-window status [--json]` | What every account is doing, and which to use now. |
| `claude-window which` | Just the recommendation, and what is unusable. |
| `claude-window switch [2]` | Point your own Claude Code at an account. The only command that writes to `~/.claude`. |
| `claude-window doctor` | Check the setup and say what is wrong. |
| `claude-window realign [--confirm]` | Show, then optionally apply, a spacing correction. |
| `claude-window log [2] [-f]` | A ping log, or every account's interleaved. |
| `claude-window accounts` | List the configured accounts. |
| `claude-window check` | Validate the accounts without changing anything. |
| `claude-window ping [2]` | Send one ping. This is what the timer runs. |
| `claude-window init [2]` | Build one account's checkpoint. Setup does this for you. |
| `claude-window install-command` | Rewrite the `claude-window` launcher in `bin/`. |
| `claude-window uninstall [--purge]` | Remove the timers; with `--purge`, the generated files too. |
| `./uninstall.sh [--purge]` | The same, from the shell. |

`claude-window help <command>` explains any of them. Only `switch` changes which account you use, and only when you type it.

Uninstalling stops the timers and removes every unit, and by default leaves this directory's state, checkpoints and logs alone so that re-installing carries on where it left off. `--purge` removes those too, leaving the checkout as git has it. Neither form ever deletes a ping directory or a parked login: those hold sign-ins you performed, and signing you out is not an uninstaller's business — a parked login is also the *only* copy of itself, so deleting one would cost a browser round-trip to recover.

## Files

| File | Purpose |
|---|---|
| `claude_early_window.py` | The whole tool. |
| `install.sh` / `uninstall.sh` | Prerequisite checks, then the wizard; and the teardown. |
| `test_early_window.py` | Over 720 checks. `python3 test_early_window.py`. |
| `fake_claude.py` | A stand-in CLI, so the tests never contact Claude or spend usage. |
| `accounts.example.json` | A starting point for `accounts.json`. |

Created while running (all gitignored): `accounts.json`, `state/<account>/`, `schedule.json`, `bin/claude-window`. Systemd units go to `~/.config/systemd/user/`. The account directories `~/.claude-1`, `~/.claude-2` … belong to the tool, including the empty `pingcwd` inside each that its pings run from. `~/.claude-switch/<name>` holds a parked login per account and exists only if you use `switch`, which is also the only thing that ever writes to `~/.claude` or `~/.claude.json`.

The tests cover the decisions that fail silently — which reset time to believe, how to space windows for the least dead time, whether a ping can start a window at the wrong moment, which accounts are safe to recommend, and whether anything writes where it should not — along with the words each command prints in each state it can be in, because a recommendation nobody can act on is a bug too. Switching gets the same treatment, and one test there is worth more than the rest: after a switch, no login may exist in two places at once. That is the failure that would otherwise show up as an unexplained logout eight hours later, and it is checked by counting refresh tokens across every directory involved. A full install, uninstall, purge and re-install runs end to end in a sandboxed home directory. They spend no usage: a fake CLI stands in for Claude, so all of that can be exercised with no account at all, and nothing they do touches a running install.

## Billing: subscription vs. API

Whether usage counts against your subscription or a pay-as-you-go API account is decided by **how you are signed in**, not by which mode the CLI runs in.

- This is only useful when Claude Code is signed in to a **Pro or Max subscription**. With an API key the pings would simply be billed per token.
- If `ANTHROPIC_API_KEY` is set, the CLI prefers it and bills the API account. The tool runs Claude with a clean environment that leaves that variable out.
- Pings run in interactive mode rather than `--print`, because `claude --print` under a subscription login has a reported problem where it can be billed as API usage ([anthropics/claude-code#43333](https://github.com/anthropics/claude-code/issues/43333)).

## Notes and caveats

- Not affiliated with or endorsed by Anthropic. Worth knowing what you are running: this sends one saved line to your subscription every 30 minutes, day and night, whether or not you are at the machine. Anthropic's [consumer terms](https://www.anthropic.com/legal/consumer-terms) address reaching the service by automated means; Anthropic also ships Claude Code for scripted and headless use. On the other side of the ledger, the pings create no capacity — every window holds exactly the quota you paid for, and nothing here exceeds a limit or asks for more. Running several subscriptions is a separate decision with its own considerations. Read the terms and decide for yourself.
- Pings are cheap but not free, and they also draw a little from the separate **weekly** limit — about 48 pings a day per account.
- Pings ask Claude for no thinking and never update the CLI. Thinking is billed as output and a ping's reply is discarded; an update rewrites the tool definitions that sit at the front of every cached prompt, which would make your own open sessions expensive to resume. Neither affects how you run Claude Code yourself.
- Accounts must be genuinely different Claude accounts. Signing in twice as the same one looks like it works and buys nothing; setup checks for it.
- **Do not use `/login` or `/logout` inside a ping directory.** That is how the tool knows which account it is pinging. Change accounts by editing `accounts.json` and re-running `./install.sh`.
- A booked correction does not survive a reboot. Harmless: the next ordinary ping reads the reset times again and books another.
- It relies on where Claude Code stores sessions and on the window reset time it reports. Both are internal details that a future release could change; the tests would notice, and `doctor` reports what it can verify.
- Linux only. Running the pings needs systemd; `--no-pings` does not, but switching reads the credentials file Claude Code keeps on Linux, which macOS replaces with the Keychain. macOS and Windows are not supported either way.

## Where the pings run

Each account pings from an empty directory of its own, `~/.claude-<n>/pingcwd`, and not from this checkout.

Claude Code puts the working directory's branch, working-tree status and recent commits into the **cached** part of every prompt. A ping running inside a git repository therefore loses its cache every time that repository changes — and the repository this ships from is one somebody commits to. In a week of logs that was the sole cause of every cache miss: a clean sweep of hits, broken only by the hours when commits were landing in the working directory.

An empty directory has nothing left to change, which is the whole point. The checkpoint conversation is registered against the directory it was created in and cannot be resumed from anywhere else, so if you upgrade from a version that pinged elsewhere, `./install.sh` rebuilds it — one message per account. `claude-window doctor` says so if it ever needs doing.

## Why 30 minutes

Two things decide the interval.

**Keeping pings free.** Every ping replays the identical saved conversation, so Claude serves it from cache, and cache reads are not deducted from your rate limit. The cache lasts about an hour and each ping refreshes it, so anything comfortably under an hour keeps almost every ping free.

**Landing on the boundary.** A window lasts 5 hours and a new one only starts on the first ping *after* the old one ends. 30 minutes divides 5 hours evenly, so a ping falls exactly on each boundary. An interval that does not divide evenly — 59 minutes, say — would leave nearly an hour with no window running at all.

30 minutes also means a single missed ping is not a disaster: the next attempt is half an hour away, still inside the cache lifetime.

The interval is defined once, as `INTERVAL_MIN`.

## License

[MIT](LICENSE)
