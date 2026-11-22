"""The group's default verb."""
from wk.core.decl import Command, Opt

COMMAND = Command(opts=(Opt("--twice", "say it twice"),), default=True, readonly=True)


def main(args, inv):
    print("one" * (2 if args.twice else 1))
