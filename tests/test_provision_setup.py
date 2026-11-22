import unittest

from tests.provision import lanes
from tests.world import World


class TestMachineSetup(unittest.TestCase):
    def setUp(self):
        lanes(self, "tests.provision.toy")
        self.w = World()

    def test_it_runs_every_lanes_stages_in_order_and_counts_the_changes(self):
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.out), (0, "first        changed\nsecond       changed\nthird        changed\nlin: 3 changed\n"))
        self.assertEqual(r.effects, ["[lin] write /done/first", "[lin] write /done/second", "[lin] write /done/third"])

    def test_a_second_run_changes_nothing_and_says_so(self):
        self.w.wk("machine", "setup", "lin")
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.out, r.effects),
                         (0, "first        unchanged\nsecond       unchanged\nthird        unchanged\nlin: no changes\n", []))

    def test_a_stage_that_fails_stops_the_run_naming_it_and_how_to_re_run_it(self):
        self.w.machines["lin"].files["/fail/second"] = "1"
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.out, r.effects), (1, "first        changed\n", ["[lin] write /done/first"]))
        self.assertEqual(r.err, "error: stage second failed on lin: second broke\n"
                                "    fix that and re-run it alone: wk machine setup lin --stage second\n"
                                "    then the rest: wk machine setup lin\n")

    def test_a_server_that_stops_answering_fails_the_stage_as_needing_a_person(self):
        self.w.machines["lin"].down = True
        r = self.w.wk("machine", "setup", "lin")
        self.assertEqual((r.code, r.err.splitlines()[0]), (4, "error: stage first failed on lin: cannot reach lin: the fake is down"))

    def test_stage_runs_that_stage_alone(self):
        r = self.w.wk("machine", "setup", "lin", "--stage", "second")
        self.assertEqual((r.out, r.effects), ("second       changed\nlin: 1 changed\n", ["[lin] write /done/second"]))

    def test_an_unknown_stage_is_refused_naming_the_stages(self):
        r = self.w.wk("machine", "setup", "lin", "--stage", "fourth")
        self.assertEqual((r.code, r.err, r.calls), (1, "error: no stage named fourth: the stages are first, second, third\n", []))

    def test_the_client_is_refused_naming_doctor(self):
        r = self.w.wk("machine", "setup", "desk")
        self.assertEqual((r.code, r.err), (1, "error: desk is the client, and setup converges a server: `wk doctor` reports what the client needs\n"))

    def test_an_unknown_machine_is_refused_naming_the_machines(self):
        self.assertEqual(self.w.wk("machine", "setup", "nope").err, "error: no machine named nope: machines/ has box, desk, lin, mac, vault\n")

    def test_a_borrowed_box_and_the_credential_server_get_the_same_stages(self):
        for name in ("box", "vault"):
            self.assertEqual(self.w.wk("machine", "setup", name).out.splitlines()[-1], "%s: 3 changed" % name)

    def test_a_dry_run_prints_each_stages_change_and_makes_none(self):
        r = self.w.wk("machine", "setup", "lin", "--dry-run")
        self.assertEqual((r.plan, r.effects, self.w.machines["lin"].files),
                         (["[lin] write /done/first", "[lin] write /done/second", "[lin] write /done/third"], [], {}))
