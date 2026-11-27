"""Prove a server builds, runs and tests today's WebKit, end to end, with Ansible: ansible/check.yml.

It runs `ansible-playbook --limit <server> ansible/check.yml` and its exit code is ansible-playbook's. On the server,
as its login types it: `wk new check-<time>`, `wk build` of each check preset (jsc-release, and mac-release on a
Mac), `wk run` of a line in jsc, `wk test` of one JSC stress test, and `wk rm` of the workspace however the run ends.
`--dry-run` runs Ansible's check mode, which runs none of them."""

from wk.core.decl import CONTROLLER, Command
from wk.provision import ansible

COMMAND = Command(on=CONTROLLER, takes=("server",))


def main(args, inv):
    return ansible.play(inv, inv.fleet.server(args.server).name, None, "check.yml")
