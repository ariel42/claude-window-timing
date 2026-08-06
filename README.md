# claude-early-window

A small background service that keeps your Claude Code 5-hour usage window running around the clock, so it always **starts before you do**.

The result: when you sit down to work, you are already inside a window that is almost completely unused, and the next fresh window arrives sooner — usually in about half the time you would otherwise wait.

## The problem

Claude Code gives you a **5-hour usage window**. The clock does not start at a fixed time of day — it starts the moment you send your first message.

So if you begin work at 9:00 and haven't used Claude before that, your window runs 9:00 to 14:00. If you use it all up by 11:30, you now wait until 14:00 doing nothing. The window started late because *you* started late, and the whole day is pushed back with it.

A lot of people work around this by hand: send Claude a throwaway "hi" early in the morning so the window starts then, not when they actually sit down.

## What this tool does

It sends that throwaway message for you, every 30 minutes, all day and all night.

The messages are tiny — a single "bye" to a saved one-line conversation — so they use almost none of your allowance. But they keep a window always running in the background.

Here is the same morning, with the tool running:

| | Without the tool | With the tool |
|---|---|---|
| Window starts | 9:00 — when you sit down | 6:40 — a background ping started it |
| Window ends | 14:00 | 11:40 |
| How much is left when you start at 9:00 | all of it | almost all of it — the pings used a sliver |
| How long until a **fresh** window | 5 hours | 2 hours 40 minutes |

You get the same full capacity, but you don't wait as long for the next refill. Where the window happens to be when you sit down is luck — sometimes it just reset and you have nearly 5 hours, sometimes it is about to reset and you have a few minutes. Averaged over many days it works out to roughly **2.5 hours**, versus a flat 5 hours if the window only ever started when you did.

The background conversation is never something you talk to. You open your own Claude Code sessions exactly as usual.

## Staying on schedule

This is the part that makes it reliable over weeks rather than days.

A window lasts 5 hours, and the pings are 30 minutes apart, so a ping lands exactly on the moment each window ends. That ping immediately starts the next window, with no gap. As long as that keeps happening, everything stays lined up on its own.

**Sometimes a ping doesn't happen.** Your laptop was asleep. The internet was down. Claude was having a bad day. Or you used the window up yourself, so Claude refused the ping until your limit reset.

When the ping at the end of a window is missed, the next window starts late — and it stays late. Every window after it is measured from that late start, so a single missed ping pushes your whole schedule back permanently.

**An example.** Your windows have been ending neatly at 17:00.

- Your laptop is asleep at 17:00, so no ping happens.
- It wakes at 17:20, pings, and a new window starts — 20 minutes late.
- That window now ends at 22:20 instead of 22:00. The next one ends at 03:20. The 20 minutes are gone for good, and every future window inherits the delay.

**The fix.** Claude Code knows exactly when your current window ends, and now this tool reads that. Shortly before the window is due to end, it books one extra ping for **30 seconds after** that exact moment.

So in the example above, the tool sees the window ends at 22:20, books a ping for 22:20:30, and that ping lands right on the boundary. Because the regular every-30-minutes rhythm restarts from whenever the last ping happened, everything after it is back in step: 22:50:30, 23:20:30, and so on — landing on the next boundary again. **One correction and the schedule is fixed.**

**Why 30 seconds late instead of exactly on time?** Because being early and being late are not equally bad:

- A ping that arrives a moment **too early** finds the old window still running. Nothing new starts, and the tool waits another 30 minutes. Cost: 30 minutes.
- A ping that arrives a moment **too late** starts the new window a few seconds late. Cost: a few seconds.

So it deliberately aims a little late. The extra ping only gets booked when the moment is less than 30 minutes away, because that is the point where the tool has the freshest and most accurate reading.

### There are two limits, and both have a say

Claude has a 5-hour limit *and* a separate weekly one. A ping has to get past **both**, so the tool aims at whichever of the two frees up **last**.

Almost always that is just the 5-hour window — the weekly limit is nowhere near full and simply doesn't come into it. It only starts to matter once the weekly limit is actually used up:

- **Weekly limit frees up after the window ends.** Say the window ends at 22:00 but the weekly limit doesn't reset until Sunday. A ping at 22:00 would just be refused. So the tool aims at Sunday instead.
- **Weekly limit frees up before the window ends.** Say the weekly limit resets at 14:00 but the current window runs until 18:00. A ping at 14:00 gets through, but there is already a window running, so nothing new starts. The tool aims at 18:00.

