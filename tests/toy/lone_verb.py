"""A group with no default verb."""
from wk.core.decl import Command

COMMAND = Command(readonly=True)


def main(args, inv):
    print("verb")
