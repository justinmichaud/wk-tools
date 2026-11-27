"""Open a workspace in Zed, on the server that holds it.

The controller says which server holds <ws>; its checkout there is opened as Zed's remote project
`ssh://<user>@<server>/<checkout>`."""

from wk.api import client
from wk.core.act import die
from wk.core.decl import WORKSTATION, Command

COMMAND = Command(on=WORKSTATION, takes=("ws",))


def main(args, inv):
    server = client.holders(inv).get(args.ws)
    if server is None:
        die("no server holds a workspace named %s: `wk ls` lists them" % args.ws)
    user = inv.fleet.get(server).get("user")
    src = "%s/ws/%s/src" % (inv.wk_home_on(inv.machine(server)), args.ws)
    return inv.here.act(["zed", "ssh://%s%s%s" % (user + "@" if user else "", server, src)]).rc
