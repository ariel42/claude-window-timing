# AGENTS.md

Guidance for AI coding agents — and humans — changing this repository. What the tool does and why is in [docs/how-it-works.md](docs/how-it-works.md); how people use it is in [docs/guide.md](docs/guide.md). Read the first before changing anything in the scheduling or ping path.

## What this is

A tool that keeps Claude Code's 5-hour usage windows running on a schedule — a cache-served ping every 30 minutes, on Anthropic's own 30-minute reset grid — and, with several subscriptions, spaces their windows as evenly as the grid allows, recommends which account to spend (`which`) and moves the user's own Claude Code to it (`switch`). It never sits in the path of the user's requests. The pinger is Linux and systemd; the switcher runs on Linux and macOS.

## Layout

| Path | What it is |
|---|---|
| `claude_window_timing.py` | The whole tool: one module, Python standard library only. Sections are marked by `# ----` banners; every non-obvious decision is explained by a comment where it is made. |
| `test_window_timing.py` | The test suite: plain functions using `check()` / `check_true()`, registered in the tuple in `main()`. No pytest. |
| `fake_claude.py` | A stand-in for the Claude Code CLI (PTY session, session JSONL, statusLine, `auth status`), steered by a `.fake_claude.json` control file. |
| `fake_security.py` | A stand-in for macOS's `security`: the Keychain items, exit codes and output Claude Code and the switcher rely on, measured on a real Mac, kept in a JSON file per test home. |
| `install.sh`, `uninstall.sh` | Prerequisite checks, then `setup`; and the teardown. |
| `docs/` | The design and the user guide. `README.md` is the landing page and stays short. |

Key functions, by path through a ping: `decide_tick` (hold or ping, with its bookkeeping) → `run_interactive` → `turn_outcome` / `classify_turn` (what the run produced) → `record_ping` (what it writes into state) → `maybe_schedule_anchor`. The planner is `plan_slots`, `earliest_slot`, `spacing_plan` and `should_open_window`. A login's credential is reached only through `read_credential`, `write_credential`, `remove_credential` and `has_credential` — a file on Linux, a Keychain item (`keychain_service`) on macOS — and a switch is `switch_account` → `switch_blockers` → `_perform_switch`, which holds Claude Code's own refresh locks (`hold_claude_code_locks`) while `_move_logins` runs `take_login`, `backup_user_login`, `install_login` and `park_login`.

## Running the tests

```bash
python3 test_window_timing.py
```

Between a minute and a half and two and a half minutes, depending on the machine. It needs no Claude account, no network and no Keychain: `HOME`, the state directory and the systemd unit directory are redirected into a sandbox, the CLI is `fake_claude.py`, and `security` is `fake_security.py` — the suite never touches a real Keychain, which on a Mac holds the developer's live login. One test books real transient systemd user units under a test-only name and removes them; it is skipped where there is no user manager. It runs on Linux and macOS, and every test in `CREDENTIAL_TESTS` runs a second time playing the other platform, so each machine checks both the file and the Keychain half of the switch. A new test function must also be added to the tuple in `main()`, or it never runs — and to `CREDENTIAL_TESTS` too if it reads or moves a login.

## Rules

These hold everywhere. Each one exists because breaking it fails silently.

1. **Only `switch` writes to `~/.claude`, `~/.claude.json` or — on macOS — the Keychain item `Claude Code-credentials`**, and only when the user types it, after backing up the login and `~/.claude.json`. A test names the single function allowed to write there.
2. **Never refresh, copy or duplicate an OAuth login.** Claude Code rotates refresh tokens strictly, so a second holder of a grant is signed out within hours. A switch *moves* a login between `~/.claude` and its store in `~/.claude-switch/` — on macOS between their Keychain items; a dead one in the way is moved to `.orphaned/`, never deleted.
3. **A credential is read and written only through the credential store** (`read_credential`, `write_credential`, `remove_credential`, `has_credential`), never by opening `.credentials.json`. On macOS Claude Code reads the Keychain item first and the file only when there is none, so a file written there is a login it never sees. A failure to read is not an absence: a switch stops on it.
4. **Pings run the CLI interactively through a PTY, never with `--print`** — headless use is the part of Claude Code whose billing Anthropic has been revising (see the billing notes in `docs/guide.md`) — with `ANTHROPIC_API_KEY` stripped from the environment, no thinking, no CLI update, and from the account's empty `pingcwd` so the cached prompt never changes.
5. **Time is UTC on Anthropic's 30-minute grid** (`GRID_SEC`). The timer is `OnCalendar=*-*-* *:00,30:<second> UTC`. Do not introduce local-time scheduling or a cadence measured from the last run.
6. **A ping counts only if Claude served it.** `classify_turn` decides: a `<synthetic>` turn is a failure unless it is a rate-limit refusal, which is an answer. Never treat "a new assistant turn exists" as success.
7. **Spacing goes through `decide_tick`.** Holds are whole slots; a hold is never a lockout; the last account that can serve is never held; a hold lapses when its ticks stop; the safety valve is a bug detector, not a policy.
8. **Standard library only, one module.** No dependencies.
9. **Every command the tool prints or the docs show must parse.** A test feeds every `claude-window …` invocation in the module, `README.md`, `docs/*.md` and this file to the real argument parser.

## How to write tests

- **Tests call the real functions. Never copy implementation logic into a test.** A copy passes while the code it was copied from drifts. If a test needs a piece of logic, move that logic into the module as a function and call it from both places — that is why `decide_tick`, `turn_outcome`, `record_ping`, `failure_record` and `cyclic_gaps` exist.
- **Simulations model only Claude's side.** The `_World` class in the tests plays Claude: windows open on the grid, limits refuse, and a person can use an account — which the tool learns of only through its own next ping, as in reality. Likewise `claude_code_saves`, `claude_code_reads` and `claude_code_service` play where Claude Code keeps a login: fixtures sign in through them and invariant checks read through them, so a tool that filed a login where Claude Code does not look fails instead of agreeing with itself. The tool's side is always the real code.
- **Independent oracles are fine** when they reach the answer a different way: the brute force over every assignment that checks `plan_slots` is checking, not copying.
- **Every fix comes with a test that fails without it.** For changes to the planner, also confirm the randomized test (`test_the_spacing_survives_a_randomized_beating`) or a direct test catches the old behaviour.
- **Model real behaviour from real data.** The synthetic turns `fake_claude.py` writes were copied from real session files; do the same for anything new.

## Working on a machine with a live install

- The systemd service runs `claude_window_timing.py` from this checkout. An edit is live at the next tick, at `:00:30` and `:30:30` UTC, so keep the module importable throughout.
- `state/`, `accounts.json`, `schedule.json` and `bin/` are gitignored runtime data. Never `git clean -xdf` or `uninstall --purge` a live install.
- The suite's check that it never touches the checkout's `state/` can be tripped by a live ping firing mid-run. Re-run it clear of a tick before investigating.
- After changing the timer template, re-run `./install.sh`; `doctor` reports an installed timer older than the code.

## Style

- **Comments say why**, at the point where a decision could fail quietly. Keep that density in new code.
- **User-facing text is plain and specific**, and never raises a problem without saying what to do about it.
- **Commit messages:** a short imperative summary, then what was wrong, the evidence, and why the fix is right.
- **Docs:** keep `README.md` short; details go in `docs/`.
