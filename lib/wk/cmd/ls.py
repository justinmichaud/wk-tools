"""List the workspaces: name, server, driver and state.

On a server only its own are listed. Elsewhere every server is asked, and one that does not answer is named; the exit
code is then 4. `--json` on a server prints {"workspaces": [{"name", "driver", "state", "path"}]}; elsewhere each row
also has "server", and "unanswered" names the servers that did not answer."""

import dataclasses
import json

from wk.core.act import warn
from wk.core.conf import KINDS
from wk.core.decl import Command, Opt
from wk.core.errors import NEEDS_PERSON
from wk.workspace import lifecycle

COMMAND = Command(on=KINDS, opts=(Opt("--json", "print JSON"),), readonly=True)


def main(args, inv):
    fleetwide = not inv.fleet.here.holds_workspaces
    found, unknown = inv.fleet.workspaces() if fleetwide else (lifecycle.listing(inv, inv.here), [])
    rows = [dataclasses.asdict(ws) for ws in sorted(found, key=lambda ws: (ws.name, ws.server))]
    if args.json and fleetwide:
        print(json.dumps({"workspaces": rows, "unanswered": unknown}))
    elif args.json:
        print(json.dumps({"workspaces": [{k: row[k] for k in ("name", "driver", "state", "path")} for row in rows]}))
    else:
        for row in rows:
            print("%(name)-24s %(server)-16s %(driver)-10s %(state)s" % row)
    for name in unknown:
        warn("%s did not answer: its workspaces are unknown" % name)
    return NEEDS_PERSON if unknown else 0
