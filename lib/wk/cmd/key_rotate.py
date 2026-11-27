"""Replace a server's push keys: a new writable deploy key on each fork, the server's older ones deleted.

For each fork in wk_forks (ansible/group_vars/all.json) a new ed25519 key is made and added to the fork as a writable
deploy key titled `wk <server> <UTC time>`; then every other key of that fork titled `wk <server> ...` is deleted.
The private keys are printed as one JSON object, {"<owner>/<repo>": "<private key>"}. It asks GitHub with
github-token. Two callers: the credentials role of `wk machine setup`, and the server itself at each `wk ai`, through
its rotation key, whose line in this machine's authorized_keys runs this command and nothing else."""

import json

from wk.core.decl import CONTROLLER, Command
from wk.creds import rotation

COMMAND = Command(on=CONTROLLER, takes=("server",))


def main(args, inv):
    print(json.dumps(rotation.rotate(inv, inv.fleet.server(args.server).name), sort_keys=True))
