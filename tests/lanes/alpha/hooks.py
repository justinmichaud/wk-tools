from wk.core.hooks import DoctorRow, GcRow, Stage


def stages(inv, conf):
    return [Stage(30, "alpha-late", lambda: False), Stage(10, "alpha-early", lambda: True)]


def doctor(inv, conf, ws):
    return [DoctorRow("alpha", "ok")]


def gc(inv, conf):
    return [GcRow("alpha-cache", "3 files", lambda: None)]
