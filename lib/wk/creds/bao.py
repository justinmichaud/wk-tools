"""OpenBao, the one credential store, through its own CLI: KV version 2, one value per name under secret/wk/.

`bao` runs on the client, as the person: wk names the address, and the token is the one `bao login` keeps.
openbao.org/docs/commands/kv: `kv get -field=value`, `kv put <path> value=-` (the value on stdin) and
`kv list -format=json`, each with `-mount=secret`; a folder's name ends in `/`."""

import json

from wk.core.act import die
from wk.core.errors import NEEDS_PERSON
from wk.core.seams import Secrets
from wk.core.vars import VARS

KV = "-mount=secret"
PREFIX = "wk/"
ABSENT = "No value found"       # what `bao kv get` and `bao kv list` say of a path that holds nothing; both exit 2


def address(inv):
    return "http://%s:%s" % (inv.fleet.credential.name, VARS["wk_openbao_port"])


class Bao(Secrets):
    def __init__(self, machine, addr):
        self.m, self.addr = machine, addr

    def _bao(self, run, words, input=None):
        r = run(["bao", "kv"] + words, env={"BAO_ADDR": self.addr}, input=input)
        if not r.ok and ABSENT not in r.err:
            die("`bao kv %s` failed (exit %d): %s\n    the store is %s: it is asked with the bao CLI (brew install openbao), "
                "logged in (BAO_ADDR=%s bao login)" % (" ".join(words), r.rc, r.err.strip(), self.addr, self.addr), NEEDS_PERSON)
        return r

    def get(self, name):
        r = self._bao(self.m.run, ["get", "-field=value", KV, PREFIX + name])
        return r.out.rstrip("\n") if r.ok else None

    def put(self, name, value):
        self._bao(self.m.act, ["put", KV, PREFIX + name, "value=-"], value)

    def list(self, under=""):
        r = self._bao(self.m.run, ["list", "-format=json", KV, PREFIX + under])
        names = []
        for key in json.loads(r.out) if r.ok else []:
            names += self.list(under + key) if key.endswith("/") else [under + key]
        return sorted(names)


def connect(inv):
    return Bao(inv.here, address(inv))
