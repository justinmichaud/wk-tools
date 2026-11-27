"""Start an agent, claude or pi, in a workspace.

`wk ai claude <ws>` starts Claude Code in the checkout with Remote Control on, named after the workspace;
`wk ai pi <ws>` starts pi. The words after `--` go to the agent: `wk ai claude <ws> -- -r` resumes a session, and
`wk ai claude <ws> -- -p <prompt>` answers one prompt, which needs no terminal.

The agent runs as the login, in the workspace, as `wk enter` runs a command: inside the container on Linux, with the
credentials a shell there has. Claude Code reaches claude.ai through the injector. Before it starts, the controller
rotates this server's push keys; a rotation that fails refuses the session, and `--force` starts it with the keys the
server has."""

from wk.agent import session
from wk.core.decl import SERVERS, Command

COMMAND = Command(on=SERVERS, takes=("agent", "ws"), tail=True)


def main(args, inv):
    return session.start(inv, args.agent, args.ws, args.tail)
