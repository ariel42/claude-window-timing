#!/usr/bin/env python3
"""
A stand-in for macOS's `security` command, for the tests.

On macOS Claude Code keeps its login in the Keychain and reads and writes it
by running /usr/bin/security; `switch` does the same, so that it reads exactly
what Claude Code reads. The suite must never touch a real Keychain -- on a Mac
that is somebody's live login -- so this plays one, on any platform: the items
live in the JSON file named by FAKE_SECURITY_DB, and it refuses to run without
one.

Only the commands Claude Code and the tool use are reproduced, with the output
and exit codes measured with the real tool on macOS 26.5:

  find-generic-password -a ACCOUNT -s SERVICE [-w]
      With -w: the data and a newline -- or the data as lowercase hex when it
      holds a byte outside printable ASCII (seen for "é" and for a newline).
      Without -w: the item's attributes, never its data. Exit 44 when there is
      no such item, with a message on stderr.
  add-generic-password [-U] -a ACCOUNT -s SERVICE (-X HEX | -w DATA)
      Exit 45 when the item exists and -U was not given; 2 for hex that is not
      hex, with the usage text on stderr.
  delete-generic-password -a ACCOUNT -s SERVICE
      "password has been deleted." on stderr; exit 44 when there is no item.
  -i
      Commands from stdin, one per line, quoted the way a shell quotes them --
      how Claude Code writes, so the data never appears in `ps`. Exits with the
      status of the last command; a command that fails says
      "<command>: returned <status>" on stderr, as the real one does.

The database may also ask for the failures a real Keychain produces, which
nothing else can provoke:

  {"fail": {"find": 36, "add": 36, "delete": 36}}
      exit with that status instead: 36 is a locked Keychain, 51 a refusal.
      "find" covers reads with and without -w.
  {"drop_writes": true}
      report a write as done and keep nothing, which is what the read-back
      after every write is there to catch.

Every command is appended to "log" in the database, data left out, so a test
can see what was asked of the Keychain without being able to read a secret
out of the log. Each entry ends with how the command arrived: "stdin" through
-i, or "argv" -- where anything on it is visible to every user in `ps`.
"""

import json
import os
import shlex
import sys

NOT_FOUND = 44
DUPLICATE = 45


def _db_path():
    path = os.environ.get("FAKE_SECURITY_DB")
    if not path:
        sys.stderr.write("fake_security: FAKE_SECURITY_DB is not set\n")
        sys.exit(1)
    return path


def _load():
    try:
        with open(_db_path()) as f:
            return json.load(f)
    except (IOError, OSError, ValueError):
        return {}


def _save(db):
    path = _db_path()
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory, 0o700)
    tmp = "{}.tmp.{}".format(path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(db, f, indent=1, sort_keys=True)
    os.replace(tmp, path)


def _key(account, service):
    return "{}\t{}".format(account, service)


def _options(command, words):
    """
    -a/-s/-X/-w values and the bare flags, from one command's words.

    -w takes a value only when adding: finding, it is a flag, and Claude Code
    puts it between the others -- `find-generic-password -a USER -w -s SERVICE`.
    """
    valued = ("-a", "-s", "-X", "-w") if command == "add-generic-password" \
        else ("-a", "-s")
    values, flags, index = {}, set(), 0
    while index < len(words):
        word = words[index]
        if word in valued and index + 1 < len(words):
            values[word] = words[index + 1]
            index += 2
            continue
        flags.add(word)
        index += 1
    return values, flags


def _printable(data):
    return all(0x20 <= byte <= 0x7e for byte in data)


def run(words, db, out, err, via="argv"):
    """One command. Returns its exit status."""
    if not words:
        return 0
    command, (values, flags) = words[0], _options(words[0], words[1:])
    account, service = values.get("-a", ""), values.get("-s", "")
    db.setdefault("log", []).append(
        [command] + sorted(flags) + [account, service, via])
    items = db.setdefault("items", {})
    failing = (db.get("fail") or {})
    verb = {"find-generic-password": "find",
            "add-generic-password": "add",
            "delete-generic-password": "delete"}.get(command)
    if verb is None:
        err.write('security: unknown command "{}"\n'.format(command))
        return 1
    if failing.get(verb):
        err.write("security: the operation could not be completed "
                  "(fake failure {})\n".format(failing[verb]))
        return int(failing[verb])

    key = _key(account, service)
    if verb == "find":
        if key not in items:
            err.write("security: SecKeychainSearchCopyNext: The specified item "
                      "could not be found in the keychain.\n")
            return NOT_FOUND
        if "-w" in flags:
            data = items[key].encode("utf-8")
            out.write((data.decode("ascii") if _printable(data)
                       else data.hex()) + "\n")
        else:
            out.write('keychain: "fake.keychain-db"\nclass: "genp"\n'
                      'attributes:\n    "acct"<blob>="{}"\n'
                      '    "svce"<blob>="{}"\n'.format(account, service))
        return 0

    if verb == "add":
        if key in items and "-U" not in flags:
            err.write("security: SecKeychainItemCreateFromContent (<default>): "
                      "The specified item already exists in the keychain.\n")
            return DUPLICATE
        if "-X" in values:
            try:
                data = bytes.fromhex(values["-X"]).decode("utf-8")
            except ValueError:
                err.write("security: Unable to convert password data (-X must "
                          "specify valid hex digits)\nUsage: "
                          "add-generic-password [-h] [-a account] ...\n")
                return 2
        else:
            data = values.get("-w", "")
        if not db.get("drop_writes"):
            items[key] = data
        return 0

    if key not in items:
        err.write("security: SecKeychainSearchCopyNext: The specified item "
                  "could not be found in the keychain.\n")
        return NOT_FOUND
    del items[key]
    err.write("password has been deleted.\n")
    return 0


def main():
    args = sys.argv[1:]
    db = _load()
    if args[:1] == ["-i"]:
        status = 0
        for line in sys.stdin.read().splitlines():
            words = shlex.split(line)
            if not words:
                continue
            status = run(words, db, sys.stdout, sys.stderr, via="stdin")
            if status:
                sys.stderr.write("{}: returned {}\n".format(words[0], status))
    else:
        status = run(args, db, sys.stdout, sys.stderr)
    _save(db)
    return status


if __name__ == "__main__":
    sys.exit(main())
