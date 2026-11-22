from wk.core.act import die
from wk.core.decl import Opt
from wk.core.hooks import DoctorRow, GcRow, Stage


def mark(inv, conf, name):
    """A stage that writes /done/<name> once, and fails while /fail/<name> is there."""
    def run():
        m = inv.machine(conf.name)
        if m.exists("/fail/" + name):
            die("%s broke" % name)
        if m.exists("/done/" + name):
            return False
        m.write("/done/" + name, "1")
        return True
    return run


def stages(inv, conf):
    return [Stage(30, "third", mark(inv, conf, "third")), Stage(10, "first", mark(inv, conf, "first"))]


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
