"""Print the machines as Ansible's inventory: what ansible/inventory.py hands `ansible-playbook`.

One JSON object: a group per kind (controller, linux, mac), each machine's conf as variables named wk_<key>, and for
every host the controller's literals, its name and wk home, and this checkout's commit."""

import json

from wk.core.decl import CONTROLLER, Command
from wk.provision import ansible

COMMAND = Command(on=CONTROLLER, readonly=True)


def main(args, inv):
    print(json.dumps(ansible.inventory(inv), indent=2, sort_keys=True))
