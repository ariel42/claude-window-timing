#!/usr/bin/env python3
"""
A stand-in for the Claude Code CLI, for end-to-end tests.

The tool drives Claude through a PTY and reads three things back: the session
JSONL it writes on exit, the statusLine payloads it emits while running, and
whatever appears on the terminal. Everything else about the real CLI is
irrelevant to us, so this reproduces exactly those three and nothing more.

That makes it possible to run the whole install — wizard, checkpoints, units,
launcher — in a pristine directory with no Claude account, no network and no
usage spent, which is the one path that cannot otherwise be tested end to end.

`auth status --json` is answered too, because `doctor` asks the CLI that
question rather than reading the credentials itself.

Behaviour is steered by a `.fake_claude.json` file in the working directory,
*not* by environment variables: the tool builds a deliberately minimal
environment for its child, so anything a test exported would be stripped before
it arrived. Every key is optional.

    {"limited": "session"|"weekly",   refuse the way a rate limit refuses
     "five_hour_reset": <epoch>,      what to report as the 5-hour reset
     "weekly_reset": <epoch>,         ... and the weekly one
     "five_hour_pct": <int>,          usage percentages to report
     "weekly_pct": <int>,
     "silent": true,                  never answer, to exercise the timeout
     "hang_prompt": true,             block on a first-run prompt, as a fresh
                                      account did before --no-chrome
     "auth": {...}}                   what `auth status --json` should report;
                                      false makes it print nothing parseable
"""

import json
import os
import subprocess
import sys
import time
import uuid


CONTROL_FILE = ".fake_claude.json"


def _control_path():
    """
    Where the control file lives: the working directory, or failing that the
    config directory.

    Two places rather than one because the tool's working directory is its own
    business and has changed once already; the config directory is passed in
    CLAUDE_CONFIG_DIR and is the one thing a stand-in can always find.
    """
    here = os.path.join(os.getcwd(), CONTROL_FILE)
    if os.path.exists(here):
        return here
    config = os.environ.get("CLAUDE_CONFIG_DIR")
    if config:
        return os.path.join(config, CONTROL_FILE)
    return here


def control():
    """How this run should behave. Missing file means 'like a healthy account'."""
    try:
        with open(_control_path()) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def session_path(session_id):
    """Mirror Claude Code's own layout, including the mangled working directory."""
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude")
    directory = os.path.join(config, "projects", os.getcwd().replace("/", "-"))
    if not os.path.isdir(directory):
        os.makedirs(directory)
    return os.path.join(directory, session_id + ".jsonl")


def emit_statusline(command, api_ms):
    """Run the statusLine command the way Claude Code does: payload on stdin."""
    if not command:
        return
    settings = control()
    payload = {
        "cost": {"total_api_duration_ms": api_ms},
        "rate_limits": {
            "five_hour": {
                "used_percentage": settings.get("five_hour_pct", 3),
                "resets_at": settings.get("five_hour_reset",
                                          time.time() + 4 * 3600),
            },
            "seven_day": {
                "used_percentage": settings.get("weekly_pct", 20),
                "resets_at": settings.get("weekly_reset",
                                          time.time() + 3 * 86400),
            },
        },
    }
    try:
        proc = subprocess.Popen(command, shell=True, stdin=subprocess.PIPE)
        proc.communicate(json.dumps(payload).encode())
    except OSError:
        pass


def append(path, entry):
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def auth_status():
    """
    Answer `auth status --json` the way the real CLI does.

    Defaults to a healthy Pro login, so a test only has to say what is *wrong*.
    A control value of false stands for a CLI that answers with something this
    cannot parse, which must count as "cannot tell" rather than as a fault.
    """
    settings = control()
    if "auth" in settings and not settings["auth"]:
        sys.stdout.write("not json\n")
        return 0
    report = settings.get("auth")
    if report is None:
        report = {"loggedIn": True, "subscriptionType": "pro"}
    sys.stdout.write(json.dumps(report) + "\n")
    return 0


def main():
    args = sys.argv[1:]
    if args[:2] == ["auth", "status"]:
        return auth_status()
    session_id, resumed, settings = None, False, {}
    for index, arg in enumerate(args):
        if arg == "--session-id" and index + 1 < len(args):
            session_id = args[index + 1]
        elif arg == "--resume" and index + 1 < len(args):
            session_id, resumed = args[index + 1], True
        elif arg == "--settings" and index + 1 < len(args):
            try:
                settings = json.loads(args[index + 1])
            except ValueError:
                settings = {}
    session_id = session_id or str(uuid.uuid4())
    status_command = (settings.get("statusLine") or {}).get("command")
    path = session_path(session_id)

    sys.stdout.write("\x1b[?25l fake claude ready in {}\r\n".format(os.getcwd()))
    sys.stdout.flush()

    if control().get("hang_prompt"):
        # What a genuinely fresh account did before --no-chrome: sit on a
        # first-run question forever, with nobody to press a key.
        sys.stdout.write("Claude in Chrome extension detected\r\n"
                         "  1. Yes, use my browser\r\n"
                         "  2. No, keep browser tools off\r\n")
        sys.stdout.flush()
        while True:
            time.sleep(1)

    # The first report carries the duration inherited from the restored session,
    # which is the reading the tool must take as its baseline rather than mistake
    # for its own reply landing.
    baseline_ms = 6000 if resumed else 0
    emit_statusline(status_command, baseline_ms)

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        prompt = line.strip()
        if not prompt:
            continue
        if prompt.startswith("/exit"):
            break

        limited = control().get("limited")
        if limited:
            when = "Aug 10, 10pm" if limited == "weekly" else "9:30pm"
            text = ("You've hit your {} limit · resets {} "
                    "(Asia/Jerusalem)".format(
                        "weekly" if limited == "weekly" else "session", when))
            append(path, {"type": "user", "uuid": str(uuid.uuid4()),
                          "message": {"role": "user", "content": prompt}})
            append(path, {"type": "assistant", "uuid": str(uuid.uuid4()),
                          "message": {"role": "assistant",
                                      "content": [{"type": "text", "text": text}],
                                      "usage": {"input_tokens": 10,
                                                "output_tokens": 20}}})
            sys.stdout.write(text + "\r\n")
            sys.stdout.flush()
            emit_statusline(status_command, baseline_ms + 1500)
            continue

        if control().get("silent"):
            continue                      # never answers: exercises the timeout

        append(path, {"type": "user", "uuid": str(uuid.uuid4()),
                      "message": {"role": "user", "content": prompt}})
        append(path, {"type": "assistant", "uuid": str(uuid.uuid4()),
                      "message": {"role": "assistant",
                                  "content": [{"type": "text",
                                               "text": "ok, " + prompt}],
                                  "usage": {"input_tokens": 10,
                                            "output_tokens": 25,
                                            "cache_read_input_tokens": 6800,
                                            "cache_creation_input_tokens": 0}}})
        sys.stdout.write("ok, {}\r\n".format(prompt))
        sys.stdout.flush()
        # The jump in this counter is how the tool knows the reply has landed.
        emit_statusline(status_command, baseline_ms + 2500)

    sys.stdout.write("goodbye\r\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
