"""Print the agent lane's doctor rows for a server, or for a workspace wherever it is, as `wk doctor` does."""
from wk.agent import hooks
from wk.core.decl import Command
from wk.workspace import lifecycle

COMMAND = Command(takes=("server", "name?"), readonly=True)


def main(args, inv):
    ws = lifecycle.find(inv, inv.machine(args.server), args.name) if args.name else None
    for row in hooks.doctor(inv, inv.fleet.get(args.server), ws):
        print(" | ".join((row.name, row.status, row.detail, row.remedy)))
