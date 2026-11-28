"""Open a shell in a workspace, or run one command there.

`wk enter <ws>` is a login shell in the checkout, the person's: it asks the controller for their credentials when this
server holds none (push keys made anew, the GitHub and Bugzilla logins), so `git push`, `git-webkit` and `gh` work in
it. An agent session running on this server could read them too, which is said; `wk ai` removes them again. `wk enter <ws> -- <command>` has that shell run the command and returns its exit
code; it gets a terminal when wk has one. A container that is not running is started first."""

from wk.api import client
from wk.core.act import die, warn
from wk.creds import human
from wk.core.decl import SERVERS, Command
from wk.core.errors import NEEDS_PERSON
from wk.core.seams import READY
from wk.workspace import lifecycle

COMMAND = Command(on=SERVERS, takes=("ws",), tail=True)


def main(args, inv):
    ws = lifecycle.need(inv, args.ws)
    driver, tty = inv.driver(ws.driver, inv.here), inv.stdin.isatty()
    if not tty and not args.tail:
        die("a shell needs a terminal and there is none: name a command, `wk enter %s -- <command>`" % ws.name, NEEDS_PERSON)
    running = human.agents(inv.here)
    if running:
        warn("an agent session runs on %s and can read this shell's credentials:\n    %s"
             % (inv.fleet.hostname, "\n    ".join(running)))
    client.credentials(inv)
    if ws.state != READY:
        driver.create(ws)
    return inv.here.act(driver.enter_argv(ws, args.tail, tty), stream=True, tty=tty).rc
