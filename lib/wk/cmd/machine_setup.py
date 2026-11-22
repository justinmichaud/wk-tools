"""Converge a server with Ansible: the playbook ansible/site.yml, against the inventory made of machines/*.conf.

It runs `ansible-playbook --limit <server>` and its exit code is ansible-playbook's. Every task is idempotent: a
second run reports changed=0, and killed anywhere and typed again, setup converges. `--stage <role>` runs one role
(wk, pueue, mirror, sdk, agents, openbao, injector, credentials, borrowed-claude); `--dry-run` runs Ansible's check
mode with diffs. The server fetches exactly this checkout's commit, so uncommitted changes and an unpushed commit
are refused. Nothing is installed with a package manager, and nothing needs root: what a server must already have is
asserted, and named when it is absent."""

from wk.core.act import die
from wk.core.decl import Command, Opt
from wk.provision import ansible

COMMAND = Command(where="client", takes=("server",), opts=(Opt("--stage", "run this role alone", "role"),))


def main(args, inv):
    conf = inv.fleet.get(args.server)
    if conf.kind == "client":
        die("%s is the client, and setup converges a server: `wk doctor` reports what the client needs" % conf.name)
    roles = ansible.roles(inv)
    if args.stage not in [None] + roles:
        die("no stage named %s: the stages are %s" % (args.stage, ", ".join(roles)))
    ansible.given(inv)
    return ansible.play(inv, conf.name, args.stage)
