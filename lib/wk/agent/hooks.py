"""The agents' part of `wk doctor` and of the cache `wk gc` keeps."""

import re

from wk.agent import session, wrapper
from wk.agent.session import REMEDY, Session
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.vars import VARS

WRAPPER = {wrapper.HOLDS: OK, wrapper.LEAKS: MISSING, wrapper.ABSENT: MISSING, wrapper.UNKNOWN: UNKNOWN}
NODE = tuple(int(n) for n in VARS["wk_node"].split("."))    # the oldest node pi runs on


def pins(inv, conf):
    return ["agents"]


def node(m):
    """(ok | missing | unknown, why) for the node pi needs."""
    r = m.run(["node", "--version"])
    found = re.match(r"v(\d+)\.(\d+)\.", r.out)
    if not r.ok:
        return MISSING, "no node runs"
    if not found:
        return UNKNOWN, "node --version printed %r" % r.out.strip()
    if tuple(map(int, found.groups())) < NODE:
        return MISSING, "node %s is older than %s" % (r.out.strip(), VARS["wk_node"])
    return OK, r.out.strip()


def doctor(inv, conf, ws):
    if not conf.holds_workspaces:
        return []
    m, fix = inv.machine(conf.name), "wk machine setup %s" % conf.name
    if ws:
        verdict, why, _ = Session(inv, conf, ws, {}).measure()
        return [DoctorRow("credential wrapper", WRAPPER[verdict], why or wrapper.SAYS[conf.kind],
                          REMEDY[verdict] % {"server": conf.name, "ws": ws.name})]
    home = inv.wk_home_on(m)

    def row(name, paths, detail):
        good = all(m.exists(path) for path in paths)
        return DoctorRow(name, OK if good else MISSING, "" if good else detail, "" if good else fix)
    status, why = node(m)
    return [row("claude", [session.claude_bin(home)], "no Claude Code %s in the cache" % VARS["wk_claude_version"]),
            DoctorRow("node", status, why, "" if status == OK else "install node >= %s and npm on %s, then: %s" % (VARS["wk_node"], conf.name, fix)),
            row("pi", [session.pi_bin(home)], "no pi in the cache"),
            row("build-tool shims", ["%s/shims/%s" % (home, tool) for tool in VARS["wk_build_tools"]], "a shim is absent from %s/shims" % home),
            row("agent config", [m.home + "/.claude/" + name for name in ("CLAUDE.md", "settings.json")],
                "CLAUDE.md or settings.json is absent from ~/.claude")]
