# Multi-platform switcher — status, design, and what to do next

Committed on branch `macos-switcher` and kept up to date there. It is meant to be enough on its own: a new session should be able to read only this file and carry on. **Delete it, in its own commit, once the work is completely done** (section 7D). Last updated **2026-10-11**.

On a machine checked out on `main`, read it with `git fetch origin && git show origin/macos-switcher:multi-platform-switcher-plan.md`.

---

## 1. Goal

Run the **switcher** — `claude-window switch`, and the `which` / `status` / `doctor` it relies on — on **macOS and Windows**, independently of the **pinger**, which stays Linux + systemd. The intended setup: one always-on Linux machine pings; laptops on any OS only switch (`./install.sh --no-pings`) and read a `schedule.json` copied from the pinger.

## 2. Status at a glance

| | Linux | macOS | Windows |
|---|---|---|---|
| Pinger | works (live on big2t) | not supported (needs systemd) | not supported |
| `which` / `status` / `doctor` from a copied `schedule.json` | works | works | module now *imports*; never run on real Windows |
| `switch` | works; now also holds Claude Code's refresh locks | **built and tested; not yet tried with two real subscriptions** | refused with a clear message |
| Test suite | green (Ubuntu VM 2,326 checks; big2t 2,326) | green (2,316 checks) | not run |

**The one thing blocking the macOS work from going to `main`:** a real switch between two live subscriptions on the Mac (section 7A). The user has one active subscription right now (ariel42dev); aba4000's Pro lapsed on 2026-10-10. Wait for a second account, then do 7A.

## 3. Where the work is

- Branch **`macos-switcher`**, pushed to `origin` (github.com/ariel42/claude-window-timing). `main` is untouched at `7bf419a`.
- Commits on the branch, oldest first. Each one was green on the Mac, the OrbStack Ubuntu VM and big2t when made:
  1. `98d1560` Stop the suite depending on the machine it runs on — tests only; also green against `main`'s module.
  2. `47cf67d` Switch accounts on macOS, through the Keychain item Claude Code reads — the credential store, `fake_security.py`, nine tests, docs.
  3. `6c39a54` Never move a login while Claude Code is refreshing it — Claude Code's own locks (both platforms).
  4. `957fb31` Refuse a switch where Claude Code keeps its login somewhere else — `CLAUDE_CONFIG_DIR` / `CLAUDE_SECURESTORAGE_CONFIG_DIR`.
  5. `8deac58` Load the module where fcntl and pty do not exist — first step toward Windows.
  6. `b9aaaa7` Say how long the suite takes — AGENTS.md.
  7. This plan, committed so that the state of the work travels with the branch.
- The Mac checkout (`/opt/dev/claude-window-timing` on the Mac) is on `macos-switcher`. It has no install: no `accounts.json`, no `state/`, no `~/.claude-switch`.
- big2t's checkout (`/opt/dev/claude-window-timing`, user `ubuntu`) is the **live pinger on `main`**. Never switch branches there or edit it: its timer runs that code at every `:00:30`/`:30:30` UTC tick. Test branch code in a copy, `~/cwt-test` (section 6).

## 4. The design as built

A switch moves two things together between `~/.claude` and a per-account store `~/.claude-switch/<n>/`: the **credential**, which decides billing, and the **identity block** (`oauthAccount` in `~/.claude.json`), which decides what Claude Code says you are. Unchanged invariants: **move, never copy** (two holders of one grant means one is signed out within hours); a parked store holds the only copy; backups before writing; everything under a lock; refuse rather than guess (no `--force`).

**The credential store** — module section "Where Claude Code keeps a login". Every credential access goes through these functions; nothing opens `.credentials.json` directly (AGENTS.md rule 3):

