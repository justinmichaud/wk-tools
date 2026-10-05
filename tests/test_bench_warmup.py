"""The A/B warmup round: what it measures, what it refuses, and that its runs
never reach the statistics."""
import contextlib
import io
import json
import os
import sys
import unittest

from tests.support import REPO, WkTest
from tests.test_slots import load_driver

sys.path.insert(0, str(REPO / "lib"))
from wk import samply  # noqa: E402
from wk.bench import record, report, scores  # noqa: E402
from wk.machine import Fake  # noqa: E402


def warmup_check(a, b, same_width=False):
    """What the A/B's warmup gate (lib/wk/bench/board_ab.py) is told refuses it, one reason per line."""
    return "\n".join(scores.warmup_check(str(a), str(b), same_width))


# A 32-bit ARM web process on the v3d hardware driver with the JIT warm, as the
# board's probe prints it: an ELF header as decimal bytes, the maps lines that
# name a driver, and the anonymous executable mappings the JIT took.
PROBE_32_HW = """pid=417
exe=/var/wk/slots/base/root/bin/WPEWebProcess
elf= 127 69 76 70 1 1 1 0 0 0 0 0 0 0 0 0 2 0 40 0
dri=/usr/lib/dri/v3d_dri.so 
gl=/usr/lib/libEGL.so.1 /usr/lib/libGLESv2.so.2 
drifd=/dev/dri/renderD128
execmap=b6a00000-b8a00000
execmap=b9000000-b9010000
"""

PROBE_64_SOFTWARE = """pid=91
exe=/var/wk/slots/base/root/bin/WPEWebProcess
elf= 127 69 76 70 2 1 1 0 0 0 0 0 0 0 0 0 2 0 183 0
dri=/usr/lib/dri/swrast_dri.so 
gl=/usr/lib/libEGL.so.1 
execmap=ffff8000-ffffc000
"""

PROBE_NO_JIT = """pid=91
exe=/var/wk/slots/base/root/bin/WPEWebProcess
elf= 127 69 76 70 2 1 1 0 0 0 0 0 0 0 0 0 2 0 183 0
dri=/usr/lib/dri/v3d_dri.so 
gl=/usr/lib/libEGL.so.1 
"""


