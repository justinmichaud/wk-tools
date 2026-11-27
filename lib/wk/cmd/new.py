"""Make a workspace: a checkout of WebKit, and its container where the server has one.

The name is first reserved with the controller, which refuses a name another server holds, naming it. A controller
that does not answer is a barrier: forced, the workspace is made here with no reservation. The checkout is cloned from
the server's mirror, so no network is used. Killed and typed again, it remakes the workspace from wherever it got to."""

from wk.api import client
from wk.core.act import die
from wk.core.decl import SERVERS, Command
from wk.core.seams import CREATING
from wk.workspace import lifecycle

COMMAND = Command(on=SERVERS, takes=("ws",))


def main(args, inv):
    lifecycle.check(args.ws)
    client.reserve(inv, args.ws)
    if lifecycle.find(inv, inv.here, args.ws).state == CREATING:
        die("a `wk new %s` is making it on %s already: wait for it to end" % (args.ws, inv.fleet.hostname))
    return lifecycle.make(inv, args.ws)
