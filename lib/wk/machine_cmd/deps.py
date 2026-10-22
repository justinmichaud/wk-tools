"""A build machine's probe (remote/probe.sh, run there) and the age of its provisioning."""

import os

import treehash
from wk.kv import kv


def inputs_hash(root):
    return treehash.digest(os.path.join(str(root), "remote"), set())[:16]


def probe(t, root):
    script = "".join(open(os.path.join(str(root), "remote", f)).read() for f in ("deps.sh", "probe.sh"))
    r = t._far().run(["bash", "-s"], input=script, timeout=t.probe_seconds)
    return r.out if r.ok else ""


def stale(t, root):
    r = t._sh('cat "$HOME/.wk-remote" 2>/dev/null')
    marker = kv(r.out) if r.ok else {}
    if not r.ok or not r.out.strip():
        return "no ~/.wk-remote there, so nothing has provisioned it"
    if not marker.get("inputs"):
        return "provisioned before this record existed"
    if marker["inputs"] == inputs_hash(root):
        return None
    return "remote/provision.sh or remote/deps.sh has changed since it ran"