class TestTheDriversEvidence(WkTest):
    """What lib/wk/bench/board_driver.py reads off the live web process, the GPU's counters and the browser log."""

    def setUp(self):
        super().setUp()
        self.d = load_driver()

    def test_reads_width_and_machine_off_the_live_process(self):
        r = self.d.warmup_record(PROBE_32_HW)
        self.assertEqual(r["elf"], {"bits": 32, "machine": "ARM"})
        r64 = self.d.warmup_record(PROBE_64_SOFTWARE)
        self.assertEqual(r64["elf"], {"bits": 64, "machine": "AArch64"})

    def test_names_the_driver_the_web_process_actually_mapped(self):
        r = self.d.warmup_record(PROBE_32_HW)
        self.assertEqual(r["gl"]["driver"], "/usr/lib/dri/v3d_dri.so")
        self.assertFalse(r["gl"]["software"])
        self.assertEqual(r["gl"]["render_nodes"], ["/dev/dri/renderD128"])

    def test_a_software_rasterizer_is_named_as_one(self):
        r = self.d.warmup_record(PROBE_64_SOFTWARE)
        self.assertTrue(r["gl"]["software"])
        self.assertIn("software rasterizer", " ".join(self.d.warmup_problems(r)))

    def test_executable_mappings_are_the_jit_evidence(self):
        r = self.d.warmup_record(PROBE_32_HW)
        self.assertEqual(r["jit"]["exec_mappings"], 2)
        self.assertEqual(r["jit"]["exec_bytes"], 0xb8a00000 - 0xb6a00000 + 0x10000)
        # The maps probe alone answers width, renderer and JIT-took-memory; the
        # GPU and tier sections come from their own probes and are checked there.
        maps_problems = [p for p in self.d.warmup_problems(r)
                         if "GPU" not in p and "DRM engine" not in p and "tier" not in p]
        self.assertEqual(maps_problems, [])

    def test_no_executable_mapping_refuses(self):
        r = self.d.warmup_record(PROBE_NO_JIT)
        self.assertEqual(r["jit"]["exec_mappings"], 0)
        self.assertIn("nothing was JITted", " ".join(self.d.warmup_problems(r)))

    def test_a_process_that_never_started_refuses_rather_than_reporting_zero(self):
        self.assertTrue(self.d.warmup_problems(self.d.warmup_record("pid=0\n")))

    BEFORE = ("gpudrv=v3d\n"
              "gpu=WPEWebProcess 417 render 1000000000\n"
              "gpu=WPEWebProcess 417 bin 500000000\n"
              "gpu=weston 300 render 9000000000\n")

    def test_engine_time_spent_during_the_run_is_the_measurement(self):
        after = ("gpudrv=v3d\n"
                 "gpu=WPEWebProcess 417 render 3500000000\n"
                 "gpu=WPEWebProcess 417 bin 700000000\n"
                 "gpu=weston 300 render 9400000000\n")
        g = self.d.gpu_delta(self.BEFORE, after)
        self.assertEqual(g["driver"], "v3d")
        self.assertEqual(g["busy_ms"], 3100)
        self.assertEqual(g["by_process_ms"]["WPEWebProcess"], 2700)
        self.assertEqual(g["by_process_ms"]["weston"], 400)

    def test_a_cpu_class_plan_records_gpu_time_but_never_requires_it(self):
        rec = {"elf": {"bits": 64}, "gl": {"mapped": ["v3d_dri.so"], "software": False},
               "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}}, "class": "cpu",
               "gpu": self.d.gpu_delta(self.BEFORE, self.BEFORE)}
        self.assertEqual(self.d.warmup_problems(rec), [])
        self.assertEqual(rec["gpu"]["busy_ms"], 0)

    def rec(self, measured, busy, nodes, mapped=("v3d_dri.so",), software=False):
        return {"elf": {"bits": 64}, "class": "gpu",
                "gl": {"mapped": list(mapped), "software": software,
                       "render_nodes": list(nodes)},
                "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}},
                "gpu": {"measured": measured, "busy_ms": busy, "driver": "v3d"}}

    def test_a_gpu_claim_needs_engine_time_or_a_render_node_and_no_software_rasterizer(self):
        node = ["/dev/dri/renderD128"]
        for measured, busy, nodes, software, problem in ((False, 0, node, False, None),
                                                          (False, 0, [], False, "nothing evidences a GPU path"),
                                                          (True, 0, node, False, "billed no engine time"),
                                                          (False, 0, node, True, "software rasterizer")):
            with self.subTest(measured=measured, nodes=nodes, software=software):
                r = self.rec(measured, busy, nodes, ("swrast_dri.so",) if software else ("v3d_dri.so",), software)
                problems = " ".join(self.d.warmup_problems(r))
                if problem:
                    self.assertIn(problem, problems)
                else:
                    self.assertEqual(problems, "")
                    self.assertTrue(any("unreadable on this driver" in n for n in r["notes"]))

    def record(self, bits, tiers):
        return {"elf": {"bits": bits}, "gl": {"mapped": ["v3d_dri.so"], "software": False},
                "jit": {"exec_mappings": 2, "tiers": tiers}, "class": "gpu",
                "gpu": {"measured": True, "busy_ms": 900, "driver": "v3d"}}

    def test_tier_lines_are_counted_off_the_browser_log(self):
        counts = self.d.tier_counts("tier=FTL 12\ntier=DFG 340\ntier=Baseline 5011\n")
        self.assertEqual(counts, {"FTL": 12, "DFG": 340, "Baseline": 5011})

    def test_a_64_bit_arm_needs_ftl_and_a_32_bit_one_dfg(self):
        for bits, tiers, problem in ((64, {"FTL": 1, "DFG": 90}, None),
                                     (64, {"FTL": 0, "DFG": 90, "Baseline": 500}, "reached no FTL compilation"),
                                     (32, {"DFG": 44, "FTL": 0}, None),
                                     (32, {"DFG": 0}, "reached no DFG compilation"),
                                     (64, {}, "no JSC compile-time report")):
            with self.subTest(bits=bits, tiers=tiers):
                problems = " ".join(self.d.warmup_problems(self.record(bits, tiers)))
                if problem:
                    self.assertIn(problem, problems)
                else:
                    self.assertEqual(problems, "")

    def test_not_probed_is_a_note_and_not_a_problem(self):
        rec = self.record(64, None)
        self.assertEqual(self.d.warmup_problems(rec), [])
        self.assertTrue(any("not probed" in n for n in rec.get("notes", [])), rec.get("notes"))


