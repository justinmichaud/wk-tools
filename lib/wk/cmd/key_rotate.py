"""Replace a server's push keys: a new writable deploy key on each fork, the server's older ones deleted.

For each fork in wk_forks (ansible/group_vars/all.json) a new ed25519 key is made and added to the fork as a writable
deploy key titled `wk <server> <UTC time>`; then every other key of that fork titled `wk <server> ...` is deleted.
The private keys are printed as one JSON object, {"<owner>/<repo>": "<private key>"}. It asks GitHub with
github-token. Two callers: the credentials role of `wk machine setup`, and the server itself at each `wk ai`, through
its rotation key, whose line in this machine's authorized_keys runs this command, with --yes, and nothing else.
It revokes keys, so it asks first; --yes answers."""

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
