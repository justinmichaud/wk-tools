"""tailnet.py retire <name> | check -- retire an offline node the fleet owns.
Exit: 0 done, 2 no such node, 3 online, 4 no credential, 5 refused, 6 unreachable."""
import json
import os
import sys
import urllib.error
import urllib.request

API = os.environ.get("WK_TAILNET_API", "https://api.tailscale.com/api/v2")


def fail(code, msg):
    print("wk-tailnet: " + msg, file=sys.stderr)
    raise SystemExit(code)


def secret():
    path = os.environ.get("WK_TS_API_SECRET_FILE", "")
    if not path or not os.path.exists(path):
        fail(4, "no tailnet API credential (%s)" % (path or "unset"))
    with open(path) as fh:
        key = fh.read().strip()
    if not key.startswith("tskey-api-"):
        fail(4, "%s does not hold a tailnet API access key (they start 'tskey-api-')" % path)
    return key


def call(method, path, key):
    req = urllib.request.Request(API + path, method=method)
    import base64
    req.add_header("Authorization",
                   "Basic " + base64.b64encode((key + ":").encode()).decode())
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace").strip()
        if e.code in (401, 403):
            fail(5, "the tailnet refused this credential (HTTP %d). Rotate it: wk key tailnet-api --replace"
                 % e.code)
        fail(5, "the tailnet API said HTTP %d for %s %s: %s" % (e.code, method, path, detail[:200]))
    except Exception as e:                                  # network, DNS, timeout
        fail(6, "could not reach the tailnet API: %s" % e)


def devices(key):
    return (call("GET", "/tailnet/-/devices", key) or {}).get("devices") or []


def matches(devs, name):
    """A rename leaves FQDN `name` and self-reported `hostname` disagreeing, so both are matched, on the whole first label only: a prefix would take `rpi3-bench` with `rpi3`."""
    want = name.lower()
    out = []
    for d in devs:
        labels = {(d.get("name") or "").split(".")[0].lower(),
                  (d.get("hostname") or "").lower()}
        if want in labels:
            out.append(d)
    return out


def cmd_retire(name):
    key = secret()
    hits = matches(devices(key), name)
    if not hits:
        fail(2, "no node named '%s' on the tailnet" % name)
    online = [d for d in hits if d.get("online")]
    if online:
        fail(3, "'%s' is online right now (id %s, last seen %s). A live node of that name is a\n"
                "    running board, not a leftover; nothing here removes one."
             % (name, online[0].get("id", "?"), online[0].get("lastSeen", "?")))
    for d in hits:
        call("DELETE", "/device/%s" % d["id"], key)
        print("retired %s (id %s, last seen %s)"
              % (d.get("name", name), d.get("id", "?"), d.get("lastSeen", "unknown")))


def cmd_check():
    key = secret()
    n = len(devices(key))
    print("ok: the credential works (%d device%s on the tailnet)" % (n, "" if n == 1 else "s"))


def main(argv):
    if len(argv) == 2 and argv[1] == "check":
        return cmd_check()
    if len(argv) == 3 and argv[1] == "retire":
        return cmd_retire(argv[2])
    fail(1, "usage: tailnet.py retire <name> | check")


if __name__ == "__main__":
    main(sys.argv)
