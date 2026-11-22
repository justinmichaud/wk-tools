"""Stop what runs on a server: `wk stop --tasks` kills every queued and running task there.

It asks once, naming the tasks. From the client, `--on <server>` says which server."""

from wk.core import seams
from wk.core.act import confirm, info
from wk.core.decl import Command, Opt
from wk.core.errors import Usage

COMMAND = Command(where="server", opts=(Opt("--tasks", "kill every queued and running task"), Opt("--on", "the server to stop them on", "server")),
                  destructive=True)


def main(args, inv):
    if not args.tasks:
        raise Usage("say what to stop: --tasks")
    tasks, here = inv.tasks(inv.here), inv.fleet.hostname
    live = [t for t in tasks.status() if not t.ended]
    if not live:
        info("no task is queued or running on %s" % here)
        return 0
    confirm("kill %d task(s) on %s (%s)?" % (len(live), here, ", ".join(t.label for t in live)))
    for t in sorted(live, key=lambda t: t.state != seams.QUEUED):    # the queued first, or one starts as the running one dies
        tasks.kill(t.id)
    return 0
