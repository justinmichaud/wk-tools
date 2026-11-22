"""The agents' part of `wk machine setup` and `wk doctor`."""

from wk.agent import install, wrapper
from wk.agent.session import REMEDY, Session
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow, Stage

WRAPPER = {wrapper.HOLDS: OK, wrapper.LEAKS: MISSING, wrapper.ABSENT: MISSING, wrapper.UNKNOWN: UNKNOWN}


def stages(inv, conf):
    if not conf.holds_workspaces:
        return []
    m = inv.machine(conf.name)
    return [Stage(50, "claude", lambda: install.claude(m, install.wk_home(inv, m), conf.name)),
            Stage(50, "pi", lambda: install.pi(m, install.wk_home(inv, m), conf.name)),
            Stage(50, "build-tool shims", lambda: install.put(m, install.shims(install.wk_home(inv, m)), mode="755")),
            Stage(50, "agent config", lambda: install.put(m, install.config(m)))]


def doctor(inv, conf, ws):
    if not conf.holds_workspaces:
        return []
    m, fix = inv.machine(conf.name), "wk machine setup %s" % conf.name
    if ws:
        verdict, why, _ = Session(inv, conf, ws, {}).measure()
        return [DoctorRow("credential wrapper", WRAPPER[verdict], why or wrapper.SAYS[conf.kind],
                          REMEDY[verdict] % {"server": conf.name, "ws": ws.name})]
    home = install.wk_home(inv, m)

    def row(name, good, detail):
        return DoctorRow(name, OK if good else MISSING, "" if good else detail, "" if good else fix)
    node, why = install.node(m)
    return [row("claude", m.exists(install.claude_bin(home)), "no Claude Code %s in the cache" % install.CLAUDE_VERSION),
            DoctorRow("node", node, why, "" if node == OK else "install node >= %d.%d and npm on %s, then: %s" % (install.NODE + (conf.name, fix))),
            row("pi", m.exists(install.pi_bin(home)), "no pi in the cache"),
            row("build-tool shims", not install.stale(m, install.shims(home)), "a shim under %s/shims is absent or differs" % home),
            row("agent config", not install.stale(m, install.config(m)), "CLAUDE.md or settings.json under ~/.claude is absent or differs")]
