"""Print the machines as Ansible's inventory: what ansible/inventory.py hands `ansible-playbook`.

One JSON object: a group per kind (linux, mac, borrowed), the group `credential`, each server's conf as variables
named wk_<key>, and for every host the client's literals, this checkout's commit and the store's address."""

import json

from wk.core.decl import Command
from wk.provision import ansible

COMMAND = Command(where="client", readonly=True)


def main(args, inv):
    print(json.dumps(ansible.inventory(inv), indent=2, sort_keys=True))
