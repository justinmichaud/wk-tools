"""`wk bench ab <task> --kill` (lib/wk/bench/ab.py's kill, over lib/wk/job.py's one kill): the A/B's process and every
step it started are signalled, TERM then KILL after WK_KILL_WAIT, and the record says cancelled once they are gone.
Against tests/test_ab_plan.py's Fake world and its fake clock.

Run: python3 tests/run.py -k test_ab_kill
"""
import unittest

from tests.test_ab_plan import ABTest
from wk import record as progress
from wk.act import Refused
from wk.bench import ab

class TestTheKill(ABTest):
    """`wk bench ab <task> --kill`: the A/B's process and every step it started, the record cancelled once they are gone."""

    def record(self, w, pid):
        recs = progress.Records(w.reg.store.record_dir(), clock=w.clock, env=w.env, machine=w.fake)
        return recs.begin("ab", "here", "t1", "wk bench ab t1 --kill", "/x/ab.log", ["wk sysimage build"], pid=pid)

    def kill(self, w, task="t1"):
        return self.quiet(ab.kill, w.reg, w.clock, task)

    def test_the_process_and_its_steps_go_and_the_record_says_cancelled(self):
        w = self.world()
        w.fake.pids |= {4000, 4001}
        w.fake.answer(["sh", "-c"], out="4001\n4000\n")
        t = self.record(w, 4000)
        rc, err = self.kill(w)
        self.assertEqual(rc, 0, err)
        self.assertEqual((w.fake.pids, t.field("exit")), (set(), "cancelled"))

    def test_a_process_that_ignores_term_is_killed_after_the_bound(self):
        w = self.world()
        w.fake.pids.add(4000)
        w.fake.answer(["sh", "-c"], out="4000\n")
        w.fake.kill = lambda pid, sig=15: (w.fake.pids.discard(pid) if sig == 9 else None) or True
        w.env["WK_KILL_WAIT"] = "3"
        t = self.record(w, 4000)
        rc, err = self.kill(w)
        self.assertEqual(rc, 0, err)
        self.assertEqual((t.field("exit"), w.clock.slept.count(1) >= 3), ("cancelled", True))

    def test_no_record_is_a_refusal_naming_where_the_tasks_are_listed(self):
        rc, err = self.kill(self.world(), "nosuch")
        self.assertIsInstance(rc, Refused)
        self.assertIn("wk bench ls", err)

    def test_a_finished_task_is_not_killed(self):
        w = self.world()
        w.fake.pids.add(4000)
        self.record(w, 4000).end(0)
        rc, err = self.kill(w)
        self.assertIsInstance(rc, Refused)
        self.assertEqual(w.fake.pids, {4000})


if __name__ == "__main__":
    unittest.main()
