#!/usr/bin/env python3
"""PreToolUse hook on Bash: deny git commands that rewrite the working tree or index."""
import json
import re
import shlex
import sys

DENIED = {"stash", "checkout", "restore", "reset", "clean", "switch"}
READ_ONLY_STASH = {"list", "show"}
GLOBAL_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}


def verbs(command):
    for segment in re.split(r"&&|\|\||[;|&\n()]", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            words = segment.split()
        for i, w in enumerate(words):
            if w != "git" and not w.endswith("/git"):
                continue
            j = i + 1
            while j < len(words) and words[j].startswith("-"):
                j += 2 if words[j] in GLOBAL_WITH_VALUE else 1
            if j < len(words):
                yield words[j], words[j + 1:]


def main():
    data = json.load(sys.stdin)
    command = (data.get("tool_input") or {}).get("command") or ""
    for verb, rest in verbs(command):
        if verb not in DENIED:
            continue
        if verb == "stash" and rest and rest[0] in READ_ONLY_STASH:
            continue
        reason = ("`git %s` rewrites the working tree or index and can discard uncommitted work "
                  "other agents are relying on. Read the committed version with `git show HEAD:<path>` "
                  "and compare with `git diff` instead; if the tree really must change, ask the user." % verb)
        json.dump({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                          "permissionDecision": "deny",
                                          "permissionDecisionReason": reason}}, sys.stdout)
        return


main()
