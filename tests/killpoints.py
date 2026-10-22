"""Kill points, dry run and final state for a mutating command, as one table row: `Row(case, make, run, state)`.
`converges` runs the command against a Fake world, kills it after every effect count in turn, re-runs it and asserts
the final state an uninterrupted run reaches; a kill that already left that state has converged (re-running a
finished `wk new` is a refusal, not a repair). tests/test_killpoints.py holds the table."""
import os
import sys
import unittest

from tests.support import REPO, owed

sys.path.insert(0, str(REPO / "lib"))
from tests.fakes import Fake, Killed  # noqa: E402


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
        for m in (w, w.fake):   # the killed process is gone, and with it every claim made in its pid
            if isinstance(m, Fake):
                m.pids.discard(os.getpid())
        if final_state(w) == want:
            continue
        run_once(w)
        case.assertEqual(final_state(w), want, "killed after effect %d" % n)
    case.fail("the flow made more than %d effects" % max_effects)


class Row:
    """One mutating command. `case` is the TestCase whose setUp the command needs; `make(case)` builds a world,
    `run(case, w)` runs the command once in it, `state(w)` is what it leaves. `mutations(w)` is the plan a dry run
    holds against a wet one (the world's own by default); `dry` is False where the wet run's plan depends on what it
    found, and a string is the owed reason a dry run is not yet the wet run's plan."""

    def __init__(self, case, make, run, state, mutations=None, effects=60, dry=True):
        self.case, self.make, self.run, self.state, self.effects = case, make, run, state, effects
        self.mutations = mutations or (lambda w: w.fake.mutations())
        self.dry = dry

    def tests(self, name):
        row = self
        assert not [m for m in dir(self.case) if m.startswith("test")], "%s: a row's case is a base with no tests" % name

        def test_killed_after_any_effect_and_rerun_converges(case):
            converges(case, lambda: row.make(case), lambda w: row.run(case, w), row.state, row.effects)

        def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(case):
            wet = row.make(case)
            row.run(case, wet)
            dry = row.make(case)
            before = row.state(dry)
            os.environ["WK_DRY_RUN"] = "1"
            try:
                row.run(case, dry)
            finally:
                del os.environ["WK_DRY_RUN"]
            case.assertEqual(row.mutations(dry), row.mutations(wet))
            case.assertTrue(row.mutations(dry), "the wet run made no mutation")
            case.assertEqual(row.state(dry), before)

        methods = {"test_killed_after_any_effect_and_rerun_converges": test_killed_after_any_effect_and_rerun_converges}
        if isinstance(self.dry, str):
            test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing = owed(self.dry)(test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing)
        if self.dry:
            methods["test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing"] = test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing
        title = "".join(w.capitalize() for w in name.replace("-", " ").split())
        return type("Test" + title, (self.case,), dict(methods, __qualname__="Test" + title, __module__="tests.test_killpoints"))


class TestConverges(unittest.TestCase):
    def world(self):
        w = Fake("toy")
        w.fake = w
        return w

    def test_a_flow_that_redoes_every_effect_on_a_rerun_passes(self):
        def run_once(w):
            w.write("/a", "1")
            w.write("/b", "1")
        converges(self, self.world, run_once, lambda w: dict(w.files))

    def test_a_flow_that_trusts_a_half_made_thing_fails_naming_the_kill(self):
        def run_once(w):
            if w.exists("/a"):
                return
            w.write("/a", "1")
            w.write("/b", "1")
        with self.assertRaises(AssertionError) as cm:
            converges(self, self.world, run_once, lambda w: dict(w.files))
        self.assertIn("killed after effect 1", str(cm.exception))

    def test_a_flow_killed_after_its_work_is_done_is_not_rerun(self):
        def run_once(w):
            if w.exists("/b"):
                raise AssertionError("re-run of a finished flow")
            w.write("/a", "1")
            w.write("/b", "1")
            w.remove("/scratch")
        converges(self, self.world, run_once, lambda w: dict(w.files))

    def test_a_flow_that_never_finishes_within_the_budget_fails(self):
        def run_once(w):
            for i in range(5):
                w.write("/f%d" % i, "x")
        with self.assertRaises(AssertionError) as cm:
            converges(self, self.world, run_once, lambda w: dict(w.files), max_effects=3)
        self.assertIn("more than 3 effects", str(cm.exception))
