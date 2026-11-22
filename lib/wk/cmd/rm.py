"""Remove a workspace: its container, its checkout and its build.

Refused while a task of the workspace is queued or running, and while a `wk new` is making it."""

from wk.core.act import confirm, die, info
from wk.core.decl import Command, Opt
from wk.core.errors import BUSY
from wk.core.seams import ABSENT, CREATING
from wk.workspace import lifecycle

COMMAND = Command(where="server", takes=("ws",), opts=(Opt("--on", "the server it is on, when two hold the name", "server"),),
                  destructive=True)


def main(args, inv):
    lifecycle.check(inv, args.ws, args.on)
    ws = lifecycle.find(inv, inv.here, args.ws)
    if ws.state == ABSENT:
        die("no workspace named %s on %s: `wk ls` lists them" % (ws.name, ws.server))
    if ws.state == CREATING:
        die("a `wk new %s` is making it on %s: wait for it to end" % (ws.name, ws.server), BUSY)
    tasks = [t for t in inv.tasks(inv.here).status() if not t.ended and t.label.split(":")[0] == ws.name]
    if tasks:
        die("workspace %s has a %s task (%s): wait for it, or end it with `wk stop --tasks`"
            % (ws.name, tasks[0].state, tasks[0].label), BUSY)
    confirm("remove workspace %s on %s, with its checkout and its build?" % (ws.name, ws.server))
    lifecycle.wipe(inv, inv.here, ws)
    info("removed workspace %s from %s" % (ws.name, ws.server))
