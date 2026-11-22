"""List the names of the credentials the store holds, one to a line."""

from wk.core.decl import Command

COMMAND = Command(where="client", readonly=True)


def main(args, inv):
    for name in inv.secrets().list():
        print(name)
