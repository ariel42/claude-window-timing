# claude-window-timing

**Your Claude Code usage window — already running when you sit down.**

Claude Code's 5-hour usage window starts with your first message. Sit down at 9:00 and your next fresh window is five hours away; run out at 11:30 and you wait until 14:00. **claude-window-timing** starts your windows before you need them, keeps them on schedule around the clock, and — with more than one subscription — spaces them so a fresh window is never far away.

It runs quietly in the background on Linux, needs nothing beyond Python and systemd, and never sits between you and Claude.

[Quick start](#quick-start) · [How it works](docs/how-it-works.md) · [User guide](docs/guide.md)

## What you get

- **Half the wait for a fresh window.** You arrive to a window that is already running, so the next fresh one is on average 2½ hours away instead of a fixed 5.
- **More subscriptions, one steady supply.** Two Pro or Max accounts give you a fresh window every 2½ hours; three, every 1h40m on average; four, every 1h15m — and so on. The windows are held apart automatically, and fall back into place on their own after an outage or a busy day.
- **A straight answer to "which account now?"** `claude-window which` names the window to spend first, and skips any account that can't serve you: a spent weekly limit, a lapsed plan, an expired sign-in.
- **Switch in one command.** `claude-window switch` moves your Claude Code to that account. No logging out, no browser — and the sessions you already have open follow it.
- **Practically free.** The pings are answered from Claude's prompt cache. Measured on an otherwise idle Pro account: under 1% of its weekly limit after almost a week of pings.
- **Out of your way.** No wrapper, no proxy, nothing in the path of your requests. Nothing that runs on a timer ever touches your `~/.claude`.
- **Honest about problems.** `claude-window doctor` catches the failures that are otherwise silent — an expired sign-in, a lapsed subscription, a timer that stopped.

## The same morning

| | Without it | With it |
|---|---|---|
| Your window started | 9:00, with your first message | 6:30, with a background ping |
| You sit down at 9:00 with | a full window | a full window |
| Your next fresh window | 14:00 — five hours away | 11:30 — two and a half hours away |

Where in the window you arrive varies from day to day — sometimes it has just reset, sometimes it is about to — so the gain is an average: **2½ hours instead of 5**.

## Two or more subscriptions

| Subscriptions | A fresh window arrives | Average wait for one |
|---|---|---|
| 1 | every 5 hours | 2h 30m |
| 2 | every 2½ hours | 1h 15m |
| 3 | every 1h40m on average (1½–2 hours apart) | 51m |
| 4 | every 1h15m on average (1–1½ hours apart) | 39m |
| 5 | every hour | 30m |

The windows are kept apart without anything to approve: when an account's window ends, the tool waits for the slot that keeps the spacing before opening the next one. That wait is never a lockout — use the account and its window opens at once. ([Why these numbers](docs/how-it-works.md#more-than-one-subscription).)

When it's time to change accounts:

```
$ claude-window which
Use account 1 (personal)
  the only account usable right now; its window ends 2026-08-13 03:30:00 IDT (in 3h41m12s)
  That is the window to spend; how you use the account is up to you.
  Point your own Claude Code at it:  claude-window switch 1

  1 (personal)  usable — 71% left, window ends in 3h41m12s   <- use this
  2 (work)      unusable until 2026-08-13 01:00:00 IDT — its 5-hour limit is spent
  3 (spare)     unusable until 2026-08-14 22:00:00 IDT — its weekly limit is spent
```

## Quick start

You need Linux with systemd, [Claude Code](https://github.com/anthropics/claude-code) installed, Python 3, and one or more Claude Pro or Max subscriptions. There is nothing to `pip install`.

```bash
git clone https://github.com/ariel42/claude-window-timing
cd claude-window-timing
./install.sh
```

The setup wizard asks how many accounts you have, walks you through signing each one in, and starts a timer for each. From then on it runs by itself:

```bash
claude-window status     # what every account is doing
claude-window which      # which account to spend right now
claude-window switch     # point your Claude Code at it
claude-window doctor     # check that everything is healthy
```

Run it on one always-on machine and every machine you work on benefits: a usage window belongs to the account, not the computer. Your other machines can still `which` and `switch` — see [several machines](docs/guide.md#several-machines).

## Before you install

claude-window-timing sends a small automated request to each of your subscriptions every 30 minutes, day and night — about 340 per account per week. Anthropic's [consumer terms](https://www.anthropic.com/legal/consumer-terms) address automated access to its services; read them, and decide whether this use is right for you. This is an independent project, not affiliated with or endorsed by Anthropic. [More on this](docs/guide.md#before-you-install).

## Why not just a cron job?

- **A cron job drifts.** One missed ping — a sleeping laptop, a dropped connection — delays that window and every window after it, with nothing to pull them back. claude-window-timing aims at the window boundary itself, on the same 30-minute grid Anthropic uses, so a missed ping costs one late start and nothing more.
- **A usage monitor** tells you how much of your window is left. It can't start your next one earlier.
- **A proxy or router** puts software in the path of every request you make. `switch` hands Claude Code a different login and gets out of the way.

## Built to be trusted

- **Small and readable.** One Python file, standard library only, no daemon — just systemd timers. Every design decision is explained where it is made.
- **Thoroughly tested.** Over 1,700 automated checks, run against a stand-in CLI with no account and no network — including exhaustive and randomized simulations of the scheduling.
- **Measured, not assumed.** Anthropic's 30-minute grid was confirmed on over 150 real window resets, and the cost of the pings was measured on a real account over most of a week. [How it works](docs/how-it-works.md) explains every mechanism and what it rests on.

## Documentation

- **[How it works](docs/how-it-works.md)** — the design: the pings and the prompt cache, Anthropic's 30-minute grid, keeping windows on schedule, and spacing several subscriptions.
- **[User guide](docs/guide.md)** — installation in detail, `which` and `switch`, several machines, `doctor`, every command, uninstalling, and limitations.
- **[AGENTS.md](AGENTS.md)** — for AI coding agents and contributors: the code's layout, its invariants, and how it is tested.

## License

[MIT](LICENSE)
