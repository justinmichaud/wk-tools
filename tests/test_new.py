import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from tests.ws import CLONE, FETCHED, HOME, MAKE, MIRROR, WIPE, server
from wk.core import seams
from wk.core.machine import Result

ASKED = ["[box] wk ls --json", "[lin] wk ls --json", "[mac] wk ls --json"]


def make(**how):
    w = World(here="lin", **how)
    server(w)
    return w


class TestNew(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_it_clones_wires_makes_the_drivers_part_and_writes_ready_last(self):
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.calls), (0, ASKED + [FETCHED] + MAKE))
        self.assertIn("==> workspace x is ready on lin: `wk enter x`\n", r.err)
        self.assertEqual(self.w.machines["lin"].files[HOME + "/ws/x/.ready"], "")
        self.assertEqual(self.w.made, {("lin", "x")})

    def test_a_half_made_workspace_is_wiped_and_remade(self):
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].files[HOME + "/ws/x/build/junk"] = "1"
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (0, WIPE + MAKE))
        self.assertNotIn(HOME + "/ws/x/build/junk", self.w.machines["lin"].files)

    def test_a_made_workspace_is_refused(self):
        self.w.workspace("x")
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: workspace x is already on lin: `wk enter x` uses it, `wk rm x` removes it\n", []))

    def test_a_mirror_that_was_never_fetched_is_refused_naming_setup(self):
        self.w.machines["lin"].on(("git", "-C", MIRROR, "rev-parse"), Result(128))
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.effects),
                         (1, "error: the mirror on lin lacks a branch of main: `wk machine setup lin` makes it\n", []))

    def test_a_clone_that_fails_ends_it_with_what_git_said_and_no_ready(self):
        self.w.machines["lin"].on(("git", "clone"), Result(128, "", "fatal: no space left\n"))
        r = self.w.wk("new", "x")
        self.assertEqual(r.code, 1)
        self.assertIn("failed on lin (exit 128): fatal: no space left\n    `wk new x` again remakes it\n", r.err)
        self.assertEqual(r.effects, [CLONE])

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



    def test_every_server_is_asked_before_anything_is_made(self):
        r = self.w.wk("new", "x")
        self.assertEqual(r.calls[:3], ASKED)

    def test_a_name_in_use_on_any_server_is_refused(self):
        self.w.workspace("x", on="mac")
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.calls),
                         (1, "error: workspace x is already on mac (ready): `wk rm x` removes it, or pick another name\n", ASKED))

    def test_a_name_being_made_or_half_made_on_another_server_is_refused(self):
        self.w.workspace("x", on="mac", state=seams.BROKEN)
        self.assertIn("error: workspace x is already on mac (broken)", self.w.wk("new", "x").err)
        self.w.machines["mac"].on(("pgrep",), "4242\n")
        self.assertIn("error: workspace x is already on mac (creating)", self.w.wk("new", "x").err)

    def test_while_a_server_does_not_answer_no_name_can_be_shown_free_so_it_is_refused(self):
        self.w.machines["mac"].down = True
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects, r.err), (4, [], "error: mac did not answer, so a workspace named x cannot be shown to be on no server\n"
                                                             "    check `tailscale status`\n    --force crosses this\n"))

    def test_force_makes_it_while_another_server_does_not_answer_and_says_so_again_at_exit(self):
        self.w.machines["mac"].down = True
        r = self.w.wk("new", "x", "--force")
        self.assertEqual((r.code, r.effects), (0, MAKE))
        self.assertTrue(r.err.splitlines()[-2].startswith("warning: this command was forced past: mac did not answer, so"), r.err)

    def test_a_name_being_made_on_this_server_is_refused(self):
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].on(("pgrep",), "4242\n")
        self.assertIn("error: workspace x is already on lin (creating)", self.w.wk("new", "x").err)

    def test_this_servers_own_half_made_workspace_is_not_a_name_in_use(self):
        self.w.workspace("x", state=seams.BROKEN)
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.effects), (0, WIPE + MAKE))

    def test_it_is_refused_on_the_client(self):
        r = World().wk("new", "x")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: wk new runs on the server that holds the workspace, and this is desk", r.err)
