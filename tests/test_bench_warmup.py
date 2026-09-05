"""The A/B warmup round: what it measures, what it refuses, and that its runs
never reach the statistics.

Round 0 of every A/B is a leg per arm that is thrown away. It exists because a
measured round records only what the run *claimed* -- `gpu_renderer=gl` is set
the moment weston reports an output, and nothing checks which mesa driver the
web process resolved to, how wide it is, or whether it JITted at all. The
warmup leg reads those off the live process (bench/wk_board_driver.py) and
carries a profile.

Unit tests only: the probe's raw board output is a fixture, so no board.

Run: python3 -m unittest tests.test_bench_warmup -v
"""
import json
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests.support import REPO, WkTest
from tests.test_slots import load_driver

WKDATA = REPO / "lib" / "wkdata.py"


def tmpdir(case):
    d = Path(tempfile.mkdtemp(prefix="wk-warmup-"))
    case.addCleanup(shutil.rmtree, d, True)
    return d


def wkdata(*args):
    return subprocess.run(["python3", str(WKDATA), *args], cwd=str(REPO),
                          capture_output=True, text=True, timeout=30)


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


class TestWarmupProbe(WkTest):
    def setUp(self):
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
        r = self.d.warmup_record("pid=0\n")
        problems = self.d.warmup_problems(r)
        self.assertTrue(problems)


class TestGpuLoad(WkTest):
    """Which driver is mapped says the driver loaded; engine time says it worked."""

    def setUp(self):
        self.d = load_driver()

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

    def test_a_run_that_billed_the_gpu_nothing_refuses(self):
        rec = {"elf": {"bits": 64}, "gl": {"mapped": ["v3d_dri.so"], "software": False},
               "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}}, "class": "gpu",
               "gpu": self.d.gpu_delta(self.BEFORE, self.BEFORE)}
        self.assertIn("billed no engine time", " ".join(self.d.warmup_problems(rec)))

    def test_a_cpu_class_plan_records_gpu_time_but_never_requires_it(self):
        """JetStream renders almost nothing; demanding engine time there would
        refuse every correct run."""
        rec = {"elf": {"bits": 64}, "gl": {"mapped": ["v3d_dri.so"], "software": False},
               "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}}, "class": "cpu",
               "gpu": self.d.gpu_delta(self.BEFORE, self.BEFORE)}
        self.assertEqual(self.d.warmup_problems(rec), [])
        self.assertEqual(rec["gpu"]["busy_ms"], 0)

    def test_no_counters_and_no_render_node_refuses_rather_than_reading_as_zero(self):
        rec = {"elf": {"bits": 64}, "gl": {"mapped": ["v3d_dri.so"], "software": False,
                                           "render_nodes": []},
               "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}}, "class": "gpu",
               "gpu": self.d.gpu_delta("", "")}
        self.assertIn("nothing evidences a GPU path",
                      " ".join(self.d.warmup_problems(rec)))