class TestWarmupCheck(WkTest):
    def check(self, a, b, same_width=False):
        for name, doc in (("a.json", a), ("b.json", b)):
            if doc is not None:
                (self.tmp / name).write_text(json.dumps(doc))
        return warmup_check(self.tmp / "a.json", self.tmp / "b.json", same_width)

    def record(self, bits=64, driver="/usr/lib/dri/v3d_dri.so", software=False, maps=2):
        return {"elf": {"bits": bits, "machine": "AArch64" if bits == 64 else "ARM"},
                "gl": {"driver": driver, "software": software, "mapped": [driver],
                       "libs": [], "render_nodes": ["/dev/dri/renderD128"]},
                "jit": {"exec_mappings": maps, "exec_bytes": 33554432,
                        "verdict": "JIT active",
                        "tiers": {"FTL" if bits == 64 else "DFG": 7}},
                "gpu": {"measured": True, "busy_ms": 900, "driver": "v3d"},
                "problems": []}

    def test_two_good_arms_pass_and_widths_may_differ_across_two_images(self):
        self.assertEqual(self.check(self.record(), self.record()), "")
        self.assertEqual(self.check(self.record(bits=64), self.record(bits=32)), "")

    def test_each_arm_that_is_not_what_the_ab_claims_refuses(self):
        for said, a, b, same_width in (("arm B produced no warmup evidence", self.record(), None, False),
                                       ("different drivers", self.record(), self.record(driver="/usr/lib/dri/swrast_dri.so", software=True), False),
                                       ("64-bit and 32-bit", self.record(bits=64), self.record(bits=32), True),
                                       ("elf/gl/jit/problems", {"meta": {"interval": 1.0}, "threads": []}, self.record(), False)):
            with self.subTest(said):
                for f in self.tmp.glob("*.json"):
                    f.unlink()
                self.assertIn(said, self.check(a, b, same_width))


class TestWarmupEvidenceIsPerBoard(WkTest):

    def test_each_board_reads_back_its_own_evidence(self):
        d = self.tmp
        (d / "warmup").mkdir()
        for board, bits in (("rpi3", 32), ("rpi5", 64)):
            for arm in ("a", "b"):
                (d / "warmup" / ("%s-%s.evidence.json" % (board, arm))).write_text(json.dumps(
                    {"elf": {"bits": bits, "machine": "ARM"},
                     "gl": {"driver": "/usr/lib/dri/v3d_dri.so", "software": False},
                     "jit": {"exec_mappings": 1, "exec_bytes": 4096, "verdict": "JIT active"}}))
        self.assertIn("32-bit", " ".join(report.warmup_lines(report.warmup_load(str(d), "rpi3"))))
        self.assertIn("64-bit", " ".join(report.warmup_lines(report.warmup_load(str(d), "rpi5"))))


class TestWarmupNeverEntersTheStatistics(WkTest):
    def task(self):
        d = self.tmp
        (d / "task.json").write_text(json.dumps({
            "task": "t", "subject": {"kind": "slots", "spec": "base,pr"},
            "devices": [{"device": "rpi5", "profile": "p"}], "plans": ["speedometer2.1"],
            "rounds": 1, "slots": ["base", "pr"]}))
        runs = d / "runs"
        runs.mkdir()
        for name, warmup, rnd, arm in (("r0a", True, "0", "a"), ("r0b", True, "0", "b"),
                                       ("r1a", False, "1", "a"), ("r1b", False, "1", "b")):
            r = runs / name
            r.mkdir()
            env = {"plan": "speedometer2.1", "machine": "rpi5",
                   "ab": {"round": rnd, "arm": arm}}
            if warmup:
                env["warmup"] = True
            (r / "env.json").write_text(json.dumps(env))
            (r / "result.json").write_text(json.dumps({"Speedometer-2": {}}))
        return d

    def test_a_warmup_run_is_not_counted_as_a_round(self):
        st = record.task_state(str(self.task()), False)
        self.assertEqual((st["ended"], st["usable"]), (2, 1))


