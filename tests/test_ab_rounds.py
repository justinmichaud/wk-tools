"""The one rounds engine every A/B runs (lib/wk/bench/ab.py's Rounds): --rounds the floor, --detect the precision to
resolve, --max-rounds the ceiling, the lead alternating, a restart running only what the task lacks, lost rounds
stopping it; the stopping rule asked in process of the task's own paired runs (scores.resolved)."""
import contextlib
import io
import json
import sys

from tests.support import REPO, WkTest, scratch_dir
from tests.test_ab_precision import speedometer_doc, write_runs

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.bench import ab, record, scores  # noqa: E402

QUIET = [100.0, 100.02, 99.98, 100.0, 100.01, 99.99]
NOISY = [99.0, 101.0, 99.0, 101.0, 99.0, 101.0]


def refused(fn, *args):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            fn(*args)
        except Refused:
            return err.getvalue()
    raise AssertionError("not refused")


class TestTheDefaults(WkTest):
    def test_rounds_are_exact_unless_a_precision_is_asked_for_and_then_go_on_to_the_ceiling(self):
        for o, defaults, want in (({}, ab.DEFAULTS, (5, 5, 0.0)), ({"detect": "0.3"}, ab.DEFAULTS, (5, 40, 0.3)),
                                  ({"detect": "0.3", "max_rounds": "12"}, ab.DEFAULTS, (5, 12, 0.3)),
                                  ({}, ab.PLANTED, (5, 40, 0.3)), ({"detect": "0.0"}, ab.PLANTED, (5, 5, 0.0))):
            with self.subTest(o=o, planted=defaults is ab.PLANTED):
                self.assertEqual(ab.check_plan(o, ["p"], defaults)[:3], want)

    def test_a_ceiling_below_the_floor_and_a_non_number_are_refused(self):
        for o, said in (({"detect": "0.3", "max_rounds": "4", "rounds": "9"}, "below --rounds"), ({"detect": "a lot"}, "not a percentage"),
                        ({"detect": "-1"}, "not a percentage"), ({"detect": "0.3", "max_rounds": "x"}, "takes a number"),
                        ({"rounds": "0"}, "at least 1"), ({"count": "x"}, "--count takes a number"), ({"plans": ["x;y"]}, "not a plan name")):
            with self.subTest(o=o):
                self.assertIn(said, refused(ab.check_plan, o, ["p"]))


class TestTheEngine(WkTest):
    def play(self, rounds, top, detect, resolves_at=None, lost=(), have=(), plans=(None,)):
        legs, asked = [], []

        def leg(i, a, first, plan):
            legs.append((i, a, plan))
            return i not in lost

        def resolved(i):
            asked.append(i)
            return resolves_at is not None and i >= resolves_at
        with contextlib.redirect_stderr(io.StringIO()) as err:
            outcome, kept, lost_n = ab.Rounds(rounds, top, detect).play(leg, resolved, have, plans=plans)
        return outcome, kept, lost_n, legs, asked, err.getvalue()

    def test_the_floor_the_resolving_round_the_ceiling_a_restart_and_lost_rounds_each_end_it(self):
        """--detect 0 asks nothing; the rule is not asked before the floor; three lost rounds stop it."""
        for args, kw, want, said in (
                ((2, 2, 0.0), {}, ("rounds-done", 2, 0, [1, 2], []), ""),
                ((3, 40, 0.3), {"resolves_at": 1}, ("resolved-at-round-3", 3, 0, [1, 2, 3], [3]), "resolved"),
                ((2, 40, 0.3), {"resolves_at": 6}, ("resolved-at-round-6", 6, 0, [1, 2, 3, 4, 5, 6], [2, 3, 4, 5, 6]), ""),
                ((2, 4, 0.3), {}, ("hit-max-rounds", 4, 0, [1, 2, 3, 4], [2, 3, 4]), "--max-rounds 4 reached"),
                ((4, 4, 0.0), {"have": {1, 3}}, ("rounds-done", 4, 0, [2, 4], []), "round 1/4 -- recorded already"),
                ((2, 40, 0.3), {"lost": (3, 4, 5)}, ("lost-at-round-5", 2, 3, [1, 2, 3, 4, 5], [2, 3, 4]), "3 rounds lost")):
            with self.subTest(args=args, kw=kw):
                outcome, kept, lost, legs, asked, err = self.play(*args, **kw)
                self.assertEqual((outcome, kept, lost, sorted({i for i, _, _ in legs}), asked), want)
                self.assertIn(said, err)

    def test_the_lead_alternates_and_each_round_runs_every_plan(self):
        _, _, _, legs, _, _ = self.play(2, 2, 0.0, plans=("p", "q"))
        self.assertEqual(legs, [(1, 0, "p"), (1, 1, "p"), (1, 0, "q"), (1, 1, "q"), (2, 1, "p"), (2, 0, "p"), (2, 1, "q"), (2, 0, "q")])


class TestResolvedIsThePrecisionRule(WkTest):
    def test_quiet_rounds_resolve_and_noisy_ones_do_not(self):
        with scratch_dir() as tmp:
            self.assertTrue(scores.resolved(write_runs(tmp, "qa", QUIET), write_runs(tmp, "qb", QUIET[::-1]), 0.3))
            self.assertFalse(scores.resolved(write_runs(tmp, "na", NOISY), write_runs(tmp, "nb", [v + 0.5 for v in NOISY]), 0.3))
            self.assertFalse(scores.resolved([str(tmp / "absent")], [str(tmp / "absent")], 0.3))

    def test_an_ab_asks_it_of_its_own_task_for_every_plan(self):
        """Paired the way the report pairs them: a round both arms finished, from this task's runs."""
        with scratch_dir() as tmp:
            taskdir = tmp / "t"
            record.task_write(str(taskdir), {"task": "t", "requested": "x", "devices": "rpi5", "plans": "speedometer3", "rounds": "2",
                                             "slots": "a,b"}, ["wk"])
            for i, (va, vb) in enumerate(zip(QUIET, QUIET[::-1]), 1):
                for arm, v in (("a", va), ("b", vb)):
                    d = taskdir / "runs" / ("r%d%s" % (i, arm))
                    d.mkdir(parents=True)
                    (d / "result.json").write_text(json.dumps(speedometer_doc([[v]])))
                    (d / "env.json").write_text(json.dumps({"machine": "rpi5", "plan": "speedometer3", "ab": {"round": str(i), "arm": arm}}))
            a = ab.AB.__new__(ab.AB)
            a.taskdir, a.ws, a.labels, a.plans, a.engine = str(taskdir), "", ("a", "b"), ["speedometer3"], ab.Rounds(2, 40, 0.3)
            a.system = type("S", (), {"machine_name": "rpi5"})()
            self.assertTrue(a.resolved(6))
            a.plans = ["speedometer3", "jetstream3"]
            self.assertFalse(a.resolved(6), "a plan with no paired rounds resolves nothing")
