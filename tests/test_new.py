import json
import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from tests.ws import CLONE, HOME, MAKE, MIRROR, RESERVE, WIPE, server
from wk.core import seams
from wk.core.machine import Result

RECORD = "/home/u/wk/ws/x"


def make(**how):
    w = World(here="lin", **how)
    server(w)
    return w


class TestNew(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_reserves_the_name_then_clones_wires_makes_the_drivers_part_and_writes_ready_last(self):
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (0, [RESERVE] + MAKE))
        self.assertIn("==> workspace x is ready on lin: `wk enter x`\n", r.err)
        self.assertEqual((self.w.machines["lin"].files[HOME + "/ws/x/.ready"], self.w.machines["vault"].files[RECORD]), ("", "lin"))
        self.assertEqual(self.w.made, {("lin", "x")})

    def test_a_half_made_workspace_is_wiped_and_remade(self):
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].files[HOME + "/ws/x/build/junk"] = "1"
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (0, [RESERVE] + WIPE + MAKE))
        self.assertNotIn(HOME + "/ws/x/build/junk", self.w.machines["lin"].files)

    def test_a_made_workspace_is_refused(self):
        self.w.workspace("x")
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: workspace x is already on lin: `wk enter x` uses it, `wk rm x` removes it\n", [RESERVE]))

    def test_a_mirror_that_was_never_fetched_is_refused_naming_setup(self):
        self.w.machines["lin"].on(("git", "-C", MIRROR, "rev-parse"), Result(128))
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: the mirror on lin lacks a branch of main: `wk machine setup lin` makes it\n", [RESERVE]))

    def test_a_clone_that_fails_ends_it_with_what_git_said_and_no_ready(self):
        self.w.machines["lin"].on(("git", "clone"), Result(128, "", "fatal: no space left\n"))
        r = self.w.wk("new", "x")
        self.assertEqual(r.code, 1)
        self.assertIn("failed on lin (exit 128): fatal: no space left\n    `wk new x` again remakes it\n", r.err)
        self.assertEqual(r.effects, [RESERVE, CLONE])

    def test_a_name_that_cannot_be_a_directory_and_a_container_is_refused(self):
        for name in ("X", "a_b", "a/b", "a.b"):
            r = self.w.wk("new", name)
            self.assertEqual((r.code, r.calls), (1, []), name)
        self.assertIn("error: a_b cannot name a workspace: use lowercase letters, digits and hyphens", self.w.wk("new", "a_b").err)

    def test_killed_after_any_effect_and_typed_again_it_converges(self):
        converges(self, lambda: make(), "new", "x")

    def test_a_remake_converges_too(self):
        def half():
            w = make()
            w.workspace("x", state=seams.BROKEN)
            return w
        converges(self, half, "new", "x")

    def test_the_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, lambda: make(), "new", "x")

    def test_the_name_is_reserved_before_anything_is_made(self):
        self.assertEqual(self.w.wk("new", "x").effects[0], RESERVE)

    def test_a_name_another_server_holds_is_refused_naming_it(self):
        self.w.reserve("x", "mac")
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: workspace x is mac's: `wk rm x` there removes it, or pick another name\n", [RESERVE]))
        self.assertEqual(self.w.machines["vault"].files[RECORD], "mac")

    def test_a_controller_that_does_not_answer_is_a_barrier(self):
        self.w.machines["vault"].down = True
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects, r.err), (4, [], "error: the controller, vault, did not answer (the fake is down)\n"
                                                             "    so x is not reserved for this server; forced, the workspace is made here with no "
                                                             "reservation\n    --force crosses this\n"))

    def test_force_makes_it_here_with_no_reservation_and_says_so_again_at_exit(self):
        self.w.machines["vault"].down = True
        r = self.w.wk("new", "x", "--force")
        self.assertEqual((r.code, r.effects), (0, MAKE))
        self.assertTrue(r.err.splitlines()[-2].startswith("warning: this command was forced past: the controller, vault, did not answer"), r.err)

    def test_a_refusal_of_the_controllers_is_a_barrier_naming_what_it_said(self):
        self.w.machines["vault"].on(("tailscale", "whois", "--json", "lin"), json.dumps({"Node": {"Name": "stranger.tail.ts.net."}}))
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (4, [RESERVE]))
        self.assertIn("error: the controller, vault, answered HTTP 403: stranger has no conf on the controller\n", r.err)

    def test_a_name_being_made_on_this_server_is_refused(self):
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].on(("pgrep",), "4242\n")
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (1, [RESERVE]))
        self.assertIn("error: a `wk new x` is making it on lin already: wait for it to end\n", r.err)

    def test_it_is_refused_on_a_workstation(self):
        r = World().wk("new", "x")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: wk new runs on a Linux server or a Mac server (box, lin, mac), and this is desk, a workstation", r.err)