- `credential_store()` returns `"keychain"` on darwin and `"file"` elsewhere. It is decided at call time, so the tests can play either platform.
- `read_credential(login)` returns the text Claude Code would use, or `None`. It **raises `CredentialStoreError` (an OSError)** when the credential cannot be read; a switch stops on that rather than treating it as absent.
- `credential_json(login)` is the forgiving parse, for describing a login; it returns `{}` on any problem.
- `has_credential(login)` reads no secret. A Keychain that cannot be asked counts as yes.
- `write_credential(login, text)` and `remove_credential(login)`. Writing replaces what was there, as the file write always did on Linux; the caller has taken the old login first.
- **macOS** — the item is named by `keychain_service(login)`: `Claude Code-credentials` for the user's own `~/.claude`, otherwise `Claude Code-credentials-` + `sha256(NFC(login.config_dir))[:8]`. The account is `keychain_account()`.
  - Writes go through `security -i` with `-X <hex>`, so the secret never appears in `ps`. Past 4032 characters it falls back to argv, as Claude Code does.
  - The text is stored in compact ASCII JSON (`keychain_form`), because `security -w` prints anything else as hex, which Claude Code cannot parse.
  - Every write is read back before the source copy is removed.
  - Reads fall back to `<dir>/.credentials.json` when there is no item, as Claude Code's do.
  - A plaintext file beside an item is cleared by `_clear_plaintext`: removed if it is a copy of a login this move has accounted for, otherwise sent to `.orphaned/`, with a printed line saying where.
  - Exit codes: 44 means not found; 36 means locked, which produces the message telling you to run `security unlock-keychain`.
- `SECURITY_PATH = "/usr/bin/security"` is a variable so that tests can point it at the stand-in.

**The switch path:**

1. `switch_account` runs the early refusals:
   - `platform_blocker()`: Linux is fine; macOS needs `/usr/bin/security`; anything else is refused.
   - `account_overrides()`, the API-key settings.
   - `config_dir_overrides()`, for `CLAUDE_CONFIG_DIR` / `CLAUDE_SECURESTORAGE_CONFIG_DIR`.
2. `repair_interrupted_switch`, then `switch_blockers`, which does a strict read of the parked login, and an optional `offer_sign_in`.
3. Under our own lock (`acquire_switch_lock`, an flock on `~/.claude-switch/.lock`), `_perform_switch` calls **`hold_claude_code_locks(user_login())`**. It takes Claude Code's own proper-lockfile directory locks:
   - `~/.claude/.oauth_refresh.lock` (abandoned after 60 s untouched);
   - `realpath(~/.claude) + ".lock"` (60 s);
   - `~/.claude/.storage-write.lock` (15 s).

   It waits up to `CLAUDE_LOCK_WAIT_SEC` = 10 s for a refresh in progress, takes over only abandoned locks, and leaves a live holder's lock alone.
4. `_move_logins` does `take_login` → `backup_user_login` (always a file) → switch marker → `install_login` → `park_login`, then **releases Claude Code's locks** before the slow part (opening a held window, the live reading).

**Elsewhere:**
- Session discovery uses `/proc` on Linux and `ps -axo pid=,uid=,comm=` on macOS (`claude_sessions_in_ps`). Pings aren't told apart on macOS; none run there.
- `orphan_login` gives each orphan its own directory, so two in the same second no longer collide.
- `doctor` (`switch_findings`) reports an unreadable Keychain once, and the config-dir variables.
- `fcntl` and `pty` imports are optional.

## 5. Facts established, and how to re-check them

Claude Code versions examined: 2.1.238 (Mac CLI, `~/.local/share/claude/versions/2.1.238`), 2.1.295 (big2t CLI), 2.1.296 (Mac VS Code extension, `~/.vscode/extensions/anthropic.claude-code-2.1.296-darwin-arm64/resources/native-binary/claude`). macOS 26.5.

The JS is embedded in the binary. Find the relevant code by searching the raw bytes in Python, as in `data.find(b'...')`. Search strings:
- `-credentials"` — the Keychain naming function sits next to `CLAUDE_SECURESTORAGE_CONFIG_DIR`;
- `name:"keychain"` — the read/write/delete commands;
- `.oauth_refresh.lock` — the refresh lock;
- `.storage-write"` — the save lock;
- `tengu_oauth_token_refresh_lock_acquiring` — the refresh flow;
- `postedRefreshToken` — the compare-and-swap save.

