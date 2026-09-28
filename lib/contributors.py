#!/usr/bin/env python3
import argparse
import json
import sys


def main(argv):
    p = argparse.ArgumentParser(prog="contributors.py")
    verbs = p.add_subparsers(dest="verb", required=True)
    verbs.add_parser("bugzilla-login", help="that contributor's Bugzilla login: the first email of their entry in "
                     "WebKit's metadata/contributors.json on stdin, as webkitpy's Committer.bugzilla_email answers; "
                     "exit 1 when no entry names that account").add_argument("github_user")
    a = p.parse_args(argv)
    for entry in json.load(sys.stdin):
        if entry.get("github") == a.github_user and entry.get("emails"):
            sys.stdout.write(entry["emails"][0] + "\n")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
