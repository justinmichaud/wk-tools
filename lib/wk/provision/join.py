"""A Mac server joins the tailnet by one line a person types in Terminal on the freshly installed Mac: Homebrew, its
`tailscale` formula, the open-source tailscaled as the system's daemon, then `tailscale up --ssh` under a one-off,
pre-approved, tagged auth key the controller makes through the tailnet's API, named for the Mac's conf."""

import json
import shlex

from wk.core import act
from wk.core.act import die
from wk.core.errors import NEEDS_PERSON
from wk.core.vars import VARS
from wk.creds import rules

EXPIRY = 24 * 3600
# Homebrew's installer refuses sudo it would have to ask for when it runs unattended, so the line asks first.
LINE = ('sudo -v && NONINTERACTIVE=1 /bin/bash -c "$(curl -fsSL %(brew)s)" && eval "$(/opt/homebrew/bin/brew shellenv)" '
        '&& brew install tailscale && sudo "$(brew --prefix)/bin/tailscaled" install-system-daemon '
        '&& sudo "$(brew --prefix)/bin/tailscale" up --ssh --auth-key=%(key)s --hostname=%(name)s')


def token(inv):
    value = inv.secrets().get("tailnet-api-key")
    if not value:
        die("the join key is made with tailnet-api-key, and the controller holds none: `wk key set tailnet-api-key`")
    return value


def key(inv, name, api_key):
    """A new auth key that joins one machine, pre-approved and tagged as a server; None under --dry-run."""
    body = {"capabilities": {"devices": {"create": {"reusable": False, "ephemeral": False, "preauthorized": True,
                                                    "tags": VARS["wk_server_tags"]}}},
            "expirySeconds": EXPIRY, "description": "wk install %s" % name}
    status, doc = act.act("tailscale", "POST /tailnet/-/keys for %s" % name,
                          lambda: rules.ask("POST", VARS["wk_tailnet_api"] + "/tailnet/-/keys", api_key, json.dumps(body).encode(),
                                            {"Content-Type": "application/json"}), (None, {}))
    if act.flags.dry_run:
        return None
    if status != 200 or rules.tailnet_key_check(str(doc.get("key", ""))):
        said = "answered HTTP %s: %s" % (status, doc.get("message", "")) if status else "did not answer (%s)" % doc["message"]
        die("the tailnet's API made no auth key: it %s\n    tailnet-api-key must be allowed to make keys tagged %s: `wk key check`"
            % (said, ", ".join(VARS["wk_server_tags"])), NEEDS_PERSON)
    return doc["key"]


def line(auth_key, name):
    return LINE % {"brew": shlex.quote(VARS["wk_homebrew_install"]), "key": shlex.quote(auth_key), "name": shlex.quote(name)}
