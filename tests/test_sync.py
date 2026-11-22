import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from tests.ws import HOME, MIRROR, git, server
from wk.core import seams
from wk.core.machine import Result
from wk.workspace import mirror

REFRESH = ["[lin] git -C %s fetch origin" % MIRROR, "[lin] git -C %s symbolic-ref HEAD refs/heads/main" % MIRROR]


def fetch(name, on="lin"):
    return "[%s] git -C %s/ws/%s/src fetch origin" % (on, HOME, name)


def make(**how):
    w = World(**how)
    for name in ("lin", "mac", "box", "vault"):
        server(w, name)
    w.workspace("x")
    w.workspace("y")
    w.workspace("half", state=seams.BROKEN)
    return w


class TestSyncOnAServer(unittest.TestCase):
    def setUp(self):
        self.w = make(here="lin")

    def test_the_mirror_is_fetched_and_its_head_reset_then_every_made_workspace_fetches_from_it(self):
        r = self.w.wk("sync")
        self.assertEqual((r.code, r.effects), (0, REFRESH + [fetch("x"), fetch("y")]))

    def test_naming_a_workspace_fetches_the_mirror_and_that_one(self):
        self.assertEqual(self.w.wk("sync", "y").effects, REFRESH + [fetch("y")])

    def test_a_mirror_that_is_absent_is_made_and_configured_first(self):
        w = World(here="lin")
        git(w.machines["lin"])
        r = w.wk("sync")
        self.assertEqual(r.effects, ["[lin] git init --bare --quiet %s" % MIRROR, "[lin] write %s/config" % MIRROR] + REFRESH)
        self.assertEqual(w.machines["lin"].files[MIRROR + "/config"], mirror.CONFIG)

    def test_a_fetch_that_fails_ends_it_with_what_git_said(self):
        self.w.machines["lin"].on(("git", "-C", MIRROR, "fetch"), Result(128, "", "fatal: unable to access\n"))
        r = self.w.wk("sync")
        self.assertEqual((r.code, r.effects), (1, REFRESH[:1]))
        self.assertIn("(exit 128): fatal: unable to access\n    check that lin reaches github.com, then `wk sync` again\n", r.err)

    def test_a_workspaces_fetch_that_fails_names_the_retry(self):
        self.w.machines["lin"].on(("git", "-C", HOME + "/ws/x/src", "fetch"), Result(1, "", "fatal: bad object\n"))
        r = self.w.wk("sync", "x")
        self.assertEqual(r.code, 1)
        self.assertIn("fatal: bad object\n    `wk sync x` again\n", r.err)

    def test_a_workspace_that_is_not_made_is_refused_before_anything_is_fetched(self):
        r = self.w.wk("sync", "half")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("error: workspace half on lin is not made (broken)", r.err)

    def test_killed_after_any_effect_and_typed_again_it_converges(self):
        def bare():
            w = World(here="lin")
            git(w.machines["lin"], fetched=False)
            w.workspace("x")
            return w
        converges(self, bare, "sync")
        dry_run_is_the_plan(self, bare, "sync")


class TestSyncOnTheClient(unittest.TestCase):
    def setUp(self):
        self.w = make()
        self.w.workspace("m", on="mac")

    def test_with_no_name_every_server_syncs_in_turn(self):
        r = self.w.wk("sync")
        self.assertEqual(r.code, 0)
        self.assertEqual(r.effects, [line.replace("[lin]", "[box]") for line in REFRESH] + REFRESH + [fetch("x"), fetch("y")]
                         + [line.replace("[lin]", "[mac]") for line in REFRESH] + [fetch("m", "mac")]
                         + [line.replace("[lin]", "[vault]") for line in REFRESH])

    def test_a_named_workspace_is_synced_on_its_server_alone(self):
        r = self.w.wk("sync", "m")
        self.assertEqual(r.effects, [line.replace("[lin]", "[mac]") for line in REFRESH] + [fetch("m", "mac")])

    def test_a_server_that_does_not_answer_is_warned_about_and_the_rest_still_sync(self):
        self.w.machines["lin"].down = True
        r = self.w.wk("sync")
        self.assertEqual((r.code, fetch("m", "mac") in r.effects), (4, True))
        self.assertIn("warning: cannot reach lin", r.err)

    def test_a_server_whose_sync_fails_fails_the_whole(self):
        self.w.machines["box"].on(("git", "-C", MIRROR, "fetch"), Result(128, "", "fatal\n"))
        r = self.w.wk("sync")
        self.assertEqual((r.code, fetch("m", "mac") in r.effects), (1, True))

    def test_a_fleet_with_no_server_syncs_nothing(self):
        r = World(confs={"desk": "kind=client"}).wk("sync")
        self.assertEqual((r.code, r.calls), (0, []))

    def test_the_dry_run_is_the_plan_across_the_hand_over(self):
        dry_run_is_the_plan(self, make, "sync", "x")
