"""Report the tasks: what is building, testing or queued, and how the last run under each name ended.

On a server it lists that server's tasks; `wk status <ws>` lists one workspace's. On the client with no <ws> it asks
every server and names those that did not answer. Each line is a state (queued, running, stalled, done, failed,
killed, oom), the task's label `<ws>:<what>:<preset>`, and its age. A running task whose log has not grown for 300 s
is called silent, and after 1800 s stalled.

--log prints the end of the workspace's latest log, --follow streams it until the task ends, --wait blocks while
anything is queued or running.

Exit code: 0 idle or done, 1 failed or killed, 2 busy, 3 stalled or out of memory, 4 a server did not answer."""

import json

from wk.core import route
from wk.core.act import die, info, warn
from wk.core.decl import Command, Opt
from wk.core.errors import BUSY, Usage
from wk.core.fleet import UNKNOWN
from wk.tasks import status

LOG_LINES = 100
COMMAND = Command(takes=("ws?",), readonly=True,
                  opts=(Opt("--log", "print the last %d lines of the workspace's latest log" % LOG_LINES),
                        Opt("--follow", "stream the workspace's latest log until its task ends"),
                        Opt("--wait", "block while a task is queued or running"), Opt("--json", "print one JSON object")))


def local(inv, ws):
    """(this server's rows, the servers that did not answer: none)."""
    tasks = inv.tasks(inv.here)
    found = tasks.status()
    rows = status.rows([t for t in found if ws in (None, t.ws)], tasks.quiet, inv.clock.now())
    return [dict(row, server=inv.fleet.hostname) for row in rows], []


def fleet(inv):
    """(every answering server's rows, the servers that did not answer)."""
    def ask(machine):
        try:
            return json.loads(machine.run(["wk", "status", "--json"], timeout=30).out)["tasks"]
        except ValueError:
            return UNKNOWN
    answers = inv.fleet.ask(ask)
    return ([row for rows in answers.values() if rows is not UNKNOWN for row in rows],
            [name for name, rows in answers.items() if rows is UNKNOWN])


def main(args, inv):
    client = inv.fleet.here.kind == "client"
    if client and args.ws:
        return route.hand_over(inv, route.locate(inv, args.ws).server)
    if (args.log or args.follow) and not args.ws:
        raise Usage("--log and --follow read one workspace's task: name the workspace")
    read = (lambda: fleet(inv)) if client else (lambda: local(inv, args.ws))
    if args.log or args.follow:
        tasks = inv.tasks(inv.here)
        mine = [t for t in tasks.status() if t.ws == args.ws]
        if not mine:
            die("%s has run no task: `wk build %s <preset>` starts one" % (args.ws, args.ws))
        if args.follow:
            tasks.follow(mine[-1].id)
        else:
            print("\n".join(status.normalise(tasks.log(mine[-1].id)).rstrip("\n").split("\n")[-LOG_LINES:]))
    rows, unanswered = read()
    while args.wait and status.code(rows, unanswered) == BUSY:
        inv.clock.sleep(10)
        rows, unanswered = read()
    if args.json:
        print(json.dumps({"tasks": rows, "unanswered": unanswered}, sort_keys=True))
    else:
        for row in rows:
            print(("%-12s " % row["server"] if client else "") + status.line(row))
            if row["state"] == "running" and row["silent"] >= status.WARN:
                warn("%s has printed nothing for %ds: after %ds it is called stalled" % (row["label"], row["silent"], status.STALL))
        if not rows:
            info("no tasks%s" % ("" if client else " on %s" % inv.fleet.hostname))
        for name in unanswered:
            warn("%s did not answer: its tasks are unknown" % name)
    return status.code(rows, unanswered)
