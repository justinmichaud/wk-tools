"""`wk ai`: one agent session in a workspace, started as `wk enter` starts a command: the login's own shell, in the
checkout, inside the container on Linux, once the controller has rotated the server's push keys. The agents themselves
are the playbook's agents role's to install, under <wk_home>/cache/agents."""

import json

from wk import creds
from wk.api import client
from wk.core.act import die, info
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.workspace import lifecycle

# On a server of the owner's an agent asks permission for nothing (DESIGN.md, Trust).
MODE = ["--dangerously-skip-permissions"]


def claude_bin(home):
    return "%s/cache/agents/claude/%s/.local/share/claude/versions/%s" % (home, VARS["wk_claude_version"], VARS["wk_claude_version"])


def pi_bin(home):
    return home + "/cache/agents/pi/node_modules/.bin/pi"


def claude(inv, ws, home):
    """(the start line, what the session's environment adds)."""
    return [claude_bin(home)] + MODE + ["--remote-control", ws.name], dict(creds.agent_env(inv), DISABLE_AUTOUPDATER="1")


def pi(inv, ws, home):
    return [pi_bin(home)], {}


START = {"claude": claude, "pi": pi}
# What the playbook installs an agent from; it installs none of it.
NEEDS = {"claude": "", "pi": "install node >= %s and npm on %%s, then: " % VARS["wk_node"]}


def start(inv, agent, name, tail):
    if agent not in START:
        die("no agent named %s: the agents are %s" % (agent, " and ".join(START)))
    tty = inv.stdin.isatty()
    if not tty and not tail:
        die("an agent's session needs a terminal and there is none: give the agent its prompt, `wk ai %s %s -- -p <prompt>`" % (agent, name),
            NEEDS_PERSON)
    conf, m, ws = inv.fleet.here, inv.here, lifecycle.ready(inv, name)
    argv, extra = START[agent](inv, ws, inv.wk_home)
    if not m.exists(argv[0]):
        die("%s is not installed on %s: there is no %s\n    %swk machine setup %s, typed on %s"
            % (agent, conf.name, argv[0], NEEDS[agent] and NEEDS[agent] % conf.name, conf.name, inv.fleet.controller.name))
    if agent == "claude":
        trust(m, ws.path + "/src")
    client.rotate(inv)
    info("starting %s in %s" % (agent, name))
    env = ["env", "WK_WORKSPACE=" + ws.name] + ["%s=%s" % kv for kv in sorted(extra.items())]
    return m.act(inv.driver(ws.driver, m).enter_argv(ws, env + argv + tail, tty=tty), stream=True, tty=tty).rc


def answered(text, checkout):
    """~/.claude.json with onboarding, the Remote Control dialog and trust in `checkout` answered, or None when it
    already says so. `text` is the file as it is, None when there is none."""
    doc = json.loads(text or "{}")
    before = json.dumps(doc, sort_keys=True)
    doc.update(hasCompletedOnboarding=True, remoteDialogSeen=True)
    doc.setdefault("projects", {}).setdefault(checkout, {})["hasTrustDialogAccepted"] = True
    return None if json.dumps(doc, sort_keys=True) == before else json.dumps(doc, indent=2) + "\n"


def trust(m, checkout):
    """Claude's start-up dialogs answered for `checkout`, so a session starts instead of stopping at one."""
    path = m.home + "/.claude.json"
    try:
        text = answered(m.read(path), checkout)
    except (ValueError, AttributeError, TypeError):
        die("%s is not the JSON object Claude Code keeps there, so its start-up answers cannot be recorded\n"
            "    remove it and start again" % path)
    if text:
        m.write(path, text, mode="600")
