"""Write two files, crash-safely."""
from wk.core.act import info
from wk.core.decl import Command

COMMAND = Command()


def main(args, inv):
    info("poking")
    inv.here.write("/poke/a", "1")
    inv.here.act(["touch", "/poke"])
    inv.here.write("/poke/b", "1")
