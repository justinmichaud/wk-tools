"""Fetch WebKit into a server's mirror, then into its workspaces.

The mirror fetches from GitHub; each workspace then fetches `origin` from the mirror.
`wk sync <ws>` fetches that one workspace; `wk sync` on a server fetches every one there; `wk sync` on the client
does that on every server. Only remote-tracking branches move: no checkout is touched."""

from wk.core import route
from wk.core.act import info, warn
from wk.core.decl import Command
from wk.core.errors import Unreachable
from wk.workspace import lifecycle, mirror

COMMAND = Command(takes=("ws?",))


def main(args, inv):
    if inv.fleet.here.kind == "client":
        return route.hand_over(inv, route.locate(inv, args.ws).server) if args.ws else everywhere(inv)
    m, home = inv.here, inv.wk_home
    found = [lifecycle.need(inv, args.ws)] if args.ws else [ws for ws in lifecycle.listing(inv, m) if lifecycle.made(m, ws)]
    info("fetching the mirror from GitHub")
    mirror.configure(m, home)
    mirror.fetch(m, home)
    for ws in found:
        info("fetching origin in %s" % ws.name)
        lifecycle.fetch(m, ws)


def everywhere(inv):
    codes = [0]
    for conf in inv.fleet.servers():
        info("syncing %s" % conf.name)
        try:
            codes.append(route.hand_over(inv, conf.name))
        except Unreachable as e:
            warn(str(e))
            codes.append(e.status)
    return max(codes)
