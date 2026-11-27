"""Run on a workspace's server."""
from wk.core.decl import SERVERS, Command, Opt

COMMAND = Command(on=SERVERS, takes=("ws?",), opts=(Opt("--rc", "exit with it", "n"),), tail=True)


def main(args, inv):
    print("ran on %s: %s %s" % (inv.fleet.hostname, args.ws, args.tail))
    inv.here.write("/far/%s" % args.ws, "1")
    return int(args.rc or 0)
