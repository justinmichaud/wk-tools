"""Credentials in `wk doctor`: what delivery leaves on a server, and the store and the injector on the controller."""

from wk.core import hooks
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.creds import agent, injector, net


def delivered(inv, conf, m, wk):
    """One row for what delivery leaves on a server."""
    want = {"%s/%s/.credentials.json" % (wk, agent.LOGIN_DIR): "600", "%s/%s" % (wk, agent.CA): "644",
            wk + "/secrets": "d700", wk + "/secrets/deploy": "d700", wk + "/secrets/env.sh": "600",
            wk + "/secrets/ssh_config": "600", wk + "/secrets/rotate-key": "600", m.home + "/.pi/agent/models.json": "600"}
    lacking = hooks.lacking(m, want)
    return DoctorRow("credentials", MISSING if lacking else OK, lacking and "absent or wrongly moded: " + ", ".join(sorted(lacking)) or "",
                     "wk machine setup %s --stage credentials, typed on %s" % (conf.name, inv.fleet.controller.name))


def holds(status, body):
    """The injector answers a plain request with its banner and whether it holds a login."""
    said = body.decode(errors="replace").splitlines()
    if not said or not said[0].startswith(injector.BANNER.decode()):
        return False, "something else answers there"
    if said[1:] != ["login held"]:
        return False, "".join(said[1:])
    return True, "it holds the claude.ai login"


def answering(url, remedy):
    """The injector's row: it is asked one plain request."""
    try:
        good, detail = holds(*net.request("GET", url))
    except net.NoAnswer as e:
        return DoctorRow("injector", UNKNOWN, "%s did not answer (%s)" % (url, e), remedy)
    return DoctorRow("injector", OK if good else MISSING, detail, remedy)


def doctor(inv, conf, ws):
    if ws is not None:
        return []
    m = inv.machine(conf.name)
    if conf.holds_workspaces:
        return [delivered(inv, conf, m, inv.wk_home_on(m))]
    store = inv.wk_home_on(m) + "/secrets"
    lacking = hooks.lacking(m, {store: "d700"})
    return [DoctorRow("secrets", MISSING if lacking else OK, lacking and "%s is absent or not mode 0700" % store or "",
                      "mkdir -m 700 %s, then `wk key set <name>` for each" % store),
            answering("http://%s/" % agent.injector(inv), "wk machine setup %s --stage controller" % conf.name)]
