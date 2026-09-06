"""The two gates that stand between a PGO collection and a number nobody can
attribute (build/mac-pgo.sh, bench/mac-browser-check.py, bench/mac-profile-check.py).

Both refuse on evidence taken from the run itself, so both are exercised here
against readings rather than against a Mac: a throttled window, a machine with
no Metal device, a benchmark leg that died after one iteration.

Run: python3 -m unittest tests.test_mac_gates -v
"""
import importlib.util
import os
import unittest

from tests.support import REPO, WkTest, bash, func_body, scratch_dir


def load(name):
    path = REPO / "bench" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BROWSER = load("mac-browser-check")
PROFILE = load("mac-profile-check")

GOOD_READING = {"webgl": "WebGL 2.0", "renderer": "Apple GPU",
                "raf_hz": 57.2, "screen": [1024, 768]}
GOOD_CLIENTS = {1732: "com.apple.WebKit.GPU.Development"}

# The real invocation, not the dry run's printf of the same command.
RUN_COLLECTION = "env WK_WEBKIT_SCRIPTS="


class TestTheBrowserGate(WkTest):
    def verdict(self, reading=None, clients=None, min_raf=45.0):
        return BROWSER.faults(dict(GOOD_READING if reading is None else reading),
                              GOOD_CLIENTS if clients is None else clients,
                              "AppleParavirtGPU", min_raf)

    def test_an_accelerated_unthrottled_run_passes(self):
        self.assertEqual(self.verdict(), [])

    def test_a_throttled_window_is_refused(self):
        """The failure this exists for: a backgrounded or napped window still
        finishes a benchmark, and the score is of the throttle."""
        reading = dict(GOOD_READING, raf_hz=8.0)
        self.assertTrue(any("throttle" in f for f in self.verdict(reading)))

    def test_a_machine_with_no_metal_device_is_refused(self):
        reading = dict(GOOD_READING, webgl=None)
        self.assertTrue(any("no WebGL" in f for f in self.verdict(reading)))

    def test_a_run_no_webkit_gpu_process_touched_is_refused(self):
        self.assertTrue(any("did not reach that device" in f
                            for f in self.verdict(clients={})))

    def test_a_page_that_never_reported_is_refused(self):
        found = self.verdict(reading={}, clients={})
        self.assertTrue(any("never reported" in f for f in found))

    def test_a_screen_run_benchmark_cannot_use_is_refused(self):
        reading = dict(GOOD_READING, screen=[0, 0])
        self.assertTrue(any("size a window from" in f for f in self.verdict(reading)))

    def test_the_bar_is_the_callers_to_set(self):
        self.assertEqual(self.verdict(min_raf=10.0), [])
        self.assertNotEqual(self.verdict(min_raf=59.0), [])


def profile_tree(root, benchmarks=PROFILE.BENCHMARKS, libraries=PROFILE.LIBRARIES,
                 compressed=True):
    for benchmark in benchmarks:
        os.makedirs(root / benchmark, exist_ok=True)
        for library in libraries:
            (root / benchmark / f"{library}.profdata").write_bytes(b"x")
    os.makedirs(root / "output", exist_ok=True)
    os.makedirs(root / "arm64", exist_ok=True)
    for library in libraries:
        (root / "output" / f"{library}.profdata").write_bytes(b"x")
        if compressed:
            (root / "arm64" / f"{library}.profdata.compressed").write_bytes(b"x" * 4096)


