"""Run on a workstation only."""
from wk.core.decl import WORKSTATION, Command

COMMAND = Command(on=WORKSTATION, readonly=True)


def main(args, inv):
    print("at the desk")
