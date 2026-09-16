"""`wk gc` trims tart's pulled-image cache (cmd/gc's gc_tart_cache).

The macOS guests are pulled from an OCI registry, so what tart keeps beside
them is re-downloadable and belongs to a budget the way ccache does -- not to a
person running `tart prune` by hand. A local VM is a workspace and goes with
`wk rm`, which is why this prunes caches only, tart's own default.

Driven against a `tart` stub recording its argv and a fake TART_HOME, so no
guest and no real cache is touched.

Run: python3 -m unittest tests.test_gc_tart_cache -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, stub_path

GC = REPO / "cmd" / "gc"


def lift(func):
    out = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(GC)],
                         capture_output=True, text=True).stdout
    assert out.strip(), f"could not lift {func} from cmd/gc"
    return out


class TestTheCacheIsTrimmedToItsBudget(WkTest):
    def _run(self, tart_body, budget="20", cache_kb_before=200_000_000,
             cache_kb_after=20_000_000):
        home = self.tmp / "tart"
        (home / "cache").mkdir(parents=True)
        log = self.tmp / "tart.log"
        log.write_text("")
        # `du` is stubbed so the sizes are the test's, not the filesystem's.
        du = ("#!/bin/sh\n"
              f'if [ -f "{log}" ] && grep -q prune "{log}"; then echo "{cache_kb_after}\t$2";'
              f' else echo "{cache_kb_before}\t$2"; fi\n')
        with stub_path({"tart": tart_body, "du": du}) as binp:
            return subprocess.run(
                ["bash", "-c",
                 f'. "{REPO}/lib/common.sh"\n'
                 f'WK_TART_CACHE_GB={budget}; TART_HOME="{home}"\n'
                 + lift("gc_tart_cache") + "\ngc_tart_cache\n"],
                capture_output=True, text=True, timeout=60,
                env={"PATH": f"{binp}:/usr/bin:/bin", "HOME": str(self.tmp),
                     "WK_TEST_TART_LOG": str(log)}), log

    OK_TART = ('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$WK_TEST_TART_LOG"\nexit 0\n')

    def test_it_prunes_to_the_declared_budget(self):
        cp, log = self._run(self.OK_TART)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("prune --space-budget 20", log.read_text(),
                      "gc did not trim the cache to its budget")

    def test_it_prunes_caches_and_never_the_guests(self):
        """`--entries vms` would delete a workspace; tart's default is caches."""
        _, log = self._run(self.OK_TART)
        self.assertNotIn("--entries vms", log.read_text(),
                         "gc asked tart to remove local VMs")

    def test_it_reports_what_it_freed(self):
        cp, _ = self._run(self.OK_TART)
        out = cp.stdout + cp.stderr
        self.assertRegex(out, r"freed \d+ GB", out)

    def test_a_cache_already_inside_the_budget_frees_nothing(self):
        cp, _ = self._run(self.OK_TART, cache_kb_before=1000, cache_kb_after=1000)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("freed", cp.stdout + cp.stderr)

    def test_a_failing_tart_warns_and_gc_carries_on(self):
        """gc reclaims several things; one of them failing ends none of the rest."""
        cp, _ = self._run("#!/bin/sh\nexit 1\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("was not trimmed", cp.stdout + cp.stderr)

    def test_without_tart_it_is_a_no_op(self):
        cp = subprocess.run(
            ["bash", "-c",
             f'. "{REPO}/lib/common.sh"\nWK_TART_CACHE_GB=20; TART_HOME="{self.tmp}"\n'
             + lift("gc_tart_cache") + "\ngc_tart_cache\necho DONE\n"],
            capture_output=True, text=True, timeout=60,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp)})
        self.assertIn("DONE", cp.stdout, cp.stdout + cp.stderr)


class TestTheBudgetIsDeclaredOnce(unittest.TestCase):
    def test_the_budget_and_the_cache_path_live_in_one_place(self):
        store = (REPO / "lib" / "store.sh").read_text()
        for pattern in (r'^WK_TART_CACHE_GB="\$\{WK_TART_CACHE_GB:-\d+\}"',
                        r'^TART_HOME="\$\{TART_HOME:-\$HOME/\.tart\}"'):
            self.assertIsNotNone(re.search(pattern, store, re.M),
                                 f"lib/store.sh does not declare {pattern}")
        for cmd in ("gc", "disk"):
            text = (REPO / "cmd" / cmd).read_text()
            self.assertNotIn('TART_HOME="${TART_HOME:-$HOME/.tart}"', text,
                             f"cmd/{cmd} keeps a second copy of the tart path")


if __name__ == "__main__":
    unittest.main()
