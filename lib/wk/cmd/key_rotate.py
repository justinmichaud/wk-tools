"""Replace a server's push keys: a new writable deploy key on each fork, the server's older ones deleted.

For each fork in wk_forks (ansible/group_vars/all.json) a new ed25519 key is made and added to the fork as a writable
deploy key titled `wk <server> <UTC time>`; then every other key of that fork titled `wk <server> ...` is deleted.
The private keys are printed as one JSON object, {"<owner>/<repo>": "<private key>"}. It asks GitHub with
github-token. The credentials role of `wk machine setup` runs it; the controller's API (`wk serve`) runs the same
rotation when a server asks at each `wk ai`. It revokes keys, so it asks first; --yes answers."""

import json

from wk.core.act import confirm
from wk.core.decl import CONTROLLER, Command
from wk.core.vars import VARS
from wk.creds import rotation

COMMAND = Command(on=CONTROLLER, takes=("server",), destructive=True)


def main(args, inv):
    server = inv.fleet.server(args.server).name
    confirm("replace %s's push keys, revoking its older deploy keys on %s?" % (server, ", ".join(VARS["wk_forks"])))
    print(json.dumps(rotation.rotate(inv, server), sort_keys=True))