| Fact | Evidence |
|---|---|
| Own login: generic password, service `Claude Code-credentials`, account `$USER` (else `os.userInfo().username`; anything outside `[a-zA-Z0-9._-]+` becomes `claude-code-user`) | source; seen in `security dump-keychain` on the Mac (`acct "ariel"`) |
| With `CLAUDE_CONFIG_DIR=<s>`: `Claude Code-credentials-` + `sha256(NFC(<s>))[:8]`, from the exact string, unresolved | source; `/tmp/claude-window-probe` → `-cf2bd579` was read by real `claude auth status`; with a trailing slash it was not |
| `CLAUDE_SECURESTORAGE_CONFIG_DIR` overrides the storage dir and name; an empty `CLAUDE_CONFIG_DIR` counts as unset | source |
| Payload = the Linux `.credentials.json` JSON (`claudeAiOauth`) | read of the real item's shape (no values printed) |
| Write: `security -i` fed `add-generic-password -U -a "<acct>" -s "<svc>" -X "<hex>"\n`; argv past 4032 chars. Read: `find-generic-password -a <acct> -w -s <svc>`. Delete: `delete-generic-password -a -s` | source |
| No item → Claude Code reads `<dir>/.credentials.json`; the item wins when both exist; a successful Keychain write deletes the file when the Keychain had no item | source; observed with fake items and files |
| `security` reads Claude Code's items without a GUI prompt (0.01 s) | measured |
| Exit codes 44 not found, 45 duplicate, 2 bad usage, 36 locked; `-i` passes the inner status through | measured |
| `-w` prints data + `\n`, or lowercase hex when the data has a non-printable byte (`é`, newline) | measured |
| Identity block stays in `~/.claude.json` on macOS | source (`join(CLAUDE_CONFIG_DIR \|\| homedir, ".claude.json")`) |
| Keychain reads are cached 30 s in-process; a change check (`jk`) clears the token memo and re-reads before requests. **Whether an open session follows a switch on macOS has not been observed** — the PTY test with fake credentials was inconclusive | source only |
| Refresh = lock `.oauth_refresh.lock` + legacy `<dir>.lock` (60 s stale, touched every 5 s), then fresh read → POST → compare-and-swap save under `.storage-write.lock` (15 s). If the stored token changed underneath, it adopts the stored one | source (all three versions); **observed**: real 2.1.238 (Mac) and 2.1.295 (big2t) create both refresh locks, and with a fresh `.oauth_refresh.lock` made by another process they wait and never take the second |

**Re-check recipe** (after a Claude Code update; use fake credentials only, never your real login):

1. Make a throwaway dir with a `.claude.json` holding an `oauthAccount`, plus fake credentials. On macOS, put them in an item named for the dir with `security -i`. On Linux, use a `.credentials.json` file.
2. Block the network: on macOS `sandbox-exec -p '(version 1)(allow default)(deny network-outbound (remote ip))' …`; on big2t, `HTTPS_PROXY=HTTP_PROXY=http://127.0.0.1:9`. Unprivileged namespaces are blocked there; watch `ss -tnp` for anything external.
3. Run `CLAUDE_CONFIG_DIR=<dir> claude auth status --json`; it reports the fake subscription if it read your item.
4. For the locks, give it expired credentials and run `claude -p hi`, polling for the lock directories.
5. Delete every item you made, and confirm the real item's `mdat` is unchanged with `security find-generic-password -a "$USER" -s "Claude Code-credentials"`. That form prints attributes only.

Never let a real-Keychain check use the tool's own `user_login()` with a sandboxed `~/.claude`: it still maps to the real `Claude Code-credentials` item. Set `ew.KEYCHAIN_SERVICE` to a throwaway prefix first and assert every service name starts with it.

## 6. Testing — how, and where

