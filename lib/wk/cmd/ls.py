"""List the workspaces.

On a server, its own: name, server, driver and state. Elsewhere, every workspace name the controller holds and the
server holding it."""

from wk.api import client
from wk.core.conf import KINDS
from wk.core.decl import Command
from wk.workspace import lifecycle

COMMAND = Command(on=KINDS, readonly=True)


def main(args, inv):
    if inv.fleet.here.holds_workspaces:
        for ws in sorted(lifecycle.listing(inv, inv.here), key=lambda ws: ws.name):
            print("%-24s %-16s %-10s %s" % (ws.name, ws.server, ws.driver, ws.state))
    else:
        for name, server in sorted(client.holders(inv).items()):
            print("%-24s %s" % (name, server))
