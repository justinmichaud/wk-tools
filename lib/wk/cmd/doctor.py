"""Report what this machine needs as ok, MISSING or unknown, with the command that fixes it.

On the controller: the controller. On a server: that server; `wk doctor <ws>` reports one workspace there and
`--all` adds every workspace. unknown is a reading that could not be taken. Exit code: 0 all ok, 1 something is
missing, 4 something is unknown and nothing missing."""

from wk.core import hooks
from wk.core.decl import CONTROLLER, SERVERS, Command, Opt
from wk.core.errors import FAILED, NEEDS_PERSON, Usage
from wk.core.hooks import MISSING, OK, UNKNOWN
from wk.workspace import lifecycle

COMMAND = Command(on=CONTROLLER + SERVERS, takes=("ws?",), opts=(Opt("--all", "every workspace too"),), readonly=True)
LABEL = {OK: "ok", MISSING: "MISSING", UNKNOWN: "unknown"}


def main(args, inv):
    here = inv.fleet.here
    if (args.ws or args.all) and not here.holds_workspaces:
        raise Usage("%s holds no workspace: `wk doctor %s` is typed on the server that holds it" % (here.name, args.ws or "--all"))
    wanted = [lifecycle.need(inv, args.ws)] if args.ws else lifecycle.listing(inv, inv.here) if args.all else []
    groups = ([] if args.ws else [(here.name, hooks.collect("doctor", inv, here, None))]) + [
        ("%s (workspace on %s)" % (ws.name, here.name), hooks.collect("doctor", inv, here, ws)) for ws in wanted]
    for title, found in groups:
        print(title)
        for row in found:
            print(("  %-8s %-14s %s" % (LABEL[row.status], row.name, row.detail)).rstrip())
            if row.status != OK and row.remedy:
                print("  %-8s fix: %s" % ("", row.remedy))
    statuses = {row.status for _, found in groups for row in found}
    return FAILED if MISSING in statuses else NEEDS_PERSON if UNKNOWN in statuses else 0
