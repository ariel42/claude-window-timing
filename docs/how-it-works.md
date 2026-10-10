# How claude-window-timing works

The design, the facts it rests on, and why each decision is the way it is. For installing and day-to-day use, see the [user guide](guide.md); for the code's layout and rules, see [AGENTS.md](../AGENTS.md).

## At a glance

- Claude Code's 5-hour usage window starts with the first request on an account, not at a fixed hour.
- The tool keeps a window running on every account, around the clock, by sending each one a tiny **ping** every 30 minutes: a single "bye" replayed into a saved one-line conversation, answered from Claude's prompt cache.
- Anthropic **floors every window start to a 30-minute grid** in UTC. The pings fire on the same grid — at `:00:30` and `:30:30` UTC — so each one lands just after a possible window boundary, and a missed ping never shifts the ones after it.
- With several subscriptions, windows are **spaced as evenly as the grid allows**: 2h30m apart for two, 1h40m apart on average for three, and so on. Spacing is kept by sometimes *not* opening an account's next window until the right slot. That wait is never a lockout: using the account opens its window immediately.
- Nothing the tool runs on a timer touches your own Claude Code configuration. The one command that does is `claude-window switch`, and only when you type it.

## The problem

Begin work at 9:00 with no earlier usage, and your window runs 9:00–14:00. Burn through it by 11:30 and you wait two and a half hours doing nothing. The window started late because *you* did, and the rest of your day inherits that: every window that day is anchored to the moment you happened to sit down.

The cost is not theoretical. It is the difference between a limit you hit at 16:00 and one you hit at 13:00, every day, for the same money.

Plenty of people work around it by hand: fire a throwaway "hi" at Claude early in the morning so the window starts then, not when they actually sit down. That works, and it is exactly as reliable as remembering to do it before coffee.

## The pings

The tool sends that message for you, every 30 minutes, all day and all night.

