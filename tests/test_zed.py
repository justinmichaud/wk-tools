"""`wk zed <ws>` on a workstation: the one server holding the workspace is found, and Zed opens its checkout there."""

import unittest

from tests.world import CONFS, World
from wk.core.machine import Result


def make(**confs):
    w = World(confs=dict(CONFS, **confs))
    w.machines["desk"].on(("zed",), "")
    return w


class TestZed(unittest.TestCase):
    def test_it_opens_the_checkout_on_the_server_that_holds_it_as_its_confs_user(self):
        w = make(mac="kind=mac\nuser=jm")
        w.workspace("x", on="mac")
        w.workspace("y", on="lin")
        r = w.wk("zed", "x")
        self.assertEqual((r.code, r.effects), (0, ["[desk] zed ssh://jm@mac/home/u/wk/ws/x/src"]))

    def test_a_server_with_no_user_is_reached_as_this_login(self):
        w = make()
        w.workspace("y", on="lin")
        self.assertEqual(w.wk("zed", "y").effects, ["[desk] zed ssh://lin/home/u/wk/ws/y/src"])

    def test_zeds_exit_code_is_its_own(self):
        w = make()
        w.workspace("y", on="lin")
        w.machines["desk"].on(("zed",), Result(2))
        self.assertEqual(w.wk("zed", "y").code, 2)

    def test_a_name_no_server_holds_is_refused(self):
        r = make().wk("zed", "x")
        self.assertEqual((r.code, r.err, r.effects), (1, "error: no server holds a workspace named x: `wk ls` lists them\n", []))

    def test_a_name_two_servers_hold_is_refused_naming_both(self):
        w = make()
        w.workspace("x", on="lin")
        w.workspace("x", on="mac")
        r = w.wk("zed", "x")
        self.assertEqual((r.code, r.err, r.effects), (1, "error: x is on lin and mac: `wk rm x` on all but one of them\n", []))

    def test_a_server_that_does_not_answer_is_a_barrier_that_force_crosses(self):
        w = make()
        w.workspace("x", on="lin")
        w.machines["box"].down = True
        r = w.wk("zed", "x")
        self.assertEqual((r.code, r.err, r.effects), (4, "error: box did not answer, so which server holds x is unknown\n    check `tailscale status`\n"
                                                         "    --force crosses this\n", []))
        self.assertEqual(w.wk("zed", "x", "--force").effects, ["[desk] zed ssh://lin/home/u/wk/ws/x/src"])

    def test_a_dry_run_opens_nothing(self):
        w = make()
        w.workspace("x", on="lin")
        r = w.wk("zed", "x", "--dry-run")
        self.assertEqual((r.code, r.effects, r.plan), (0, [], ["[desk] zed ssh://lin/home/u/wk/ws/x/src"]))

    def test_it_runs_on_a_workstation_alone(self):
        self.assertIn("error: wk zed runs on a workstation (desk), and this is lin, a Linux server", make().wk("zed", "x", on="lin").err)