- **`fake_security.py`** stands in for `/usr/bin/security`. Its items live in the JSON file named by `FAKE_SECURITY_DB`, and it refuses to run without one. It models the measured behaviour, plus failure injection: `{"fail": {"find"|"add"|"delete": status}}` and `{"drop_writes": true}`. Its log records how each command arrived (`stdin` or `argv`), with no data.
- **The suite never touches a real Keychain.** `main()` sets `ew.SECURITY_PATH` to the fake and wraps `ew._security` so the database follows the home holding `ew.USER_CONFIG_DIR` (`fake_keychain()`). It also clears `CLAUDE_CONFIG_DIR`, `CLAUDE_SECURESTORAGE_CONFIG_DIR` and the API-key variables, and presents a machine with systemd (`_command_exists`).
- **Claude's side is modelled independently** in the tests: `claude_code_service`, `claude_code_saves`, `claude_code_reads`, `claude_code_has`, `claude_code_forgets` and `claude_code_refresh_locks`. Fixtures sign in through them and invariants are checked through them. Never copy tool logic into a test (AGENTS.md); the naming model is pinned to the real observation in `test_the_keychain_item_is_the_one_claude_code_reads`.
- **`CREDENTIAL_TESTS`** (just above `main()`) run a second time playing the other platform (`sys.platform` flipped), so Linux exercises the Keychain path and the Mac the file path. A new test that reads or moves a login goes in both `main()`'s list and `CREDENTIAL_TESTS`.
- **Mutation checks:** for every new behaviour, break the code in a scratch copy and confirm a test fails. The script, given a label, an old string, a new string and the tests to run: copies the repo files to a temp dir, replaces exactly one occurrence in the module, runs only those tests in `main()`'s sandbox, and reports CAUGHT or MISSED. To run single tests in the sandbox, exec `main()`'s prologue (everything before `def run(test):`) and call the test functions. Every new test on the branch was checked this way.
- **Where to run:**
  - **Mac:** `python3 test_window_timing.py` — about two minutes.
  - **OrbStack Ubuntu VM `cwt-linux`** (on the Mac; it has a systemd user manager; the repo is visible at `/mnt/mac/opt/dev/claude-window-timing`): copy the tree to `~/cwt` inside it, then `orb -m cwt-linux bash -lc 'export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH; cd ~/cwt && python3 test_window_timing.py'` — about 80 s. Run it both with and without a dummy `~/.local/bin/claude-window`; the second matches big2t.
  - **big2t:** from the Mac, `rsync -a --exclude .git --exclude state --exclude accounts.json --exclude schedule.json --exclude bin --exclude __pycache__ --exclude .DS_Store ./ ubuntu@big2t:cwt-test/`, then `ssh ubuntu@big2t 'export XDG_RUNTIME_DIR=/run/user/$(id -u) PATH=$HOME/.local/bin:$PATH; cd ~/cwt-test && python3 test_window_timing.py'` — about 2½ minutes. It includes the real-systemd anchor test. **Never** run it in `/opt/dev/claude-window-timing` there.
- **Real-machine checks done** (fake logins only):
  - a real-`/usr/bin/security` switch on the Mac under a throwaway service prefix, both ways;
  - `setup --no-pings` in a sandboxed home on the Mac;
  - a real-filesystem switch on big2t in a throwaway home, including waiting out a 2-second refresh lock.

  In each case the real login's timestamp was unchanged afterwards.

## 7. What to do next

### A. Validate on the Mac with two real subscriptions (blocks merging)

The user observes Shabbat: no user-facing steps from Friday evening through Saturday.

1. **Preconditions:**
   - Two active subscriptions.
   - On big2t, account 2 re-added in `accounts.json` (re-run `./install.sh` there and sign in), so `schedule.json` lists both.
   - The Mac checkout on `macos-switcher`.
