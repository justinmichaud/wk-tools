"""Print the arguments as parsed.

The second paragraph."""
import json

from wk.core.decl import Command, Opt

COMMAND = Command(takes=("first", "second?"), opts=(Opt("--loud", "say it loudly"), Opt("--to-path", "where to", "path")),
                  tail=True, readonly=True)


def main(args, inv):
    print(json.dumps(vars(args), sort_keys=True))
