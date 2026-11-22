"""Provisioning is Ansible's (ansible/site.yml). This is what wk adds: the inventory, made of machines/*.conf, and the
one command line that runs the playbook.

    ansible-playbook -i ansible/inventory.py [--limit <server>] [--tags <role>] [--check --diff] ansible/site.yml

ansible/inventory.py prints `wk machine inventory`, so the same line typed by hand runs the same playbook."""

import json
import os

from wk.core import act
from wk.core.act import barrier, die
from wk.core.errors import NEEDS_PERSON
from wk.core.machine import ALIVE
from wk.core.vars import VARS
from wk.creds import bao, injector
from wk.webkit import PROJECT

INSTALL = "brew install ansible, or your distribution's ansible package"


def head(inv):
    """This checkout's commit, or "" when it has none."""
    return inv.here.run(["git", "-C", inv.root, "rev-parse", "HEAD"]).out.strip()


def host(conf):
    """A conf as Ansible's variables: its kind, its role, and each of its literals, as wk_<key>."""
    return dict({"wk_" + key: value for key, value in conf.values.items()}, wk_kind=conf.kind, wk_role=conf.role)


def inventory(inv):
    """What Ansible's script inventory plugin reads: a group per kind, `credential`, and each server's variables. The
    client's literals are every host's defaults."""
    fleet = inv.fleet
    servers, vault = fleet.servers(), fleet.credential
    groups = {kind: {"hosts": [c.name for c in servers if c.kind == kind]} for kind in ("linux", "mac", "borrowed")}
    shared = dict(host(fleet.here), wk_root=inv.root, wk_commit=head(inv), wk_project=PROJECT, wk_credential_server=vault.name,
                  wk_bao_addr=bao.address(inv), wk_claude_placeholder=json.loads(injector.placeholder()),
                  ansible_ssh_common_args=" ".join(ALIVE))
    return dict(groups, credential={"hosts": [vault.name]}, all={"vars": shared}, _meta={"hostvars": {c.name: host(c) for c in servers}})


def given(inv):
    """A machine is given a commit and nothing else: one it can fetch, with no change beside it."""
    sha = head(inv)
    if not sha:
        die("%s has no commit to give a machine: run wk from a clone of its repository" % inv.root)
    if inv.here.run(["git", "-C", inv.root, "status", "--porcelain"]).out.strip():
        barrier("this checkout has uncommitted changes, and a machine is given a commit and nothing else\n"
                "    commit or remove them: git -C %s status --short" % inv.root)
    if not inv.here.run(["git", "-C", inv.root, "branch", "--remotes", "--contains", sha]).out.strip():
        barrier("commit %s is on no remote branch, and a machine fetches its commit from %s\n    git -C %s push"
                % (sha[:12], VARS["wk_repo"], inv.root))


def roles(inv):
    """The playbook's roles. Each is tagged with its own name, which is what `--stage` names."""
    return sorted(os.listdir(os.path.join(inv.root, "ansible", "roles")))


def play(inv, limit, tag):
    """Run the playbook against `limit` (None: every server), `tag` alone (None: every role); returns its exit code.
    Under --dry-run it runs in Ansible's check mode, which changes nothing and prints what would."""
    here, files = inv.here, inv.root + "/ansible/"
    if not here.run(["ansible-playbook", "--version"]).ok:
        die("this machine has no ansible-playbook, which is what provisions a server\n    %s" % INSTALL, NEEDS_PERSON)
    dry = act.flags.dry_run
    argv = (["ansible-playbook", "-i", files + "inventory.py"] + (["--limit", limit] if limit else []) + (["--tags", tag] if tag else [])
            + (["--check", "--diff"] if dry else []) + [files + "site.yml"])
    return (here.run if dry else here.act)(argv, stream=True).rc