2. **On the Mac:**
   - Copy `schedule.json` from big2t into the Mac checkout.
   - Run `./install.sh --no-pings`. It should recognise the account the Mac's Claude Code is on and ask for one sign-in fewer.
   - Sign in the other store exactly as printed: `CLAUDE_CONFIG_DIR=/Users/ariel/.claude-switch/<n> claude`, then `/login`.
3. **Dry checks:**
   - `claude-window doctor` should be clean.
   - `claude-window accounts` and `claude-window which` should name the right accounts.
   - List the items with `security dump-keychain | grep -A2 '"svce".*Claude Code-credentials'`. Expect the plain item plus one hashed item per parked login, named as `keychain_service` computes.
4. **Real switch:**
   - Open a Claude Code session first and note `/status`.
   - Run `claude-window switch`. Expect "Switched…", a backup path, and the live reading of the new account.
   - Check with `claude auth status --json`, which should show the new account.
   - In the open session, send a message, then check `/status`. Note how long it takes to follow; the source suggests up to 30 s.
   - Switch back, and repeat the checks.
5. **Invariant:** no grant may be in two places. Compare refresh-token fingerprints, never printing tokens, across:
   - the plain item;
   - the hashed store items;
   - big2t's ping directories, which are separate grants by design and must differ.

   Run `claude-window doctor` again afterwards.
6. **Leave it a day,** then confirm nobody was signed out: `claude auth status` for both stores after switching to each.
7. **If all is well:** merge `macos-switcher` into `main` (after 7D if the work is finished by then, so this file does not land on `main`), re-run the suite on the Mac and big2t, then `git pull` on big2t's live checkout. The pinger runs the new module from the next tick; the Linux behaviour is unchanged by design, and the suite checks it. Push `main`.

   **If not:** fix on the branch, with a test that fails without the fix.

### B. Windows (plan step 5; needs a real Windows machine)

Known so far:
- The binary references `isWindowsCredManagerAvailable`, so Credential Manager is likely, perhaps with a plaintext fallback. Verify on a real machine, the same way as section 5.
- The module now imports there.
- `switch` is refused by `platform_blocker`.

To build:
- a store implementation, if it's Credential Manager (via `cmdkey`? PowerShell? — no dependencies allowed);
- the lock (`msvcrt.locking`);
- session discovery (`tasklist`);
- the `os.getuid()` uses;
- a Python setup entry point, since `install.sh` is bash;
- test guards.

Decide first whether native Windows is worth it at all; WSL already counts as Linux.

### C. Known gaps, deliberately left

- `status`, `which` and `doctor` describe `~/.claude` as "your Claude Code" even when `CLAUDE_CONFIG_DIR` is set; only `switch` refuses.
- Non-production `OAUTH_FILE_SUFFIX` builds (`-local-oauth`, `-custom-oauth`) name the item differently. Ignored: not something users run.
- A Keychain GUI prompt could outlast `KEYCHAIN_TIMEOUT_SEC` (30 s) or Claude Code's 15-second save-lock limit. None was ever seen.
- The ping path's transcript slug uses the unresolved working directory. A symlinked home could break pings; seen only as `/var` → `/private/var` in the suite on the Mac.
- Docs on the branch say macOS switching works. That is true of the tested code, but it should be re-read before merging, together with 7A's findings about open sessions.

### D. When everything is done

When 7A has passed, B has been built or explicitly dropped, and the gaps in C are fixed or accepted:

1. Delete this file in its own commit.
2. Update `MEMORY.md` and the `multi-platform-switcher` memory to say the work is finished.
3. Delete the OrbStack VM if it is no longer wanted: `orb delete cwt-linux`.

## 8. History

- **2026-10-10/11** (big2t session): published `main`; wrote the first version of this plan; `switch` refused on macOS.
- **2026-10-11** (Mac session):
  - fact-finding;
  - credential store and macOS switching;
  - suite portability;
  - Claude Code locks;
  - config-dir refusal;
  - optional imports;
  - real-machine checks on the Mac, the OrbStack VM and big2t;
  - branch `macos-switcher` pushed;
  - this plan moved from a local, git-excluded note into the branch, at the user's request.
