"""Kill-point support: `converges` runs a flow against a Fake world, kills it
after every effect count in turn, re-runs it and asserts the final state an
uninterrupted run reaches. A kill that already left that state has converged:
re-running a finished `wk new` is a refusal, not a repair. Its own tests are
loaded through tests/test_wk_machine.py.

Run: python3 tests/run.py -k tests.killpoints
"""
import sys
import types
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.machine import Fake, Killed  # noqa: E402


def converges(case, make_world, run_once, final_state, max_effects=60):
    ref = make_world()
    run_once(ref)
    want = final_state(ref)
    for n in range(max_effects):
        w = make_world()
        w.fake.stop_after = n
        try:
            run_once(w)
        except Killed:
            pass
        else:
            return
        w.fake.stop_after = None
        if final_state(w) == want:
            continue
        run_once(w)
        case.assertEqual(final_state(w), want, "killed after effect %d" % n)
    case.fail("the flow made more than %d effects" % max_effects)


def _world():
    return types.SimpleNamespace(fake=Fake("toy"))


def _files(w):
    return dict(w.fake.files)


class TestConverges(unittest.TestCase):
    def test_a_flow_that_redoes_every_effect_on_a_rerun_passes(self):
        def run_once(w):
            w.fake.write("/a", "1")
            w.fake.write("/b", "1")
        converges(self, _world, run_once, _files)

    def test_a_flow_that_trusts_a_half_made_thing_fails_naming_the_kill(self):
        def run_once(w):
            if w.fake.exists("/a"):
                return
            w.fake.write("/a", "1")
            w.fake.write("/b", "1")
        with self.assertRaises(AssertionError) as cm:
            converges(self, _world, run_once, _files)
        self.assertIn("killed after effect 1", str(cm.exception))

    def test_a_flow_killed_after_its_work_is_done_is_not_rerun(self):
        def run_once(w):
            if w.fake.exists("/b"):
                raise AssertionError("re-run of a finished flow")
            w.fake.write("/a", "1")
            w.fake.write("/b", "1")
            w.fake.remove("/scratch")
        converges(self, _world, run_once, _files)

    def test_a_flow_that_never_finishes_within_the_budget_fails(self):
        def run_once(w):
            for i in range(5):
                w.fake.write("/f%d" % i, "x")
        with self.assertRaises(AssertionError) as cm:
            converges(self, _world, run_once, _files, max_effects=3)
        self.assertIn("more than 3 effects", str(cm.exception))
