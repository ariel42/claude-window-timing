# claude-early-window

A lightweight systemd user service that keeps your Claude Code 5-hour usage window rolling in the background. The payoff: whenever you sit down to work, you start with a **full window's capacity** already available and — on average — only about **2.5 hours** until it refreshes into the next full window. That's roughly **half** the up-to-5-hour wait you'd otherwise face when a window only starts the moment you do.

Under the hood it drives the **interactive** Claude CLI (not `claude -p`), which is what keeps each ping on your subscription's usage window rather than billing the API — see [Billing](#billing-subscription-vs-api).

## Background

Claude Code subscriptions meter usage in a rolling **5-hour window**. The window does not follow a fixed clock: it starts the moment you send your first prompt and resets five hours later. Begin work at 9 am with no prior activity and your window runs 9 am–2 pm — so you may exhaust it in the middle of your most productive hours.

A common manual workaround is to send a throwaway message earlier in the day, shifting the window — and its reset — to a more convenient time. This tool automates that idea and runs it around the clock.

Every 30 minutes it sends a tiny ping from a single reused session. The pings cost almost nothing against your usage (see [How the timing works](#how-the-timing-works)), but they keep your windows continuously rolling. As a result, when you start work:

- you are already inside a window that is **nearly untouched**, because the overnight pings consume almost none of it; and
- because windows have been resetting on a steady cadence, **a fresh window reliably resets during your working hours**, giving you more usable capacity across the day.

In practice this means you sit down with a full window's capacity already available and, on average, only about **2.5 hours** until it refreshes into the next full window — roughly **half** the 5-hour wait you'd face if the window had only started when you sat down.

The background session is never something you interact with; you open your own Claude Code sessions as usual.

## How it works

1. **Setup** creates a frozen checkpoint — a single session containing just `hi` → response — and backs up the session file in that state.
2. **Every 30 minutes** a systemd user timer runs the script, which:
   - restores the session file to the frozen checkpoint,
   - resumes the session **in interactive mode** (not `claude -p`) and sends `bye`,
   - waits for the reply, then exits.
3. Because the checkpoint is restored before every run, the session never grows: the server always sees the same two-turn conversation, so the cost of a ping stays constant no matter how long the tool has been running.

Since you sit down at an arbitrary point in this rolling cycle, the time left on the current window when you start can be anything from 0 to 5 hours — 2.5 hours is the average, not a guarantee. The window is nearly untouched either way, so its full capacity is available regardless of how much time is left before it resets.

## Features

- **Half the wait for a fresh window (the main payoff)** — you almost always sit down inside a nearly-untouched window, so full capacity is available immediately and the next reset arrives in about **2.5 hours** on average instead of 5 — roughly half the usual wait.
- **Subscription-safe by design** — drives the *interactive* Claude CLI, not `claude -p`. The print/headless path has a billing bug that can charge API rates even under a subscription ([#43333](https://github.com/anthropics/claude-code/issues/43333)), and Anthropic's announced (currently paused) change would move `claude -p`, the Agent SDK, and GitHub Actions usage off subscription limits entirely. Interactive terminal usage stays on the subscription, so the pings keep doing their job. See [Billing](#billing-subscription-vs-api).
- **Negligible usage cost** — the reused prompt is served from Anthropic's prompt cache, and cache reads are not charged against your usage window, so in practice only about 60 tokens per ping are actually counted. See [How the timing works](#how-the-timing-works).
- **Minimal footprint** — each ping disables all built-in tools (`--tools ""`) and MCP servers (`--strict-mcp-config`) and uses the smallest model (`--model haiku --effort low`), keeping the request to roughly 15K cached tokens.
- **Constant size** — a frozen checkpoint is restored before every run, so neither the on-disk session nor the context sent to the server ever accumulates.
- **Confirmed pings** — every run verifies that the turn actually completed and records its token cost (cache read vs. write) in the log, so a failed ping is visible rather than silently assumed to have worked.
- **Runs as a systemd user service** — no root-owned units, and no `sudo` to install or manage it. `sudo` is used only — and optionally — to enable linger so the timer keeps running while you are logged out.
- **Keychain-friendly** — runs inside your user session, so OAuth credentials held in the system keyring are available without extra configuration.
- **Self-contained** — pure Python standard library with no dependencies to install; all paths are derived at runtime, with nothing hardcoded.
- **Automatic log rotation** — the log is trimmed to the most recent 48 hours on each run.

## Requirements

- Linux with systemd
- Python 3.6 or later
- [Claude Code CLI](https://claude.ai/download), installed and signed in to a **Pro or Max subscription** (see [Billing](#billing-subscription-vs-api))

## Quick start

```bash
git clone <repo-url>
cd claude-early-window
chmod +x install.sh uninstall.sh
./install.sh
```

`install.sh` runs the prerequisite checks, creates the checkpoint session, and installs the systemd user timer. The timer starts immediately and fires every 30 minutes.

## Managing the service

```bash
systemctl --user status claude-early-window.timer        # status and next run time
systemctl --user list-timers claude-early-window.timer
journalctl --user -u claude-early-window.service         # service output
```

Each run is also recorded in `claude_early_window.log`.

## Files

| File | Purpose |
|---|---|
| `claude_early_window.py` | Main script. Run with `--init` for one-time setup; otherwise performs a single ping. |
| `install.sh` | One-step install: prerequisite checks, checkpoint creation, and timer deployment. |
| `uninstall.sh` | Removes the systemd user units; leaves runtime files in place. |

The systemd units are installed to `~/.config/systemd/user/` and managed with `systemctl --user`.

### Runtime files (created by install.sh, gitignored)

| File | Purpose |
|---|---|
| `early_window_session_id.txt` | UUID of the checkpoint session. |
| `early_window_checkpoint.jsonl.bak` | Backup of the frozen `hi` session state. |
| `claude_early_window.log` | Rolling 48-hour log. |

## Resetting the checkpoint

If the checkpoint session becomes invalid, recreate it:

```bash
rm early_window_session_id.txt early_window_checkpoint.jsonl.bak
./install.sh
```

## Uninstalling

```bash
./uninstall.sh
```

To also remove the runtime files:

```bash
rm -f early_window_session_id.txt early_window_checkpoint.jsonl.bak claude_early_window.log
```

## Billing: subscription vs. API

Whether usage counts against your subscription or your pay-as-you-go API account is decided by **authentication**, not by which mode the CLI runs in:

- The tool is only useful when Claude Code is signed in to a **Pro or Max subscription** (OAuth). Under API-key authentication the pings would be billed per token, and the tool serves no purpose.
- If `ANTHROPIC_API_KEY` is set, the CLI prefers it and bills the API account. The script runs Claude with a clean environment that omits `ANTHROPIC_API_KEY`, keeping pings on your subscription.
- Pings run in interactive mode rather than `--print`, because `claude --print` under OAuth has a reported issue where it can be billed as API usage ([anthropics/claude-code#43333](https://github.com/anthropics/claude-code/issues/43333)).

## Notes and caveats

- This project is not affiliated with or endorsed by Anthropic. Running an automated background process against a subscription around the clock may conflict with Anthropic's terms of service; use it at your own discretion.
- Pings are cheap but not free. They also draw a small amount from the separate **7-day** usage limit (roughly 48 pings per day).
- The tool depends on the Claude Code session-storage layout under `~/.claude/projects/`, an internal detail that could change in future CLI releases.
- Tested on Linux with systemd only; macOS and Windows are not supported.

## How the timing works

The 30-minute interval balances two goals: keeping each ping free, and minimizing the gap between consecutive windows.

**Keeping pings free.** Every ping reuses the *identical* frozen session, so the bulk of the request — roughly 15K tokens of system prompt — is byte-for-byte the same each time and is served from the prompt cache. And:

> Cache reads are not deducted from your rate limit. — [Anthropic documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)

So a cached ping costs on the order of 60 counted tokens (the new `bye` plus the short reply), with the ~15K-token prompt riding along for free. The catch is the cache lifetime: Anthropic's documented default is a 5-minute TTL with a 1-hour option (server-controlled), but in practice Claude Code sessions are cached for about an hour and each read refreshes that lifetime. Any interval comfortably under ~1 hour keeps every ping a free read.

**Minimizing the window gap.** A 5-hour window starts on your first prompt and resets exactly five hours later, and a new window only begins on the next ping *after* the previous one expires. If the interval doesn't divide evenly into five hours, that leaves a stretch with no active window — a 59-minute interval, for example, leaves a ~54-minute gap. **30 minutes divides the 5-hour window evenly**, so consecutive windows sit effectively back-to-back (a few minutes of scheduling jitter aside). That matters because it means when you sit down at a random time you almost always land *inside* an active, nearly-untouched window, with — on average — about **2.5 hours** until it resets into the next full window. (2.5 hours is the theoretical minimum for a 5-hour window with random arrival; any gap only pushes the average higher.)

A 30-minute interval also adds resilience: a single missed ping (a transient error, or the machine asleep) no longer risks a long gap or a cold cache, because the next attempt is only 30 minutes away — still within the ~1-hour cache lifetime. The cost is ~48 tiny pings per day instead of ~24, which is still negligible.

The interval is defined in one place: `INTERVAL_MIN` in `install.sh` (which sets the timer's `OnUnitActiveSec`). Each run logs `cache_read` versus `cache_write`, so you can confirm pings are being served from cache. If you'd rather halve the request count and don't mind a larger between-window gap, a value just under 60 also works (it stays within the cache lifetime); going *above* ~1 hour is the real mistake, since the cache would lapse and each ping would become a counted ~15K-token write.

## License

[MIT](LICENSE)
