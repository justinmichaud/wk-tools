import json
import unittest

from tests.world import World
from wk.core import seams
from wk.core.machine import Result


class TestLsOnAServer(unittest.TestCase):
    def setUp(self):
        self.w = World(here="lin")

    def test_json_is_the_contract_a_workstation_reads(self):
        self.w.workspace("x")
        r = self.w.wk("ls", "--json")
        self.assertEqual((r.code, json.loads(r.out)),
                         (0, {"workspaces": [{"name": "x", "driver": "container", "state": "ready", "path": "/home/u/wk/ws/x"}]}))

    def test_a_server_with_no_workspace_lists_none(self):
        r = self.w.wk("ls", "--json")
        self.assertEqual((r.code, r.out, self.w.wk("ls").out), (0, '{"workspaces": []}\n', ""))

    def test_text_is_one_line_per_workspace_in_name_order(self):
        self.w.workspace("b")
        self.w.workspace("a", state=seams.BROKEN)
        self.assertEqual(self.w.wk("ls").out, "a                        lin              container  broken\n"
                                              "b                        lin              container  ready\n")

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
        r = self.w.wk("ls", "--json")
        self.assertEqual((r.code, r.out), (1, ""))
        self.assertIn("error: cannot tell whether a `wk new x` is running on lin: pgrep failed (exit 127): pgrep: not found\n", r.err)
        self.assertEqual(self.w.wk("ls", "--force").out.split()[-1], "broken")

    def test_listing_changes_nothing(self):
        self.w.workspace("x", state=seams.BROKEN)
        before = self.w.state()
        self.assertEqual((self.w.wk("ls").effects, self.w.state()), ([], before))


class TestLsOnAWorkstation(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.w.workspace("b", on="lin")
        self.w.workspace("a", on="mac", state=seams.BROKEN)

    def test_every_servers_workspaces_are_merged(self):
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.err), (0, ""))
        self.assertEqual(r.out, "a                        mac              plain      broken\n"
                                "b                        lin              container  ready\n")

    def test_json_names_each_workspaces_server(self):
        r = self.w.wk("ls", "--json")
        self.assertEqual(json.loads(r.out), {"unanswered": [], "workspaces": [
            {"name": "a", "server": "mac", "path": "/home/u/wk/ws/a", "driver": "plain", "state": "broken"},
            {"name": "b", "server": "lin", "path": "/home/u/wk/ws/b", "driver": "container", "state": "ready"}]})

    def test_a_server_that_does_not_answer_is_named_and_the_listing_needs_a_person(self):
        self.w.machines["mac"].down = True
        self.w.machines["box"].on(("pgrep",), Result(2, "", "bad"))
        self.w.workspace("c", on="box", state=seams.BROKEN)
        r = self.w.wk("ls")
        self.assertEqual((r.code, r.out.split()), (4, ["b", "lin", "container", "ready"]))
        self.assertEqual(r.err, "warning: box did not answer: its workspaces are unknown\nwarning: mac did not answer: its workspaces are unknown\n")
        self.assertEqual(json.loads(self.w.wk("ls", "--json").out)["unanswered"], ["box", "mac"])

    def test_the_controller_asks_every_server_too(self):
        self.assertEqual(self.w.wk("ls", on="vault").out, self.w.wk("ls").out)
