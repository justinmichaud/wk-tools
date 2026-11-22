"""`wk ai`: one agent session in a workspace, under the credential wrapper, measured before it starts."""

import json

from wk.agent import install, wrapper
from wk.core.act import die, info
from wk.core.seams import READY, Workspace

INJECTOR_PORT = "8443"      # the injector's, unless the credential server's conf says injector_port=
# Claude Code refuses --dangerously-skip-permissions to root, which the login on a borrowed box is.
CLAUDE_MODE = {"linux": ["--dangerously-skip-permissions"], "mac": ["--dangerously-skip-permissions"],
               "borrowed": ["--permission-mode", "auto"]}
WHY = {wrapper.LEAKS: "does not hold", wrapper.ABSENT: "cannot run", wrapper.UNKNOWN: "could not be measured"}
REMEDY = {wrapper.HOLDS: "",
          wrapper.LEAKS: "no agent starts until it holds: this is a fault in wk (lib/wk/agent/wrapper.py) to report with the line above",
          wrapper.ABSENT: "wk machine setup %(server)s",
          wrapper.UNKNOWN: "check that the workspace runs: wk enter %(ws)s"}


def claude(inv, conf, ws, home):
    """(the start line, what the session's environment adds). The injector swaps the placeholder login in flight, so
    it is the session's proxy and its CA is trusted; the file login is read from ~/.claude on macOS too."""
    credential = inv.fleet.credential
    proxy = "http://%s:%s" % (credential.name, credential.get("injector_port", INJECTOR_PORT))
    return ([install.claude_bin(home)] + CLAUDE_MODE[conf.kind] + ["--remote-control", ws.name],
            {"HTTPS_PROXY": proxy, "https_proxy": proxy, "NODE_EXTRA_CA_CERTS": home + "/injector-ca.pem",
             "CLAUDE_SECURESTORAGE_CONFIG_DIR": inv.machine(conf.name).home + "/.claude", "DISABLE_AUTOUPDATER": "1"})


def pi(inv, conf, ws, home):
    return [install.pi_bin(home)], {}


START = {"claude": claude, "pi": pi}


def held(inv, name):
    """The Workspace called `name` on this server, ready."""
    try:
        rows = json.loads(inv.here.run(["wk", "ls", "--json"], timeout=30).out)["workspaces"]
    except ValueError:
        die("%s did not answer `wk ls --json`, so whether it holds %s is unknown" % (inv.fleet.hostname, name))
    found = [row for row in rows if row["name"] == name]
    if not found:
        die("no workspace named %s on %s: `wk ls` lists them, `wk new %s` makes it" % (name, inv.fleet.hostname, name))
    if found[0]["state"] != READY:
        die("workspace %s is %s, not ready\n    wait for the `wk new` making it, or: wk rm %s && wk new %s"
            % (name, found[0]["state"], name, name))
    return Workspace(server=inv.fleet.hostname, **found[0])


class Session:
    """One workspace's agent session on the server `conf`: its wrapper, its environment, and the wrapper's probe."""

    def __init__(self, inv, conf, ws, extra):
        self.inv, self.conf, self.ws, self.extra = inv, conf, ws, extra
        self.m = inv.machine(conf.name)
        self.driver = inv.driver(ws.driver, self.m)
        self.home = install.wk_home(inv, self.m)
        self.hidden = [self.home + "/secrets"]

    def inside(self, argv):
        return self.m.run(self.driver.exec_argv(self.ws, argv), timeout=30)

    def environment(self):
        """The allowlisted part of the workspace's own environment, this terminal's TERM, the shims first on PATH and
        what the agent adds; None when the workspace's environment cannot be read."""
        env = wrapper.minimal(wrapper.variables(self.inside(["env", "-0"]).out))
        if "HOME" not in env or "PATH" not in env:
            return None
        env.update({name: self.inv.env[name] for name in ("TERM",) if name in self.inv.env})
        env.update(self.extra, WK_AGENT="1", WK_WORKSPACE=self.ws.name, PATH="%s/shims:%s" % (self.home, env["PATH"]))
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
    conf, m, ws = inv.fleet.here, inv.here, held(inv, name)
    argv, extra = START[agent](inv, conf, ws, inv.wk_home)
    if not m.exists(argv[0]):
        die("%s is not installed on %s: there is no %s\n    wk machine setup %s" % (agent, conf.name, argv[0], conf.name))
    session = Session(inv, conf, ws, extra)
    verdict, why, env = session.measure()
    if verdict != wrapper.HOLDS:
        die("the credential wrapper %s for %s on %s: %s\n    %s"
            % (WHY[verdict], name, conf.name, why, REMEDY[verdict] % {"server": conf.name, "ws": name}))
    if agent == "claude":
        trust(m, ws.path + "/src")
    info("starting %s in %s: %s" % (agent, name, wrapper.SAYS[conf.kind]))
    return m.act(session.driver.exec_argv(ws, session.wrap(argv + tail, env), tty=True), stream=True, tty=True).rc


def trust(m, checkout):
    """Claude's start-up dialogs answered for `checkout`, so a session starts instead of stopping at one."""
    path = m.home + "/.claude.json"
    try:
        text = install.answered(m.read(path), checkout)
    except (ValueError, AttributeError, TypeError):
        die("%s is not the JSON object Claude Code keeps there, so its start-up answers cannot be recorded\n"
            "    remove it and start again" % path)
    if text:
        m.write(path, text, mode="600")
