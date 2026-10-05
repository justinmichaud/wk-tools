"""`info` is a texinfo reader on every machine this repo runs on."""
TIER = "lint"
import re
import unittest

from tests.support import REPO, WkTest, bash

IN_TARGET = ("bench/mac-raiser.sh",
             "bench/mac-quiet-desktop.sh", "bench/mac-window-probe.sh",
             "build/guard.sh")

WORDS = ("info", "warn", "log", "die", "debug", "changed", "unchanged")
CALLS = re.compile(r"^\s*(%s) " % "|".join(WORDS), re.M)


class TestALibraryASourcedTargetUsesCanReport(WkTest):
    def _words_after_sourcing(self, rel):
        checks = "; ".join(f'echo "{w}=$(type -t {w} || echo none)"' for w in WORDS)
        return bash(f'set -euo pipefail\n. "$WK_ROOT/{rel}"\n{checks}\n')

    def test_each_one_ends_up_with_functions_and_not_binaries(self):
        wrong = []
        for rel in IN_TARGET:
            if not CALLS.search((REPO / rel).read_text()):
                continue
            cp = self._words_after_sourcing(rel)
            if cp.returncode != 0:
                wrong.append(f"{rel}: does not source standalone: {cp.stderr.strip()[:200]}")
                continue
            for line in cp.stdout.split():
                word, _, kind = line.partition("=")
                if kind not in ("function", "none"):
                    wrong.append(f"{rel}: {word} is a {kind}, not this repo's")
        self.assertEqual(wrong, [], "\n  ".join([""] + wrong))

    def test_the_raiser_the_pgo_collection_calls_can_say_what_it_did(self):
        cp = self._words_after_sourcing("bench/mac-raiser.sh")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("info=function", cp.stdout)
        self.assertIn("warn=function", cp.stdout)

    def test_a_library_with_no_definitions_does_not_get_this_repos_words(self):
        cp = bash('set -u; . "$WK_ROOT/bench/mac-window-probe.sh"; '
                  'echo "info=$(type -t info || echo none)"')
        self.assertNotIn("info=function", cp.stdout, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
