"""Matching a string against declared glob patterns happens in one place.

A pattern that travels as data -- a job's `pid_match`, cmd/ab's image
profile -- is a `case` word. bash pathname-expands a word only when it is
split out of an unquoted expansion, so a matcher that loops `for p in $list`
matches from one cwd and not from another: inside a WebKit checkout,
`*Tools/Scripts/build-*` is the two files it names. `match_any`
(lib/common.sh) splits with `read -a`, which never expands, and it is the
only function that uses a variable as a pattern; every other site calls it.

Run: python3 -m unittest tests.test_pattern_matching -v
"""
TIER = "lint"
import re
import shlex
import unittest

from tests.support import REPO, bash, func_body, glob_bait, shell_files

PRELUDE = f'. "{REPO}/lib/common.sh"\n'
WANT = "*build-in-target.sh* *Tools/Scripts/build-*"
ARGS = "perl Tools/Scripts/build-webkit --jsc-only --debug --makeargs=-j75 "


def match(args, want, cwd):
    cp = bash(PRELUDE + "match_any %s %s && echo MATCH || echo NOPE"
              % (shlex.quote(args), shlex.quote(want)), cwd=str(cwd), timeout=30)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip().splitlines()[-1]


class TestMatchAnyIsIndependentOfTheCwd(unittest.TestCase):
    def test_the_bait_cwd_expands_the_pattern_words(self):
        """The fixture proves itself: word-split there, the pattern is filenames."""
        with glob_bait(WANT) as cwd:
            cp = bash("want=%s; for p in $want; do echo \"$p\"; done" % shlex.quote(WANT),
                      cwd=str(cwd), timeout=30)
        self.assertEqual(["xbuild-in-target.shx", "xTools/Scripts/build-x"], cp.stdout.split())

    def test_a_declared_pattern_matches_from_inside_a_checkout(self):
        with glob_bait(WANT) as cwd:
            self.assertEqual("MATCH", match(ARGS, WANT, cwd))
        self.assertEqual("MATCH", match(ARGS, WANT, REPO))

    def test_an_unrelated_command_line_never_matches(self):
        with glob_bait(WANT) as cwd:
            self.assertEqual("NOPE", match("bash -lc sleep 300", WANT, cwd))
            self.assertEqual("NOPE", match("", WANT, cwd))
            self.assertEqual("NOPE", match(ARGS, "", cwd))


# A `case` arm or a `[[ == ]]` whose pattern is a variable, outside match_any.
VAR = r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?"
VARIABLE_PATTERN = re.compile(
    r"^\s*(?:;;\s*)?%s\)|\bcase\b.*\bin\s+%s\)|\S\|%s\)"
    r"|\[\[[^]]*(?:==|!=)\s*%s" % (VAR, VAR, VAR, VAR))


class TestOnlyMatchAnyUsesAVariableAsAPattern(unittest.TestCase):
    def test_no_other_shell_file_matches_a_variable_pattern(self):
        offenders = []
        for path in shell_files():
            text = path.read_text(errors="replace")
            if path == REPO / "lib" / "common.sh":
                text = text.replace(func_body(text, "match_any"), "")
            for n, line in enumerate(text.splitlines(), 1):
                if VARIABLE_PATTERN.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{n}: {line.strip()}")
        self.assertEqual(offenders, [],
                         "a variable as a case pattern outside match_any (lib/common.sh) -- "
                         "call match_any instead:\n" + "\n".join(offenders))

    def test_match_any_is_the_one_that_reads_the_pattern_without_expanding(self):
        body = func_body((REPO / "lib" / "common.sh").read_text(), "match_any")
        self.assertIn("read -ra", body)
        self.assertNotRegex(body, r"for \w+ in \$2")


if __name__ == "__main__":
    unittest.main()
