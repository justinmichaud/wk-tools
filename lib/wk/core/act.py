"""The gate every state change passes, and what a command says and asks.

    from wk.core.act import act, info, warn, die, confirm, barrier

A command changes state through a Machine's mutators (act, write, remove, spawn, copy) or a seam built on them; each
ends in `act` here. A seam that is not a Machine (an HTTP API) calls `act` itself."""

import sys

from wk.core.errors import FAILED, NEEDS_PERSON, REFUSED_FOR_NOW, Bug, Refused


class Flags:
    """One invocation's global flags and what it has asked and forced so far. The dispatcher makes one per command."""

    def __init__(self, dry_run=False, yes=False, force=False, quiet=False, stdin=None):
        self.dry_run, self.yes, self.force, self.quiet, self.stdin = dry_run, yes, force, quiet, stdin
        self.readonly = self.destructive = self.confirmed = False
        self.forced = []


flags = Flags()


def begin(new):
    global flags
    flags = new


def act(who, what, do, dry=None):
    """Run `do()`, the state change `what` on machine `who`. Under --dry-run print it instead and return `dry`."""
    if flags.readonly:
        raise Bug("a readonly command changes state: [%s] %s" % (who, what))
    if flags.destructive and not flags.confirmed:
        raise Bug("a destructive command acts before it asks: [%s] %s" % (who, what))
    if flags.dry_run:
        sys.stderr.write("would: [%s] %s\n" % (who, what))
        return dry
    return do()


def info(message):
    if not flags.quiet:
        sys.stderr.write("==> %s\n" % message)


def warn(message):
    sys.stderr.write("warning: %s\n" % message)


def die(message, status=FAILED):
    raise Refused(message, status)


def confirm(prompt):
    """Ask once before a destructive action. Returns when the answer is yes; any other answer ends the command."""
    if flags.dry_run:
        sys.stderr.write("would ask: %s [y/N]\n" % prompt)
    elif not flags.yes:
        if not flags.stdin.isatty():
            die("%s -- not asked: there is no terminal\n    re-run in a terminal, or pass --yes" % prompt, NEEDS_PERSON)
        sys.stderr.write("%s [y/N] " % prompt)
        sys.stderr.flush()
        if not flags.stdin.readline().strip().lower().startswith("y"):
            die("declined: nothing is changed")
    flags.confirmed = True


def barrier(message, retry=False):
    """A refusal that exists because of a rule: only --force crosses it, and the dispatcher repeats it at exit.
    `retry` marks a barrier that lifts by itself (exit 75)."""
    if not flags.force:
        die("%s\n    --force crosses this" % message, REFUSED_FOR_NOW if retry else FAILED)
    warn("forced past: %s" % message)
    flags.forced.append(message)
