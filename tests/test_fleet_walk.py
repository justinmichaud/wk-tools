"""fleet.each: every place asked through its own wk at once, in one vocabulary for a place that was not asked."""
import sys
import unittest

from tests.fakes import FakeDriver, FakeRegistry
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, places  # noqa: E402
from wk.machine import Fake  # noqa: E402


def registry(drivers):
    by_name = {d.name: d for d in drivers}
    return FakeRegistry({}, Fake(), lambda name, env: by_name[name], names=list(by_name))


class TestEach(unittest.TestCase):
    def test_each_place_is_asked_for_its_own_rows_under_its_label(self):
        box, vm = FakeDriver("box", out="row\r\n"), FakeDriver("container", kind="container")
        answers = fleet.each(registry([box, vm]), ["ls", "--json"])
        self.assertEqual([(a.name, a.rc, a.out, a.why) for a in answers], [("box", 0, "row\n", ""), ("container", 0, "", "")])
        args, env, quiet = box.asked[0]
        self.assertEqual((args, env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"], quiet), (("ls", "--json"), "box", "1", True))
        self.assertNotIn("WK_ROW_LABEL", vm.asked[0][1], "the podman machine labels its rows as this machine's itself")

    def test_a_place_not_asked_says_why_and_one_with_no_far_side_is_the_callers(self):
        drivers = [FakeDriver("here", side="none"), FakeDriver("far", side="unreachable", why="timed out"),
                   FakeDriver("ctr", kind="container", side="stopped"), FakeDriver("bare", side="no-wk")]
        answers = fleet.each(registry(drivers), ["status"])
        self.assertEqual([(a.side, a.rc) for a in answers], [("none", None), ("unreachable", None), ("stopped", None), ("no-wk", None)])
        self.assertEqual([a.why for a in answers], ["", "unreachable over ssh: timed out",
                                                    "the podman machine 'wk' is stopped -- 'wk start' brings it up",
                                                    "no wk-tools there yet -- 'wk machine setup bare'"])
        self.assertEqual([d.asked for d in drivers], [[], [], [], []])

    def test_an_unknown_place_is_an_answer_naming_it(self):
        reg = registry([])
        a, = fleet.each(reg, ["status"], names=["ghost"])
        self.assertEqual((a.side, a.driver), ("unknown", None))
        self.assertIn("ghost", a.why)


class TestListing(unittest.TestCase):
    def test_rows_of_every_answering_place_and_a_warning_for_each_other(self):
        warned = []
        drivers = [FakeDriver("box", out="a\n\nb\n"), FakeDriver("old", rc=2), FakeDriver("far", side="unreachable"),
                   FakeDriver("here", side="none")]
        self.assertEqual(fleet.listing(registry(drivers), ["bench", "ls", "--continued"], "tasks", warned.append), ["a", "b"])
        self.assertEqual(len(warned), 2)
        self.assertIn("'old' did not answer the listing", warned[0])
        self.assertIn("far: unreachable over ssh, so the tasks", warned[1])


class TestResolution(unittest.TestCase):
    def registry(self, names, holding, ws):
        fake = Fake()
        fake.answer(["podman", "inspect"], rc=125)

        def make(name, env):
            return places.Container(name, str(REPO), dict(env, WK_STORE="/store/" + name, WK_IN_VM="1"), fake)
        reg = FakeRegistry({}, fake, make, names=names)
        for name in holding:
            fake.dirs.add(reg.load(name).store.ws_dir(ws))
        return reg

    def test_a_name_on_two_places_refuses_naming_both(self):
        with self.assertRaises(LookupError) as e:
            self.registry(["alpha", "beta", "gamma"], {"alpha", "beta"}, "demo").ws_place("demo")
        self.assertIn("alpha beta", str(e.exception))
        self.assertNotIn("gamma", str(e.exception))

    def test_a_name_on_one_place_resolves(self):
        self.assertEqual(self.registry(["alpha", "beta"], {"beta"}, "demo").ws_place("demo"), "beta")


if __name__ == "__main__":
    unittest.main()
