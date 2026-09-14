#!/usr/bin/env python3
import json
import sys

USAGE = ("bugzilla-login <github-user>: that contributor's Bugzilla login -- the first "
         "email of their entry in WebKit's metadata/contributors.json on stdin, which is "
         "what webkitpy's Committer.bugzilla_email answers; exit 1 when no entry names "
         "that account\n")


def main(argv):
    if len(argv) != 3 or argv[1] != "bugzilla-login":
        sys.stderr.write(USAGE)
        return 2
    for entry in json.load(sys.stdin):
        if entry.get("github") == argv[2] and entry.get("emails"):
            sys.stdout.write(entry["emails"][0] + "\n")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
