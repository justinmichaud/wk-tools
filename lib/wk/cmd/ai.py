"""Start an agent, claude or pi, in a workspace, with every credential hidden from it.

`wk ai claude <ws>` starts Claude Code in the checkout with Remote Control on, named after the workspace;
`wk ai pi <ws>` starts pi. The words after `--` go to the agent: `wk ai claude <ws> -- -r` resumes a session, and
`wk ai claude <ws> -- -p <prompt>` answers one prompt, which needs no terminal.

The agent runs under the credential wrapper: it cannot read <wk_home>/secrets, it inherits no token and no ssh
agent, and the build tools on its PATH refuse it and name `wk build`. The wrapper is measured before every start,
and an agent is not started under one that does not hold. It may commit; it cannot push.

A borrowed box is off the tailnet and holds no credential: typed on the client, the ssh session that starts the agent
also forwards a port of the box to the injector, for as long as the session lasts. Typed on the box itself, `wk ai
claude` is refused."""

from wk import creds
from wk.agent import session
from wk.core.decl import Command

COMMAND = Command(where="server", takes=("agent", "ws"), tail=True, forward=creds.forward)


def main(args, inv):
    return session.start(inv, args.agent, args.ws, args.tail)
