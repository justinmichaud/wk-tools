"""Report the tasks: what is building, testing or queued, and how the last run under each name ended.

It lists the server's tasks; `wk status <ws>` lists one workspace's. Each line is a state (queued, running, stalled,
done, failed, killed), the task's label `<ws>:<what>:<preset>`, and its age. A running task whose log has not grown for 300 s
is called silent, and after 1800 s stalled.

--log prints the end of the workspace's latest log, --follow streams it until the task ends, --wait blocks while
anything is queued or running.

Exit code: 0 idle or done, 1 failed or killed, 2 busy, 3 stalled."""

import json

from wk.core.act import die, info, warn
from wk.core.decl import SERVERS, Command, Opt
from wk.core.errors import BUSY, Usage
from wk.tasks import status

LOG_LINES = 100
COMMAND = Command(on=SERVERS, takes=("ws?",), readonly=True,
                  opts=(Opt("--log", "print the last %d lines of the workspace's latest log" % LOG_LINES),
                        Opt("--follow", "stream the workspace's latest log until its task ends"),
                        Opt("--wait", "block while a task is queued or running"), Opt("--json", "print one JSON object")))


def read(inv, ws):
    """This server's rows."""
    tasks = inv.tasks(inv.here)
    rows = status.rows([t for t in tasks.status() if ws in (None, t.ws)], tasks.quiet, inv.clock.now())
    return [dict(row, server=inv.fleet.hostname) for row in rows]


def main(args, inv):
    if (args.log or args.follow) and not args.ws:
        raise Usage("--log and --follow read one workspace's task: name the workspace")
    if args.log or args.follow:
        tasks = inv.tasks(inv.here)
        mine = [t for t in tasks.status() if t.ws == args.ws]
        if not mine:
            die("%s has run no task: `wk build %s <preset>` starts one" % (args.ws, args.ws))
        if args.follow:
            tasks.follow(mine[-1].id)
        else:
            print("\n".join(status.normalise(tasks.log(mine[-1].id)).rstrip("\n").split("\n")[-LOG_LINES:]))
    rows = read(inv, args.ws)
    while args.wait and status.code(rows) == BUSY:
        inv.clock.sleep(10)
        rows = read(inv, args.ws)
    if args.json:
        print(json.dumps({"tasks": rows}, sort_keys=True))
    else:
        for row in rows:
            print(status.line(row))
            if row["state"] == "running" and row["silent"] >= status.WARN:
                warn("%s has printed nothing for %ds: after %ds it is called stalled" % (row["label"], row["silent"], status.STALL))
        if not rows:
            info("no tasks on %s" % inv.fleet.hostname)
    return status.code(rows)
