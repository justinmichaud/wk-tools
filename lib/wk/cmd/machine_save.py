"""Save a workstation's settings into this checkout, with Ansible: ansible/save.yml.

It runs `ansible-playbook --limit <workstation> ansible/save.yml`, which fetches the person's Zed settings and keymap
from the workstation into ansible/roles/workstation/files/zed/, the files `wk machine setup` writes there. Nothing is
committed: the change is this checkout's, and once committed and pushed every later setup applies it."""

from wk.core import act
from wk.core.act import die, info
from wk.core.conf import THE
from wk.core.decl import CONTROLLER, Command
from wk.provision import ansible

COMMAND = Command(on=CONTROLLER, takes=("workstation",))
SAVED = "ansible/roles/workstation/files"


def main(args, inv):
    conf = inv.fleet.get(args.workstation)
    if conf.kind != "workstation":
        die("%s is %s, and only a workstation's settings are saved" % (conf.name, THE[conf.kind]))
    rc = ansible.play(inv, conf.name, None, "save.yml")
    if rc == 0 and not act.flags.dry_run:
        changed = inv.here.run(["git", "-C", inv.root, "status", "--porcelain", "--", SAVED]).out.rstrip()
        info("%s's settings are saved in %s/%s%s" % (conf.name, inv.root, SAVED, (
            ", changed:\n%s\n    keep them: git -C %s commit -m \"%s's settings\" -- %s && git -C %s push" % (
                changed, inv.root, conf.name, SAVED, inv.root)) if changed else ", unchanged"))
    return rc
