"""Start an agent, claude or pi, in a workspace, with every credential hidden from it.

`wk ai claude <ws>` starts Claude Code in the checkout with Remote Control on, named after the workspace;
`wk ai pi <ws>` starts pi. The words after `--` go to the agent: `wk ai claude <ws> -- -r` resumes a session.

The agent runs under the credential wrapper: it cannot read <wk_home>/secrets, it inherits no token and no ssh
agent, and the build tools on its PATH refuse it and name `wk build`. The wrapper is measured before every start,
and an agent is not started under one that does not hold. It may commit; it cannot push."""

from wk.agent import session
from wk.core.decl import Command

COMMAND = Command(where="server", takes=("agent", "ws"), tail=True)


def main(args, inv):
    return session.start(inv, args.agent, args.ws, args.tail)
