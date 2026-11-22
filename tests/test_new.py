import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import CONFS, World
from tests.ws import CLONE, FETCHED, HOME, MAKE, MIRROR, WIPE, server
from wk.core import seams
from wk.core.machine import Result

ASKED = ["[box] wk ls --json", "[lin] wk ls --json", "[mac] wk ls --json"]


def make(**how):
    w = World(**how)
    server(w)
    return w


class TestNewOnTheServer(unittest.TestCase):
    def setUp(self):
        self.w = make(here="lin")

    def test_it_clones_wires_makes_the_drivers_part_and_writes_ready_last(self):
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.calls), (0, [FETCHED] + MAKE))
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

    def test_on_naming_another_server_is_refused_on_a_server(self):
        r = self.w.wk("new", "x", "--on", "mac")
        self.assertEqual((r.code, r.err, r.calls), (1, "error: this is lin, not mac: type it on the client, which hands it to mac\n", []))

    def test_killed_after_any_effect_and_typed_again_it_converges(self):
        converges(self, lambda: make(here="lin"), "new", "x")

    def test_a_remake_converges_too(self):
        def half():
            w = make(here="lin")
            w.workspace("x", state=seams.BROKEN)
            return w
        converges(self, half, "new", "x")

    def test_the_dry_run_is_the_plan(self):
        dry_run_is_the_plan(self, lambda: make(here="lin"), "new", "x")


class TestNewOnTheClient(unittest.TestCase):
    def setUp(self):
        self.w = make()

    def test_every_server_is_asked_then_the_command_is_handed_to_the_one_named(self):
        r = self.w.wk("new", "x", "--on", "lin")
        self.assertEqual((r.code, r.calls), (0, ASKED + ["[lin] wk new x --on lin", FETCHED] + MAKE))
        self.assertEqual(self.w.wk("ls").out.split(), ["x", "lin", "container", "ready"])

    def test_without_on_the_server_is_the_client_confs_default_server(self):
        w = make(confs=dict(CONFS, desk="kind=client\ndefault_server=lin"))
        r = w.wk("new", "x")
        self.assertEqual((r.code, r.calls[3], r.effects), (0, "[lin] wk new x", MAKE))

    def test_no_server_named_anywhere_is_refused(self):
        r = self.w.wk("new", "x")
        self.assertEqual((r.code, r.err, r.calls),
                         (1, "error: no server to make x on: pass --on <server>, or set default_server= in machines/desk.conf\n", []))

    def test_a_machine_that_holds_no_workspace_is_refused(self):
        r = self.w.wk("new", "x", "--on", "vault")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: vault holds no workspace: it is the credential server", r.err)

    def test_a_name_in_use_on_any_server_is_refused(self):
        self.w.workspace("x", on="mac")
        r = self.w.wk("new", "x", "--on", "lin")
        self.assertEqual((r.code, r.err, r.calls),
                         (1, "error: workspace x is already on mac (ready): `wk rm x` removes it, or pick another name\n", ASKED))

    def test_a_name_being_made_or_half_made_on_another_server_is_refused(self):
        self.w.workspace("x", on="mac", state=seams.BROKEN)
        self.assertIn("error: workspace x is already on mac (broken)", self.w.wk("new", "x", "--on", "lin").err)
        self.w.machines["mac"].on(("pgrep",), "4242\n")
        self.assertIn("error: workspace x is already on mac (creating)", self.w.wk("new", "x", "--on", "lin").err)

    def test_a_half_made_one_on_the_chosen_server_is_remade(self):
        self.w.workspace("x", on="lin", state=seams.BROKEN)
        r = self.w.wk("new", "x", "--on", "lin")
        self.assertEqual((r.code, r.effects), (0, WIPE + MAKE))

    def test_while_a_server_does_not_answer_no_name_can_be_shown_free_so_it_is_refused(self):
        self.w.machines["mac"].down = True
        r = self.w.wk("new", "x", "--on", "lin")
        self.assertEqual((r.code, r.effects, r.err), (4, [], "error: mac did not answer, so a workspace named x cannot be shown to be on no server\n"
                                                             "    check `tailscale status`\n    --force crosses this\n"))

    def test_force_makes_it_while_another_server_does_not_answer_and_says_so_again_at_exit(self):
        self.w.machines["mac"].down = True
        r = self.w.wk("new", "x", "--on", "lin", "--force")
        self.assertEqual((r.code, r.effects), (0, MAKE))
        self.assertTrue(r.err.splitlines()[-2].startswith("warning: this command was forced past: mac did not answer, so"), r.err)

    def test_the_chosen_server_not_answering_cannot_be_forced(self):
        self.w.machines["lin"].down = True
        r = self.w.wk("new", "x", "--on", "lin", "--force")
        self.assertEqual((r.code, r.effects), (4, []))
        self.assertIn("error: cannot reach lin", r.err)

    def test_from_the_client_it_converges_and_its_dry_run_is_the_plan(self):
        converges(self, make, "new", "x", "--on", "lin")
        dry_run_is_the_plan(self, make, "new", "x", "--on", "lin")
