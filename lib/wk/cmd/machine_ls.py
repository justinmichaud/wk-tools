"""List the machines: one line per machines/<name>.conf, with its kind and role."""

from wk.core.decl import Command

COMMAND = Command(where="client", readonly=True, default=True)


def main(args, inv):
    for conf in inv.fleet.machines():
        print(("%-20s %-9s %s" % (conf.name, conf.kind, conf.role)).rstrip())
