"""ansible/group_vars/all.json: the pins and ports the playbook installs by and this program names. One copy, read by both."""

import json
import os

with open(os.path.join(os.path.dirname(__file__), *[os.pardir] * 3, "ansible", "group_vars", "all.json")) as _f:
    VARS = json.load(_f)
