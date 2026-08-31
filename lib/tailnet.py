"""The tailnet's control plane: retiring a node the fleet owns.

Every other thing wk asks of the tailnet it asks of the local daemon (a
node's address, whether a peer is up -- lib/reach.sh). This is the one
administrative act, and it exists because a name is an identity here: a
board is reached by its tailnet name and nothing about how to reach it is
written down (CLAUDE.md, "Cattle, not pets"). A card written for a board
whose old node still holds that name joins renamed `<name>-1` and is
unreachable, so a write refuses -- and without this the remedy was a person
at the admin console in the middle of a lane built to be repeatable.

What keeps that narrow is not the credential but the gate below: this
removes a node whose name the caller has already matched against the fleet's
own (boot/machines/*.conf), and only while it is **offline**. An online node
of that name is a live board, and a reprovision that silently evicted one
would be the more expensive accident.

    tailnet.py retire <name>   remove the offline node holding <name>
    tailnet.py check           does the credential still work

The credential is machine-local and gitignored (`wk key tailnet-api`), read
from the file named by WK_TS_API_SECRET_FILE. Exit codes are the contract:
0 done, 2 no such node, 3 the node is online, 4 no usable credential, 5 the
tailnet refused us.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.tailscale.com/api/v2"


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
    # Basic auth with the key as the username and an empty password, which is
    # what the API documents; no dependency on curl's -u parsing.
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
        fail(5, "could not reach the tailnet API: %s" % e)


def devices(key):
    return (call("GET", "/tailnet/-/devices", key) or {}).get("devices") or []


def matches(devs, name):
    """A device whose name is this one. The API's `name` is the FQDN
    (`rpi3-bench.tail1234.ts.net`) and `hostname` is what the machine calls
    itself; a rename leaves the two disagreeing, so both are asked, and only
    an exact match on the first label counts -- never a prefix, or retiring
    `rpi3` would take `rpi3-bench` with it."""
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
