from wk.core.hooks import DoctorRow, GcRow


def doctor(inv, conf, ws):
    return [DoctorRow("alpha", "ok")]


def gc(inv, conf):
    return [GcRow("alpha-cache", "3 files", lambda: None)]
