"""Report what each machine needs as ok, MISSING or unknown, with the command that fixes it.

On the client: the client, then every server. On a server: that server. `wk doctor <ws>` reports one workspace, on
its server; `--all` adds every workspace. unknown is a reading that could not be taken; a machine that does not
answer is one unknown row. Exit code: 0 all ok, 1 something is missing, 4 something is unknown and nothing missing."""

import dataclasses
import json

from wk.core import hooks, route
from wk.core.decl import Command, Opt
from wk.core.errors import FAILED, NEEDS_PERSON, Unreachable
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.provision import rows
from wk.workspace import lifecycle

COMMAND = Command(takes=("ws?",), opts=(Opt("--all", "every workspace too"), Opt("--json", "print the rows as JSON")), readonly=True)
LABEL = {OK: "ok", MISSING: "MISSING", UNKNOWN: "unknown"}


def machine(inv, conf):
    """A machine's group: every lane's rows, or the one row saying it did not answer."""
    try:
        inv.machine(conf.name).run(["true"])
        return (conf.name, [DoctorRow("reachable", OK)] + hooks.collect("doctor", inv, conf, None))
    except Unreachable as e:
        return (conf.name, [DoctorRow("reachable", UNKNOWN, str(e).split("\n")[0], "tailscale status")])


def held(inv, ws):
    """A workspace's groups, from the server that holds it."""
    try:
        said = inv.machine(ws.server).run(["wk", "doctor", ws.name, "--json"])
        return [(group["group"], [DoctorRow(**row) for row in group["rows"]]) for group in json.loads(said.out)]
    except Unreachable as e:
        return [(ws.name, [DoctorRow("reachable", UNKNOWN, str(e).split("\n")[0], "tailscale status")])]
    except ValueError:
        return [(ws.name, [DoctorRow("doctor", UNKNOWN, "`wk doctor %s --json` on %s answered %s" % (ws.name, ws.server, said.err.strip() or "nothing"),
                                     "wk machine setup %s" % ws.server)])]


def on_client(args, inv):
    if args.ws:
        return held(inv, route.locate(inv, args.ws))
    groups = [(inv.fleet.hostname, rows.client(inv))] + [machine(inv, conf) for conf in inv.fleet.machines() if conf.kind != "client"]
    if args.all:
        found, silent = route.workspaces(inv)
        groups += [(name, [DoctorRow("workspaces", UNKNOWN, "`wk ls --json` did not answer", "wk machine setup %s" % name)]) for name in silent]
        groups += [group for ws in found for group in held(inv, ws)]
    return groups


def on_server(args, inv):
    here = inv.fleet.here
    wanted = [lifecycle.need(inv, args.ws)] if args.ws else lifecycle.listing(inv, inv.here) if args.all else []
    return ([] if args.ws else [machine(inv, here)]) + [("%s (workspace on %s)" % (ws.name, here.name), hooks.collect("doctor", inv, here, ws))
                                                        for ws in wanted]


def show(groups):
    for title, found in groups:
        print(title)
        for row in found:
            print(("  %-8s %-14s %s" % (LABEL[row.status], row.name, row.detail)).rstrip())
            if row.status != OK and row.remedy:
                print("  %-8s fix: %s" % ("", row.remedy))


def main(args, inv):
    groups = on_client(args, inv) if inv.fleet.here.kind == "client" else on_server(args, inv)
    if args.json:
        print(json.dumps([{"group": title, "rows": [dataclasses.asdict(row) for row in found]} for title, found in groups]))
    else:
        show(groups)
    statuses = {row.status for _, found in groups for row in found}
    return FAILED if MISSING in statuses else NEEDS_PERSON if UNKNOWN in statuses else 0