Each one is tiny — a single "bye" to a saved one-line conversation — and they cost as close to nothing as makes no difference, because Claude serves them from its prompt cache and [cache reads are not deducted from your rate limit](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

Same morning, with it running:

| | Without | With |
|---|---|---|
| Window starts | 9:00 — when you sit down | 6:30 — a background ping started it |
| Window ends | 14:00 | 11:30 |
| Left when you start at 9:00 | all of it | all of it |
| Wait for a **fresh** window | 5 hours | 2 hours 30 minutes |

Same capacity, less waiting. Where the window happens to sit when you arrive is luck — sometimes it just reset, sometimes it is about to. Averaged over many days that is **about 2.5 hours of waiting instead of a flat 5**.

That average is real even if you start work at the same hour every day: 5 does not divide 24. Each day's boundaries land an hour later than the last — 09:30 today, 10:30 tomorrow, 11:30 the day after — so the offset you meet at 9:00 walks through the whole cycle every five days rather than settling on one unlucky value.

### What a ping costs

- **Against the 5-hour limit: almost nothing.** Almost every ping is a cache read. The exceptions are small and known: once a day, around midnight, a few hundred tokens of the prompt are written afresh (most likely the date, which Claude Code puts in it); now and then the whole cache is rebuilt, a handful of times a week; and a Claude Code update rewrites it once. A miss costs what one ordinary short request costs.
- **Against the weekly limit: under 1%.** Measured on a Pro account left otherwise unused from one weekly reset to the next: about 270 pings over five and a half days, and its weekly figure read 0% at the end. Claude reports that figure in whole percentages, so this says "less than one point a week", not "nothing".
- Pings ask Claude for no thinking and never update the CLI. Thinking is billed as output and a ping's reply is discarded; an update rewrites the tool definitions that sit at the front of every cached prompt, which would make your own open sessions expensive to resume.

### Where the pings run

Each account pings from an empty directory of its own, `~/.claude-<n>/pingcwd`, and not from this checkout.

Claude Code puts the working directory's branch, working-tree status and recent commits into the **cached** part of every prompt. A ping running inside a git repository therefore loses its cache every time that repository changes. In the logs kept while developing this, that was the sole cause of routine cache misses: a clean sweep of hits, broken only by the hours when commits were landing in the working directory.

An empty directory has nothing left to change, which is the whole point. The checkpoint conversation is registered against the directory it was created in and cannot be resumed from anywhere else, so if you upgrade from a version that pinged elsewhere, `./install.sh` rebuilds it — one message per account. `claude-window doctor` says so if it ever needs doing.

### What counts as a ping that got through

Claude Code records every failed request as an ordinary-looking assistant turn of its own making — for a rate-limit refusal, and equally for an expired login, an organisation that has turned subscription access off (which is how a lapsed subscription answers), a billing failure or an overloaded server. Those turns are marked `model: "<synthetic>"` and `isApiErrorMessage`, and bill nothing.

The tool tells them apart. A rate-limit refusal is still an answer: the account is reachable, a limit is spent, and the refusal says when it lifts. Anything else synthetic is a ping that did not happen — it counts as a failure, is never taken as proof that the account is alive, and makes the run exit non-zero so systemd marks the unit failed. An error that waiting will not fix (a sign-in problem, a lapsed plan, billing) takes the account out of the rotation at once and is reported by `doctor` in Claude's own words.

## Anthropic's 30-minute grid

Anthropic snaps every 5-hour window to a 30-minute grid, flooring the start: a ping at 15:30:44 opens a window that reports resetting at 20:30:00, not 20:30:44. The grid is in UTC — every reset is a whole number of half hours since the epoch.

Two consequences shape everything else:

1. **A window's phase can only be one of ten values** in a 5-hour cycle. Exact 5/N spacing is reachable only when 5/N hours is a whole number of half hours — for two accounts and for five.
2. **Opening a window part-way through a grid cell throws away the rest of the cell**, because the window is dated from the cell's start.

This is an observation about someone else's service rather than a documented guarantee, so it is checked rather than assumed: every distinct reset the tool is told about is compared against the grid, the tally is kept, and `doctor` reports a miss. Over 150 real resets, across two accounts, had all landed on the grid when this was written.

## Staying on schedule

A window lasts 5 hours and the pings are 30 minutes apart, so a ping lands exactly on the moment each window ends and immediately starts the next. As long as that keeps happening, everything stays lined up on its own.

**Sometimes a ping does not happen.** The machine slept, the network dropped, or you used the window up yourself and Claude refused the ping until your limit reset. A window that should have started then starts at the next ping instead.

**Why that does not compound.** The pings are on the clock, not on a stopwatch: `:00:30` and `:30:30` UTC, five seconds apart per account so that several do not start at once. Every boundary is on the same grid, so every boundary is a moment the timer was going to fire at anyway. A missed ping costs one window a late start of at most half an hour and moves nothing after it — there is no drifting cadence to repair, because there is no cadence, only a clock.

**Why UTC.** In local time, a clock in a time zone whose offset ends in :45 would fire a quarter of an hour into every grid cell, and on the night the clocks go back it would skip two ticks.

**Why 30 seconds late rather than exactly on time.** Early and late are not equally bad. A ping a moment *early* finds the old window still running, achieves nothing, and waits another 30 minutes. A ping a moment *late* starts the new window a few seconds late. So it deliberately aims late.

For a boundary that somehow lands off the grid, the tool still books one extra ping 30 seconds after the moment Claude reported — a one-shot systemd unit, called the *anchor*. It is the safety net for the grid assumption rather than the thing the schedule depends on. An install whose timer predates the UTC calendar keeps booking anchors for every boundary until `./install.sh` rewrites its units.

The pings do not catch up after downtime. A machine that was asleep pings at the next half hour and is immediately back in step; nothing is replayed for the time it was off, which is right — a missed ping is a missed ping, and the one after it lands on the boundary anyway.

### Why 30 minutes

- **Keeping pings free.** The prompt cache lasts about an hour and each ping refreshes it, so anything comfortably under an hour keeps almost every ping a cache read.
- **Landing on the boundary.** A new window only starts on the first ping *after* the old one ends. 30 minutes divides 5 hours evenly, so a ping falls exactly on each boundary.
- **Landing on Anthropic's grid.** This is what makes 30 the only sensible answer rather than one of several. An interval that does not divide 30 walks across the grid: 25 minutes, say, would take six different positions in the cell and throw away an average of 12 minutes of any window it opened, while costing 20% more pings.

The interval is defined once in the source, as `INTERVAL_MIN`, and the grid beside it as `GRID_SEC`.

## More than one subscription

Two accounts give you twice the quota — but left alone their windows drift into whatever arrangement chance produces, and that arrangement matters more than it looks.

The tool spaces them as evenly as they can go. A window can only start in one of ten slots of a 5-hour cycle, so "evenly" means the best arrangement of those slots — the one that makes the average wait for a fresh window shortest:

| Accounts | Windows sit | Average wait for a fresh one |
|---|---|---|
| 1 | — | 2h30m |
| 2 | 2h30m apart | 1h15m |
| 3 | 1h30m, 1h30m, 2h apart | 51m |
| 4 | 1h, 1h, 1h30m, 1h30m apart | 39m |
| 5 | 1h apart | 30m |

Three and four accounts cannot sit exactly 5/N apart — 1h40m and 1h15m are not whole slots — and the arrangement above costs one minute of average wait at three accounts, and ninety seconds at four, against an ideal no schedule can reach.

The objective behind the table: for gaps *g* between windows around the cycle, the expected wait for a fresh window from a random moment is proportional to the sum of *g*², and that sum is smallest — and the largest gap is smallest too — when the gaps are as equal as whole slots allow.

### Why even spacing is worth having

No arrangement creates capacity. Whatever your windows are doing, you get the same number of refills per day. What changes is the **shape** of the supply — and because unused quota expires the moment a window ends, shape is worth real money.

- **A fresh window arrives within about 5/N hours** instead of up to 5. With two accounts the worst case halves and the average wait drops from 2.5 hours to 1.25.
- **Less quota expires unused.** When windows reset together, all your capacity shares one deadline and whatever you could not burn is gone. Staggered, the deadlines are spread out.

That holds for everyone, not just heavy users: your own throughput caps how much extra quota is worth to you, so a steadier supply is never worse than a lumpy one of the same size.

### How the spacing is kept

A window starts on the first ping *after* the previous one ends, so the one thing the tool can do to an account's schedule is **not open its next window yet**. That is the whole mechanism. When an account's window ends, the next tick asks a single question — *is this the slot the plan wants this account's window in?* — and pings, or waits for the slot that is.

The plan comes from an exhaustive search: of every set of slots spaced as evenly as possible (at most 252 of them), the one reachable with the least total waiting, each account starting no earlier than the first slot it can actually be served in. Every wait is a whole number of slots.

**Waiting is not a lockout**, and that is what makes it safe to do unasked. A held account is fully usable: use it and its window opens there and then, with all of its quota, and the plan is worked out again from wherever things now stand. `which` tells you when the account it recommends is being held, and `switch` to a held account opens its window on the spot. Nothing is booked, nothing has to be confirmed, and there is no correction too large to make, because no correction takes anything away from you.

One limitation follows from that: a held account is neither pinged nor read, so if you use one, the tool learns of it at that account's next ping rather than at once, and plans the others around the old picture until then. It costs at most a slightly worse arrangement for a few hours, and the next correction repairs it.

**It converges, and stays put.** Every hold is a whole number of slots, so each one moves the arrangement strictly closer to the best one, and once there nothing is ever held again — so nothing you do with your accounts can move it. Only an outage, or an account joining or leaving the rotation, can. From every starting arrangement of two and three accounts, and several hundred of four, it settles within one window plus the longest hold — eight and a half hours at the very worst. A randomized run of outages, spent weekly limits and accounts used at random settles every time once things calm down.

**Three rules override the plan:**

- **Never hold the last account that can serve.** If every other account is spent until after the held account's slot, it opens now. Saving a minute of average wait is not worth leaving you with nothing.
- **A hold does not survive the machine being off.** If the held ticks stop coming, the hold is dropped and the windows are planned afresh.
- **A safety valve.** No plan holds an account for a whole window; if that ever happens it is pinged anyway, and `doctor` reports it as the bug it would be.

A fresh install is the realistic worst case: setting up each account sends it one message, so their windows all start within minutes of each other, and for the first few hours some accounts will be held at the end of a window to spread them out. Use them anyway if you need them.

### Who counts as N

N is not how many subscriptions you own. It is how many will **start a window at their next boundary** — recomputed from observation on every ping. One test decides it: *can this account serve a request no later than the moment its current window ends?*

- **Out of 5-hour quota — still counts.** It becomes usable again at exactly its boundary, the ping 30 seconds later gets through, and its next window starts on time. Nothing was lost, so nothing needs re-spacing.
- **Weekly limit spent, subscription lapsed, sign-in expired, or silent for a whole window — does not count.** Its boundary passes with nothing getting through, so no window begins, and a slot held for a window that never starts is a hole in the rotation.

The difference is not cosmetic. Three accounts with one out of action, counted as three, are spaced for three — which bunches the two that still supply windows into part of the day and leaves the rest of it empty. Counted properly they sit 2h30m apart and cover it:

```
Spacing
  Windows should sit 2h30m00s apart (2 of 3 accounts are holding a window).
    account 1 (personal) next window starts 2026-08-13 01:30:00 IDT
    account 2 (work)     next window starts 2026-08-13 04:00:00 IDT
    account 3 (spare)    not holding a window right now — its weekly limit is spent,
                         which outlasts its current window
  Spacing is correct.
```

**An account that is out of the rotation is still pinged**, on the same 30-minute schedule, and that is deliberate: an ordinary ping getting through is the only thing that ever notices an account coming back — a weekly limit resetting, a renewed subscription, a fresh login, a plan upgrade. It rejoins on the spot, and nothing is ever required of you.

### Both limits have a say

Claude has a 5-hour limit *and* a separate weekly one. A ping must satisfy **both**, so the tool aims at whichever frees up last.

Beyond that, the scheduler never asks which limit is in the way. For deciding when the next window can start, an account is either able to serve a ping or it is not, and a spent weekly limit, a lapsed subscription, a revoked sign-in and a dead network are the same state. They also recover the same way: the ordinary pings never stop, so the first one that succeeds puts the account straight back into rotation — including when you upgrade a plan, or a window ends early because of a quota reset.

The one place the distinction *is* drawn is the advice about which account to use, where "back in 40 minutes" and "needs you to renew a subscription" are worth telling apart.

## How it compares

There are three familiar ways to attack this problem, and this tool is none of them.

**A cron job that sends a message.** The obvious version. It knows nothing about where your window boundary actually is, so it drifts: miss one ping — a suspended laptop, a dropped network, a limit you spent yourself — and the next window starts late, and *every* window after it inherits that late start, with nothing to pull it back. The natural way to write one is `claude -p`, the headless mode whose billing Anthropic has been revising (see [billing](guide.md#billing-subscription-vs-api) in the user guide). And nothing about it is arranged around the prompt cache, so it pays for pings that could have been free.

**A usage monitor.** Tells you how much of your window is left, which is worth knowing and completely orthogonal: it observes the window, it does not start one earlier.

**A wrapper, proxy or router that switches accounts for you.** These sit in front of the CLI and multiplex your requests. They work, at the price of putting software in the path of every request you make, and of a config directory that is no longer just yours. `claude-window switch` hands Claude Code a different login and gets out of the way, so there is nothing left running to fail, and a failure while switching costs a backup file rather than your session.

What this one does instead:

- **It aims at the window boundary**, on the same UTC grid Anthropic uses, so a missed ping cannot drag the rest out of step.
- **The pings are engineered to be free**: one identical saved conversation, replayed from a directory whose contents never change, inside the cache lifetime.
- **It runs several subscriptions as one supply**: windows spaced as evenly as the grid allows, kept that way automatically, and a straight answer to which account to use.
- **It stays out of your Claude Code.** Its own directories, its own logins, its own conversations. Nothing that runs on a timer ever writes to `~/.claude` or `~/.claude.json`; `switch` does, only when you run it, to two files, after copying both somewhere safe — and a test names the single function allowed to write there and fails the day a second one appears.
- **It refuses to bill you by surprise.** Pings run in interactive mode rather than `--print`, and `ANTHROPIC_API_KEY` is stripped from their environment so a ping can never land on a pay-as-you-go account.
- **It says when it is broken.** Most failures here are silent — a timer that will never fire again, a login that expired, two accounts that are secretly the same account. `claude-window doctor` names them.
- **It is small enough to read.** Standard library only, one file, no service to trust and nothing running unless a timer fires. Every design decision that could fail quietly is written down at the point in the source where it applies.

## What it relies on

- **Where Claude Code stores its sessions, and the reset times it reports.** Both are internal details a future release could change. The tests would notice, and `doctor` reports what it can verify.
- **The 30-minute grid**, checked on every ping as described above.
- **systemd user timers** on the machine that pings, with lingering enabled so they keep running when you log out.
