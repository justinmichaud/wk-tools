"""Provisioning is Ansible's (ansible/site.yml, and ansible/install.yml for a server's operating system). This is what
wk adds: the inventory, made of machines/*.conf, and the one command line that runs a playbook.

    ansible-playbook -i ansible/inventory.py [--limit <server>] [--tags <role>] [--check --diff] ansible/site.yml

ansible/inventory.py prints `wk machine inventory`, so the same line typed by hand runs the same playbook."""

import json
import os

from wk.core import act
from wk.core.act import barrier, die
from wk.core.errors import NEEDS_PERSON
from wk.core.machine import ALIVE
from wk.core.vars import VARS
from wk.creds import injector, store
from wk.webkit import PROJECT

INSTALL = "sudo apt install ansible"
# Ansible dies when the login's locale is one this machine lacks, as a Mac's LANG is on Ubuntu.
LOCALE = {"LC_ALL": "C.UTF-8"}


def head(inv):
    """This checkout's commit, or "" when it has none."""
    return inv.here.run(["git", "-C", inv.root, "rev-parse", "HEAD"]).out.strip()


def websocat(home):
    """The pinned websocat in the controller's cache: install.yml runs it, the controller role fetches it."""
    return "%s/cache/websocat-%s/websocat" % (home, VARS["wk_websocat_version"])


def literals(conf):
    return {"wk_" + key: value for key, value in conf.values.items()}


def host(conf):
    """A conf as Ansible's variables: its kind and each of its literals, as wk_<key>; its user= is also Ansible's login,
    and the controller is this machine, reached without ssh."""
    return dict(literals(conf), wk_kind=conf.kind, **({"ansible_user": conf.get("user")} if conf.get("user") else {}),
                **({"ansible_connection": "local"} if conf.kind == "controller" else {}))


def inventory(inv):
    """What Ansible's script inventory plugin reads: a group per kind and each machine's variables. The controller's
    literals are every host's defaults; every machine's user= is every host's `wk_users`, which a workstation's ssh
    config names."""
    fleet = inv.fleet
    hosts, controller = fleet.machines(), fleet.controller
    groups = {kind: {"hosts": [c.name for c in hosts if c.kind == kind]} for kind in ("workstation", "controller", "linux", "mac")}
    shared = dict(literals(controller), wk_root=inv.root, wk_commit=head(inv), wk_project=PROJECT, wk_controller=controller.name,
                  wk_controller_home=inv.wk_home, wk_store=store.directory(inv.wk_home), wk_websocat=websocat(inv.wk_home),
                  wk_claude_placeholder=json.loads(injector.placeholder()), ansible_ssh_common_args=" ".join(ALIVE),
                  wk_users={c.name: c.get("user") for c in hosts if c.kind != "workstation" and c.get("user")})
    return dict(groups, all={"vars": shared}, _meta={"hostvars": {c.name: host(c) for c in hosts}})


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


def play(inv, limit, tag, book="site.yml", more=()):
    """Run the playbook `book` against `limit` (None: every host), `tag` alone (None: every role), with `more`
    options; returns its exit code. Under --dry-run it runs in Ansible's check mode, which changes nothing and
    prints what would."""
    here, files = inv.here, inv.root + "/ansible/"
    if not here.run(["ansible-playbook", "--version"], env=LOCALE).ok:
        die("this machine has no ansible-playbook, which is what provisions a server\n    %s" % INSTALL, NEEDS_PERSON)
    dry = act.flags.dry_run
    argv = (["ansible-playbook", "-i", files + "inventory.py"] + (["--limit", limit] if limit else []) + (["--tags", tag] if tag else [])
            + list(more) + (["--check", "--diff"] if dry else []) + [files + book])
    return (here.run if dry else here.act)(argv, stream=True, env=LOCALE).rc