class TestTheProfileGate(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.root = self._scratch.__enter__()

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def read(self, summaries):
        """summaries: path -> llvm-profdata summary, keyed by the last two
        path components so a test names 'speedometer3/WebCore.profdata'."""
        def summary(path):
            key = "/".join(str(path).split(os.sep)[-2:])
            return summaries.get(key, {"total_functions": 40000,
                                       "maximum_function_count": 900000})
        return PROFILE.collect(str(self.root), "arm64", summary)

    def real_reading(self):
        """The shape a whole collection really has (measured in a guest,
        2026-09-06): counts differ by three orders of magnitude between a
        JavaScript benchmark and a rendering one, and coverage does not."""
        return {
            "speedometer3": {"JavaScriptCore": (17904, 119251764),
                             "WebCore": (29176, 41270953), "WebKit": (13975, 4754838)},
            "jetstream3": {"JavaScriptCore": (23621, 238390050),
                           "WebCore": (20877, 68910122), "WebKit": (13869, 692003)},
            "motionmark": {"JavaScriptCore": (12783, 335116774),
                           "WebCore": (19266, 920270849), "WebKit": (13517, 753197581)},
            "output": {"JavaScriptCore": (24129, 669667609),
                       "WebCore": (33017, 920270849), "WebKit": (15266, 756192470)},
        }

    def read_real(self, overrides=None):
        rows = self.real_reading()
        for where, libs in (overrides or {}).items():
            rows[where].update(libs)
        summaries = {f"{where}/{lib}.profdata": {"total_functions": f, "maximum_function_count": c}
                     for where, libs in rows.items() for lib, (f, c) in libs.items()}
        return self.read(summaries)

    def test_a_full_collection_passes(self):
        profile_tree(self.root)
        self.assertEqual(PROFILE.faults(self.read({})), [])

    def test_a_benchmark_that_never_finished_is_named(self):
        profile_tree(self.root)
        os.remove(self.root / "motionmark" / "WebCore.profdata")
        found = PROFILE.faults(self.read({}))
        self.assertTrue(any("motionmark/WebCore.profdata" in f and "did not finish" in f
                            for f in found), found)

    def test_a_missing_compressed_copy_is_named(self):
        """The measured build reads only this one, so its absence is silent."""
        profile_tree(self.root, compressed=False)
        found = PROFILE.faults(self.read({}))
        self.assertTrue(any("the measured build reads" in f for f in found), found)

    def test_an_all_zero_profile_is_refused(self):
        profile_tree(self.root)
        found = PROFILE.faults(self.read(
            {f"output/{lib}.profdata": {"total_functions": 40000,
                                        "maximum_function_count": 0}
             for lib in PROFILE.LIBRARIES}))
        self.assertTrue(any("every counter in it is zero" in f for f in found), found)

    def test_a_profile_with_almost_no_functions_is_refused(self):
        profile_tree(self.root)
        found = PROFILE.faults(self.read(
            {"output/JavaScriptCore.profdata": {"total_functions": 12,
                                                "maximum_function_count": 3}}))
        self.assertTrue(any("nothing ran long enough" in f for f in found), found)

    def test_a_real_collection_passes(self):
        """The reading a whole collection in a guest actually produced. A
        rule that cannot accept this one refuses every good profile: the
        counts across its three legs span three orders of magnitude."""
        profile_tree(self.root)
        self.assertEqual(PROFILE.faults(self.read_real()), [])

    def test_a_workload_that_barely_touches_a_library_is_not_a_fault(self):
        """jetstream3's total count in WebKit is 545x smaller than
        motionmark's, because one is a JavaScript benchmark and the other is a
        rendering one. That is the benchmark, not a broken leg."""
        profile_tree(self.root)
        found = PROFILE.faults(self.read_real())
        self.assertFalse([f for f in found if "jetstream3" in f], found)

    def test_a_leg_that_gave_up_early_is_refused(self):
        """It still writes its file; what gives it away is how little of the
        library it reached, which does not vary with the workload."""
        profile_tree(self.root)
        found = PROFILE.faults(self.read_real(
            {"jetstream3": {"JavaScriptCore": (900, 238390050)}}))
        self.assertTrue(any("jetstream3 touched 900" in f and "gave up early" in f
                            for f in found), found)

    def test_a_leg_that_wrote_a_file_and_ran_nothing_is_refused(self):
        profile_tree(self.root)
        found = PROFILE.faults(self.read_real(
            {"motionmark": {"WebCore": (19266, 0)}}))
        self.assertTrue(any("no counter above zero" in f for f in found), found)

    def test_an_unreadable_profile_is_refused(self):
        profile_tree(self.root)
        found = PROFILE.faults(self.read(
            {"output/WebKit.profdata": {"error": ["not a profile"]}}))
        self.assertTrue(any("llvm-profdata cannot read" in f for f in found), found)


class TestTheBuildIsGatedOnThem(WkTest):
    """Neither check is worth anything if a build can finish without it."""

    def test_the_collection_runs_the_browser_check_against_the_instrumented_build(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_collect")
        self.assertIn('--build-directory "$instr"', body)
        self.assertLess(body.index("mac-browser-check.py"), body.index(RUN_COLLECTION),
                        "the browser is checked before anything is profiled")

    def test_a_failed_browser_check_stops_the_build(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_collect")
        gate = body[body.index("mac-browser-check.py"):]
        self.assertIn("return 1", gate.split(RUN_COLLECTION)[0])

    def test_nothing_else_re_checks_what_the_profile_gate_already_asks(self):
        """`pgo_build` used to test for the compressed directory itself; the
        gate refuses on every library missing from it, which is strictly more."""
        build = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "pgo_build")
        self.assertNotIn("produced no profile", build)

    def test_the_profile_is_read_back_before_the_measured_phase(self):
        text = (REPO / "build" / "mac-pgo.sh").read_text()
        body = func_body(text, "_pgo_collect")
        self.assertGreater(body.index("mac-profile-check.py"), body.index(RUN_COLLECTION))
        self.assertIn("return 1", body[body.index("mac-profile-check.py"):])
        # and pgo_build runs the measured phase only after _pgo_collect succeeded
        build = func_body(text, "pgo_build")
        self.assertLess(build.index("_pgo_collect"), build.index("WK_ENABLE_PGO_USE=YES"))
        self.assertIn("_pgo_collect \"$instr\" \"$pgo\" \"$arch\" || return $?", build)


class TestBothArmsProfileAgainstOneBenchmark(WkTest):
    """speedometer3 and jetstream3 name a moving branch in their plan files, so
    an unpinned collection can profile the two arms against two revisions of the
    benchmark and call the difference the patch's."""

    def test_the_collection_is_handed_a_pinned_copy_of_each(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_collect")
        self.assertIn("_pgo_payload_args", body)
        self.assertIn("--benchmark-custom-options",
                      func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_payload_args"))

    def test_it_seeds_through_the_one_seeder(self):
        """A second clone of a benchmark is a second answer to which revision
        this fleet measures."""
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_payload_args")
        self.assertIn("seed_payload", body)
        self.assertNotIn("git clone", body)

    def test_a_pin_that_fails_stops_the_collection(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_collect")
        gate = body[body.index("_pgo_payload_args"):body.index(RUN_COLLECTION)]
        self.assertIn("return 1", gate)

    def test_what_was_pinned_is_recorded_beside_the_profile(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_collect")
        self.assertIn('"$pgo/payload-pins"', body)


class TestAnArmIsReclaimedOnceItIsStaged(WkTest):
    """Measured 2026-09-06: one profile-guided arm leaves 56 GB of instrumented
    products and 45 GB of measured ones in the guest, and the next arm wants the
    same room. Both are spent the moment the arm is on the benchmark install."""

    def test_the_reclaim_follows_the_stage_and_not_the_build(self):
        body = func_body((REPO / "bench" / "mac-ab.sh").read_text(), "build_and_stage")
        self.assertLess(body.index("bench stage"), body.index("reclaim_products"))
        self.assertIn("staged as", body[:body.index("reclaim_products")])

    def test_it_names_the_products_from_the_files_that_define_them(self):
        """A third spelling of Release-pgo/-instr is one that goes stale on its
        own and deletes the wrong directory, or nothing."""
        body = func_body((REPO / "bench" / "mac-ab.sh").read_text(), "reclaim_products")
        self.assertIn("config_build_dir", body)
        self.assertIn("PGO_INSTR_SUFFIX", body)
        self.assertNotIn("Release-pgo", body)

    def test_the_suffix_has_one_definition(self):
        text = (REPO / "build" / "mac-pgo.sh").read_text()
        self.assertIn("PGO_INSTR_SUFFIX=", text)
        self.assertIn('"$final$PGO_INSTR_SUFFIX"', text)
        self.assertNotIn('"$final-instr"', text)


class TestPyobjcIsProvisionedNotAssumed(WkTest):
    """Xcode's python3 carries no pyobjc, so a macOS install that measures
    anything installs it -- and every path that provisions one does."""

    def test_the_probe_reads_the_running_interpreter(self):
        with scratch_dir() as tmp:
            fake = tmp / "python3"
            fake.write_text('#!/bin/sh\necho 11.1\n')
            fake.chmod(0o755)
            cp = bash(f'. "$WK_ROOT/bench/mac-pyobjc.sh"; '
                      f'WK_PYOBJC_PYTHON={fake}; wk_pyobjc_have && echo HAVE || echo MISSING')
            self.assertIn("HAVE", cp.stdout)

    def test_a_different_version_is_not_good_enough(self):
        with scratch_dir() as tmp:
            fake = tmp / "python3"
            fake.write_text('#!/bin/sh\necho 9.0\n')
            fake.chmod(0o755)
            cp = bash(f'. "$WK_ROOT/bench/mac-pyobjc.sh"; '
                      f'WK_PYOBJC_PYTHON={fake}; wk_pyobjc_have && echo HAVE || echo MISSING')
            self.assertIn("MISSING", cp.stdout)
            cp = bash(f'. "$WK_ROOT/bench/mac-pyobjc.sh"; '
                      f'WK_PYOBJC_PYTHON={fake}; wk_pyobjc_findings')
            self.assertTrue(cp.stdout.startswith("wrong\t"), cp.stdout)

    def test_every_macos_install_wk_provisions_gets_it(self):
        for rel in ("vm/desktop.sh", "bench/mac-bench-firstboot.sh", "host/macos/tools.sh"):
            self.assertIn("wk_pyobjc_install", (REPO / rel).read_text(), rel)

    def test_the_guest_carries_it_in_the_settle_and_in_the_base_inputs(self):
        text = (REPO / "targets" / "vm.sh").read_text()
        self.assertIn("mac-pyobjc.sh", func_body(text, "_settle_desktop"))
        self.assertIn("mac-pyobjc.sh", func_body(text, "_base_inputs_hash"))

    def test_the_benchmark_install_is_given_it_by_both_writers(self):
        """--build-pkg and --repair write the same payload, from one table."""
        text = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        self.assertIn("bench/mac-pyobjc.sh", func_body(text, "bench_payload_files"))
        for name in ("do_build_pkg", "do_repair"):
            self.assertIn("stage_payload", func_body(text, name), name)

    def test_a_repaired_volume_is_given_this_tree_and_not_the_one_it_has(self):
        """The A/B's planted job runs the copy in the payload, so a volume
        re-armed from an older wk-tools runs an older lane."""
        text = (REPO / "bench" / "mac-bench-volume.sh").read_text()
        body = func_body(text, "stage_payload")
        self.assertIn("wk-tools/", body)
        self.assertIn("authorized_keys", body)
        for name in ("do_build_pkg", "do_repair"):
            self.assertNotIn("wk-tools/", func_body(text, name), name)

    def test_the_pgo_gate_asks_the_one_reader(self):
        body = func_body((REPO / "build" / "mac-pgo.sh").read_text(), "_pgo_screen_faults")
        self.assertIn("wk_pyobjc_have", body)


if __name__ == "__main__":
    unittest.main()
