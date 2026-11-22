"""Claim to read, and write."""
from wk.core.decl import Command

COMMAND = Command(readonly=True)


def main(args, inv):
    inv.here.write("/peeked", "1")
