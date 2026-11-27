"""Credentials in `wk doctor` and `wk gc`: what delivery leaves on a server, and the injector and the store on the
credential server."""

import json

from wk.core import hooks
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.vars import VARS
from wk.creds import agent, bao, injector, net


def pins(inv, conf):
    return ["openbao-" + VARS["wk_openbao_version"]]


def delivered(conf, m, wk):
    """One row for what delivery leaves on a server."""
    want = {"%s/%s/.credentials.json" % (wk, agent.LOGIN_DIR): "600", "%s/%s" % (wk, agent.CA): "644",
            wk + "/secrets": "d700", wk + "/secrets/deploy": "d700", wk + "/secrets/env.sh": "600",
            wk + "/secrets/ssh_config": "600", m.home + "/.pi/agent/models.json": "600"}
    lacking = hooks.lacking(m, want)
    return DoctorRow("credentials", MISSING if lacking else OK, lacking and "absent or wrongly moded: " + ", ".join(sorted(lacking)) or "",
                     "wk key sync %s" % conf.name)


def asked(name, url, remedy, verdict):
    """A row for a service asked one plain request: `verdict(status, body)` is (whether it is as it should be, the detail)."""
    try:
        good, detail = verdict(*net.request("GET", url))
    except net.NoAnswer as e:
        return DoctorRow(name, UNKNOWN, "%s did not answer (%s)" % (url, e), remedy)
    return DoctorRow(name, OK if good else MISSING, detail, remedy)


def holds(status, body):
    """The injector answers a plain request with its banner and whether it holds a login."""
    said = body.decode(errors="replace").splitlines()
    if not said or not said[0].startswith(injector.BANNER.decode()):
        return False, "something else answers there"
    if said[1:] != ["login held"]:
        return False, "".join(said[1:])
    return True, "it holds the claude.ai login"


def unsealed(status, body):
    said = json.loads(body) if status == 200 else {}
    return said.get("initialized") is True and said.get("sealed") is False, "HTTP %d, initialized %s, sealed %s" % (
        status, said.get("initialized"), said.get("sealed"))


def doctor(inv, conf, ws):
    if ws is not None:
        return []
    m, remedy = inv.machine(conf.name), "wk machine setup %s" % conf.name
    if conf.holds_workspaces:
        return [delivered(conf, m, inv.wk_home_on(m))]
    return [asked("injector", "http://%s/" % agent.injector(inv), remedy, holds),
            asked("openbao", bao.address(inv) + "/v1/sys/seal-status", remedy, unsealed)]
