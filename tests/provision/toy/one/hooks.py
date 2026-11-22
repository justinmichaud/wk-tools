from wk.core.act import die
from wk.core.decl import Opt
from wk.core.hooks import DoctorRow, GcRow


def doctor(inv, conf, ws):
    """The rows a test wrote as /rows/<name> = status|detail|remedy; for a workspace, one row holding its path."""
    m = inv.machine(conf.name)
    if ws:
        return [DoctorRow("checkout", "ok", ws.path)]
    return [DoctorRow(name, *m.read("/rows/" + name).split("|")) for name in m.listdir("/rows")]


def gc(inv, conf):
    """The rubble a test wrote as /junk/<name> = size|flag; the listing fails while /junk-unlisted is there."""
    m = inv.machine(conf.name)
    if m.exists("/junk-unlisted"):
        die("du died in /junk")
    return [GcRow(name, m.read("/junk/" + name).split("|")[0], lambda name=name: m.remove("/junk/" + name), m.read("/junk/" + name).split("|")[1])
            for name in m.listdir("/junk")]


def gc_flags():
    return [Opt("--deep", "take what costs a rebuild")]
