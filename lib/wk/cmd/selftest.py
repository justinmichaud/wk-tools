"""Run wk's own tests: tests/, against fakes, reaching no machine.

`wk selftest <pattern>` runs only the tests whose name matches (unittest's -k)."""

import sys

from wk.core.decl import Command

COMMAND = Command(takes=("pattern?",), readonly=True)


def main(args, inv):
    argv = [sys.executable, "-I", "-m", "unittest", "discover", "-t", inv.root, "-s", inv.root + "/tests"]
    return inv.here.run(argv + (["-k", args.pattern] if args.pattern else []), stream=True).rc
