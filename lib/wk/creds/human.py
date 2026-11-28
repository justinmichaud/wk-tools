"""What a person's session holds and an agent's never does (DESIGN.md, Trust): one push key per fork and the logins
git-webkit and gh read, as shell variables.

    made(inv, "moose")      on the controller: {"deploy": {fork: private key}, "env": {variable: value}}
    script(env)             the file `wk enter` writes as <wk home>/secrets/credentials.sh, which env.sh sources
    agents(m)               the agent sessions running on a server, which can read whatever a person's session holds

The controller makes them at a person's request, the API's POST /credentials, and `wk enter` writes them; `wk ai`
removes them and has the controller revoke the push keys before an agent starts."""

import re
import shlex
import urllib.parse

from wk.creds import rotation
from wk.webkit import PROJECT

FILE = "credentials.sh"
BUGZILLA = urllib.parse.urlsplit(PROJECT["bugzilla"]).hostname.upper().replace(".", "_")
# What git-webkit (webkitcorepy.credentials: <HOST>_USERNAME and <HOST>_TOKEN or _PASSWORD) and gh read.
ENV = (("GITHUB_COM_USERNAME", "github-user"), ("GITHUB_COM_TOKEN", "github-token"), ("GH_TOKEN", "github-token"),
       (BUGZILLA + "_USERNAME", "bugzilla-user"), (BUGZILLA + "_PASSWORD", "bugzilla-password"))
# An agent's own process: its program is one the agents role installed. `ps`, which Linux and macOS read alike.
AGENT = re.compile(r"\s*(\d+) (\S*/cache/agents/(claude|pi)/\S*)(.*)")


def made(inv, server):
    store = inv.secrets()
    env = {var: store.get(name) for var, name in ENV}
    return {"deploy": rotation.rotate(inv, server), "env": {var: value for var, value in env.items() if value}}


def script(env):
    return "# Written by `wk enter` for a person's session; `wk ai` removes it.\n" + "".join(
        "export %s=%s\n" % (var, shlex.quote(value)) for var, value in sorted(env.items()))


def agents(m):
    """One line per running agent session: its process id, the agent, and its words."""
    found = [AGENT.fullmatch(line) for line in m.run(["ps", "-A", "-o", "pid=,args="]).out.splitlines()]
    return ["%s %s%s" % (f.group(1), f.group(3), f.group(4)[:100]) for f in found if f]
