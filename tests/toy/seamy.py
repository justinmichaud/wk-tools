"""Use every seam a lane provides."""
from wk.core.decl import SERVERS, Command
from wk.core.seams import READY, Workspace

COMMAND = Command(on=SERVERS, takes=("ws",))


def main(args, inv):
    ws = Workspace(args.ws, inv.fleet.hostname, inv.wk_home + "/ws/" + args.ws, inv.fleet.here.driver, READY)
    inv.driver(ws.driver, inv.here).create(ws)
    inv.secrets().put("token", "v")
    print(inv.tasks(inv.here).add(["make"], ws.name, group="build"))
