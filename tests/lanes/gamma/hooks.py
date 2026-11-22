from wk.core.hooks import DoctorRow, Stage


def stages(inv, conf):
    return [Stage(20, "gamma-%s" % conf, lambda: False)]


def doctor(inv, conf, ws):
    return [DoctorRow("gamma", "unknown", "the probe died", "wk machine setup %s" % conf)]