class TestArtifactsOfOneLegDoNotCollide(WkTest):
    """The evidence file and the profile capture are two artifacts of one leg.
    Naming both `<machine>-<arm>.json` made samply's capture overwrite the
    evidence, and the gate then passed because a samply profile is valid JSON
    (2026-09-05, task 20260905T200814Z)."""

    def test_the_two_paths_are_never_the_same(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn("$machine-$PI_AB_ARM.evidence.json", body)
        self.assertIn("$machine-$PI_AB_ARM.profile.$(pi_profile_ext)", body)
        self.assertNotIn('WK_BOARD_WARMUP="${warmdir:+$warmdir/$machine-$PI_AB_ARM.json}"',
                         body)

    def test_a_json_file_that_is_not_evidence_is_refused(self):
        d = tmpdir(self)
        # a samply profile: valid JSON, no evidence fields
        (d / "a.json").write_text(json.dumps({"meta": {"interval": 1.0}, "threads": []}))
        (d / "b.json").write_text(json.dumps(
            {"elf": {"bits": 64}, "gl": {}, "jit": {}, "problems": []}))
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("is not warmup evidence", cp.stdout)
        self.assertIn("elf/gl/jit/problems", cp.stdout)


class TestGpuClaimMatchesWhatTheDriverCanSay(WkTest):
    """v3d on 6.6.22 publishes no fdinfo drm-engine-* counters at all, so
    engine time is unreadable there. Refusing every run on that board forever
    would be refusing an unmeasurable quantity, not a bad arm."""

    def setUp(self):
        self.d = load_driver()

    def rec(self, measured, busy, nodes, mapped=("v3d_dri.so",), software=False):
        return {"elf": {"bits": 64}, "class": "gpu",
                "gl": {"mapped": list(mapped), "software": software,
                       "render_nodes": list(nodes)},
                "jit": {"exec_mappings": 1, "tiers": {"FTL": 3}},
                "gpu": {"measured": measured, "busy_ms": busy, "driver": "v3d"}}

    def test_no_counters_but_a_held_render_node_is_a_note_not_a_refusal(self):
        r = self.rec(False, 0, ["/dev/dri/renderD128"])
        self.assertEqual(self.d.warmup_problems(r), [])
        self.assertTrue(any("unreadable on this driver" in n for n in r["notes"]))

    def test_no_counters_and_no_render_node_still_refuses(self):
        r = self.rec(False, 0, [])
        self.assertIn("nothing evidences a GPU path", " ".join(self.d.warmup_problems(r)))

    def test_counters_that_exist_and_read_zero_still_refuse(self):
        r = self.rec(True, 0, ["/dev/dri/renderD128"])
        self.assertIn("billed no engine time", " ".join(self.d.warmup_problems(r)))

    def test_a_software_rasterizer_refuses_whatever_the_counters_say(self):
        r = self.rec(False, 0, ["/dev/dri/renderD128"],
                     mapped=("swrast_dri.so",), software=True)
        self.assertIn("software rasterizer", " ".join(self.d.warmup_problems(r)))


class TestJitTierIsConfirmed(WkTest):
    """A JIT that took executable memory may still never have left baseline."""

    def setUp(self):
        self.d = load_driver()

    def record(self, bits, tiers):
        return {"elf": {"bits": bits}, "gl": {"mapped": ["v3d_dri.so"], "software": False},
                "jit": {"exec_mappings": 2, "tiers": tiers}, "class": "gpu",
                "gpu": {"measured": True, "busy_ms": 900, "driver": "v3d"}}

    def test_tier_lines_are_counted_off_the_browser_log(self):
        counts = self.d.tier_counts("tier=FTL 12\ntier=DFG 340\ntier=Baseline 5011\n")
        self.assertEqual(counts, {"FTL": 12, "DFG": 340, "Baseline": 5011})

    def test_a_64_bit_arm_needs_an_ftl_compilation(self):
        self.assertEqual(self.d.warmup_problems(self.record(64, {"FTL": 1, "DFG": 90})), [])
        self.assertIn("reached no FTL compilation",
                      " ".join(self.d.warmup_problems(
                          self.record(64, {"FTL": 0, "DFG": 90, "Baseline": 500}))))

    def test_a_32_bit_arm_needs_a_dfg_compilation_and_never_an_ftl_one(self):
        self.assertEqual(self.d.warmup_problems(self.record(32, {"DFG": 44, "FTL": 0})), [])
        self.assertIn("reached no DFG compilation",
                      " ".join(self.d.warmup_problems(self.record(32, {"DFG": 0}))))

    def test_no_report_at_all_refuses_rather_than_passing(self):
        self.assertIn("no JSC compile-time report",
                      " ".join(self.d.warmup_problems(self.record(64, {}))))


class TestWarmupCheck(WkTest):
    def arms(self, a, b):
        d = tmpdir(self)
        (d / "a.json").write_text(json.dumps(a))
        (d / "b.json").write_text(json.dumps(b))
        return d

    def record(self, bits=64, driver="/usr/lib/dri/v3d_dri.so", software=False, maps=2):
        return {"elf": {"bits": bits, "machine": "AArch64" if bits == 64 else "ARM"},
                "gl": {"driver": driver, "software": software, "mapped": [driver],
                       "libs": [], "render_nodes": ["/dev/dri/renderD128"]},
                "jit": {"exec_mappings": maps, "exec_bytes": 33554432,
                        "verdict": "JIT active",
                        "tiers": {"FTL" if bits == 64 else "DFG": 7}},
                "gpu": {"measured": True, "busy_ms": 900, "driver": "v3d"},
                "problems": []}

    def test_two_good_arms_pass(self):
        d = self.arms(self.record(), self.record())
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_a_missing_arm_refuses(self):
        d = tmpdir(self)
        (d / "a.json").write_text(json.dumps(self.record()))
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("arm B produced no warmup evidence", cp.stdout)

    def test_different_renderers_refuse(self):
        d = self.arms(self.record(), self.record(driver="/usr/lib/dri/swrast_dri.so",
                                                 software=True))
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"))
        self.assertEqual(cp.returncode, 1)
        self.assertIn("different drivers", cp.stdout)

    def test_widths_may_differ_across_two_images(self):
        d = self.arms(self.record(bits=64), self.record(bits=32))
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_widths_may_not_differ_across_two_slots_of_one_image(self):
        d = self.arms(self.record(bits=64), self.record(bits=32))
        cp = wkdata("warmup-check", str(d / "a.json"), str(d / "b.json"), "--same-width")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("64-bit and 32-bit", cp.stdout)


class TestWarmupEvidenceIsPerBoard(WkTest):
    """One task can hold several boards at once (wk ab --devices rpi3,rpi4), so
    the evidence is keyed by machine or the boards overwrite each other."""

    def test_each_board_reads_back_its_own_evidence(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("wkdata", WKDATA)
        wk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wk)
        d = tmpdir(self)
        (d / "warmup").mkdir()
        for board, bits in (("rpi3", 32), ("rpi5", 64)):
            for arm in ("a", "b"):
                (d / "warmup" / ("%s-%s.evidence.json" % (board, arm))).write_text(json.dumps(
                    {"elf": {"bits": bits, "machine": "ARM"},
                     "gl": {"driver": "/usr/lib/dri/v3d_dri.so", "software": False},
                     "jit": {"exec_mappings": 1, "exec_bytes": 4096, "verdict": "JIT active"}}))
        self.assertIn("32-bit", " ".join(wk.warmup_lines(wk.warmup_load(str(d), "rpi3"))))
        self.assertIn("64-bit", " ".join(wk.warmup_lines(wk.warmup_load(str(d), "rpi5"))))


class TestWarmupNeverEntersTheStatistics(WkTest):
    def task(self):
        d = tmpdir(self)
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
        cp = wkdata("task-status", str(self.task()))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        fields = dict(l.split("=", 1) for l in cp.stdout.splitlines() if "=" in l)
        self.assertEqual(fields["ended"], "2")
        self.assertEqual(fields["usable"], "1")


class TestSubtestExclusions(WkTest):
    """One arm that cannot run a subtest disqualifies it for both arms: JSC
    disables wasm SIMD below 64-bit outright (Options.cpp, `#if !CPU(X86_64)
    && !CPU(ARM64)`), so a SIMD-built module cannot run on a 32-bit engine."""

    PLAN = json.dumps({"subtests": {"": ["a-wasm", "argon2-wasm", "b", "c"]}})

    def resolve(self, exclude):
        return subprocess.run(
            ["python3", str(WKDATA), "subtests", "--exclude", exclude],
            input=self.PLAN, cwd=str(REPO), capture_output=True, text=True, timeout=15)

    def test_the_kept_set_is_the_plan_minus_the_exclusions(self):
        cp = self.resolve("argon2-wasm")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(), ["a-wasm", "b", "c"])

    def test_excluding_nothing_keeps_everything(self):
        self.assertEqual(self.resolve("").stdout.split(), ["a-wasm", "argon2-wasm", "b", "c"])

    def test_a_name_the_plan_does_not_have_refuses(self):
        cp = self.resolve("nope")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("names no subtest", cp.stdout + cp.stderr)

    def test_excluding_everything_refuses(self):
        cp = self.resolve("a-wasm,argon2-wasm,b,c")
        self.assertEqual(cp.returncode, 1)

    def test_the_declared_rows_name_real_jetstream3_subtests_and_a_reason(self):
        rows = [l.split(None, 3) for l in
                (REPO / "bench" / "subtest-exclusions.conf").read_text().splitlines()
                if l.strip() and not l.startswith("#")]
        self.assertTrue(rows)
        for plan, bits, name, why in rows:
            self.assertIn(bits, ("32", "64"), name)
            self.assertTrue(why.strip(), "%s carries no reason" % name)

    def test_an_excluded_run_says_so_in_the_report(self):
        cp = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0, 'lib'); import wkdata;"
             "print(chr(10).join(wkdata._axis_check_lines("
             "{'subtests_excluded': 'argon2-wasm,dotnet-aot-wasm'},"
             "{'subtests_excluded': 'argon2-wasm,dotnet-aot-wasm'})))"],
            cwd=str(REPO), capture_output=True, text=True, timeout=15)
        self.assertIn("2 subtest(s) excluded from both arms", cp.stdout)

    def test_arms_with_different_sets_are_a_warning_not_a_note(self):
        cp = subprocess.run(
            ["python3", "-c",
             "import sys; sys.path.insert(0, 'lib'); import wkdata;"
             "print(chr(10).join(wkdata._axis_check_lines("
             "{'subtests_excluded': 'argon2-wasm'}, {'plan': 'jetstream3'})))"],
            cwd=str(REPO), capture_output=True, text=True, timeout=15)
        self.assertIn("warning: the arms ran different subtest sets", cp.stdout)


