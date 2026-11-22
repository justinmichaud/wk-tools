"""What a server needs for tasks: the pinned pueue, its daemon, and the one-at-a-time build group."""

import json

from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.tasks.pueue import LIMITS, VERSION, Pueue


def pins(inv, conf):
    return ["pueue-" + VERSION]


def doctor(inv, conf, ws):
    if ws is not None:
        return []
    m, remedy = inv.machine(conf.name), "wk machine setup %s" % conf.name
    pueue = Pueue(inv, m)
    installed = all(m.exists("%s/%s" % (pueue.bin, tool)) for tool in ("pueue", "pueued"))
    r = m.run(pueue.argv("status", "--json"))
    rows = [DoctorRow("pueue", OK, VERSION) if installed else DoctorRow("pueue", MISSING, "%s is not in %s" % (VERSION, pueue.bin), remedy)]
    if not r.ok:
        return rows + [DoctorRow("pueued", MISSING, "the daemon does not answer", remedy),
                       DoctorRow("build group", UNKNOWN, "the daemon does not answer", remedy)]
    limit = json.loads(r.out)["groups"].get("build", {}).get("parallel_tasks")
    return rows + [DoctorRow("pueued", OK), DoctorRow("build group", OK, "one build at a time") if limit == LIMITS["build"]
                   else DoctorRow("build group", MISSING, "its parallel limit is %s, not %d" % (limit, LIMITS["build"]), remedy)]
