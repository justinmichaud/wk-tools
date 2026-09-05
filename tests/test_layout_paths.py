"""cmd/test: a layout-test path the workspace does not have is refused
before the runner starts.

The defect this closes: run-webkit-tests answers a path it cannot find with
"Found 0 tests; running 0, skipping 0. All tests skipped." and exit 0, and
`wk test` read that exit code as success -- it printed "TESTS OK" for a run
that had executed nothing. Measured 2026-09-04 on a guest and on buildbox4
with the same mistyped path.

The block is lifted from cmd/test by its opening statement, and driven with
`t_exec` standing in for the reach into the workspace.

Run: python3 -m unittest tests.test_layout_paths -v
"""
import subprocess
import unittest

from tests.support import REPO, bash, scratch_dir

LIFTED = subprocess.run(
    ["sed", "-n", '/^if \\[ "$SUITE" = layout \\]/,/^fi$/p', str(REPO / "cmd" / "test")],
    capture_output=True, text=True).stdout


class TestLayoutPathCheck(unittest.TestCase):
    def setUp(self):
        self.assertTrue(LIFTED.strip(), "the path check was not found in cmd/test")

    def _run(self, paths, present=(), suite="layout", dry=""):
        """The block, with a checkout of <present> under a scratch directory
        and `t_exec` running its script there the way a container exec does."""
        with scratch_dir(prefix="wk-layout-paths-") as d:
            src = d / "WebKit"
            for rel in present:
                p = src / "LayoutTests" / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("<html>\n")
            (src / "LayoutTests").mkdir(parents=True, exist_ok=True)
            pre = (f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n'
                   f'NAME=probe-ws\nSUITE={suite}\nDRY={dry!r}\n'
                   f'SRC={str(src)!r}\n'
                   't_exec() { shift; sh -c "$3"; }\n'
                   'set -- ' + " ".join(repr(p) for p in paths) + "\n")
            return bash(pre + LIFTED + '\necho REACHED-THE-RUNNER\n')

    def test_a_path_that_is_not_there_is_refused_by_name(self):
        cp = self._run(["fast/dom/Element/id-attribute.html"])
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("REACHED-THE-RUNNER", cp.stdout)
        self.assertIn("fast/dom/Element/id-attribute.html", cp.stdout + cp.stderr)

    def test_a_path_that_is_there_runs(self):
        cp = self._run(["fast/dom/Comment"], present=["fast/dom/Comment/basic.html"])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REACHED-THE-RUNNER", cp.stdout)

    def test_every_missing_one_is_named_not_just_the_first(self):
        """A person who mistyped two paths should not have to run twice."""
        cp = self._run(["fast/gone-a.html", "fast/dom/Comment", "fast/gone-b.html"],
                       present=["fast/dom/Comment/basic.html"])
        out = cp.stdout + cp.stderr
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("fast/gone-a.html", out)
        self.assertIn("fast/gone-b.html", out)

    def test_a_query_string_is_not_part_of_the_path(self):
        """WPT tests are named with one, and the file on disk has no `?`."""
        cp = self._run(["fast/dom/Comment/basic.html?variant=1"],
                       present=["fast/dom/Comment/basic.html"])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REACHED-THE-RUNNER", cp.stdout)

    def test_the_whole_suite_with_no_paths_is_not_a_missing_path(self):
        cp = self._run([])
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REACHED-THE-RUNNER", cp.stdout)

    def test_the_jsc_suite_is_not_checked_against_layouttests(self):
        """`wk test <ws> <path>` without --layout names a JSC test, which
        does not live under LayoutTests/."""
        cp = self._run(["stress/some-jsc-test.js"], suite="jsc")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REACHED-THE-RUNNER", cp.stdout)

    def test_a_dry_run_reaches_no_workspace(self):
        """--dry-run prints the resolved run without a workspace to ask."""
        cp = self._run(["fast/gone.html"], dry="1")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("REACHED-THE-RUNNER", cp.stdout)


if __name__ == "__main__":
    unittest.main()