class TestRunOrderAndSettling(WkTest):
    def wkd(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("wkdata", WKDATA)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

    def runs(self, order):
        """order is the arm of each run in time order, e.g. 'ABBA'."""
        a, b = [], []
        for i, arm in enumerate(order):
            entry = ("/t/runs/2026090%d/result.json" % i, {}, {})
            (a if arm == "A" else b).append(entry)
        return a, b

    def test_always_leading_with_a_is_reported(self):
        a, b = self.runs("ABABABABAB")
        lines = self.wkd()._order_lines(a, b)
        self.assertTrue(lines)
        self.assertIn("not counterbalanced", lines[0])
        self.assertIn("B runs 1.0 position", lines[0])

    def test_a_counterbalanced_order_is_not_flagged(self):
        a, b = self.runs("ABBAABBA")
        self.assertEqual(self.wkd()._order_lines(a, b), [])

    def test_blocked_runs_are_flagged_hardest(self):
        a, b = self.runs("AAAAABBBBB")
        lines = self.wkd()._order_lines(a, b)
        self.assertIn("5.0 position", lines[0])

    def test_every_reboot_gets_a_discarded_settle_run(self):
        leg = subprocess.run(
            ["bash", "-c", 'sed -n "/^pi_system_leg/,/^}/p" "$1/cmd/pi"', "_", str(REPO)],
            capture_output=True, text=True, timeout=15).stdout
        self.assertIn("PI_SETTLE=1", leg)
        self.assertEqual(leg.count("pi_bench_once"), 2,
                         "a booted leg runs one discarded run then the measured one")

    def test_the_ab_loops_lead_with_each_arm_in_turn(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertEqual(body.count("i % 2"), 2,
                         "both --ab and --ab-systems counterbalance their rounds")

    def test_the_clock_is_pinned_not_merely_governed(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn("scaling_min_freq", body)
        self.assertIn("dvfs_pinned", body)


class TestScoreAgainstItsOwnSubtests(WkTest):
    """The check a person kept doing by hand: a score and the subtest times it
    is built from must move opposite ways."""

    def wkd(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("wkdata", WKDATA)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        return m

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
        lines = self.wkd()._consistency_lines(self.rows(100.0, 105.0, 1000.0, 950.0))
        self.assertTrue(lines[0].startswith("note:"))
        self.assertFalse([l for l in lines if l.startswith("warning:")])

    def test_less_work_and_a_lower_score_disagrees_in_sign(self):
        lines = self.wkd()._consistency_lines(self.rows(100.0, 99.2, 1000.0, 953.5))
        joined = " ".join(lines)
        self.assertIn("disagree in SIGN", joined)
        self.assertIn("do not quote either", joined)

    def test_a_shape_it_cannot_read_says_nothing_rather_than_guessing(self):
        self.assertEqual(self.wkd()._consistency_lines([]), [])


class TestProfilerChoice(WkTest):
    """lib/profiler.sh: samply where upstream publishes one, sysprof where it does not."""

    def resolve(self, machine, sysprof):
        return subprocess.run(
            ["bash", "-c",
             '. "$1/lib/profiler.sh"; profiler_resolve "$2" "$3"',
             "_", str(REPO), machine, sysprof],
            capture_output=True, text=True, timeout=15)

    def test_aarch64_uses_samply(self):
        cp = self.resolve("aarch64", "no")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split()[0], "samply")

    def test_x86_64_uses_samply(self):
        self.assertEqual(self.resolve("x86_64", "no").stdout.split()[0], "samply")

    def test_the_arch_asked_about_is_the_userspace_not_the_kernel(self):
        """A lib32 image reports aarch64 from `uname -m` and has no 64-bit
        loader; resolving on that staged a samply the board could not exec
        (2026-09-05, rpi5-32: "cannot execute: required file not found")."""
        probe = subprocess.run(
            ["bash", "-c",
             'grep -c "uname -m" "$1/cmd/pi" || true', "_", str(REPO)],
            capture_output=True, text=True, timeout=15)
        stage = subprocess.run(
            ["bash", "-c",
             'sed -n "/^pi_profiler_stage/,/^}/p" "$1/cmd/pi"', "_", str(REPO)],
            capture_output=True, text=True, timeout=15)
        self.assertNotIn("uname -m", stage.stdout,
                         "the profiler is resolved from the kernel arch again")
        self.assertIn("pi_userspace_arch", stage.stdout)

    def test_armv7_falls_to_the_image_sysprof(self):
        cp = self.resolve("armv7l", "yes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split()[0], "sysprof")

    def test_armv7_without_sysprof_refuses_and_names_both_remedies(self):
        cp = self.resolve("armv7l", "no")
        self.assertEqual(cp.returncode, 1)
        self.assertIn("sysprof-cli", cp.stdout)
        self.assertIn("samply", cp.stdout)


if __name__ == "__main__":
    unittest.main()
