"""Run on the client only."""
from wk.core.decl import Command

COMMAND = Command(where="client", readonly=True)


def main(args, inv):
    print("at the desk")
