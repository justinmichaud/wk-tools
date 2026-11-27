"""The provisioning lane's own rows: wk itself on a server, what the controller needs, and the downloads in a cache no
lane pins."""

from wk.core import hooks
from wk.core.act import die
from wk.core.vars import VARS
from wk.provision import rows


def doctor(inv, conf, ws):
    if ws is not None:
        return []
    return rows.server(inv, conf) if conf.holds_workspaces else rows.controller(inv, conf)


def pins(inv, conf):
    return ["websocat-" + VARS["wk_websocat_version"]] if conf.kind == "controller" else []


def stale(m, path):
    r = m.run(["du", "-sk", path])
    if not r.ok:
        die("cannot size %s on %s: %s" % (path, m.name, r.err.strip()))
    return hooks.GcRow("cache/" + path.split("/")[-1], "%d MB" % (int(r.out.split()[0]) // 1024), lambda: m.remove(path))


def gc(inv, conf):
    m = inv.machine(conf.name)
    cache = inv.wk_home_on(m) + "/cache"
    pinned = hooks.collect("pins", inv, conf)
    return [stale(m, cache + "/" + name) for name in m.listdir(cache) if name not in pinned]
