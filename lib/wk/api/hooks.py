"""The controller's API in `wk doctor`: whether it answers this machine, asked from here as a command would ask it."""

from wk.api import client
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow


def doctor(inv, conf, ws):
    if ws is not None:
        return []
    controller = inv.fleet.controller.name
    status, doc = client.ask(inv, "GET", "/ws")
    remedy = ("wk machine setup %s --stage controller; and the tailnet's policy lets %s reach %s:%d"
              % (controller, conf.name, controller, client.port(inv.fleet.controller)))
    if status == 200:
        return [DoctorRow("api", OK, "%s answers: %d workspace names" % (controller, len(doc.get("names", {}))))]
    return [DoctorRow("api", MISSING if status else UNKNOWN, client.said(inv, status, doc), remedy)]
