"""Remove a workspace: its container, its checkout and its build; then its name is released with the controller.

Refused while a task of the workspace is queued or running, and while a `wk new` is making it. A name this server
holds with no workspace here, which a removal killed before its release leaves, is released."""

from wk.api import client
from wk.core.act import barrier, confirm, die, info
from wk.core.decl import SERVERS, Command
from wk.core.errors import BUSY
from wk.core.seams import ABSENT, CREATING
from wk.workspace import lifecycle

COMMAND = Command(on=SERVERS, takes=("ws",), destructive=True)


def main(args, inv):
    lifecycle.check(args.ws)
    ws = lifecycle.find(inv, inv.here, args.ws)
    if ws.state == ABSENT:
        if client.holders(inv).get(ws.name) != ws.server:
            die("no workspace named %s on %s: `wk ls` lists them" % (ws.name, ws.server))
        confirm("release the name %s, which %s holds with no workspace?" % (ws.name, ws.server))
        client.release(inv, ws.name)
        info("released the name %s" % ws.name)
        return
    if ws.state == CREATING:
        barrier("a `wk new %s` is making it on %s: wait for it to end" % (ws.name, ws.server), BUSY)
    tasks = [t for t in inv.tasks(inv.here).status() if not t.ended and t.ws == ws.name]
    if tasks:
        barrier("workspace %s has a %s task (%s): wait for it, or end it with `wk stop --tasks`"
                % (ws.name, tasks[0].state, tasks[0].label), BUSY)
    confirm("remove workspace %s on %s, with its checkout and its build?" % (ws.name, ws.server))
    lifecycle.wipe(inv, inv.here, ws)
    client.release(inv, ws.name)
    info("removed workspace %s from %s" % (ws.name, ws.server))
