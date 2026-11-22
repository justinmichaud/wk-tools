"""Open a shell in a workspace, or run one command there.

`wk enter <ws>` is a login shell in the checkout. `wk enter <ws> -- <command>` runs the command there and returns
its exit code; it gets a terminal when wk has one. A container that is not running is started first."""

import shlex

from wk.core.act import die
from wk.core.decl import Command
from wk.core.errors import NEEDS_PERSON
from wk.core.seams import READY
from wk.workspace import lifecycle

COMMAND = Command(where="server", takes=("ws",), tail=True)


def main(args, inv):
    ws = lifecycle.need(inv, args.ws)
    driver, tty = inv.driver(ws.driver, inv.here), inv.stdin.isatty()
    if not tty and not args.tail:
        die("a shell needs a terminal and there is none: name a command, `wk enter %s -- <command>`" % ws.name, NEEDS_PERSON)
    if ws.state != READY:
        driver.create(ws)
    if tty:
        argv = driver.enter_argv(ws) + (["-c", shlex.join(args.tail)] if args.tail else [])
    else:
        argv = driver.exec_argv(ws, args.tail)
    return inv.here.act(argv, stream=True, tty=tty).rc
