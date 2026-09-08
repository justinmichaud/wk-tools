"""The rule an unattended A/B stops on: `wkdata ab-precision` / `wk bench
precision` (lib/wkdata.py `_t_crit`, `_mde_pct`, `_headline_score`,
`cmd_ab_precision`).

A macOS A/B is told what difference it has to be able to see -- 0.3% by
default -- and keeps alternating until the rounds it has resolve that. These
tests pin the arithmetic against hand computation, the headline score against
the three shapes run-benchmark writes, and the CLI the autorun calls
(bench/mac-bench-autorun.sh's plan_resolves).

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

# Speedometer-3's suite Score, verbatim from the run tolken recorded at
# 20260908T013816Z: one iteration holding the suite's ten internal repeats.
SPEEDOMETER3_SCORE = [[45.89523573855222, 59.14195205953914, 60.54171927847663,
                       60.173485084449055, 60.77675277201982, 60.48310563744575,
                       60.68187936639455, 60.41834555023595, 60.4687751723019,
                       60.67131654040054]]
SPEEDOMETER3_HEADLINE = 58.925256719981554

# MotionMark-1.3.1's and JetStream3.0's suite Scores are never written into the
# file: each declares itself the geometric mean of its first-level children's
# Scores, taken here from the same three runs (20260908T014425Z, 20260908T015002Z).
MOTIONMARK_CHILDREN = {
    "Multiply": [7922], "Canvas Arcs": [33210.42571189579],
    "Leaves": [7001.279586095617], "Paths": [61142.468975891396],
    "Canvas Lines": [74103.33388396194], "Images": [1353.3237787397766],
    "Design": [494.84825734139923], "Suits": [5808.520811092943],
}
MOTIONMARK_HEADLINE = 8688.12060608443

JETSTREAM3_CHILDREN = {
    "zlib-wasm": [133.91157892360562], "WSL": [11.629705977926855],
    "web-ssr": [392.97059234263094], "validatorjs": [881.4427660414407],
    "UniPoker": [1494.5726401742033], "typescript-lib": [11.132386338335484],
    "tsf-wasm": [394.093086955008], "transformersjs-bert-wasm": [100.92254385768433],
    "threejs": [170.29189048263814], "sync-fs": [789.5819787738635],
    "Sunspider": [1702.5798544296954], "stanford-crypto-sha256": [2189.1651492985507],
    "stanford-crypto-pbkdf2": [2280.149164822347], "stanford-crypto-aes": [791.0720184524865],
    "sqlite3-wasm": [174.57108981262633], "splay": [914.8038758142987],
    "source-map-wtb": [788.8428651219822], "segmentation": [137.72909705606716],
    "richards-wasm": [429.73702357540986], "richards": [1469.6565598589918],
    "regexp-octane": [1226.9365685472499], "raytrace-public-class-fields": [1091.843722436252],
    "raytrace-private-class-fields": [912.4924337783879], "raytrace": [1083.6886018133064],
    "proxy-vue": [2084.0371957357524], "proxy-mobx": [3065.451785351372],
    "prismjs-startup-es6": [889.7818945766387], "prettier-wtb": [141.9221214624567],
    "postcss-wtb": [62.20743945260026], "pdfjs": [402.71532348119376],
    "OfflineAssembler": [497.30958678898276], "octane-code-load": [2141.056830403366],
    "navier-stokes": [1156.694989499941], "multi-inspector-code-load": [773.5537234675598],
    "mobx-startup": [401.76031401513313], "ML": [226.0627002029101],
    "mandreel": [246.92066797333965], "lazy-collections": [606.399737275025],
    "Kotlin-compose-wasm": [11.108923621082807],
    "json-stringify-inspector": [1831.3717579971178],
    "json-parse-inspector": [743.0875686890996], "jsdom-d3-startup": [35.756430075358665],
    "js-tokens": [488.80256446933726], "j2cl-box2d-wasm": [274.38776503471485],
    "hash-map": [1161.633007034483], "gbemu": [251.95886710298467],
    "gaussian-blur": [928.6464662379545], "FlightPlanner": [2224.637183180727],
    "first-inspector-code-load": [552.2095157121762], "esprima-next-wtb": [191.11303289468304],
    "espree-wtb": [160.15266526832488], "earley-boyer": [1459.6709925424252],
    "doxbee-promise": [983.6452141994519], "doxbee-async": [1711.3678715783683],
    "dotnet-interp-wasm": [25.502815976232068], "dotnet-aot-wasm": [62.361013072945966],
    "delta-blue": [2233.1448561315715], "Dart-flute-todomvc-wasm": [82.2988312677895],
    "crypto": [2806.4510195981684], "chai-wtb": [553.1296060269145],
    "cdjs": [478.5643415312772], "Box2D": [807.617833367575],
    "bomb-workers": [179.55930503740157], "bigint-noble-ed25519": [176.94324203134573],
    "Basic": [1350.6301809427002], "babylonjs-startup-es6": [52.84251285672862],
    "babylonjs-scene-es6": [114.32473171792522], "babylon-wtb": [158.7477046701494],
    "Babylon": [1243.1512456462538], "babel-wtb": [210.95274338787604],
    "babel-minify-wtb": [160.98225872186978], "async-fs": [611.1330170645954],
    "argon2-wasm": [335.17151607995027], "Air": [1279.2315640499544],
    "ai-astar": [1264.72366132107], "acorn-wtb": [102.28288682410073],
    "8bitbench-wasm": [44.49961647994483],
}
JETSTREAM3_HEADLINE = 407.8831325301141


def wkd(*args):
    return subprocess.run(["python3", str(WKDATA), *args],
                          capture_output=True, text=True, timeout=60)


def speedometer_doc(score=None):
    """Speedometer-3's shape: the suite Score materialised, and a descriptor
    list above the leaf Times."""
    return {"debugOutput": [None], "Speedometer-3": {
        "metrics": {"Score": {"current": score or SPEEDOMETER3_SCORE},
                    "Time": ["Total", "Geometric"]},
        "tests": {"TodoMVC-JavaScript-ES5": {
            "metrics": {"Time": ["Total"]},
            "tests": {"Adding100Items": {"metrics": {"Time": {"current": [[10.36, 9.94]]}}}}}}}}


def aggregate_doc(suite, aggregator, children):
    """JetStream3's and MotionMark's shape: the suite Score is a declaration and
    the numbers live one level down."""
    return {"debugOutput": [None], suite: {
        "metrics": {"Score": [aggregator]},
        "tests": {name: {"metrics": {"Score": {"current": vals}}}
                  for name, vals in children.items()}}}


def write_docs(root, name, docs):
    """One run directory per round, each holding a result.json."""
    out = []
    for i, doc in enumerate(docs):
        d = root / f"{name}{i}"
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(doc))
        out.append(d)
    return out


def write_runs(root, name, values):
    """One run directory per round, each carrying the plan's headline Score."""
    return write_docs(root, name, [speedometer_doc([[v]]) for v in values])


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


