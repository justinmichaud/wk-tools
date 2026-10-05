"""cmd/selftest's guard: the lock applies to `--live` alone, so the tiers"""
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, builds_on_the_books_env, clean_env, run

HOLDER = """
import os, sys, time
sys.path.insert(0, %r)
from wk.clock import Clock
from wk.lock import Lock
from wk.machine import Local
from wk.store import Store
Lock(Store(os.environ), Local(), Clock()).hold("selftest", 0)
print("held", flush=True)
time.sleep(60)
""" % str(REPO / "lib")


class TestOneLiveSelftestAtATime(WkTest):
    def setUp(self):
        super().setUp()
        self.env = dict(builds_on_the_books_env(self.tmp / "books"), WK_LOCK_DIR=str(self.tmp / "locks"))

    def selftest(self, *args):
        return run("selftest", *args, env=self.env, timeout=90).stdout

    def _while_a_run_holds_the_lock(self, *args):
        holder = subprocess.Popen([sys.executable, "-c", HOLDER], env=clean_env(self.env),
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.communicate)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder.pid, self.selftest(*args)

    def test_a_second_live_run_is_refused_naming_the_holder_and_the_way_on(self):
        pid, out = self._while_a_run_holds_the_lock("--live", "nosuchtestzz")
        self.assertIn("already running here (pid %d)" % pid, out, out)
        self.assertIn("wk selftest\n", out, out)
        self.assertNotIn("tiers:", out, out)

    def test_a_default_run_takes_no_lock_so_two_run_at_once(self):
        _, out = self._while_a_run_holds_the_lock("test_the_run_is_not_execd_so_the_lock_is_dropped")
        self.assertNotIn("already running here", out, out)
        self.assertIn("tiers: lint,unit  tests: 1 ", out, out)

    def test_the_run_is_not_execd_so_the_lock_is_dropped(self):
        out = self.selftest("--live", "nosuchtestzz")
        self.assertIn("tests/run.py --live -k nosuchtestzz", out, out)
        locks = self.tmp / "locks"
        left = list(locks.iterdir()) if locks.exists() else []
        self.assertEqual(left, [], out)


if __name__ == "__main__":
    unittest.main()
