"""Write files without end."""
from wk.core.decl import Command

COMMAND = Command()


def main(args, inv):
    for n in range(9):
        inv.here.write("/flood/%d" % n, "1")
