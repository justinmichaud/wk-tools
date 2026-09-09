"""The macOS perf build: `mac-release-pgo`, the config every macOS number is
taken from (build/configs.sh, build/mac-pgo.sh, build/pgo-run-benchmark.py).

Three phases with a benchmark run between them, so the shape of each phase --
and the fact that the instrumented one never lands in the measured one's
products directory -- is what these tests pin.

Run: python3 -m unittest tests.test_mac_pgo -v
"""
import os
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, run, scratch_dir

CONFIG = "mac-release-pgo"


def config_fields(config, os_name, kind="vm"):
    cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/arch.sh"
. "{REPO}/build/configs.sh"
WK_TARGET_KIND={kind}
config_load {config} {os_name} {kind}
echo "BUILDSYS=$CFG_BUILDSYS"
echo "VARIANT=$CFG_VARIANT"
echo "PGO=$CFG_PGO"
echo "ARGS=$CFG_ARGS"
echo "DIR=$(config_build_dir /src/WebKit)"
''')
    return cp


def pgo_dry_run(tmp):
    """build-in-target.sh's own dry run, which is the only place the three
    phases' command lines exist."""
    src = tmp / "src"
    src.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({
        "WK_DRY_RUN": "1",
        "WK_SRC": str(src),
        "WK_BUILDSYS": "xcode",
        "WK_JOBS": "8",
        "WK_BUILD_SCRIPT": "Tools/Scripts/build-webkit",
        "WK_BUILD_ARGS": "--release",
        "WEBKIT_OUTPUTDIR": str(src / "WebKitBuild" / "Release-pgo"),
        "WK_DERIVED_DATA": str(src / "WebKitBuild" / "DerivedData"),
        "WK_PGO": "1",
        "WK_PGO_DIR": str(src / "WebKitBuild" / "Release-pgo-profile"),
        "WK_NO_COMPILE_COMMANDS": "1",
    })
    return subprocess.run(["bash", str(REPO / "build" / "build-in-target.sh")],
                          capture_output=True, text=True, env=env, timeout=60)


class TestTheConfig(WkTest):
    def test_it_is_listed_so_a_reader_can_find_it(self):
        cp = run("build", "--list")
        self.assertIn(CONFIG, cp.stdout + cp.stderr)

    def test_it_is_an_xcode_config_that_asks_for_a_profile(self):
        f = dict(l.split("=", 1) for l in config_fields(CONFIG, "macos").stdout.strip().splitlines())
        self.assertEqual(f["BUILDSYS"], "xcode")
        self.assertEqual(f["PGO"], "1")
        self.assertEqual(f["ARGS"], "--release")

    def test_its_products_never_share_a_directory_with_the_plain_release(self):
        """A profile-guided build and an ordinary one are different binaries;
        one tree holding both is a number nobody can attribute."""
        pgo = dict(l.split("=", 1) for l in config_fields(CONFIG, "macos").stdout.strip().splitlines())
        plain = dict(l.split("=", 1) for l in config_fields("mac-release", "macos").stdout.strip().splitlines())
        asan = dict(l.split("=", 1) for l in config_fields("mac-release-asan", "macos").stdout.strip().splitlines())
        self.assertNotEqual(pgo["DIR"], plain["DIR"])
        self.assertNotEqual(pgo["DIR"], asan["DIR"])
        self.assertTrue(pgo["DIR"].endswith("Release-pgo"), pgo["DIR"])

    def test_a_linux_workspace_is_told_it_cannot_build_it(self):
        cp = config_fields(CONFIG, "linux")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("Xcode", cp.stdout + cp.stderr)


class TestTheThreePhases(WkTest):
    """Every phase of the dry run, which is the same command a real build runs
    with the exec removed."""

    def setUp(self):
        self._scratch = scratch_dir()
        self.tmp = self._scratch.__enter__()
        cp = pgo_dry_run(self.tmp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.out = cp.stdout + cp.stderr
        self.lines = [l for l in self.out.splitlines() if l.strip()]

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def _line(self, needle):
        hits = [l for l in self.lines if needle in l]
        self.assertEqual(len(hits), 1, f"{needle!r} in {self.lines}")
        return hits[0]

    def test_the_instrumented_phase_generates_profiles_under_thin_lto(self):
        line = self._line("ENABLE_LLVM_PROFILE_GENERATION=YES")
        self.assertIn("--lto-mode=thin", line)
        # $(inherited) is not optional: OTHER_LDFLAGS replaces every framework's
        # own link flags without it.
        self.assertIn("OTHER_LDFLAGS=$(inherited) -fprofile-generate", line)

    def test_the_instrumented_phase_builds_somewhere_else_entirely(self):
        """Not a wipe between phases: two directories, so a killed build
        re-runs rather than rebuilding what it had."""
        instr = self._line("ENABLE_LLVM_PROFILE_GENERATION=YES")
        final = self._line("WK_ENABLE_PGO_USE=YES")
        self.assertIn("Release-pgo-instr", instr)
        self.assertNotIn("Release-pgo-instr", final)
        for line in (instr, final):
            products = [w for w in line.split() if w.startswith("WK_CONFIGURATION_BUILD_DIR=")]
            self.assertEqual(len(products), 1, line)
            self.assertIn(f"WEBKIT_OUTPUTDIR={products[0].split('=', 1)[1]}", line)

    def test_the_collection_drives_the_three_benchmarks_the_weights_name(self):
        line = self._line("collect-pgo-profiles")
        for benchmark in ("speedometer3", "jetstream3", "motionmark"):
            self.assertIn(benchmark, line)
        self.assertIn("--browser minibrowser", line)
        # Against the instrumented build, never the measured one.
        self.assertIn("--build-directory", line)
        self.assertIn("Release-pgo-instr", line)
        # And through the harness that knows where the profiles land.
        self.assertIn("pgo-run-benchmark.py", line)

    def test_the_collection_starts_from_an_empty_profile_directory(self):
        """collect-pgo-profiles refuses one that is not empty, so a re-run
        after a failure has to clear it rather than stop."""
        self.assertIn("rm -rf", self._line("collect-pgo-profiles"))

    def test_the_measured_phase_uses_the_profile_with_full_lto_and_symbols(self):
        line = self._line("WK_ENABLE_PGO_USE=YES")
        self.assertIn("--lto-mode=full", line)
        self.assertIn("WK_DEFAULT_GCC_OPTIMIZATION_LEVEL=3", line)
        self.assertIn("Release-pgo-profile", line)
        # dSYMs, or a samply capture cannot be symbolicated off this machine.
        self.assertIn("DEBUG_INFORMATION_FORMAT=dwarf-with-dsym", line)
        self.assertIn("OTHER_CFLAGS=$(inherited) -fno-omit-frame-pointer", line)
        # The lower libraries copy the profile in from a sandboxed script phase.
        self.assertIn("ENABLE_USER_SCRIPT_SANDBOXING=NO", line)

    def test_the_phases_run_in_the_one_order_that_makes_sense(self):
        order = [i for i, l in enumerate(self.lines)
                 if "ENABLE_LLVM_PROFILE_GENERATION=YES" in l
                 or "collect-pgo-profiles" in l
                 or "WK_ENABLE_PGO_USE=YES" in l]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(order), 3)


class TestItRefusesAThrottledCollection(WkTest):
    """Measured on a Tart guest 2026-09-06: Setup Assistant is frontmost on
    every boot, killing it takes the console session with it, and the guest's
    /usr/bin/python3 has no pyobjc so no raiser can displace it. A collection
    there would look exactly like a good one."""

    def test_it_names_every_reason_rather_than_the_first(self):
        text = (REPO / "build" / "mac-pgo.sh").read_text()
        body = text[text.index("_pgo_screen_faults"):]
        for reason in ("has nowhere to draw", "no raiser", "no main screen",
                       "nothing this build put there"):
            self.assertIn(reason, body)

    def test_a_fault_stops_the_collection(self):
        text = (REPO / "build" / "mac-pgo.sh").read_text()
        self.assertIn("cannot present an unthrottled browser", text)
        self.assertRegex(text, r'faults=\$\(_pgo_screen_faults')

    def test_what_is_on_the_screen_is_asked_of_the_one_prober(self):
        """screen_blocker (lib/quiet.sh) is the repo's one answer to 'what is
        in front'; a second reading of the window server could disagree."""
        text = (REPO / "build" / "mac-pgo.sh").read_text()
        self.assertIn("screen_blocker", text)
        self.assertNotIn("wk_window_probe", text)


class TestItIsThePolicyAndNotAnOption(WkTest):
    """Every macOS number this repo quotes comes from a profile-guided build,
    so the lanes default to it rather than offering it."""

    def test_both_mac_lanes_default_to_it(self):
        for rel in ("bench/mac-lane.sh", "bench/mac-ab.sh"):
            text = (REPO / rel).read_text()
            line = [l for l in text.splitlines() if l.startswith("CONFIG=")]
            self.assertEqual(len(line), 1, f"{rel}: {line}")
            self.assertIn(CONFIG, line[0], rel)

    def test_the_collection_weights_are_webkits_own(self):
        """0.6 / 0.2 / 0.2 lives in Tools/Scripts/pgo-profile; naming the three
        benchmarks is all this repo may decide, and it names them once."""
        shared = (REPO / "build" / "pgo.sh").read_text()
        self.assertNotIn("0.6", shared)
        self.assertIn("PGO_BENCHMARKS=", shared)
        lane = (REPO / "build" / "mac-pgo.sh").read_text()
        self.assertNotIn("PGO_BENCHMARKS=", lane)
        self.assertIn("/pgo.sh", lane)

    def test_the_two_lanes_take_the_list_from_the_one_place(self):
        for rel in ("build/mac-pgo.sh", "image/pgo.sh"):
            text = (REPO / rel).read_text()
            self.assertIn("$PGO_BENCHMARKS", text, rel)
            self.assertNotIn("speedometer3 jetstream3", text, rel)


class TestTheProfileReachesTheMachineThatRunsIt(WkTest):
    """A dSYM is a product on this lane: the machine that profiles is the
    benchmark install, which never had the build tree."""

    def test_the_stage_no_longer_drops_them(self):
        text = (REPO / "cmd" / "bench").read_text()
        self.assertNotIn("--exclude '*.dSYM'", text)

    def test_only_the_perf_build_makes_any(self):
        self.assertIn("DEBUG_INFORMATION_FORMAT=dwarf-with-dsym",
                      (REPO / "build" / "mac-pgo.sh").read_text())


class TestTheHarnessWrapper(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.tmp = self._scratch.__enter__()
        cp = pgo_dry_run(self.tmp)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.lines = [l for l in (cp.stdout + cp.stderr).splitlines() if l.strip()]

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def _line(self, needle):
        hits = [l for l in self.lines if needle in l]
        self.assertEqual(len(hits), 1, f"{needle!r} in {self.lines}")
        return hits[0]

    def test_it_refuses_without_being_told_where_the_checkout_is(self):
        cp = subprocess.run(["python3", str(REPO / "build" / "pgo-run-benchmark.py")],
                            capture_output=True, text=True, timeout=30,
                            env={k: v for k, v in os.environ.items()
                                 if k != "WK_WEBKIT_SCRIPTS"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("WK_WEBKIT_SCRIPTS", cp.stdout + cp.stderr)

    def test_the_collection_goes_through_it_and_not_through_run_benchmark(self):
        """Without it the MiniBrowser driver names no profile directory and the
        first iteration raises. What it hands over, and to which class, is
        tests/test_pgo_harness.py against a stubbed checkout."""
        line = self._line("collect-pgo-profiles")
        self.assertIn("--run-benchmark-harness", line)
        self.assertIn("pgo-run-benchmark.py", line)


if __name__ == "__main__":
    unittest.main()
