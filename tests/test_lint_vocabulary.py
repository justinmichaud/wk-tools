"""lint.vocabulary: one spelling per concept. An image build's workspace is its
image workspace (`image_ws` in code), so the retired word for it appears in no
file outside docs/, CLAUDE.md and claude/skills/.

Run: python3 tests/run.py --lint -k test_lint_vocabulary
"""
TIER = "lint"
import re
import subprocess
import unittest

from tests.support import REPO

RETIRED = re.compile(r"(?i)\blanes?\b")
EXEMPT = ("docs/", "claude/skills/")


def hits():
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=REPO, capture_output=True, text=True, check=True).stdout.splitlines()
    for rel in sorted(set(out)):
        path = REPO / rel
        if rel == "CLAUDE.md" or rel.startswith(EXEMPT) or path.is_symlink() or not path.is_file():
            continue
        for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if RETIRED.search(line):
                yield "%s:%d: %s" % (rel, n, line.strip())


class TestVocabulary(unittest.TestCase):
    def test_the_pattern_catches_the_retired_word(self):
        word = "la" + "ne"
        for bad in ("each %s builds" % word, "two %ss" % word.upper(), "<%s>" % word):
            self.assertRegex(bad, RETIRED)
        self.assertNotRegex("a plane, %sway" % word, RETIRED)

    def test_no_file_uses_the_retired_word(self):
        self.assertEqual([], list(hits()), "say 'image workspace' (image_ws), or the precise word for the meaning")


if __name__ == "__main__":
    unittest.main()
