"""lint.vocabulary: one spelling per concept, in no file outside docs/, CLAUDE.md
and claude/skills/. An image build's workspace is its image workspace (`image_ws`
in code); a bench task has no longer name.

Run: python3 tests/run.py --lint -k test_lint_vocabulary
"""
TIER = "lint"
import re
import subprocess
import unittest

from tests.support import REPO

RETIRED = {
    "say 'image workspace' (image_ws), or the precise word for the meaning": re.compile(r"(?i)\blanes?\b"),
    "say 'bench task'": re.compile(r"(?i)\bbenchmark tasks?\b"),
}
EXEMPT = ("docs/", "claude/skills/")


def hits():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(out)):
        path = REPO / rel
        if rel == "CLAUDE.md" or rel.startswith(EXEMPT) or path.is_symlink() or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for why, pattern in RETIRED.items():
                if pattern.search(line):
                    yield "%s:%d: %s (%s)" % (rel, n, line.strip(), why)


class TestVocabulary(unittest.TestCase):
    def test_the_patterns_catch_the_retired_words(self):
        first, second = list(RETIRED.values())
        word, long = "la" + "ne", "bench" + "mark task"
        for bad in ("each %s builds" % word, "two %ss" % word.upper(), "<%s>" % word):
            self.assertRegex(bad, first)
        self.assertNotRegex("a plane, %sway" % word, first)
        for bad in ("a " + long, ("a " + long + "s").title()):
            self.assertRegex(bad, second)
        self.assertNotRegex("a bench task, a benchmark", second)

    def test_no_file_uses_a_retired_word(self):
        self.assertEqual([], list(hits()))


if __name__ == "__main__":
    unittest.main()
