"""List the names of the credentials the store holds, one to a line."""

from wk.core.decl import CONTROLLER, Command

COMMAND = Command(on=CONTROLLER, readonly=True)


def main(args, inv):
    for name in inv.secrets().list():
        print(name)
