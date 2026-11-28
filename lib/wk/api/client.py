"""What a machine asks the controller's API (wk.api.server), over HTTP on the tailnet.

    reserve(inv, "x")      wk new: the name is this server's, or the command ends
    release(inv, "x")      wk rm: the name is free again
    holders(inv)           {name: server}: wk ls and wk zed off a server, and wk rm of a name with no workspace
    credentials(inv)       wk enter: a person's credentials, push keys made anew, written under this server's secrets
    revoke(inv)            wk ai: those removed here and this server's push keys revoked, before an agent starts

A request that changes the record is printed, not sent, under --dry-run."""

import re

from wk.core import act
from wk.core.act import barrier, die, info, warn
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.creds import human, rules

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


def written(inv):
    """The files a person's credentials are written to: the variables, and one push key per fork."""
    home = inv.wk_home + "/secrets"
    return home + "/" + human.FILE, home + "/deploy"


def credentials(inv):
    """A person's credentials asked of the controller and written here, unless they are here already. One that cannot
    be had is said, and the shell opens without them."""
    script, deploy = written(inv)
    if inv.here.exists(script):
        return
    status, doc = ask(inv, "POST", "/credentials", ROTATE_TIMEOUT)
    if act.flags.dry_run:
        return
    keys, env = (doc.get("deploy"), doc.get("env")) if status == 200 else (None, None)
    if not (isinstance(keys, dict) and keys and all(FORK.fullmatch(k) and isinstance(v, str) for k, v in keys.items())
            and isinstance(env, dict)):
        warn("%s\n    so this shell has no push keys and no GitHub or Bugzilla login: `exit` and `wk enter` again asks once more"
             % (said(inv, status, doc) if status != 200 else "the controller's answer holds no credentials"))
        return
    for fork, private in sorted(keys.items()):
        inv.here.write("%s/%s" % (deploy, fork.replace("/", "--")), private, mode="600")
    inv.here.write(script, human.script(env), mode="600")
    info("delivered this session's push keys for %s and its logins (%s)" % (", ".join(sorted(keys)), ", ".join(sorted(env)) or "none"))


def revoke(inv):
    """A person's credentials removed from this server, then its push keys revoked by the controller, so an agent
    starts with none and any copy is dead. A revocation that fails is a barrier: forced, the session starts with the
    files removed and the keys still registered."""
    script, deploy = written(inv)
    for path in [script] + ["%s/%s" % (deploy, f) for f in sorted(inv.here.listdir(deploy) or [])]:
        if inv.here.exists(path):
            inv.here.remove(path)
    status, doc = ask(inv, "POST", "/revoke", ROTATE_TIMEOUT)
    if act.flags.dry_run or status == 200:
        return
    barrier("%s\n    so %s's push keys are still registered on the forks; forced, the session starts with them removed here"
            % (said(inv, status, doc), inv.fleet.hostname), NEEDS_PERSON)
