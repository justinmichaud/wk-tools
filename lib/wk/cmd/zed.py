"""Open a workspace in Zed, on the server that holds it.

Every server is asked which workspaces it holds; the one holding <ws> is opened as Zed's remote project
`ssh://<user>@<server>/<checkout>`. Refused when no server holds it, when two do, and while a server does not answer."""

from wk.core.act import barrier, die
from wk.core.decl import WORKSTATION, Command
from wk.core.errors import NEEDS_PERSON

COMMAND = Command(on=WORKSTATION, takes=("ws",))


def main(args, inv):
    found, unknown = inv.fleet.workspaces()
    held = [ws for ws in found if ws.name == args.ws]
    if len(held) > 1:
        die("%s is on %s: `wk rm %s` on all but one of them" % (args.ws, " and ".join(ws.server for ws in held), args.ws))
    if unknown:
        barrier("%s did not answer, so which server holds %s is unknown\n    check `tailscale status`" % (", ".join(unknown), args.ws),
                NEEDS_PERSON)
    if not held:
        die("no server holds a workspace named %s: `wk ls` lists them" % args.ws)
    user = inv.fleet.get(held[0].server).get("user")
    return inv.here.act(["zed", "ssh://%s%s%s/src" % (user + "@" if user else "", held[0].server, held[0].path)]).rc
