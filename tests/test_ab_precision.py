"""The rule an unattended A/B stops on: `wkdata ab-precision` / `wk bench
precision` (lib/wkdata.py `_t_crit`, `_mde_pct`, `cmd_ab_precision`).

A macOS A/B is told what difference it has to be able to see -- 0.3% by
default -- and keeps alternating until the rounds it has resolve that. These
tests pin the arithmetic against hand computation, and the CLI the autorun
calls (bench/mac-bench-autorun.sh's plan_resolves).

Run: python3 -m unittest tests.test_ab_precision -v
"""
import json
import math
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, run, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
import wkdata  # noqa: E402

WKDATA = REPO / "lib" / "wkdata.py"


def wkd(*args):
    return subprocess.run(["python3", str(WKDATA), *args],
                          capture_output=True, text=True, timeout=60)


def write_runs(root, name, values):
    """One result.json per round, each carrying the plan's headline Score."""
    out = []
    for i, v in enumerate(values):
        d = root / f"{name}{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(
            {"Speedometer-3.0": {"metrics": {"Score": {"current": [v]}},
                                 "tests": {"TodoMVC": {"metrics": {"Time": {"Total": {"current": [10.0]}}}}}}}))
        out.append(d)
    return out


def fields(stdout):
    return dict(l.split("=", 1) for l in stdout.strip().splitlines() if "=" in l)


class TestTheDistribution(WkTest):
    """The t comes out of the same incomplete beta the p-value goes into, so
    the stopping rule and the verdict cannot disagree about the distribution."""

    def test_t_critical_matches_the_published_table(self):
        for df, want in ((5, 2.571), (10, 2.228), (30, 2.042), (100, 1.984)):
            self.assertAlmostEqual(wkdata._t_crit(df, 0.05), want, places=2,
                                   msg=f"t(0.975, {df})")

    def test_the_power_point_is_the_one_80_percent_power_needs(self):
        # One-tailed 0.80 is the two-tailed 0.40 point; z is 0.8416.
        self.assertAlmostEqual(wkdata._t_crit(100000, 0.40), 0.8416, places=3)

    def test_it_agrees_with_scipy_where_scipy_is_installed(self):
        """The stdlib implementation is the one that runs on a benchmark
        install; scipy is the reference it is checked against here."""
        try:
            from scipy import stats
        except ImportError:
            self.skipTest("scipy is not installed here")
        for df in (3, 5, 10, 30, 100, 1000):
            with self.subTest(df=df):
                self.assertAlmostEqual(wkdata._t_crit(df, 0.05), stats.t.ppf(0.975, df), places=5)
                self.assertAlmostEqual(wkdata._t_crit(df, 0.40), stats.t.ppf(0.80, df), places=5)

    def test_no_spread_resolves_anything(self):
        self.assertEqual(wkdata._mde_pct([100.0] * 4, [100.0] * 4), 0.0)

    def test_one_round_a_side_resolves_nothing_at_all(self):
        self.assertIsNone(wkdata._mde_pct([100.0], [101.0]))


class TestTheArithmetic(WkTest):
    def test_the_mde_is_the_two_t_values_times_the_standard_error(self):
        a = [100.0, 101.0, 99.0, 100.0, 101.0, 99.0]
        b = [100.5, 101.5, 99.5, 100.5, 101.5, 99.5]
        na = nb = len(a)
        ma, mb = sum(a) / na, sum(b) / nb
        va = sum((x - ma) ** 2 for x in a) / (na - 1)
        vb = sum((x - mb) ** 2 for x in b) / (nb - 1)
        se2 = va / na + vb / nb
        df = se2 * se2 / ((va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1))
        want = (wkdata._t_crit(df, 0.05) + wkdata._t_crit(df, 0.40)) * math.sqrt(se2) / ma * 100
        self.assertAlmostEqual(wkdata._mde_pct(a, b), want, places=6)

    def test_it_shrinks_as_the_square_root_of_the_rounds(self):
        few = wkdata._mde_pct([100.0, 101.0, 99.0, 100.0] * 1, [100.0, 101.0, 99.0, 100.0] * 1)
        many = wkdata._mde_pct([100.0, 101.0, 99.0, 100.0] * 4, [100.0, 101.0, 99.0, 100.0] * 4)
        self.assertLess(many, few)
        self.assertAlmostEqual(many / few, 0.5, delta=0.15)


class TestTheCommand(WkTest):
    def test_a_noisy_experiment_is_not_met_and_says_how_many_more_rounds(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [99.0, 101.0, 99.0, 101.0, 99.0, 101.0])
            b = write_runs(tmp, "b", [99.5, 101.5, 99.5, 101.5, 99.5, 101.5])
            cp = wkd("ab-precision",
                     "--a", ",".join(str(p / "result.json") for p in a),
                     "--b", ",".join(str(p / "result.json") for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["met"], "no")
            self.assertEqual(f["n_a"], "6")
            self.assertGreater(float(f["mde_pct"]), 0.3)
            # 1/sqrt(n): four times the rounds halves the resolvable difference.
            self.assertAlmostEqual(int(f["rounds_needed"]),
                                   6 * (float(f["mde_pct"]) / 0.3) ** 2, delta=1)

    def test_a_quiet_experiment_is_met_and_owes_no_more_rounds(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [100.0, 100.02, 99.98, 100.0, 100.01, 99.99])
            b = write_runs(tmp, "b", [100.0, 100.01, 99.99, 100.0, 100.02, 99.98])
            cp = wkd("ab-precision",
                     "--a", ",".join(str(p / "result.json") for p in a),
                     "--b", ",".join(str(p / "result.json") for p in b))
            f = fields(cp.stdout)
            self.assertEqual(f["met"], "yes", cp.stdout)
            self.assertEqual(f["rounds_needed"], "")

    def test_the_target_is_what_moves_the_verdict(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [99.0, 101.0, 99.0, 101.0])
            b = write_runs(tmp, "b", [99.0, 101.0, 99.0, 101.0])
            strict = fields(wkd("ab-precision", "--target", "0.3",
                                "--a", ",".join(str(p / "result.json") for p in a),
                                "--b", ",".join(str(p / "result.json") for p in b)).stdout)
            loose = fields(wkd("ab-precision", "--target", "10",
                               "--a", ",".join(str(p / "result.json") for p in a),
                               "--b", ",".join(str(p / "result.json") for p in b)).stdout)
            self.assertEqual(strict["met"], "no")
            self.assertEqual(loose["met"], "yes")
            self.assertEqual(strict["mde_pct"], loose["mde_pct"])


class TestThroughTheCLI(WkTest):
    """`wk bench precision <run-a> <run-b>` -- run directories, not result
    files, because that is what a task's runs.tsv holds."""

    def test_it_takes_run_directories_and_pools_the_comma_separated_ones(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [100.0, 100.5, 99.5, 100.0])
            b = write_runs(tmp, "b", [100.2, 100.7, 99.7, 100.2])
            cp = run("bench", "precision",
                     ",".join(str(p) for p in a), ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["n_a"], "4")
            self.assertEqual(f["n_b"], "4")
            self.assertAlmostEqual(float(f["delta_pct"]), 0.2, places=2)

    def test_it_refuses_anything_but_two_sides(self):
        cp = run("bench", "precision", "/nowhere")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("run-a", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
