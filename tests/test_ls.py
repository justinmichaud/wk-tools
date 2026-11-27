import unittest

from tests.world import World
from wk.core import seams
from wk.core.machine import Result


class TestLsOnAServer(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")

    def test_a_server_with_no_workspace_lists_none(self):
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.out), (0, ""))

    def test_text_is_one_line_per_workspace_in_name_order(self):
        self.w.workspace("b")
        self.w.workspace("a", state=seams.BROKEN)
        self.assertEqual(self.w.wk("ls").out, "a                        lin              container  broken\n"
                                              "b                        lin              container  ready\n")

    def test_it_lists_its_own_and_asks_no_controller(self):
        self.w.workspace("x")
        self.w.reserve("y", "mac")
        self.w.machines["vault"].down = True
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.out.split()), (0, ["x", "lin", "container", "ready"]))

    def test_no_ready_and_a_wk_new_running_is_creating(self):
        self.w.workspace("my-ws", state=seams.BROKEN)
        self.w.machines["lin"].on(("pgrep",), "4242\n")
        r = self.w.wk("ls")
        self.assertEqual((r.out.split()[-1], r.calls), ("creating", ["[lin] pgrep -f 'wk new (.* )?[m]y-ws( |$)'"]))

    def test_ready_whose_drivers_part_is_not_up_is_broken(self):
        self.w.workspace("x")
        self.w.made.clear()
        self.assertEqual(self.w.wk("ls").out.split()[-1], "broken")

    def test_a_pgrep_that_fails_is_not_read_as_no_process(self):
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].on(("pgrep",), Result(127, "", "pgrep: not found\n"))
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.out), (1, ""))
        self.assertIn("error: cannot tell whether a `wk new x` is running on lin: pgrep failed (exit 127): pgrep: not found\n", r.err)
        self.assertEqual(self.w.wk("ls", "--force").out.split()[-1], "broken")

    def test_listing_changes_nothing(self):
        self.w.workspace("x", state=seams.BROKEN)
        before = self.w.state()
        self.assertEqual((self.w.wk("ls").effects, self.w.state()), ([], before))


class TestLsOffAServer(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.workspace("b", on="lin")
        self.w.workspace("a", on="mac", state=seams.BROKEN)

    def test_every_name_the_controller_holds_is_listed_with_its_server_and_no_server_is_asked(self):
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.err, r.out), (0, "", "a                        mac\nb                        lin\n"))
        self.assertEqual(r.calls, ["[vault] GET /ws"])

    def test_a_controller_that_does_not_answer_needs_a_person(self):
        self.w.machines["vault"].down = True
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.out), (4, ""))
        self.assertEqual(r.err, "error: the controller, vault, did not answer (the fake is down)\n"
                                "    so which server holds which workspace is unknown: check `tailscale status`\n")

    def test_the_controller_asks_its_own_api_too(self):
        self.assertEqual(self.w.wk("ls", on="vault").out, self.w.wk("ls").out)

    def test_listing_changes_nothing(self):
        before = self.w.state()
        self.assertEqual((self.w.wk("ls").effects, self.w.state()), ([], before))
