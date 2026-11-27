"""Deliver the store's credentials to your servers, or to one: the playbook's credentials role.

Each Linux and Mac server gets, under ~/wk/secrets: the deploy keys with one
ssh alias per repository, and env.sh, which an interactive shell sources so that git-webkit, gh and `git push` work
as they are. Outside it go the injector's CA, the placeholder claude.ai login, and pi's models.json, which holds the
LiteLLM key.

The values are read on this machine by the bao CLI. Ansible reports each file as changed or ok."""

from wk.core.decl import Command
from wk.provision import ansible

COMMAND = Command(where="client", takes=("server?",))


def main(args, inv):
    return ansible.play(inv, args.server and inv.fleet.server(args.server).name, "credentials")
