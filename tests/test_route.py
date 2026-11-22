import unittest

from tests import toy
from tests.world import World
from wk.core import route, seams
from wk.core.fleet import Fleet
from wk.core.inv import Inv
from wk.core.machine import Result


class TestHandOver(unittest.TestCase):
    def setUp(self):
        self.w = World(commands=[toy])
        self.w.workspace("x", on="mac")

    def test_a_server_command_typed_on_the_client_runs_on_the_workspaces_server(self):
        r = self.w.wk("far", "x", "--", "-j", "4")
        self.assertEqual((r.code, r.out), (0, "ran on mac: x ['-j', '4']\n"))
        self.assertEqual(r.effects, ["[mac] write /far/x"])
        self.assertEqual(self.w.machines["mac"].calls[-1].argv, ("wk", "far", "x", "--", "-j", "4"))

    def test_every_server_is_asked_where_the_workspace_is_and_nothing_else_runs_on_the_client(self):
        r = self.w.wk("far", "x")
        self.assertEqual(r.calls, ["[box] wk ls --json", "[lin] wk ls --json", "[mac] wk ls --json", "[mac] wk far x",
                                   "[mac] write /far/x"])

    def test_global_flags_cross_as_environment_not_as_words(self):
        r = self.w.wk("-n", "far", "x", "--yes", "--force", "--quiet")
        call = self.w.machines["mac"].calls[-1]
        self.assertEqual((call.argv, call.env), (("wk", "far", "x"), {"WK_DRY_RUN": "1", "WK_YES": "1", "WK_FORCE": "1", "WK_QUIET": "1"}))
        self.assertEqual((r.plan, r.effects), (["[mac] write /far/x"], []))

    def test_no_flag_is_no_environment(self):
        self.w.wk("far", "x")
        self.assertEqual(self.w.machines["mac"].calls[-1].env, {})

    def test_the_far_exit_code_and_stderr_come_back(self):
        self.w.machines["mac"].on(("wk", "far"), Result(3, "", "stalled\n"))
        r = self.w.wk("far", "x")
        self.assertEqual((r.code, r.err), (3, "stalled\n"))

    def test_on_names_the_server_without_asking_anyone(self):
        r = self.w.wk("far", "new", "--on", "lin")
        self.assertEqual(r.calls, ["[lin] wk far new --on lin", "[lin] write /far/new"])

    def test_on_refuses_a_machine_that_holds_no_workspace(self):
        self.assertIn("error: vault holds no workspace: it is the credential server\n    servers: box, lin, mac", self.w.wk("far", "--on", "vault").err)
        self.assertIn("error: desk holds no workspace: it is the client", self.w.wk("far", "--on", "desk").err)

    def test_on_refuses_an_unknown_machine(self):
        self.assertEqual(self.w.wk("far", "--on", "nope").err, "error: no machine named nope: machines/ has box, desk, lin, mac, vault\n")

    def test_a_server_command_naming_no_workspace_is_refused_on_the_client(self):
        r = self.w.wk("far")
        self.assertEqual((r.code, r.err.splitlines()[0]), (1, "error: names no workspace, so no server to run on"))

    def test_typed_on_a_server_it_runs_there_and_asks_no_one(self):
        r = self.w.wk("far", "x", on="mac")
        self.assertEqual((r.out, r.calls), ("ran on mac: x []\n", ["[mac] write /far/x"]))

    def test_an_unreachable_server_is_a_refusal_that_needs_a_person(self):
        self.w.machines["lin"].down = True
        r = self.w.wk("far", "y", "--on", "lin")
        self.assertEqual(r.code, 4)
        self.assertIn("error: cannot reach lin", r.err)


class TestLocate(unittest.TestCase):
    def setUp(self):
        self.w = World(commands=[toy])

    def test_a_workspace_no_server_holds_is_refused_naming_the_remedy(self):
        r = self.w.wk("far", "x")
        self.assertEqual((r.code, r.err), (1, "error: no workspace named x: `wk ls` lists them, `wk new x` makes it\n"))

    def test_absent_is_not_claimed_while_a_server_cannot_be_asked(self):
        self.w.machines["lin"].down = True
        r = self.w.wk("far", "x")
        self.assertEqual(r.code, 4)
        self.assertIn("workspace x is on no server that answered, and lin did not answer", r.err)

    def test_a_workspace_is_found_while_another_server_is_down(self):
        self.w.workspace("x", on="mac")
        self.w.machines["lin"].down = True
        self.assertEqual(self.w.wk("far", "x").out, "ran on mac: x []\n")

    def test_a_name_on_two_servers_is_refused_naming_both(self):
        self.w.workspace("x", on="mac")
        self.w.workspace("x", on="lin")
        self.assertIn("error: workspace x is on lin and mac: remove one", self.w.wk("far", "x").err)


class TestWorkspaces(unittest.TestCase):
    def inv(self, w):
        return Inv("/", {}, Fleet(lambda: w.confs, "desk", w.machines.get, w.machines["desk"], workers=1), w.clock, None, {})

    def test_every_server_that_answers_is_listed_and_the_others_are_named_unknown(self):
        w = World()
        x = w.workspace("x", on="mac", state=seams.BROKEN)
        w.machines["lin"].down = True
        w.machines["box"].on(("wk", "ls", "--json"), Result(127, "", "wk: not found"))
        self.assertEqual(route.workspaces(self.inv(w)), ([x], ["box", "lin"]))
        self.assertEqual(x, seams.Workspace("x", "mac", "/home/u/wk/ws/x", "plain", "broken"))
