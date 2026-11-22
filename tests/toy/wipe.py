"""Remove a file, after asking."""
from wk.core.act import confirm
from wk.core.decl import Command, Opt

COMMAND = Command(opts=(Opt("--early", "act before asking"),), destructive=True)


def main(args, inv):
    if not args.early:
        confirm("remove /precious?")
    inv.here.remove("/precious")
