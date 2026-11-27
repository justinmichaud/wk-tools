"""List the machines: one line per machines/<name>.conf, with its kind."""

from wk.core.decl import CONTROLLER, Command

COMMAND = Command(on=CONTROLLER, readonly=True, default=True)


def main(args, inv):
    for conf in inv.fleet.machines():
        print("%-20s %s" % (conf.name, conf.kind))
