"""What a machine asks the controller's API (wk.api.server), over HTTP on the tailnet.

    reserve(inv, "x")      wk new: the name is this server's, or the command ends
    release(inv, "x")      wk rm: the name is free again
    holders(inv)           {name: server}: wk ls and wk zed off a server, and wk rm of a name with no workspace
    rotate(inv)            wk ai: this server's push keys replaced, and the new ones written under its secrets

A request that changes the record is printed, not sent, under --dry-run."""

import re

from wk.core import act
from wk.core.act import barrier, die, info, warn
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.creds import rules

FORK = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
ROTATE_TIMEOUT = 120
ADDRESS_TIMEOUT = 10


def port(conf):
    return int(conf.get("api_port", VARS["wk_api_port"]))


def ask(inv, method, path, timeout=None):
    """(status, the answer), or (None, {"message": why}) when the controller gave none. A change goes through `act`.
    The controller is reached at the address the tailnet gives its name: on the controller the name is loopback's."""
    controller = inv.fleet.controller
    r = inv.here.run(["tailscale", "ip", "-4", controller.name], timeout=ADDRESS_TIMEOUT)
    if not r.ok or not r.out.split():
        return None, {"message": "`tailscale ip -4 %s` said %s" % (controller.name, (r.err or r.out).strip() or "nothing")}
    url = "http://%s:%d%s" % (r.out.split()[0], port(controller), path)
    send = lambda: rules.ask(method, url, **({"timeout": timeout} if timeout else {}))     # noqa: E731
    return send() if method == "GET" else act.act(controller.name, "%s %s" % (method, path), send, (200, {}))


def said(inv, status, doc):
    name = inv.fleet.controller.name
    if status is None:
        return "the controller, %s, did not answer (%s)" % (name, doc["message"])
    return "the controller, %s, answered HTTP %d: %s" % (name, status, doc.get("message", ""))


def reserve(inv, name):
    status, doc = ask(inv, "PUT", "/ws/" + name)
    if status == 409:
        die("workspace %s is %s's: `wk rm %s` there removes it, or pick another name" % (name, doc.get("holder"), name))
    if status != 200:
        barrier("%s\n    so %s is not reserved for this server; forced, the workspace is made here with no reservation"
                % (said(inv, status, doc), name), NEEDS_PERSON)


def release(inv, name):
    status, doc = ask(inv, "DELETE", "/ws/" + name)
    if status == 409:
        warn("the name %s is %s's, so it is left as it is" % (name, doc.get("holder")))
    elif status not in (200, 404):
        barrier("%s\n    so the name %s is still reserved: `wk rm %s` again releases it" % (said(inv, status, doc), name, name), NEEDS_PERSON)


def holders(inv):
    status, doc = ask(inv, "GET", "/ws")
    if status != 200 or not isinstance(doc.get("names"), dict):
        die("%s\n    so which server holds which workspace is unknown: check `tailscale status`" % said(inv, status, doc), NEEDS_PERSON)
    return doc["names"]


def rotate(inv):
    """This server's push keys replaced by the controller and written here. A rotation that fails is a barrier: forced,
    the session starts with the keys this server has."""
    status, keys = ask(inv, "POST", "/rotate", ROTATE_TIMEOUT)
    if act.flags.dry_run:
        return
    if status != 200:
        why = said(inv, status, keys)
    elif not keys or not all(FORK.fullmatch(k) and isinstance(v, str) for k, v in keys.items()):
        why = "the controller's answer holds no push keys"
    else:
        why = None
    if why:
        barrier("%s\n    so %s's push keys are not rotated; forced, the session starts with the keys this server has"
                % (why, inv.fleet.hostname), NEEDS_PERSON)
        return
    for fork, private in sorted(keys.items()):
        inv.here.write("%s/secrets/deploy/%s" % (inv.wk_home, fork.replace("/", "--")), private, mode="600")
    info("rotated the push keys of %s" % ", ".join(sorted(keys)))