class TestSubtestExclusions(WkTest):

    def test_an_excluded_run_says_so_in_the_report(self):
        ex = {"subtests_excluded": "argon2-wasm,dotnet-aot-wasm"}
        self.assertIn("note: 2 subtest(s) excluded from both arms", "\n".join(report.axis_check_lines(ex, dict(ex))))

    def test_arms_with_different_sets_are_a_warning_not_a_note(self):
        lines = report.axis_check_lines({"subtests_excluded": "argon2-wasm"}, {"plan": "jetstream3"})
        self.assertTrue([l for l in lines if l.startswith("warning: the arms ran different subtest sets")])


class TestRunOrderAndSettling(WkTest):
    def runs(self, order):
        """order is the arm of each run in time order, e.g. 'ABBA'."""
        a, b = [], []
        for i, arm in enumerate(order):
            entry = ("/t/runs/2026090%d" % i, {}, {})
            (a if arm == "A" else b).append(entry)
        return a, b

    def test_an_order_that_is_not_counterbalanced_is_reported_and_blocked_runs_hardest(self):
        for order, said in (("ABABABABAB", "not counterbalanced -- B runs 1.0 position"), ("AAAAABBBBB", "B runs 5.0 position"), ("ABBAABBA", None)):
            with self.subTest(order):
                lines = report.order_lines(*self.runs(order))
                if said:
                    self.assertIn(said, lines[0])
                else:
                    self.assertEqual(lines, [])


class TestScoreAgainstItsOwnSubtests(WkTest):

    def rows(self, score_a, score_b, time_a, time_b, n=12):
        rows = [{"name": "Speedometer-2",
                 "Score": {"a_mean": score_a, "b_mean": score_b},
                 "Time": {"a_mean": None, "b_mean": None}}]
        for i in range(n):
            rows.append({"name": "S%d/step/Sync" % i,
                         "Score": {"a_mean": None, "b_mean": None},
                         "Time": {"a_mean": time_a / n, "b_mean": time_b / n}})
        return rows

    def test_less_work_and_a_higher_score_is_consistent(self):
        lines = report.consistency_lines(self.rows(100.0, 105.0, 1000.0, 950.0))
        self.assertTrue(lines[0].startswith("note:"))
        self.assertFalse([l for l in lines if l.startswith("warning:")])

    def test_less_work_and_a_lower_score_disagrees_in_sign(self):
        lines = report.consistency_lines(self.rows(100.0, 99.2, 1000.0, 953.5))
        joined = " ".join(lines)
        self.assertIn("disagree in SIGN", joined)
        self.assertIn("do not quote either", joined)

    def test_a_shape_it_cannot_read_says_nothing_rather_than_guessing(self):
        self.assertEqual(report.consistency_lines([]), [])


class TestProfilerChoice(WkTest):

    def test_aarch64_and_x86_64_use_samply_and_armv7_the_images_sysprof(self):
        for arch, sysprof, tool in (("aarch64", False, "samply"), ("x86_64", False, "samply"), ("armv7l", True, "sysprof")):
            with self.subTest(arch=arch):
                self.assertEqual(samply.resolve(arch, sysprof)[0], tool)

    def test_armv7_without_sysprof_refuses_and_names_both_remedies(self):
        tool, why = samply.resolve("armv7l", False)
        self.assertIsNone(tool)
        self.assertIn("sysprof-cli", why)
        self.assertIn("samply", why)


class TestSamplyFetchUnderADryRun(unittest.TestCase):

    def setUp(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.addCleanup(os.environ.pop, "WK_DRY_RUN", None)

    def fetch(self):
        m = Fake("here")
        binary = os.path.join(samply.store_dir("/cache", samply.triple("x86_64")), "samply")
        m.answer(["test", "-x", binary], rc=1)
        m.answer(["mktemp", "-d"], out="/tmp/samply-fake\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            found = samply.fetch(m, "/cache", "x86_64")
        return found, binary, err.getvalue()

    def test_it_returns_what_it_would_install_and_unpacks_nothing(self):
        found, binary, err = self.fetch()
        self.assertEqual(binary, found)
        self.assertIn("would run", err)
        self.assertIn("curl", err)
        self.assertNotIn("would not unpack", err)


if __name__ == "__main__":
    unittest.main()