Either way: the later of the two is the first moment a ping can both get through *and* start a fresh window.

**The ordinary pings never stop.** Even while the weekly limit is refusing everything, the tool keeps trying every 30 minutes. It costs nothing to keep knocking, and it means that if the limit lifts early — because you upgraded your plan, say — the very next ping picks it straight up. You are never left waiting days for a schedule the tool decided on earlier.

## Features

- **A fresh window comes sooner** — you sit down inside an almost untouched window, and the next one arrives in about 2.5 hours on average instead of 5.
- **Self-correcting schedule** — after any missed ping, the tool books one extra ping at the exact moment a new window can start and puts the whole rhythm back on time. It accounts for both the 5-hour and the weekly limit.
- **Never gives up** — pings keep going even while a limit is refusing them, so the moment anything frees up (including an upgrade) it is picked up within 30 minutes.
- **Costs almost nothing** — every ping reuses the identical saved conversation, so Claude serves it from its cache, and cached text is not counted against your usage. Only about 60 tokens per ping actually count.
- **Small footprint** — each ping turns off all tools and MCP servers and uses the smallest model, so the request stays as light as possible.
- **Never grows** — the saved conversation is reset before every ping, so it stays exactly two messages long no matter how long the tool has been running.
- **Confirmed pings** — every run checks that Claude actually replied and writes what it cost to the log, so a failed ping is visible instead of silently assumed.
- **A usage history for free** — each ping records where both limits stood, so the log shows how your usage moved through the day, with a `!` on anything at 90% or more.
- **Subscription-safe by design** — drives the *interactive* Claude CLI, not `claude -p`. See [Billing](#billing-subscription-vs-api).
- **Leaves your setup alone** — the settings it needs are passed to the background ping only. Your own Claude Code configuration is never touched.
- **Runs as a systemd user service** — no root-owned units and no `sudo` to install or manage. `sudo` is used only, and optionally, to keep the timer running while you are logged out.
- **Self-contained** — pure Python standard library, nothing to install, no hardcoded paths.

## Requirements

- Linux with systemd
- Python 3.6 or later
- [Claude Code CLI](https://claude.ai/download), signed in to a **Pro or Max subscription** (see [Billing](#billing-subscription-vs-api))

## Quick start

```bash
git clone <repo-url>
cd claude-early-window
chmod +x install.sh uninstall.sh
./install.sh
```

`install.sh` checks the prerequisites, creates the saved conversation, and installs the timer. The first ping runs a minute later, then every 30 minutes.

If you happen to be over a limit when you install, setup stops and tells you when it resets. That is deliberate: the saved conversation is created once and reused by every future ping, so it has to be built from a real reply rather than a refusal. Run `./install.sh` again once the limit has reset.

## Checking on it

```bash
python3 claude_early_window.py --status
```

```
Claude Code Early Window — status
==================================
Checkpoint    : 9ba094bc-ab11-451f-adc1-9edd0c4d582c
Last ping     : 2026-08-06 18:26:23 (0h00m02s ago)
5-hour window : 98%! used, resets 2026-08-06 21:40:00 (in 3h13m34s)
Weekly limit  : 87% used, resets 2026-08-10 22:00:00 (in 99h33m34s)
Next start-of-window opportunity: 2026-08-06 21:40:00 (in 3h13m34s)
  set by the 5-hour window   [via statusline]
Anchor        : none scheduled
Next ping     : Thu 2026-08-06 18:56:09 IDT
```

"Next start-of-window opportunity" is the first moment a ping could both get through and start a fresh window, and it says which limit decided that. "Anchor" is the extra ping described in [Staying on schedule](#staying-on-schedule) — most of the time there is none scheduled, because most of the time the regular rhythm is already correct.

The usual systemd commands work too:

```bash
systemctl --user status claude-early-window.timer
systemctl --user list-timers claude-early-window.timer
journalctl --user -u claude-early-window.service
```

Every run is also written to `claude_early_window.log`, including where both limits stood at the time:

```
[2026-08-06 18:26:23] Turn confirmed: cache_read=9581 cache_write=6035 in=10 out=63
[2026-08-06 18:26:23] Usage: 5-hour 98%! (resets 2026-08-06 21:40:00, in 3h13m37s) · weekly 87% (resets 2026-08-10 22:00:00, in 99h33m37s)
```

Since a ping happens every 30 minutes, the log doubles as a record of how your usage moved through the day. A `!` marks a limit at 90% or more, so a nearly-exhausted one is easy to spot when scanning back. The figures come from the report the tool already needs, so writing them down costs nothing.

## Files

| File | Purpose |
|---|---|
| `claude_early_window.py` | The whole tool. `--init` sets up, `--status` reports, no arguments sends one ping. |
| `install.sh` | One-step install: checks, setup, and timer deployment. |
| `uninstall.sh` | Removes the timer and cancels anything pending. Leaves your files alone. |
| `test_early_window.py` | Tests for the scheduling logic. Run with `python3 test_early_window.py`. |

The tests cover the decisions that would otherwise fail silently — which reset time to believe, which limit sets the target, and which readings are safe to act on. They never contact Claude and never spend any of your usage.

The systemd units are installed to `~/.config/systemd/user/`.

### Files created while running (gitignored)

| File | Purpose |
|---|---|
| `early_window_session_id.txt` | Which saved conversation to reuse. |
| `early_window_checkpoint.jsonl.bak` | The saved two-message conversation, restored before every ping. |
| `early_window_state.json` | When the window resets, and whether an extra ping is booked. |
| `early_window_statusline.jsonl` | How Claude reports the window reset time. Rewritten each run. |
| `claude_early_window.log` | Rolling 48-hour log. |

## Starting over

If the saved conversation stops working, recreate it:

```bash
rm early_window_session_id.txt early_window_checkpoint.jsonl.bak
./install.sh
```

## Uninstalling

```bash
./uninstall.sh
```

To also remove the files it created:

```bash
rm -f early_window_session_id.txt early_window_checkpoint.jsonl.bak \
      early_window_state.json early_window_statusline.jsonl claude_early_window.log
```

## Billing: subscription vs. API

Whether usage counts against your subscription or your pay-as-you-go API account is decided by **how you are signed in**, not by which mode the CLI runs in:

- The tool is only useful when Claude Code is signed in to a **Pro or Max subscription**. With an API key the pings would simply be billed per token and the tool would serve no purpose.
- If `ANTHROPIC_API_KEY` is set, the CLI prefers it and bills the API account. The script runs Claude with a clean environment that leaves that variable out, keeping pings on your subscription.
- Pings run in interactive mode rather than `--print`, because `claude --print` under a subscription login has a reported problem where it can be billed as API usage ([anthropics/claude-code#43333](https://github.com/anthropics/claude-code/issues/43333)). Anthropic's announced (currently paused) change would also move `claude -p`, the Agent SDK, and GitHub Actions usage off subscription limits entirely. Interactive terminal usage stays on the subscription.

## Notes and caveats

- This project is not affiliated with or endorsed by Anthropic. Running an automated background process against a subscription around the clock may conflict with Anthropic's terms of service; use it at your own discretion.
- Pings are cheap but not free. They also draw a small amount from the separate **weekly** limit (about 48 pings a day).
- If you hit your weekly limit, pings keep being sent and keep being refused until it resets. That is deliberate — see [There are two limits](#there-are-two-limits-and-both-have-a-say).
- A booked extra ping does not survive a reboot. That is harmless: after a restart the next ordinary ping reads the reset times again and books a new one if needed.
- The tool relies on where Claude Code stores its sessions (`~/.claude/projects/`) and on the window reset time it reports. Both are internal details that could change in a future release.
- Tested on Linux with systemd only. macOS and Windows are not supported.

## Why 30 minutes

Two things decide the interval.

**Keeping pings free.** Every ping reuses the identical saved conversation, so Claude recognises it and serves it from its cache. Cached text is not counted against your limit:

> Cache reads are not deducted from your rate limit. — [Anthropic documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

The cache only lasts about an hour, though, and each ping refreshes it. Anything comfortably under an hour keeps every ping free. Go over an hour and the cache lapses, and each ping suddenly costs its full size.

**Landing on the boundary.** A window lasts 5 hours, and a new one only starts on the first ping *after* the old one ends. 30 minutes goes into 5 hours a whole number of times, so a ping falls exactly on the end of each window and the next one starts immediately, with no dead gap in between. An interval that doesn't divide evenly — 59 minutes, say — would leave a stretch of nearly an hour with no window running at all, which is exactly when you might sit down.

30 minutes also means a single missed ping is not a disaster: the next attempt is only half an hour away, still inside the cache lifetime. The cost is about 48 tiny pings a day instead of 24.

The interval is defined in one place — `INTERVAL_MIN` in `claude_early_window.py` — and `install.sh` reads it from there when writing the timer.

## License

[MIT](LICENSE)
