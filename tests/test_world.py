import unittest

from tests import toy
from tests.killpoints import converges, dry_run_is_the_plan
from tests.world import World
from wk.core import seams


class TestWorld(unittest.TestCase):
    def test_a_command_reaches_the_fake_driver_tasks_and_secrets_through_inv(self):
        w = World(commands=[toy])
        w.workspace("x", on="lin")
        r = w.wk("seamy", "x")
        self.assertEqual((r.code, r.out), (0, "0\n"))
        self.assertEqual(r.effects, ["[lin] driver create x", "[store] secret put token", "[lin] task add x: make"])
        self.assertEqual((w.made, w.secrets.get("token"), w.tasks("lin").status()),
                         ({("lin", "x")}, "v", [seams.Task(0, "x", "build", seams.RUNNING, "make", at=1000.0)]))

    def test_every_seam_is_counted_by_the_kill_points_and_the_dry_run(self):
        def make():
            w = World(commands=[toy])
            w.workspace("x", on="lin")
            return w
        dry_run_is_the_plan(self, make, "seamy", "x")
        converges(self, make, "seamy", "x")

    def test_a_lane_swaps_in_its_own_seam(self):
        mine = object()
        w = World(here="lin", commands=[toy], seams={"tasks": lambda inv, machine: mine})
        self.assertRaisesRegex(AttributeError, "add", w.wk, "seamy", "x")

    def test_a_listed_workspace_takes_its_servers_driver(self):
        w = World()
        self.assertEqual((w.workspace("a", on="lin").driver, w.workspace("b", on="mac").driver), ("container", "plain"))

    def test_state_is_everything_the_fakes_hold(self):
        w = World(here="lin")
        w.machines["lin"].write("/f", "1")
        w.machines["lin"].spawn(["d"], "/log")
        self.assertEqual(w.state(), {"files": {"desk": {}, "lin": {"/f": "1"}, "mac": {}, "box": {}, "vault": {}},
                                     "spawned": {"desk": [], "lin": [(("d",), "/log")], "mac": [], "box": [], "vault": []},
                                     "tasks": {"desk": [], "lin": [], "mac": [], "box": [], "vault": []}, "secrets": {}, "made": set()})
