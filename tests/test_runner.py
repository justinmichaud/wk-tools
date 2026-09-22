"""tests/run.py's own rules -- the per-test budget, tier selection, the owed
count, the listing -- each driven against a small suite this file writes,
never the real one, with a stub podman so no gate reaches a machine; and
cmd/selftest's `--quick` tombstone.

Run: python3 -m unittest tests.test_runner -v
"""
import os
import subprocess
import sys
import textwrap
import unittest

from tests.support import REPO, WkTest, builds_on_the_books_env, run

RUN = REPO / "tests" / "run.py"

MODULES = {
    "test_a_unit.py": '''
        import unittest
        from tests.support import owed
        class T(unittest.TestCase):
            def test_unit_passes(self):
                pass
            @owed("the widget is not built yet")
            def test_owed_still_fails(self):
                self.fail("not yet")
            @owed("already landed")
            def test_owed_but_passes(self):
                pass
    ''',
    "test_b_lint.py": '''
        import unittest
        TIER = "lint"
        class T(unittest.TestCase):
            def test_lint_passes(self):
                pass
    ''',
    "test_c_live.py": '''
        import unittest
        TIER = "live"
        class T(unittest.TestCase):
            def test_live_marked(self):
                pass
    ''',
    "test_d_gated.py": '''
        import unittest
        from tests.support import requires_podman_vm
        class Plain(unittest.TestCase):
            def test_plain_runs(self):
                pass
        @requires_podman_vm()
        class Gated(unittest.TestCase):
            def test_live_by_gate(self):
                pass
    ''',
    "test_e_slow.py": '''
        import time, unittest
        class T(unittest.TestCase):
            def test_sleeps(self):
                time.sleep(0.3)
    ''',
    "test_f_tools.py": '''
        import os, subprocess, unittest
        from tests.support import stub_path
        class T(unittest.TestCase):
            def test_ssh_reaches_out(self):
                cp = subprocess.run(["ssh", "somehost", "true"], capture_output=True, text=True)
                self.assertEqual(cp.returncode, 0, cp.stderr)
            def test_ssh_with_its_own_stub(self):
                with stub_path({"ssh": "echo stubbed"}) as binp:
                    cp = subprocess.run(["ssh", "somehost", "true"], capture_output=True, text=True,
                                        env=dict(os.environ, PATH=f"{binp}:{os.environ['PATH']}"))
                self.assertEqual(cp.stdout.strip(), "stubbed", cp.stderr)
            def test_ssh_config_query(self):
                cp = subprocess.run(["ssh", "-G", "somehost"], capture_output=True, text=True)
                self.assertEqual(cp.returncode, 0, cp.stderr)
                self.assertIn("hostname somehost", cp.stdout)
    ''',
}


class RunnerTest(WkTest):
    def setUp(self):
        super().setUp()
        self.suite = self.tmp / "suite"
        self.suite.mkdir()
        (self.suite / "__init__.py").write_text("")
        for name, body in MODULES.items():
            (self.suite / name).write_text(textwrap.dedent(body))

    def runner(self, *args, budget=None, env=None):
        env = dict(os.environ, **(env or builds_on_the_books_env(self.tmp / "books")))
        env.pop("WK_TEST_TIERS", None)
        if budget is not None:
            env["WK_TEST_BUDGET"] = str(budget)
        cp = subprocess.run([sys.executable, str(RUN), "--tests", str(self.suite), *args],
                            cwd=str(self.tmp), env=env, capture_output=True, text=True,
                            timeout=120)
        return cp.returncode, cp.stdout + cp.stderr


