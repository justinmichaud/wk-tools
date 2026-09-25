"""How far apart an A/B's two arms may be (lib/wk/bench/ab.py). An A/B attributes a number to a commit, so the arms
differ by the change and nothing else: a base further back than one commit measures whatever else landed in between,
so it is refused however the base was chosen, crossably (`barrier`) and on the record. Against tests/test_ab_plan.py's
Fake world.

Run: python3 tests/run.py -k test_ab_base
"""
import os
import unittest

from tests.test_ab_plan import BASE, ABTest
from wk import act

class TestTheArmsMayDifferByOneCommit(ABTest):
    """An A/B attributes a number to a commit, so a base further back than one commit is refused however it was
    chosen, and --force is the one way past, recorded."""

    def test_one_commit_apart_or_none_is_allowed(self):
        for ahead in (0, 1):
            with self.subTest(ahead=ahead):
                os.environ["WK_DRY_RUN"] = "1"
                rc, err = self.quiet(self.world(ahead=ahead).ab().go)
                self.assertEqual(rc, 0, err)

    def test_two_commits_apart_is_refused_given_or_guessed(self):
        for base in ("", BASE):
            with self.subTest(base=base):
                os.environ["WK_DRY_RUN"] = "1"
                w = self.world(ahead=2)
                err = self.refused(w, base=base)
                self.assertIn("--base", err)
                self.assertFalse([e for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c")])

    def test_force_crosses_it(self):
        os.environ.update(WK_DRY_RUN="1", WK_FORCE="1")
        rc, err = self.quiet(self.world(ahead=2).ab().go)
        self.assertEqual(rc, 0, err)
        self.assertTrue(act._forced)


if __name__ == "__main__":
    unittest.main()
