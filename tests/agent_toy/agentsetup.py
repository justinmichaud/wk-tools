"""Run the agent lane's setup stages on a server, as `wk machine setup` does."""
from wk.agent import hooks
from wk.core.decl import Command

COMMAND = Command(where="client", takes=("server",))


def main(args, inv):
    for stage in hooks.stages(inv, inv.fleet.get(args.server)):
        print("%s: %s" % (stage.name, "changed" if stage.run() else "unchanged"))
