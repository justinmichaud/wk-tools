"""The provisioning lane's own rows: wk itself on a server, and the downloads in its cache no lane pins."""

from wk.core import hooks
from wk.core.act import die
from wk.provision import rows


def doctor(inv, conf, ws):
    return [] if ws else rows.server(inv, conf)


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
