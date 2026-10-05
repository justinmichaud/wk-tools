"""job.match_any: a command line against declared glob patterns, whatever the cwd holds."""
TIER = "lint"
import os
import sys
import unittest

from tests.support import REPO, glob_bait

sys.path.insert(0, str(REPO / "lib"))
from wk import job  # noqa: E402

WANT = "*build-in-workspace.sh* *Tools/Scripts/build-*"
ARGS = "perl Tools/Scripts/build-webkit --jsc-only --debug --makeargs=-j75 "


class TestMatchAnyIsIndependentOfTheCwd(unittest.TestCase):
    def test_a_declared_pattern_matches_from_inside_a_checkout(self):
        here = os.getcwd()
        with glob_bait(WANT) as cwd:
            os.chdir(str(cwd))
            try:
                self.assertTrue(job.match_any(ARGS, WANT))
            finally:
                os.chdir(here)

    def test_an_unrelated_command_line_never_matches(self):
        for args, want in (("bash -lc sleep 300", WANT), ("", WANT), (ARGS, "")):
            with self.subTest(args=args, want=want):
                self.assertFalse(job.match_any(args, want))


if __name__ == "__main__":
    unittest.main()
