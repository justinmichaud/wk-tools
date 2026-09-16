"""How far apart an A/B's two arms may be (cmd/ab).

An A/B attributes a number to a commit, so the two arms differ by the change
and nothing else. A base further back than one commit measures whatever else
landed in between and no number says which commit moved it -- so it is refused
however the base was chosen, and `--force` is the only way past.

The refusal is `barrier`, not `die`: it is crossable on purpose and records
itself. The rule is lifted out of cmd/ab and run against a stub barrier, the
tests/test_resources.py idiom, since reaching it for real wants a mirror, a
built image and a board.

Run: python3 -m unittest tests.test_ab_base -v
"""
import re
import unittest

from tests.support import REPO, bash

AB = REPO / "cmd" / "ab"


def lift_rule():
    """The `$AHEAD` guard and its barrier, as one backslash-continued statement."""
    lines = AB.read_text().splitlines()
    start = next((i for i, l in enumerate(lines) if l.startswith('[ "$AHEAD"')), None)
    assert start is not None, "no $AHEAD guard in cmd/ab"
    out = []
    for line in lines[start:]:
        out.append(line)
        text = "\n".join(out)
        if not line.endswith("\\") and text.count('"') % 2 == 0:
            return text
    raise AssertionError("the $AHEAD guard in cmd/ab never closed")


class TestTheArmsMayDifferByOneCommit(unittest.TestCase):
    def _run(self, ahead, base_how):
        return bash('barrier() { echo "BARRIER: $*"; exit 1; }\n'
                    f'AHEAD={ahead}\nBASE_HOW={base_how}\n'
                    + lift_rule() + '\necho ALLOWED\n')

    def test_one_commit_apart_is_the_whole_point(self):
        for how in ("given", "guessed"):
            with self.subTest(how=how):
                cp = self._run(1, how)
                self.assertIn("ALLOWED", cp.stdout, cp.stdout + cp.stderr)

    def test_the_same_commit_on_both_sides_is_allowed(self):
        """An A/A: two slots of one commit, the lane's own noise floor."""
        cp = self._run(0, "guessed")
        self.assertIn("ALLOWED", cp.stdout, cp.stdout + cp.stderr)

    def test_two_commits_apart_is_refused(self):
        cp = self._run(2, "guessed")
        self.assertIn("BARRIER:", cp.stdout, cp.stdout + cp.stderr)
        self.assertNotIn("ALLOWED", cp.stdout)

    def test_a_base_said_outright_is_refused_just_the_same(self):
        """The distance is the rule, not how the base was arrived at: naming a
        far base with --base is as unmeasurable as guessing one."""
        cp = self._run(2, "given")
        self.assertIn("BARRIER:", cp.stdout,
                      "--base exempted a distance that measures the branch")

    def test_the_refusal_names_the_remedy(self):
        cp = self._run(15305, "guessed")
        self.assertIn("--base", cp.stdout, cp.stdout)
        self.assertIn("15305", cp.stdout, "the refusal does not say how far apart they are")

    def test_it_is_crossable_rather_than_fatal(self):
        """`barrier` honours --force and records it; `die` would not."""
        self.assertRegex(lift_rule(), r"\|\|\s*barrier\b")


if __name__ == "__main__":
    unittest.main()
