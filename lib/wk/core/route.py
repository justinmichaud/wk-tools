"""Finding a workspace and handing a command to its server. Nothing is stored: every server is asked, every time.

The contract a server answers: `wk ls --json` prints one JSON object,

    {"workspaces": [{"name": "x", "driver": "plain", "state": "ready", "path": "/home/u/wk/ws/x"}]}

listing the workspaces on that server alone. Anything else it prints makes that server unknown."""

import json
import sys
import zlib

from wk.core import act
from wk.core.errors import NEEDS_PERSON, Refused, Unreachable
from wk.core.fleet import UNKNOWN
from wk.core.seams import Workspace

ENV = {"dry_run": "WK_DRY_RUN", "yes": "WK_YES", "force": "WK_FORCE", "quiet": "WK_QUIET"}
FORWARD = "WK_FORWARD"
FORWARD_PORTS = (20000, 10000)      # the first and how many: below the range Linux gives out by itself (32768 on)


def workspaces(inv):
    """(every Workspace on every server that answered, the names of the servers that did not)."""
    def listing(machine):
        try:
            rows = json.loads(machine.run(["wk", "ls", "--json"], timeout=30).out)["workspaces"]
        except ValueError:
            return UNKNOWN
        return [Workspace(server=machine.name, **row) for row in rows]
    answers = inv.fleet.ask(listing)
    return ([ws for found in answers.values() if found is not UNKNOWN for ws in found],
            [name for name, found in answers.items() if found is UNKNOWN])


def locate(inv, name):
    """The Workspace called `name`, from whichever server holds it."""
    found, unknown = workspaces(inv)
    held = [ws for ws in found if ws.name == name]
    if len(held) > 1:
        raise Refused("workspace %s is on %s: remove one with `wk rm %s --on <server>`"
                      % (name, " and ".join(ws.server for ws in held), name))
    if not held and unknown:
        raise Refused("workspace %s is on no server that answered, and %s did not answer\n    check `tailscale status`"
                      % (name, ", ".join(unknown)), NEEDS_PERSON)
    if not held:
        raise Refused("no workspace named %s: `wk ls` lists them, `wk new %s` makes it" % (name, name))
    return held[0]


def hand_over(inv, server, forward=None):
    """Run this same command on `server` and return its exit code. wk's global flags cross as environment. What
    `forward` (wk.core.decl) names is carried to a port on the server chosen by the command's words, so that typing
    it again finds the same one."""
    env = {var: "1" for flag, var in ENV.items() if getattr(act.flags, flag)}
    to, how = forward and forward(inv, inv.fleet.get(server)), {}
    if to:
        port = FORWARD_PORTS[0] + zlib.crc32(" ".join(inv.words).encode()) % FORWARD_PORTS[1]
        env[FORWARD], how["forward"] = str(port), (port, to)
    try:
        r = inv.machine(server).run(["wk"] + inv.words, env=env, stream=True, tty=inv.stdin.isatty(), **how)
    except Unreachable:
        if not to:
            raise
        raise Refused("ssh to %s ended before `wk %s` ran there: %s did not answer, or its port 127.0.0.1:%d is taken and the forward was "
                      "refused (ssh said which, above)\n    a taken port is this same command still running from another terminal: end it, "
                      "then type this again" % (server, " ".join(inv.words), server, port), NEEDS_PERSON)
    sys.stdout.write(r.out)
    sys.stderr.write(r.err)
    return r.rc
