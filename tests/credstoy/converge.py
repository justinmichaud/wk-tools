"""Run the setup stages the lanes declare for one machine, and print its doctor rows."""
from wk.core import hooks
from wk.core.decl import Command

COMMAND = Command(where="client", takes=("machine",))


def main(args, inv):
    conf = inv.fleet.get(args.machine)
    for stage in hooks.collect("stages", inv, conf):
        print("%d %s %s" % (stage.order, stage.name, "changed" if stage.run() else "unchanged"))
    for row in hooks.collect("doctor", inv, conf, None):
        print("%s|%s|%s|%s" % (row.name, row.status, row.detail, row.remedy))