class TestTiers(RunnerTest):
    def test_the_default_is_lint_then_unit_and_no_live_test_runs(self):
        rc, out = self.runner("-v")
        self.assertIn("tiers: lint,unit", out)
        self.assertIn("test_lint_passes", out)
        self.assertIn("test_unit_passes", out)
        self.assertIn("test_sleeps", out)
        self.assertIn("test_plain_runs", out)
        self.assertNotIn("test_live_marked", out)
        self.assertNotIn("Gated", out)
        self.assertIn("skipped: 0", out)

    def test_lint_alone_runs_only_the_lint_modules(self):
        rc, out = self.runner("--lint", "-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("tiers: lint  tests: 1 ", out)
        self.assertIn("test_lint_passes", out)
        self.assertNotIn("test_unit_passes", out)

    def test_live_runs_the_marked_module_and_the_gated_class_and_nothing_plain(self):
        rc, out = self.runner("--live", "-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("tiers: live  tests: 1 ", out)
        self.assertIn("test_live_marked", out)
        self.assertRegex(out, r"setUpClass \(suite\.test_d_gated\.Gated\) \.\.\. skipped .*not running")
        self.assertNotIn("test_plain_runs", out)

    def test_tiers_combine(self):
        rc, out = self.runner("--lint", "--live")
        self.assertIn("tiers: lint,live  tests: 2 ", out)

    def test_a_gate_probes_nothing_until_its_test_runs(self):
        log = self.tmp / "podman-calls"
        binp = self.tmp / "bin"
        binp.mkdir()
        (binp / "podman").write_text('#!/bin/sh\necho "$*" >> "%s"\n' % log)
        (binp / "podman").chmod(0o755)
        env = {"PATH": f"{binp}:{os.environ['PATH']}"}
        rc, out = self.runner("--list", "--live", env=env)
        self.assertIn("suite.test_d_gated.Gated.test_live_by_gate", out)
        self.assertFalse(log.exists(), "listing a gated test probed podman")
        rc, out = self.runner("--live", "-k", "live_by_gate", env=env)
        self.assertIn("machine inspect wk", log.read_text())

    def test_a_listing_names_the_selection_and_runs_nothing(self):
        rc, out = self.runner("--list", "--unit", "-k", "test_a_unit", "-k", "test_e_slow")
        self.assertEqual(rc, 0, out)
        self.assertIn("suite.test_e_slow.T.test_sleeps\n", out)
        self.assertIn("selected: 4\n", out)
        self.assertNotIn("tiers:", out)

    def test_the_runner_exports_the_selection_to_the_gates(self):
        (self.suite / "test_f_env.py").write_text(textwrap.dedent('''
            import os, unittest
            from tests.support import live_selected
            class T(unittest.TestCase):
                def test_env(self):
                    self.assertEqual(os.environ["WK_TEST_TIERS"], "unit")
                    self.assertFalse(live_selected())
        '''))
        rc, out = self.runner("--unit", "-k", "test_env")
        self.assertEqual(rc, 0, out)
        self.assertIn("tests: 1 ", out)

    def test_a_pattern_narrows_the_run(self):
        rc, out = self.runner("-k", "lint_passes", "-v")
        self.assertEqual(rc, 0, out)
        self.assertIn("tests: 1 ", out)
        self.assertIn("test_lint_passes", out)

    def test_a_selection_of_nothing_is_a_failure_echoing_the_flags(self):
        rc, out = self.runner("--lint", "-k", "nosuchtestzz")
        self.assertEqual(rc, 1, out)
        self.assertIn("nothing selected: tests/run.py --tests ", out)
        self.assertIn(" --lint -k nosuchtestzz\n", out)
        self.assertNotIn("tiers:", out)


class TestMachineToolsAreShimmed(RunnerTest):
    def test_a_unit_test_that_reaches_for_ssh_fails_naming_the_tool(self):
        rc, out = self.runner("--unit", "-k", "ssh_reaches_out")
        self.assertEqual(rc, 1, out)
        self.assertIn("unit tier reached ssh somehost true", out)

    def test_a_tests_own_stub_goes_ahead_of_the_shim(self):
        rc, out = self.runner("--unit", "-k", "ssh_with_its_own_stub")
        self.assertEqual(rc, 0, out)

    def test_ssh_dash_g_is_the_real_ssh(self):
        rc, out = self.runner("--unit", "-k", "ssh_config_query")
        self.assertEqual(rc, 0, out)

    def test_the_live_tier_is_not_shimmed(self):
        binp = self.tmp / "bin"
        binp.mkdir()
        (binp / "ssh").write_text("#!/bin/sh\nexit 0\n")
        (binp / "ssh").chmod(0o755)
        rc, out = self.runner("--live", "-k", "ssh_reaches_out",
                              env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertIn("nothing selected", out)
        (self.suite / "test_f_tools.py").write_text(
            (self.suite / "test_f_tools.py").read_text().replace("import os,", "TIER = 'live'\nimport os,"))
        rc, out = self.runner("--live", "-k", "ssh_reaches_out",
                              env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertEqual(rc, 0, out)


class TestBudget(RunnerTest):
    def test_a_test_over_budget_fails_naming_it_and_its_time(self):
        rc, out = self.runner("--unit", "-k", "sleeps", budget=0.1)
        self.assertEqual(rc, 1, out)
        self.assertRegex(out, r"over budget: .*test_e_slow\.T\.test_sleeps took 0\.[3-9]s, budget 0\.1s")
        self.assertIn("failures: 1 ", out)

    def test_a_test_under_budget_passes(self):
        rc, out = self.runner("--unit", "-k", "sleeps", budget=30)
        self.assertEqual(rc, 0, out)
        self.assertIn("failures: 0 ", out)

    def test_the_summary_lists_the_slowest_tests(self):
        rc, out = self.runner("--unit", "-k", "test_e_slow", "-k", "test_a_unit")
        self.assertIn("slowest:", out)
        self.assertRegex(out, r"slowest:\n\s+0\.[3-9]\ds\s+\S*test_e_slow\.T\.test_sleeps")

    def test_a_skip_or_an_owed_failure_past_the_budget_is_not_over_budget(self):
        (self.suite / "test_g_slow_exempt.py").write_text(textwrap.dedent('''
            import time, unittest
            from tests.support import owed
            class T(unittest.TestCase):
                def test_slow_skip(self):
                    time.sleep(0.3)
                    self.skipTest("late skip")
                @owed("slow and still owed")
                def test_slow_owed(self):
                    time.sleep(0.3)
                    self.fail("not yet")
        '''))
        rc, out = self.runner("--unit", "-k", "test_g_slow_exempt", budget=0.1)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("over budget", out)
        self.assertIn("skipped: 1  owed: 1", out)

    def test_a_live_test_has_its_own_budget(self):
        (self.suite / "test_h_live_slow.py").write_text(textwrap.dedent('''
            import time, unittest
            TIER = "live"
            class T(unittest.TestCase):
                def test_slow_live(self):
                    time.sleep(0.3)
        '''))
        rc, out = self.runner("--live", "-k", "test_h_live_slow")
        self.assertEqual(rc, 0, out)
        env = dict(builds_on_the_books_env(self.tmp / "books"), WK_TEST_BUDGET="0.1")
        rc, out = self.runner("--live", "-k", "test_h_live_slow", env=env)
        self.assertEqual(rc, 1, out)
        self.assertIn("budget 0.1s", out)


class TestOwed(RunnerTest):
    def test_an_owed_test_that_fails_counts_and_does_not_fail_the_run(self):
        rc, out = self.runner("--unit", "-k", "still_fails", "-k", "unit_passes")
        self.assertEqual(rc, 0, out)
        self.assertIn("owed: 1", out)

    def test_an_owed_test_that_passes_fails_the_run_naming_the_mark(self):
        rc, out = self.runner("--unit", "-k", "owed_but_passes")
        self.assertEqual(rc, 1, out)
        self.assertIn("owed test passed -- remove its owed mark: "
                      "suite.test_a_unit.T.test_owed_but_passes (already landed)", out)

    def test_the_owed_count_is_every_expected_failure(self):
        rc, out = self.runner("--unit", "-k", "test_a_unit", "-k", "test_d_gated", "-k", "test_e_slow")
        self.assertIn("owed: 1", out)
        self.assertIn("tests: 5 ", out)


class TestSelftestFlags(WkTest):
    def test_quick_is_a_tombstone_naming_the_default(self):
        cp = run("selftest", "--quick")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("--quick is gone", cp.stdout)
        self.assertIn("lint then unit", cp.stdout)
        self.assertNotIn("tiers:", cp.stdout)

    def test_a_tier_flag_and_a_pattern_reach_the_runner(self):
        cp = run("selftest", "--lint", "test_the_declared_options_are_the_ones_the_code_reads")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("tiers: lint  tests: 1 ", cp.stdout)


if __name__ == "__main__":
    unittest.main()
