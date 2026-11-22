"""Write two files, trusting a half-made first one."""
from wk.core.decl import Command

COMMAND = Command()


def main(args, inv):
    if not inv.here.exists("/poke/a"):
        inv.here.write("/poke/a", "1")
        inv.here.write("/poke/b", "1")
