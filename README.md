# claude-early-window

**Your Claude Code usage window starts when you send your first message — so if you start work at 9am, you wait until 2pm for a fresh one. This starts it for you at dawn.**

A small background service that keeps your 5-hour windows rolling around the clock. You sit down to an almost-untouched window, and the next one arrives sooner. With more than one Claude subscription, it spaces their windows evenly through the day so a fresh one is never far away.

No dependencies, no daemon, no network calls of its own. About 3,000 lines of Python standard library and a systemd timer.

---

## The problem

Claude Code gives you a **5-hour usage window**. The clock does not start at a fixed time of day — it starts the moment you send your first message.

Begin work at 9:00 with no earlier usage, and your window runs 9:00–14:00. Burn through it by 11:30 and you wait two and a half hours doing nothing. The window started late because *you* did, and the rest of your day inherits that.

Plenty of people work around this by hand: fire a throwaway "hi" at Claude early in the morning so the window starts then, not when they actually sit down.

## What this does

It sends that message for you, every 30 minutes, all day and all night.

Each one is tiny — a single "bye" to a saved one-line conversation — and **about 85% of them cost nothing at all**, because Claude serves them from its prompt cache and [cache reads are not deducted from your rate limit](https://platform.claude.com/docs/en/build-with-claude/prompt-caching). Measured over two days of real use: roughly 75,000 tokens per account for the ones that missed.

Same morning, with it running:

| | Without | With |
|---|---|---|
| Window starts | 9:00 — when you sit down | 6:40 — a background ping started it |
| Window ends | 14:00 | 11:40 |
| Left when you start at 9:00 | all of it | almost all of it |
| Wait for a **fresh** window | 5 hours | 2 hours 40 minutes |

Same capacity, less waiting. Where the window happens to sit when you arrive is luck — sometimes it just reset, sometimes it is about to. Averaged over many days that is **about 2.5 hours of waiting instead of a flat 5**.

## More than one subscription

If one subscription is not enough, the usual answer is a second. Two accounts give you twice the quota — but left alone their windows drift into whatever arrangement chance produces, and that arrangement matters more than it looks.

This spaces them evenly: two accounts land **2h30m apart**, three land 1h40m apart. In general, N accounts sit 5/N hours apart.

### Why even spacing is worth having

No arrangement creates capacity. Whatever your windows are doing, you get the same number of refills per day. What changes is the **shape** of the supply — and because unused quota expires the moment a window ends, shape is worth real money.

- **A fresh window always arrives within 5/N hours** instead of up to 5. With two accounts the worst case halves and the average wait drops from 2.5 hours to about 1.25.
- **Less quota expires unused.** When windows reset together, all your capacity shares one deadline and whatever you could not burn is gone. Staggered, the deadlines are spread out.

That holds for everyone, not just heavy users: your own throughput caps how much extra quota is worth to you, so a steadier supply is never worse than a lumpy one of the same size.

## What it does *not* do

Worth being plain about, because it is unusual for a tool like this:

**It does not touch how you use Claude Code.** It creates its own directories — `~/.claude-1`, `~/.claude-2` — signs each in, and pings them. It never reads or writes `~/.claude` or `~/.claude.json`. Your conversations, your trust decisions, your MCP servers and your settings are untouched, and there is a test that fails if any code goes near them.

**It does not choose an account for you.** `claude-window which` will tell you which window is most perishable. Acting on it is yours.

**It does not need you to use Claude Code on the machine running it.** A usage window belongs to the *account*, server-side — not to a directory or a computer. A window started by a ping on your home server is the same window you get on your laptop. Run the service in one place and every machine benefits.

## Quick start

```bash
git clone https://github.com/ariel42/claude-early-window
cd claude-early-window
./install.sh
```

The wizard asks how many accounts you have, shows the directories it will create, walks you through signing in to each, creates one background conversation per account, and starts a timer for each. Re-run it any time — adding or removing an account is just running it again.

Two things worth knowing:

- **Signing in sends no message to Claude**, so it starts no usage window. There is no good or bad moment, and nothing to time.
- **Sign in to each directory even if you already use that account elsewhere.** Each gets its own login rather than a copy of one, so a token refresh in a ping directory can never log you out of your own Claude Code.

You never have to be awake at a particular hour. The service works out where each window sits and spaces them itself, holding an account back when that is what it takes.

## Checking on it

```bash
claude-window status
```

```
Use account 2 (work) right now
  its window ends first, 2026-08-11 13:00:00 (in 1h21m29s)

Account 1 (personal)
  Config dir    : /home/you/.claude-1
  Last ping     : 2026-08-11 11:37:26 (0h01m05s ago)
  5-hour window : 82% used, resets 2026-08-11 15:30:00 (in 3h51m29s)
  Weekly limit  : 17% used, resets 2026-08-17 10:00:00 (in 142h21m29s)
  Next ping     : Tue 2026-08-11 12:07:13 IDT

Spacing
  Windows should sit 2h30m00s apart.
    account 1 (personal) next window starts 2026-08-11 15:30:00
    account 2 (work) next window starts 2026-08-11 13:00:00
  Spacing is correct.
```

When something is wrong rather than merely worth knowing:

```bash
claude-window doctor
```

It checks what otherwise fails silently: accounts that are secretly the same login, a lapsed subscription, a sign-in that no longer works or is about to expire, timers that stopped, runs that started but never finished, a machine clock that disagrees with Claude's, leftover units from an older install — and the one failure specific to this design, **your own Claude Code being signed in as an account nobody is pinging**, where every other check passes while you get no benefit at all.

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

### Both limits have a say

Claude has a 5-hour limit *and* a separate weekly one. A ping must satisfy **both**, so the tool aims at whichever frees up last.

Beyond that it never asks *which* limit is in the way. An account is either usable now or it is not, and a spent weekly limit, a lapsed subscription, a revoked sign-in and a dead network are the same state. They also recover the same way: the ordinary pings never stop, so the first one that succeeds puts the account straight back into rotation. **Nothing is ever required of you** — including when you upgrade a plan, which is noticed within half an hour like anything else.

## Several machines

A usage window belongs to the **account**, so run the service on **one** always-on machine and every other machine benefits for free. A second copy would only double the consumption for no gain.

Other machines need nothing at all. If you want the advice there too, copy `schedule.json` and use `claude-window which`; it carries each window's *phase*, which does not move between windows, so even a stale copy answers correctly with no network call.

## Commands

| Command | What it does |
|---|---|
| `./install.sh` | The setup wizard. Safe to re-run. |
| `claude-window` | Status. Typing it can never spend quota. |
| `claude-window status [--json]` | What every account is doing, and which to use now. |
| `claude-window which` | Just the recommendation. |
| `claude-window doctor` | Check the setup and say what is wrong. |
| `claude-window realign [--confirm]` | Show, then optionally apply, a spacing correction. |
| `claude-window log [2] [-f]` | A ping log, or every account's interleaved. |
| `claude-window accounts` | List the configured accounts. |
| `claude-window check` | Validate the accounts without changing anything. |
| `claude-window ping [2]` | Send one ping. This is what the timer runs. |
| `claude-window uninstall` | Remove the timers and anything the tool added. |
| `./uninstall.sh` | The same, from the shell. |

`claude-window help <command>` explains any of them. Nothing here changes which account you use.

## Files

| File | Purpose |
|---|---|
| `claude_early_window.py` | The whole tool. |
| `install.sh` / `uninstall.sh` | Prerequisite checks, then the wizard; and the teardown. |
| `test_early_window.py` | Over 400 checks. `python3 test_early_window.py`. |
| `fake_claude.py` | A stand-in CLI, so the tests never contact Claude or spend usage. |
| `accounts.example.json` | A starting point for `accounts.json`. |

Created while running (all gitignored): `accounts.json`, `state/<account>/`, `schedule.json`, `bin/claude-window`. Systemd units go to `~/.config/systemd/user/`. The account directories `~/.claude-1`, `~/.claude-2` … belong to the tool; `~/.claude` and `~/.claude.json` are never touched.

The tests cover the decisions that fail silently — which reset time to believe, how to space windows for the least dead time, whether a ping can start a window at the wrong moment, and whether anything writes where it should not. They spend no usage: a fake CLI stands in for Claude, so a full install can be exercised end to end with no account at all.

## Billing: subscription vs. API

Whether usage counts against your subscription or a pay-as-you-go API account is decided by **how you are signed in**, not by which mode the CLI runs in.

- This is only useful when Claude Code is signed in to a **Pro or Max subscription**. With an API key the pings would simply be billed per token.
- If `ANTHROPIC_API_KEY` is set, the CLI prefers it and bills the API account. The tool runs Claude with a clean environment that leaves that variable out.
- Pings run in interactive mode rather than `--print`, because `claude --print` under a subscription login has a reported problem where it can be billed as API usage ([anthropics/claude-code#43333](https://github.com/anthropics/claude-code/issues/43333)).

## Notes and caveats

- Not affiliated with or endorsed by Anthropic. Running an automated background process against a subscription around the clock may conflict with Anthropic's terms of service, and using several subscriptions to raise your own ceiling is at best a grey area. Both are your call; this makes no claim that either is permitted.
- Pings are cheap but not free, and they also draw a little from the separate **weekly** limit — about 48 pings a day per account.
- Accounts must be genuinely different Claude accounts. Signing in twice as the same one looks like it works and buys nothing; setup checks for it.
- **Do not use `/login` or `/logout` inside a ping directory.** That is how the tool knows which account it is pinging. Change accounts by editing `accounts.json` and re-running `./install.sh`.
- A booked correction does not survive a reboot. Harmless: the next ordinary ping reads the reset times again and books another.
- It relies on where Claude Code stores sessions and on the window reset time it reports. Both are internal details that a future release could change; the tests would notice, and `doctor` reports what it can verify.
- Linux with systemd only. macOS and Windows are not supported.

## Why 30 minutes

Two things decide the interval.

**Keeping pings free.** Every ping replays the identical saved conversation, so Claude serves it from cache, and cache reads are not deducted from your rate limit. The cache lasts about an hour and each ping refreshes it, so anything comfortably under an hour keeps almost every ping free.

**Landing on the boundary.** A window lasts 5 hours and a new one only starts on the first ping *after* the old one ends. 30 minutes divides 5 hours evenly, so a ping falls exactly on each boundary. An interval that does not divide evenly — 59 minutes, say — would leave nearly an hour with no window running at all.

30 minutes also means a single missed ping is not a disaster: the next attempt is half an hour away, still inside the cache lifetime.

The interval is defined once, as `INTERVAL_MIN`.

## License

[MIT](LICENSE)
