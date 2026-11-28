"""`wk machine save` is a front to Ansible: its refusals, the `ansible-playbook` line that runs ansible/save.yml, and what
it says of the saved files."""

import unittest

from tests import ROOT
from tests.provision import checkout, controller
from tests.world import World
from wk.core.machine import Result

CONFS = {"desk": "kind=workstation", "lin": "kind=linux\nuser=jm", "vault": "kind=controller"}
LINE = ["ansible-playbook", "-i", ROOT + "/ansible/inventory.py", "--limit", "desk", ROOT + "/ansible/save.yml"]
SAVED = "ansible/roles/workstation/files"


def make():
    w = World(here="vault", confs=CONFS)
    checkout(w.machines["vault"])
    controller(w.machines["vault"])
    w.machines["vault"].on(("ansible-playbook", "-i"), "")
    return w


class TestMachineSave(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def played(self, r):
        return [line[len("[vault] "):].split() for line in r.calls if line.startswith("[vault] ansible-playbook -i")]

    def test_it_is_one_ansible_playbook_of_save_yml_limited_to_the_workstation_whose_exit_code_it_returns(self):
        r = self.w.wk("machine", "save", "desk")
        self.assertEqual((r.code, self.played(r)), (0, [LINE]))
        self.w.machines["vault"].on(("ansible-playbook", "-i"), Result(2))
        self.assertEqual(self.w.wk("machine", "save", "desk").code, 2)

    def test_it_names_what_changed_and_how_to_keep_it(self):
        self.w.machines["vault"].on(("git", "-C", ROOT, "status", "--porcelain", "--", SAVED), " M %s/zed/settings.json\n" % SAVED)
        err = self.w.wk("machine", "save", "desk").err
        self.assertIn("desk's settings are saved in %s/%s, changed:\n M %s/zed/settings.json\n" % (ROOT, SAVED, SAVED), err)
        self.assertIn("keep them: git -C %s commit -m \"desk's settings\" -- %s && git -C %s push" % (ROOT, SAVED, ROOT), err)

    def test_unchanged_settings_are_said_to_be(self):
        self.w.machines["vault"].on(("git", "-C", ROOT, "status", "--porcelain", "--", SAVED), "")
        self.assertIn("desk's settings are saved in %s/%s, unchanged" % (ROOT, SAVED), self.w.wk("machine", "save", "desk").err)

    def test_a_server_is_refused(self):
        r = self.w.wk("machine", "save", "lin")
        self.assertEqual((r.code, r.err, self.played(r)), (1, "error: lin is a Linux server, and only a workstation's settings are saved\n", []))
