"""Credentials in `wk machine setup` and `wk doctor`: delivery to each of your servers, the injector on the credential
server, and nothing on a borrowed box."""

from wk.core import hooks
from wk.creds import deliver, service

MANAGED = ("linux", "mac")


def stages(inv, conf):
    if conf.role:
        return [hooks.Stage(60, "injector", lambda: service.setup(inv, conf))]
    if conf.kind in MANAGED:
        return [hooks.Stage(60, "credentials", lambda: deliver.converge(inv, conf))]
    return []


def doctor(inv, conf, ws):
    if ws is not None or conf.kind not in MANAGED:
        return []
    if conf.role:
        return [service.doctor(inv, conf)]
    remedy = "wk key sync %s" % conf.name
    machine = inv.machine(conf.name)
    wk = inv.wk_home_on(machine)
    wanted = [(wk + "/secrets", "700"), (wk + "/secrets/deploy", "700"), (wk + "/secrets/env.sh", "600"),
              (wk + "/secrets/ssh_config", "600"), ("%s/%s/.credentials.json" % (wk, deliver.LOGIN_DIR), "600"),
              ("%s/%s" % (wk, deliver.CA), "644"), (machine.home + "/.pi/agent/models.json", "600")]
    have = deliver.status(machine, [path for path, _ in wanted])
    if have is None:
        return [hooks.DoctorRow("credentials", hooks.UNKNOWN, "%s could not say what it holds" % conf.name, remedy)]
    rows = []
    for path, mode in wanted:
        if have[path] is None:
            rows.append(hooks.DoctorRow(path, hooks.MISSING, "absent", remedy))
        elif have[path][0] != mode:
            rows.append(hooks.DoctorRow(path, hooks.MISSING, "mode %s, not %s" % (have[path][0], mode), remedy))
        else:
            rows.append(hooks.DoctorRow(path, hooks.OK))
    return rows
