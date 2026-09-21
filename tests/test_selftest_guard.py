"""cmd/selftest's guard: the lock applies to `--live` alone, so the tiers
that need no machine run beside anything, and two default runs run at once.

Run: python3 -m unittest tests.test_selftest_guard -v
"""
import unittest

from tests.support import REPO, WK, WkTest, builds_on_the_books_env, lock_bash


class TestOneLiveSelftestAtATime(WkTest):
    def _while_a_run_holds_the_lock(self, *args):
        script = f'''
bash -c '. {REPO}/lib/common.sh; hold_lock selftest; exec sleep 30' &
p=$!
i=0; until [ -L "$(_lock_path selftest)" ] || [ $((i += 1)) -gt 50 ]; do sleep 0.1; done
"{WK}" selftest {" ".join(args)} 2>&1
kill $p 2>/dev/null; wait $p 2>/dev/null
'''
        cp = lock_bash(script, self.tmp / "locks",
                       env=builds_on_the_books_env(self.tmp / "books"), timeout=90)
        return cp.stdout + cp.stderr

    def test_a_second_live_run_is_refused_naming_the_holder_and_the_way_on(self):
        out = self._while_a_run_holds_the_lock("--live", "nosuchtestzz")
        self.assertIn("already running here (pid ", out, out)
        self.assertIn("wk selftest\n", out, out)
        self.assertNotIn("tiers:", out, out)

    def test_a_default_run_takes_no_lock_so_two_run_at_once(self):
        out = self._while_a_run_holds_the_lock("test_the_run_is_not_execd_so_the_lock_is_dropped")
        self.assertNotIn("already running here", out, out)
        self.assertIn("tiers: lint,unit  tests: 1 ", out, out)

    def test_the_run_is_not_execd_so_the_lock_is_dropped(self):
        """An `exec`d python3 keeps the pid and so keeps the lock looking
        live, but leaves nothing to release it: the next run has to break a
        lock rather than find none."""
        text = (REPO / "cmd" / "selftest").read_text()
        self.assertNotRegex(text, r"(?m)^exec python3\b")


if __name__ == "__main__":
    unittest.main()
