"""Run the credential lane's setup stages for one machine, and print its doctor rows."""
from wk.core.decl import Command
from wk.creds import hooks

COMMAND = Command(where="client", takes=("machine",))


def main(args, inv):
    conf = inv.fleet.get(args.machine)
    for stage in hooks.stages(inv, conf):
        print("%d %s %s" % (stage.order, stage.name, "changed" if stage.run() else "unchanged"))
    for row in hooks.doctor(inv, conf, None):
        print("%s|%s|%s|%s" % (row.name, row.status, row.detail, row.remedy))
