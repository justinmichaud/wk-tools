"""Make a workspace: a checkout of WebKit, and its container where the server has one.

The checkout is cloned from the server's mirror, so no network is used. A name in use on any server is refused, and
so is any name while a server does not answer, since it cannot be shown to be free. Killed and typed again, it
remakes the workspace from nothing."""

from wk.core.act import barrier, die
from wk.core.decl import SERVERS, Command
from wk.core.errors import NEEDS_PERSON
from wk.core.seams import CREATING
from wk.workspace import lifecycle

COMMAND = Command(on=SERVERS, takes=("ws",))


def main(args, inv):
    lifecycle.check(args.ws)
    found, unknown = inv.fleet.workspaces()
    if unknown:
        barrier("%s did not answer, so a workspace named %s cannot be shown to be on no server\n    check `tailscale status`"
                % (", ".join(unknown), args.ws), NEEDS_PERSON)
    held = [ws for ws in found if ws.name == args.ws and (ws.server != inv.fleet.hostname or ws.state == CREATING)]
    if held:
        die("workspace %s is already on %s (%s): `wk rm %s` removes it, or pick another name"
            % (args.ws, held[0].server, held[0].state, args.ws))
    return lifecycle.make(inv, args.ws)
