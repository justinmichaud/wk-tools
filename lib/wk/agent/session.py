"""`wk ai`: one agent session in a workspace, under the credential wrapper, measured before it starts. The agents
themselves are the playbook's agents role's to install, under <wk_home>/cache/agents."""

import json

from wk import creds
from wk.agent import wrapper
from wk.core.act import barrier, die, info
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.workspace import lifecycle

# On a server of the owner's an agent asks permission for nothing (DESIGN.md, Trust).
OWN_MODE = ["--dangerously-skip-permissions"]
# A borrowed box is not the owner's, and Claude Code refuses --dangerously-skip-permissions to root, which a login there may be.
BORROWED_MODE = ["--permission-mode", "auto"]
CLAUDE_MODE = {"linux": OWN_MODE, "mac": OWN_MODE, "borrowed": BORROWED_MODE}
WHY = {wrapper.LEAKS: "does not hold", wrapper.ABSENT: "cannot run", wrapper.UNKNOWN: "could not be measured"}
REMEDY = {wrapper.HOLDS: "",
          wrapper.LEAKS: "no agent starts until it holds: this is a fault in wk (lib/wk/agent/wrapper.py) to report with the line above",
          wrapper.ABSENT: "wk machine setup %(server)s",
          wrapper.UNKNOWN: "check that the workspace runs: wk enter %(ws)s"}


def claude_bin(home):
    return "%s/cache/agents/claude/%s/.local/share/claude/versions/%s" % (home, VARS["wk_claude_version"], VARS["wk_claude_version"])


def pi_bin(home):
    return home + "/cache/agents/pi/node_modules/.bin/pi"


def claude(inv, conf, ws, home):
    """(the start line, what the session's environment adds)."""
    return ([claude_bin(home)] + CLAUDE_MODE[conf.kind] + ["--remote-control", ws.name],
            dict(creds.agent_env(inv), DISABLE_AUTOUPDATER="1"))


def pi(inv, conf, ws, home):
    return [pi_bin(home)], {}


START = {"claude": claude, "pi": pi}


class Session:
    """One workspace's agent session on the server `conf`: its wrapper, its environment, and the wrapper's probe."""

    def __init__(self, inv, conf, ws, extra):
        self.inv, self.conf, self.ws, self.extra = inv, conf, ws, extra
        self.m = inv.machine(conf.name)
        self.driver = inv.driver(ws.driver, self.m)
        self.home = inv.wk_home_on(self.m)
        self.hidden = [self.home + "/secrets"]

    def inside(self, argv):
        return self.m.run(self.driver.exec_argv(self.ws, argv), timeout=30)

    def environment(self):
        """The allowlisted part of the workspace's own environment, this terminal's TERM, the shims and then `wk`
        first on PATH and what the agent adds; None when the workspace's environment cannot be read."""
        env = wrapper.minimal(wrapper.variables(self.inside(["env", "-0"]).out))
        if "HOME" not in env or "PATH" not in env:
            return None
        env.update({name: self.inv.env[name] for name in ("TERM",) if name in self.inv.env})
        env.update(self.extra, WK_AGENT="1", WK_WORKSPACE=self.ws.name, PATH="%s/shims:%s/bin:%s" % (self.home, self.home, env["PATH"]))
        return env

    def wrap(self, argv, env):
        return wrapper.wrap(self.conf.kind, argv, self.hidden, env)

    def measure(self):
        """(holds | leaks | absent | unknown, why, the session's environment): a stand-in under the session's own
        wrapper lists each hidden directory and prints its environment."""
        env, tool = self.environment(), wrapper.TOOL[self.conf.kind]
        if env is None:
            return wrapper.UNKNOWN, "`env -0` in the workspace printed no HOME and PATH", None
        if not self.inside(["sh", "-c", "command -v " + tool]).ok:
            return wrapper.ABSENT, "the workspace has no %s, which the wrapper is made of" % tool, env
        # LC_ALL=C: the verdict reads ls's own words.
        listings = [(path, self.inside(self.wrap(["ls", "-A", path], dict(env, LC_ALL="C")))) for path in self.hidden]
        return wrapper.verdict(listings, self.inside(self.wrap(["env", "-0"], env)), env) + (env,)


def start(inv, agent, name, tail):
    if agent not in START:
        die("no agent named %s: the agents are %s" % (agent, " and ".join(START)))
    tty = inv.stdin.isatty()
    if not tty and not tail:
        die("an agent's session needs a terminal and there is none: give the agent its prompt, `wk ai %s %s -- -p <prompt>`" % (agent, name),
            NEEDS_PERSON)
    conf, m, ws = inv.fleet.here, inv.here, lifecycle.ready(inv, name)
    argv, extra = START[agent](inv, conf, ws, inv.wk_home)
    if not m.exists(argv[0]):
        die("%s is not installed on %s: there is no %s\n    wk machine setup %s" % (agent, conf.name, argv[0], conf.name))
    session = Session(inv, conf, ws, extra)
    verdict, why, env = session.measure()
    if verdict != wrapper.HOLDS:
        refuse = barrier if env and verdict != wrapper.ABSENT else die      # --force starts an agent under a wrapper that runs
        refuse("the credential wrapper %s for %s on %s: %s\n    %s"
               % (WHY[verdict], name, conf.name, why, REMEDY[verdict] % {"server": conf.name, "ws": name}))
    if agent == "claude":
        trust(m, ws.path + "/src")
    info("starting %s in %s: %s" % (agent, name, wrapper.SAYS[conf.kind]))
    return m.act(session.driver.exec_argv(ws, session.wrap(argv + tail, env), tty=tty), stream=True, tty=tty).rc


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
