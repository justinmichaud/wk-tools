"""The tailnet API and the one auth key a machine joins nodes with. Exit: 0 done, 1 no auth key, 2 no such node or a usage
error, 3 online, 4 no credential, 5 refused, 6 unreachable."""

import argparse
import base64
import io
import json
import os
import sys

import credcheck
from credcheck import Unreachable
from wk import act, images
from wk.machine import Local, Result

API = "https://api.tailscale.com/api/v2"
TAG = "tag:wk"
KEY_DAYS = 90        # tailscale's own ceiling for an auth key


def api_url(env):
    return env.get("WK_TAILNET_API") or API


def fleet_tag(env):
    return env.get("WK_TAILNET_TAG") or TAG


class Failed(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def urllib_transport(method, url, headers, data):
    status, _headers, raw = credcheck.http(method, url, headers.items(), data, timeout=30)
    return status, raw


def secret(value, path):
    key = (value or "").strip()
    if not key:
        raise Failed(4, "no tailnet API credential (%s)" % (path or "unset"))
    if not usable("tailnet-api", key)[0]:
        raise Failed(4, "%s does not hold a tailnet API access key (they start 'tskey-api-')" % path)
    return key


def usable(rule, value):
    verdict, why = credcheck.RULES[rule].check(value, [], "", {})
    return verdict != credcheck.BAD, why


class Api:
    def __init__(self, key, base=API, transport=urllib_transport):
        self.key, self.base, self.transport = key, base, transport

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Authorization": "Basic " + base64.b64encode((self.key + ":").encode()).decode()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            status, raw = self.transport(method, self.base + path, headers, data)
        except Unreachable as e:
            raise Failed(6, "could not reach the tailnet API: %s" % e)
        if status in (401, 403):
            raise Failed(5, "the tailnet refused this credential (HTTP %d). Rotate it: wk key set tailnet-api --replace" % status)
        if status >= 400:
            raise Failed(5, "the tailnet API said HTTP %d for %s %s: %s"
                         % (status, method, path, raw.decode(errors="replace").strip()[:200]))
        return json.loads(raw) if raw else {}

    def devices(self):
        return self.call("GET", "/tailnet/-/devices").get("devices") or []


def matches(devs, name):
    """A rename leaves `name` and `hostname` disagreeing; a prefix would take `rpi3-bench` with `rpi3`."""
    want = name.lower()
    return [d for d in devs if want in {(d.get("name") or "").split(".")[0].lower(), (d.get("hostname") or "").lower()}]


def retire(api, name, out):
    hits = matches(api.devices(), name)
    if not hits:
        raise Failed(2, "no node named '%s' on the tailnet" % name)
    online = [d for d in hits if d.get("online")]
    if online:
        raise Failed(3, "'%s' is online right now (id %s, last seen %s). A live node of that name is a\n"
                        "    running board, not a leftover; nothing here removes one."
                     % (name, online[0].get("id", "?"), online[0].get("lastSeen", "?")))
    for d in hits:
        api.call("DELETE", "/device/%s" % d["id"])
        out.write("retired %s (id %s, last seen %s)\n" % (d.get("name", name), d.get("id", "?"), d.get("lastSeen", "unknown")))


def live(api, key):
    """Only "the tailnet has no such key" counts against one: a tailnet that could not be asked is no evidence."""
    try:
        keys = api.call("GET", "/tailnet/-/keys").get("keys") or []
    except Failed:
        return True
    return any(k.get("id") == (key.split("-") + ["", "", ""])[2] for k in keys)


class Fleet:
    """This machine's two tailnet credentials: the auth key it joins nodes with, and the API key that administers."""

    def __init__(self, root, env=None, machine=None, transport=urllib_transport):
        from wk.secrets import Secrets
        self.root, self.env = str(root), os.environ if env is None else env
        self.machine = machine or Local()
        self.sec = Secrets(self.root, self.env, self.machine)
        self.transport = transport

    def api(self):
        value = self.sec.cred_read("tailnet-api")
        if not (value or "").strip():
            return None
        try:
            key = secret(value, self.sec.cred_path("tailnet-api"))
        except Failed as e:
            act.die("%s -- store the API access key: wk key set tailnet-api --replace" % e, e.code)
        return Api(key, api_url(self.env), self.transport)

    def api_present(self):
        return self.api() is not None

    def stored(self):
        return (self.sec.cred_read("tailnet") or "").split("\n")[0].strip()

    def key_present(self):
        """A usable auth key on file, or the API credential that mints one."""
        key = self.stored()
        return bool(key and usable("tailnet", key)[0]) or self.api_present()

    def authkey(self):
        """The key file: the stored key while the tailnet has it, else one minted where the API credential is; "" if none."""
        path, api, key = self.sec.cred_path("tailnet"), self.api(), self.stored()
        ok, why = usable("tailnet", key)
        if key and ok and (api is None or live(api, key)):
            return path
        if api is not None:
            return self.mint(api, path, fleet_tag(self.env))
        if not key:
            act.warn("no tailnet auth key on this machine (%s) -- store one: wk key set tailnet" % path)
        else:
            act.warn("%s is not usable: %s\n  Leaving it in place rather than deleting it -- check it and re-run." % (path, why))
        return ""

    def key(self):
        return self.stored() if self.authkey() else ""

    def mint(self, api, path, tag):
        if act.dry_run():
            act.log("would mint a tailnet auth key for %s into %s" % (tag, path))
            return ""
        body = {"capabilities": {"devices": {"create": {"reusable": True, "ephemeral": False, "preauthorized": True, "tags": [tag]}}},
                "expirySeconds": KEY_DAYS * 86400, "description": "wk fleet key"}
        try:
            key = api.call("POST", "/tailnet/-/keys", body).get("key") or ""
        except Failed as e:
            act.warn("the tailnet minted no auth key for %s: %s\n  An API credential that may not grant that tag fails "
                     "exactly here:\n      wk key set tailnet-api     replace the credential that mints\n"
                     "      wk key set tailnet         store a key by hand instead" % (tag, e))
            return ""
        ok, why = usable("tailnet", key)
        if not ok:
            act.warn("the tailnet returned something that is not an auth key: %s" % why)
            return ""
        self.machine.mkdir(os.path.dirname(path))
        if not self.machine.act_run(["python3", os.path.join(self.root, "lib", "secretfile.py"), "write", path],
                                    input=key + "\n").ok:
            act.warn("could not store the minted auth key at %s" % path)
            return ""
        act.info("minted a tailnet auth key for %s (reusable, %d days) -- %s" % (tag, KEY_DAYS, path))
        return path

    def retire(self, name):
        api = self.api()
        if api is None:
            return Result(4, "", "wk-tailnet: no tailnet API credential (%s)\n" % self.sec.cred_path("tailnet-api"))
        if act.dry_run():
            act.log("would retire the tailnet node '%s'" % name)
            return Result(0)
        out = io.StringIO()
        try:
            retire(api, name, out)
        except Failed as e:
            return Result(e.code, out.getvalue(), "wk-tailnet: %s\n" % e)
        return Result(0, out.getvalue())


def main(argv, env=None, out=None, transport=urllib_transport):
    p = argparse.ArgumentParser(prog="python3 -m wk.tailnet")
    p.add_argument("verb", choices=("check", "authkey"),
                   help="check: the API credential at $WK_TS_API_SECRET_FILE works; authkey: the path of a usable auth key")
    verb = p.parse_args(argv).verb
    env = os.environ if env is None else env
    out = out or sys.stdout
    if verb == "authkey":
        path = Fleet(images.root(env), env, transport=transport).authkey()
        out.write(path)
        return 0 if path else 1
    path = env.get("WK_TS_API_SECRET_FILE", "")
    try:
        try:
            value = Local().read(path) if path else ""
        except OSError:
            value = ""
        n = len(Api(secret(value, path), api_url(env), transport).devices())
        out.write("ok: the credential works (%d device%s on the tailnet)\n" % (n, "" if n == 1 else "s"))
    except Failed as e:
        sys.stderr.write("wk-tailnet: %s\n" % e)
        return e.code
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
