"""Make a workspace: a checkout of WebKit, and its container where the server has one.

The checkout is cloned from the server's mirror, so no network is used.
Typed on the client, the server is `--on`, else the client conf's `default_server=`. A name in use on any server is
refused, and so is any name while a server does not answer, since it cannot be shown to be free. Killed and typed again, it remakes the workspace from nothing."""

from wk.core import route
from wk.core.act import barrier, die
from wk.core.errors import NEEDS_PERSON
from wk.core.decl import Command, Opt
from wk.core.seams import BROKEN
from wk.workspace import lifecycle

COMMAND = Command(takes=("ws",), opts=(Opt("--on", "the server to make it on", "server"),))


def main(args, inv):
    lifecycle.check(inv, args.ws, args.on)
    if inv.fleet.here.kind != "client":
        return lifecycle.make(inv, args.ws)
    server = args.on or inv.fleet.here.get("default_server")
    if not server:
        die("no server to make %s on: pass --on <server>, or set default_server= in machines/%s.conf" % (args.ws, inv.fleet.hostname))
    inv.fleet.server(server)
    found, unknown = route.workspaces(inv)
    if unknown:
        barrier("%s did not answer, so a workspace named %s cannot be shown to be on no server\n    check `tailscale status`"
                % (", ".join(unknown), args.ws), NEEDS_PERSON)
    held = [ws for ws in found if ws.name == args.ws and (ws.server, ws.state) != (server, BROKEN)]
    if held:
        die("workspace %s is already on %s (%s): `wk rm %s` removes it, or pick another name"
            % (args.ws, held[0].server, held[0].state, args.ws))
    return route.hand_over(inv, server)
