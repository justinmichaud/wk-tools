"""Matching a string against declared glob patterns happens in one place.

A pattern that travels as data -- a job's `pid_match` -- is matched by lib/wk/job.py's `match_any`, which never
consults the filesystem. bash pathname-expands a pattern word split out of an unquoted expansion, so a shell matcher
looping `for p in $list` would match from one cwd and not from another: inside a WebKit checkout,
`*Tools/Scripts/build-*` is the two files it names. No shell file uses a variable as a pattern.

Run: python3 -m unittest tests.test_pattern_matching -v
"""
TIER = "lint"
import os
import re
import shlex
import sys
import unittest

from tests.support import REPO, bash, glob_bait, shell_files

sys.path.insert(0, str(REPO / "lib"))
from wk import job  # noqa: E402

WANT = "*build-in-target.sh* *Tools/Scripts/build-*"
ARGS = "perl Tools/Scripts/build-webkit --jsc-only --debug --makeargs=-j75 "


class TestMatchAnyIsIndependentOfTheCwd(unittest.TestCase):
    def test_the_bait_cwd_expands_the_pattern_words(self):
        """The fixture proves itself: word-split there, the pattern is filenames."""
        with glob_bait(WANT) as cwd:
            cp = bash("want=%s; for p in $want; do echo \"$p\"; done" % shlex.quote(WANT), cwd=str(cwd), timeout=30)
        self.assertEqual(["xbuild-in-target.shx", "xTools/Scripts/build-x"], cp.stdout.split())

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


# A `case` arm or a `[[ == ]]` whose pattern is a variable.
VAR = r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
VARIABLE_PATTERN = re.compile(
    r"^\s*(?:;;\s*)?%s\)|\bcase\b.*\bin\s+%s\)|\S\|%s\)"
    r"|\[\[[^]]*(?:==|!=)\s*%s" % (VAR, VAR, VAR, VAR))


class TestNoShellFileUsesAVariableAsAPattern(unittest.TestCase):
    def test_no_shell_file_matches_a_variable_pattern(self):
        offenders = []
        for path in shell_files():
            for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
                if VARIABLE_PATTERN.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [], "a variable as a case pattern -- match in Python (lib/wk/job.py match_any):\n"
                         + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
