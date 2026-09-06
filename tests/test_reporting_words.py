"""`info` is a texinfo reader on every machine this repo runs on.

Most of this tree reports through lib/common.sh's info/warn/die, and every
`wk <cmd>` has sourced it before anything else runs. What has not is a library
sourced *inside a build target*: there is no dispatcher in there, so the words
are whatever the shell finds. Measured 2026-09-06 in a macOS guest, sourcing
lib/quiet.sh alone left both `info` and `warn` undefined, so the raiser a PGO
collection starts ends the build at its first line under `set -e`; on Linux the
same line is worse than undefined, because `info` is a texinfo reader that runs.

So each library that a target sources is sourced here in a bare shell and asked
what its reporting words are.

Run: python3 -m unittest tests.test_reporting_words -v
"""
import re
import unittest

from tests.support import REPO, WkTest, bash

# Sourced where no cmd/* has run: build/build-in-target.sh pulls in guard.sh and
# (for a PGO config) mac-pgo.sh, which sources lib/quiet.sh, which sources the
# window probe, the quiet-desktop table and the raiser.
IN_TARGET = ("lib/quiet.sh", "lib/profiler.sh", "bench/mac-raiser.sh",
             "bench/mac-quiet-desktop.sh", "bench/mac-window-probe.sh",
             "build/configs.sh", "build/guard.sh")

WORDS = ("info", "warn", "log", "die", "debug", "changed", "unchanged")
CALLS = re.compile(r"^\s*(%s) " % "|".join(WORDS), re.M)


class TestALibraryASourcedTargetUsesCanReport(WkTest):
    def _words_after_sourcing(self, rel):
        checks = "; ".join(f'echo "{w}=$(type -t {w} || echo none)"' for w in WORDS)
        return bash(f'set -euo pipefail\n. "$WK_ROOT/{rel}"\n{checks}\n')

    def test_each_one_ends_up_with_functions_and_not_binaries(self):
        """Asked only of the ones that report. The others are shipped
        standalone -- concatenated into an ssh bundle, or installed onto a
        benchmark install where lib/ does not exist -- so a source line
        reaching up out of the tree would break them."""
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
        """The one that bit: mac_raiser_on reports every step, and a PGO
        collection sources it through lib/quiet.sh with nothing else loaded."""
        cp = self._words_after_sourcing("lib/quiet.sh")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("info=function", cp.stdout)
        self.assertIn("warn=function", cp.stdout)

    def test_a_library_with_no_definitions_does_not_get_this_repos_words(self):
        """Why the rule exists, measured rather than asserted: sourcing a file
        that reports, without the definitions, leaves the words as the shell
        finds them -- absent, or someone else's binary."""
        cp = bash('set -u; . "$WK_ROOT/bench/mac-window-probe.sh"; '
                  'echo "info=$(type -t info || echo none)"')
        self.assertNotIn("info=function", cp.stdout, cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
