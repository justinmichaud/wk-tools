"""The push keys: one writable deploy key per fork in `wk_forks`, per server, replaced at every agent session.

    keys = rotate(inv, "moose")     # on the controller: {fork: a new private key}, each registered, the older ones gone

The controller makes each key and asks GitHub with github-token. A server asks for its rotation through the
controller's API (wk.api.client.rotate)."""

import json
import time

from wk.core import act
from wk.core.act import die, info
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.creds.rules import GITHUB_API, ask


def title(server, now):
    return "wk %s %s" % (server, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))


def keypair(m, comment):
    """(private, public) of a new ed25519 key with no passphrase, made in a scratch directory that is removed again."""
    scratch = m.run(["mktemp", "-d"]).out.strip()
    try:
        r = m.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", scratch + "/key"])
        if not r.ok:
            die("ssh-keygen failed on %s (exit %d): %s" % (m.name, r.rc, r.err.strip()))
        return m.read(scratch + "/key"), m.read(scratch + "/key.pub").strip()
    finally:
        m.run(["rm", "-rf", scratch])


def github(method, path, token, body=None, want=(200,)):
    """The answer of one GitHub request, refused unless its status is one of `want`."""
    status, doc = ask(method, GITHUB_API + path, token, body and json.dumps(body).encode(),
                      {"Accept": "application/vnd.github+json", "Content-Type": "application/json"})
    if status not in want:
        said = "answered HTTP %d: %s" % (status, doc.get("message", "")) if status else "did not answer (%s)" % doc["message"]
        die("%s %s %s\n    github-token must be a classic token with public_repo of the fork's owner: `wk key check`"
            % (method, path, said), NEEDS_PERSON)
    return doc


def rotate(inv, server):
    token = inv.secrets().get("github-token")
    if not token:
        die("the forks' deploy keys are managed with github-token, and there is none: `wk key set github-token`")
    name = title(server, inv.clock.now())
    keys = {}
    for fork in VARS["wk_forks"]:
        private, public = keypair(inv.here, name)
        path = "/repos/%s/keys" % fork
        made = act.act("github", "POST %s %s" % (path, name),
                       lambda: github("POST", path, token, {"title": name, "key": public, "read_only": False}, (201,)), {"id": None})
        for old in github("GET", path + "?per_page=100", token):
            if old.get("title", "").startswith("wk %s " % server) and old.get("id") != made.get("id"):
                act.act("github", "DELETE %s/%s (%s)" % (path, old["id"], old["title"]),
                        lambda: github("DELETE", "%s/%s" % (path, old["id"]), token, want=(204, 404)))
        keys[fork] = private
    info("rotated %s's deploy keys on %s" % (server, ", ".join(keys)))
    return keys
