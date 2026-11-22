"""Open a shell in a workspace, or run one command there.

`wk enter <ws>` is a login shell in the checkout, with the credentials delivered to the server: `git push`,
`git-webkit` and `gh` work in it. `wk enter <ws> -- <command>` has that shell run the command and returns its exit
code; it gets a terminal when wk has one. A container that is not running is started first."""

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
    return inv.here.act(driver.enter_argv(ws, args.tail, tty), stream=True, tty=tty).rc
