"""OpenBao, the one credential store, over its HTTP API: KV version 2, one value per name under secret/wk/.

It runs on the client, as the person: the address is BAO_ADDR and the token is BAO_TOKEN, or the file `bao login`
writes."""

import json

from wk.core.act import act, die
from wk.core.errors import NEEDS_PERSON
from wk.core.seams import Secrets
from wk.creds import net

MOUNT = "secret"
PREFIX = "wk"
TOKEN_FILE = ".vault-token"


class Bao(Secrets):
    def __init__(self, addr, token):
        self.addr, self.token = addr.rstrip("/"), token

    def _ask(self, method, kind, name, body=None):
        """(status, the answer's JSON) for 200, 204 and 404; anything else is a refusal."""
        url = "%s/v1/%s/%s/%s/%s" % (self.addr, MOUNT, kind, PREFIX, name)
        try:
            status, raw = net.request(method, url, {"X-Vault-Token": self.token}, body and json.dumps(body).encode())
        except net.NoAnswer as e:
            die("OpenBao at %s did not answer (%s), so what it holds is unknown\n    check BAO_ADDR and `bao status`" % (self.addr, e), NEEDS_PERSON)
        if status == 403:
            die("OpenBao at %s refuses this token (HTTP 403)\n    log in again: `bao login`" % self.addr, NEEDS_PERSON)
        if status not in (200, 204, 404):
            die("OpenBao at %s answered HTTP %d to %s %s/%s/%s\n    `bao status` says whether it is sealed" % (self.addr, status, method, MOUNT, kind, PREFIX))
        return status, json.loads(raw) if status == 200 else None

    def get(self, name):
        status, doc = self._ask("GET", "data", name)
        return doc["data"]["data"]["value"] if status == 200 else None

    def put(self, name, value):
        act("store", "secret put %s" % name, lambda: self._ask("POST", "data", name, {"data": {"value": value}}))

    def list(self, under=""):
        status, doc = self._ask("GET", "metadata", under + "?list=true")
        names = []
        for key in doc["data"]["keys"] if status == 200 else []:
            names += self.list(under + key) if key.endswith("/") else [under + key]
        return sorted(names)


def connect(inv):
    addr = inv.env.get("BAO_ADDR")
    if not addr:
        die("BAO_ADDR is not set, so there is no credential store to ask\n    export BAO_ADDR=https://<credential server>:8200, then `bao login`", NEEDS_PERSON)
    token = inv.env.get("BAO_TOKEN") or (inv.here.read("%s/%s" % (inv.here.home, TOKEN_FILE)) or "").strip()
    if not token:
        die("no OpenBao token: BAO_TOKEN is not set and there is no ~/%s\n    log in: `bao login`" % TOKEN_FILE, NEEDS_PERSON)
    return Bao(addr, token)
