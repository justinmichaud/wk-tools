"""Fetch WebKit into the server's mirror, then into its workspaces.

The mirror fetches from GitHub; each workspace then fetches `origin` from the mirror.
`wk sync <ws>` fetches that one workspace; `wk sync` fetches every one on the server. Only remote-tracking branches
move: no checkout is touched."""

from wk.core.act import info
from wk.core.decl import Command
from wk.workspace import lifecycle, mirror

COMMAND = Command(where="server", takes=("ws?",))


def main(args, inv):
    m, home = inv.here, inv.wk_home
    found = [lifecycle.need(inv, args.ws)] if args.ws else [ws for ws in lifecycle.listing(inv, m) if lifecycle.made(m, ws)]
    info("fetching the mirror from GitHub")
    mirror.configure(m, home)
    mirror.fetch(m, home)
    for ws in found:
        info("fetching origin in %s" % ws.name)
        lifecycle.fetch(m, ws)
