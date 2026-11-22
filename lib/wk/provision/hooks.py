"""The provisioning lane's own stage and rows: wk itself on a server, and the stale downloads in its cache.

A lane that keeps a pinned download in <wk_home>/cache names it from its own hooks module, and `wk gc` reclaims every
entry no lane names:

    def pins(inv, conf): ...        # -> [the names in <wk_home>/cache this lane's current pins use]"""

from wk.core import hooks
from wk.core.act import die
from wk.provision import rows, tools


def stages(inv, conf):
    return [hooks.Stage(10, "wk", lambda: tools.converge(inv, conf))]


def doctor(inv, conf, ws):
    return [] if ws else rows.server(inv, conf)


def stale(m, path):
    r = m.run(["du", "-sk", path])
    if not r.ok:
        die("cannot size %s on %s: %s" % (path, m.name, r.err.strip()))
    return hooks.GcRow("cache/" + path.split("/")[-1], "%d MB" % (int(r.out.split()[0]) // 1024), lambda: m.remove(path))


def gc(inv, conf):
    m, cache = inv.machine(conf.name), tools.wk_home(inv, conf) + "/cache"
    pinned = hooks.collect("pins", inv, conf)
    return [stale(m, cache + "/" + name) for name in m.listdir(cache) if name not in pinned]