class TestTheHeadlineScore(WkTest):
    """One number per run, out of the three shapes the Mac lane records. Two of
    the three never write their overall score into the file at all: they declare
    it as the geometric mean of their first-level children's Scores, and a
    stopping rule that reads nothing from them can never fire."""

    def test_speedometer3_is_the_mean_of_its_materialised_values(self):
        self.assertAlmostEqual(wkdata._headline_score(speedometer_doc()),
                               SPEEDOMETER3_HEADLINE, places=9)

    def test_motionmark_is_the_geometric_mean_of_its_eight_children(self):
        doc = aggregate_doc("MotionMark-1.3.1", "Geometric", MOTIONMARK_CHILDREN)
        self.assertAlmostEqual(wkdata._headline_score(doc), MOTIONMARK_HEADLINE, places=6)

    def test_jetstream3_is_the_geometric_mean_of_its_seventy_seven_children(self):
        doc = aggregate_doc("JetStream3.0", "Geometric", JETSTREAM3_CHILDREN)
        self.assertAlmostEqual(wkdata._headline_score(doc), JETSTREAM3_HEADLINE, places=6)

    def test_a_child_carrying_its_own_subtests_is_not_walked_into(self):
        """JetStream3's children each hold First/Worst/Average Times. The
        headline is the aggregate of the children, not of their leaves."""
        children = {"zlib-wasm": {"metrics": {"Score": {"current": [4.0]},
                                              "Time": ["Geometric"]},
                                  "tests": {"First": {"metrics": {"Time": {"current": [62.6]}}}}},
                    "WSL": {"metrics": {"Score": {"current": [9.0]}}}}
        doc = {"JetStream3.0": {"metrics": {"Score": ["Geometric"]}, "tests": children}}
        self.assertAlmostEqual(wkdata._headline_score(doc), 6.0, places=9)

    def test_it_aggregates_per_iteration_rather_than_over_the_pooled_set(self):
        """Two iterations, two subtests scoring 1 then 4. Each iteration's
        geometric mean is 1 and 4, averaging 2.5; the geometric mean of all
        four values pooled is 2.0, which is nobody's score."""
        doc = aggregate_doc("JetStream3.0", "Geometric",
                            {"x": [1.0, 4.0], "y": [1.0, 4.0]})
        got = wkdata._headline_score(doc)
        self.assertAlmostEqual(got, 2.5, places=9)
        self.assertNotAlmostEqual(got, 2.0, places=3)

    def test_total_and_arithmetic_are_applied_per_iteration_too(self):
        for aggregator, want in (("Total", 7.5), ("Arithmetic", 3.75)):
            with self.subTest(aggregator=aggregator):
                doc = aggregate_doc("Suite", aggregator, {"x": [1.0, 4.0], "y": [2.0, 8.0]})
                self.assertAlmostEqual(wkdata._headline_score(doc), want, places=9)

    def test_a_suite_with_no_score_metric_at_all_yields_nothing(self):
        self.assertIsNone(wkdata._headline_score(
            {"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [1.0]}}}}}}))


class TestAnUnreadableAggregateIsRefusedByName(WkTest):
    """A silent empty answer is what kept this defect invisible for a whole
    run, so every shape the aggregate cannot be taken from says which one it is."""

    def _refusal(self, doc):
        with self.assertRaises(SystemExit) as caught:
            wkdata._headline_score(doc)
        return str(caught.exception)

    def test_an_aggregator_this_file_does_not_implement_is_named(self):
        msg = self._refusal(aggregate_doc("JetStream3.0", "Harmonic", {"x": [1.0]}))
        self.assertIn("Harmonic", msg)
        self.assertIn("Geometric", msg, "the refusal names what it does implement")

    def test_a_declaration_with_no_aggregator_at_all_is_refused(self):
        self.assertIn("declares its Score",
                      self._refusal({"S": {"metrics": {"Score": []}, "tests": {}}}))

    def test_a_subtest_reporting_no_score_is_named(self):
        doc = aggregate_doc("JetStream3.0", "Geometric", {"x": [1.0], "y": [2.0]})
        doc["JetStream3.0"]["tests"]["y"] = {"metrics": {"Time": {"current": [1.0]}}}
        msg = self._refusal(doc)
        self.assertIn("1 of 2 first-level tests report no Score", msg)
        self.assertIn("y", msg)

    def test_subtests_disagreeing_about_the_iteration_count_are_refused(self):
        msg = self._refusal(aggregate_doc("JetStream3.0", "Geometric",
                                          {"x": [1.0, 2.0], "y": [3.0]}))
        self.assertIn("1/2 iterations", msg)

    def test_a_geometric_mean_over_a_zero_score_is_refused(self):
        msg = self._refusal(aggregate_doc("MotionMark-1.3.1", "Geometric",
                                          {"x": [0.0], "y": [7.0]}))
        self.assertIn("zero or less", msg)


class TestARunIsADirectory(WkTest):
    """`ab-precision` names run directories, the way `wk bench report` does, and
    appends result.json itself: three callers each spelling that append is three
    implementations of one convention."""

    def test_it_takes_directories_and_finds_the_result_json_inside(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [100.0, 100.5, 99.5, 100.0])
            b = write_runs(tmp, "b", [100.2, 100.7, 99.7, 100.2])
            cp = wkd("ab-precision", "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["n_a"], "4")
            self.assertAlmostEqual(float(f["delta_pct"]), 0.2, places=2)

    def test_a_directory_with_no_result_json_says_exactly_that(self):
        with scratch_dir() as tmp:
            empty = tmp / "nothing"
            empty.mkdir()
            cp = wkd("ab-precision", "--a", str(empty), "--b", str(empty))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("no result.json in this directory", cp.stdout + cp.stderr)
            self.assertIn("side A", cp.stdout + cp.stderr)
            self.assertNotIn("met=", cp.stdout)

    def test_a_result_json_that_is_not_json_is_reported_as_such(self):
        with scratch_dir() as tmp:
            d = tmp / "junk"
            d.mkdir()
            (d / "result.json").write_text("not json at all")
            cp = wkd("ab-precision", "--a", str(d), "--b", str(d))
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("not JSON", cp.stdout + cp.stderr)

    def test_a_result_json_carrying_no_suite_is_reported_as_such(self):
        with scratch_dir() as tmp:
            d = tmp / "bare"
            d.mkdir()
            (d / "result.json").write_text(json.dumps({"debugOutput": [None]}))
            cp = wkd("ab-precision", "--a", str(d), "--b", str(d))
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("no single suite carrying a Score metric", cp.stdout + cp.stderr)

    def test_one_unreadable_round_among_good_ones_is_warned_about_not_hidden(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [100.0, 100.5, 99.5])
            (a[2] / "result.json").unlink()
            b = write_runs(tmp, "b", [100.2, 100.7, 99.7])
            cp = wkd("ab-precision", "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(fields(cp.stdout)["n_a"], "2")
            self.assertIn("warning: side A", cp.stderr)

    def test_naming_no_directory_at_all_is_refused(self):
        cp = wkd("ab-precision", "--a", "", "--b", "")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no run directories given", cp.stdout + cp.stderr)


class TestTheCommand(WkTest):
    def test_a_noisy_experiment_is_not_met_and_says_how_many_more_rounds(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [99.0, 101.0, 99.0, 101.0, 99.0, 101.0])
            b = write_runs(tmp, "b", [99.5, 101.5, 99.5, 101.5, 99.5, 101.5])
            cp = wkd("ab-precision", "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
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
            cp = wkd("ab-precision", "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            f = fields(cp.stdout)
            self.assertEqual(f["met"], "yes", cp.stdout)
            self.assertEqual(f["rounds_needed"], "")

    def test_two_arms_scoring_identically_still_report_a_verdict(self):
        """The A/A control mac-ab-summary.sh warns about: with no spread at all
        Welch has no p-value, and a traceback after met=yes exits non-zero,
        which the autorun reads as "this plan does not resolve yet"."""
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [100.0, 100.0])
            b = write_runs(tmp, "b", [100.0, 100.0])
            cp = wkd("ab-precision", "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["met"], "yes")
            self.assertEqual(f["mde_pct"], "0.0000")
            self.assertEqual(f["p"], "")

    def test_the_target_is_what_moves_the_verdict(self):
        with scratch_dir() as tmp:
            a = write_runs(tmp, "a", [99.0, 101.0, 99.0, 101.0])
            b = write_runs(tmp, "b", [99.0, 101.0, 99.0, 101.0])
            args = ["--a", ",".join(str(p) for p in a), "--b", ",".join(str(p) for p in b)]
            strict = fields(wkd("ab-precision", "--target", "0.3", *args).stdout)
            loose = fields(wkd("ab-precision", "--target", "10", *args).stdout)
            self.assertEqual(strict["met"], "no")
            self.assertEqual(loose["met"], "yes")
            self.assertEqual(strict["mde_pct"], loose["mde_pct"])

    def test_the_speedometer3_verdict_the_first_mac_ab_reached_is_unchanged(self):
        """The one plan whose stopping rule could already fire. The rounds are
        constructed to the means and spread that run reported -- mean_a=58.9816,
        mean_b=58.9853, mde_pct=0.2254 over 16 rounds -- so reading the headline
        out of the suite's materialised values rather than out of the subtest
        table is proved not to move the verdict."""
        with scratch_dir() as tmp:
            spread = 0.1257
            a = write_runs(tmp, "a", [58.9816 + spread] * 8 + [58.9816 - spread] * 8)
            b = write_runs(tmp, "b", [58.9853 + spread] * 8 + [58.9853 - spread] * 8)
            cp = wkd("ab-precision", "--target", "0.3",
                     "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["n_a"], "16")
            self.assertEqual(f["mean_a"], "58.9816")
            self.assertEqual(f["mean_b"], "58.9853")
            self.assertEqual(f["mde_pct"], "0.2254")
            self.assertEqual(f["met"], "yes")
            self.assertEqual(f["rounds_needed"], "")

    def test_jetstream3_rounds_reach_a_verdict_at_all(self):
        """The defect: sixteen JetStream3 rounds a side printed met=no from
        n_a=0, so no A/B on it could ever stop on precision."""
        with scratch_dir() as tmp:
            def doc(scale):
                return aggregate_doc("JetStream3.0", "Geometric",
                                     {k: [v[0] * scale] for k, v in JETSTREAM3_CHILDREN.items()})
            a = write_docs(tmp, "a", [doc(1.0 + i * 1e-4) for i in range(-3, 3)])
            b = write_docs(tmp, "b", [doc(1.0 + i * 1e-4) for i in range(-3, 3)])
            cp = wkd("ab-precision", "--target", "0.3",
                     "--a", ",".join(str(p) for p in a),
                     "--b", ",".join(str(p) for p in b))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            f = fields(cp.stdout)
            self.assertEqual(f["n_a"], "6")
            self.assertAlmostEqual(float(f["mean_a"]), JETSTREAM3_HEADLINE, delta=0.1)
            self.assertEqual(f["met"], "yes")


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
