from wk.core.hooks import DoctorRow


def doctor(inv, conf, ws):
    return [DoctorRow("gamma", "unknown", "the probe died", "wk machine setup %s" % conf)]
