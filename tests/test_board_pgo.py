"""The boards' profile-guided build: the two cross configs (build/configs.sh),
the three-phase driver (image/pgo.sh), the collection run (cmd/pi's --pgo) and
the mixing (lib/wkpgo.py), which is WebKit's own `Tools/Scripts/pgo-profile`
told that a GLib port carries one library where the Apple ports carry three.

Nothing here builds, boots or reaches a board: the configs and the driver are
driven as shell functions, and the mixer against a stubbed checkout in the
tests/test_pgo_harness.py idiom -- a `pgo-profile` of exactly upstream's shape,
so this fails if the parts we reach into move.

Run: python3 -m unittest tests.test_board_pgo -v
"""
import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import types
import unittest

from tests.support import REPO, WkTest, bash, func_body, run, scratch_dir

WKPGO = REPO / "lib" / "wkpgo.py"


def cross(config, profile=""):
    return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/build/configs.sh"
config_cross_load {config} "{profile}" || {{ echo "REFUSED"; exit 3; }}
echo "PGO=$XCFG_PGO"
echo "CC=$XCFG_CC"
echo "CMAKE=$XCFG_CMAKE"
''')


def cross_fields(config, profile=""):
    cp = cross(config, profile)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return dict(line.split("=", 1) for line in cp.stdout.strip().splitlines())


class TestTheCrossConfigs(WkTest):
    def test_they_are_listed_where_the_other_configs_are(self):
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/build/configs.sh"; config_cross_list')
        for name in ("wpe-cross", "wpe-cross-pgo-collect", "wpe-cross-pgo-use"):
            self.assertIn(name, cp.stdout, cp.stdout)

    def test_an_unknown_one_is_refused(self):
        self.assertEqual(cross("wpe-cross-pgo").returncode, 3)

    def test_the_plain_one_adds_nothing(self):
        got = cross_fields("wpe-cross")
        self.assertEqual(got["PGO"], "")
        self.assertEqual(got["CMAKE"], "")
        self.assertEqual(got["CC"], "")

    def test_the_collection_config_instruments_with_clang(self):
        got = cross_fields("wpe-cross-pgo-collect")
        self.assertEqual(got["PGO"], "collect")
        self.assertEqual(got["CC"], "clang")
        self.assertIn("-DENABLE_LLVM_PROFILE_GENERATION=ON", got["CMAKE"])
        self.assertIn("-DPGO_PROFILE_DIR=", got["CMAKE"])

    def test_the_measured_config_needs_a_profile(self):
        cp = cross("wpe-cross-pgo-use")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("PGO_PROFILE_PATH", cp.stdout + cp.stderr)

    def test_the_measured_config_builds_against_the_one_it_is_given(self):
        got = cross_fields("wpe-cross-pgo-use", "/src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata")
        self.assertEqual(got["PGO"], "use")
        self.assertEqual(got["CC"], "clang")
        self.assertIn("-DUSE_PGO_PROFILE=ON", got["CMAKE"])
        self.assertIn("-DPGO_PROFILE_PATH=/src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata",
                      got["CMAKE"])

    def test_each_pgo_config_states_both_options(self):
        """The two share one cross build directory, and cmake refuses the pair
        (WEBKIT_OPTION_CONFLICT) -- so a config that named only its own would
        be built with whatever the other left in the cache."""
        for config, profile in (("wpe-cross-pgo-collect", ""),
                                ("wpe-cross-pgo-use", "/x.profdata")):
            flags = cross_fields(config, profile)["CMAKE"]
            self.assertIn("ENABLE_LLVM_PROFILE_GENERATION=", flags, config)
            self.assertIn("USE_PGO_PROFILE=", flags, config)

    def test_the_board_directory_is_named_once(self):
        """build/pgo.sh is the one place, because the config bakes it in and
        cmd/pi points LLVM_PROFILE_FILE at it."""
        shared = (REPO / "build" / "pgo.sh").read_text()
        self.assertIn("PGO_BOARD_DIR=", shared)
        for rel in ("build/configs.sh", "cmd/pi"):
            text = (REPO / rel).read_text()
            self.assertIn("PGO_BOARD", text, rel)
            self.assertNotIn("/var/wk/pgo", text, rel)


def wanted(profile):
    return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/image/pgo.sh"
image_profile_load {profile} >/dev/null 2>&1 || {{ echo "no such profile"; exit 9; }}
image_pgo_wanted && echo yes || echo no
''')


class TestWhichProfilesAreProfileGuided(WkTest):
    """2.52 is where upstream's cmake support arrives; before it there is
    nothing to turn on, and from it on there is no plain build to fall back to."""

    def test_every_2_52_yocto_profile_is(self):
        seen = 0
        for conf in sorted((REPO / "image" / "configs").glob("webkit-2.52-yocto-*.conf")):
            cp = wanted(conf.stem)
            self.assertEqual(cp.stdout.strip(), "yes", conf.stem + cp.stderr)
            seen += 1
        self.assertGreater(seen, 0)

    def test_no_earlier_release_is(self):
        seen = 0
        for conf in sorted((REPO / "image" / "configs").glob("wpewebkit-2.*-yocto-*.conf")):
            cp = wanted(conf.stem)
            self.assertEqual(cp.stdout.strip(), "no", conf.stem + cp.stderr)
            seen += 1
        self.assertGreater(seen, 0)

    def test_a_2_52_buildroot_profile_is_not_yet(self):
        """buildroot builds WebKit its own way (image/buildroot-webkit.sh) and
        no lane collects for it; docs/HANDOFF-board-pgo.md owns that."""
        self.assertEqual(wanted("webkit-2.52-buildroot-rpi5-64").stdout.strip(), "no")


class TestTheBoardIsReadOffTheFleet(WkTest):
    def _machine(self, profile):
        return bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/image/pgo.sh"
image_pgo_machine {profile} || echo NONE
''')

    def test_the_rpi5_profile_collects_on_the_rpi5(self):
        self.assertEqual(self._machine("webkit-2.52-yocto-rpi5-64").stdout.strip(), "rpi5")

    def test_a_profile_no_board_declares_has_none(self):
        self.assertEqual(self._machine("webkit-2.52-yocto-rpi4-32").stdout.strip(), "NONE")

    def test_a_profile_with_no_board_refuses_rather_than_building_plain(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/image/pgo.sh"
image_profile_load webkit-2.52-yocto-rpi4-32 >/dev/null 2>&1
_pgo_require_board webkit-2.52-yocto-rpi4-32
''')
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("NODE_PROFILE", cp.stdout + cp.stderr)


class TestThePhasesAndTheirOrder(WkTest):
    def _plan(self):
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/image/pgo.sh"
image_profile_load webkit-2.52-yocto-rpi5-64 >/dev/null 2>&1
image_pgo_plan webkit-2.52-yocto-rpi5-64 {"a" * 40} pr rpi5
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip().splitlines()

    def test_it_instruments_then_collects_then_rebuilds(self):
        lines = self._plan()
        kinds = [l.split()[1] for l in lines]
        self.assertEqual(kinds[0], "sysimage")
        self.assertIn("--slot pr-instr   (instrumented)", lines[0])
        self.assertIn("pi deploy", lines[1])
        self.assertIn("pgo-mix", lines[-2])
        self.assertIn("--slot pr   (against the mixed profile)", lines[-1])

    def test_it_collects_every_benchmark_the_weights_name(self):
        collected = [l.split()[4] for l in self._plan() if "--pgo " in l]
        self.assertEqual(collected, ["speedometer3", "jetstream3", "motionmark"])

    def test_the_collection_lands_in_the_builders_own_bind_mount(self):
        """The host writes it and the cross toolchain reads it back, so it is
        one directory seen from two sides and never a copy."""
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
image_pgo_dir_in pr
''')
        self.assertEqual(cp.stdout.strip(), "/src/WebKit/WebKitBuild/wk-pgo/pr")
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/image.sh"
image_pgo_dir webkit-2.52-yocto-rpi5-64 pr
''')
        self.assertTrue(cp.stdout.strip().endswith(
            "ws/yocto-webkit-2.52-yocto-rpi5-64/build/wk-pgo/pr"), cp.stdout)


# A `pgo-profile` of exactly upstream's shape: the two names lib/wkpgo.py reaches
# for, and a combine() that records what it was asked to mix, at what weights.
UPSTREAM = '''
import os
PROFILED_DYLIBS = ["JavaScriptCore", "WebCore", "WebKit"]
BENCHMARK_GROUP_WEIGHTS = [("speedometer3", 0.6), ("jetstream3", 0.2), ("motionmark", 0.2)]

def combine(args):
    import json
    rows = vars(args)
    out = rows["output"]
    for lib in PROFILED_DYLIBS:
        with open(os.path.join(out, lib + ".profdata"), "w") as f:
            f.write("combined")
    with open(os.path.join(out, "combine.json"), "w") as f:
        json.dump({"libs": PROFILED_DYLIBS,
                   "groups": {g: rows.get(g) for g, _ in BENCHMARK_GROUP_WEIGHTS}}, f)
'''

# Upstream's shape, xcrun and all: on a Linux host `subprocess.run` of a program
# that does not exist raises, so a mixer that did not blunt this would die before
# it merged anything.
UTILS = '''
import glob, os, subprocess

def locate_binary_xcrun(sdk, binary_name):
    return subprocess.run(["/usr/bin/xcrun", "-sdk", sdk, "--find", binary_name],
                          check=False, text=True, capture_output=True).stdout.strip()

class LLVMProfDataExecutable:
    @classmethod
    def detect_binaries(cls):
        found = ["/stub/llvm-profdata"]
        for sdk in ("macosx", "iphoneos"):
            path = locate_binary_xcrun(sdk, "llvm-profdata")
            if path:
                found.append(path)
        return found

    @classmethod
    def run(cls, command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0,
            stdout="Total functions: 40000\\nMaximum function count: 900000\\n", stderr="")

def merge_raw_profiles_in_directory_by_prefixes(prefixes, input_directory,
                                                output_directory=None, **kwargs):
    out = []
    for prefix in prefixes:
        raw = sorted(glob.glob(os.path.join(input_directory, prefix + "*.profraw")))
        assert raw, "merge called with nothing to merge"
        path = os.path.join(output_directory or input_directory, prefix + ".profdata")
        with open(path, "w") as f:
            f.write("merged %d" % len(raw))
        out.append(path)
    return out
'''


def stub_scripts(root):
    scripts = root / "Tools" / "Scripts"
    (scripts / "webkitpy").mkdir(parents=True)
    (scripts / "webkitpy" / "__init__.py").write_text("")
    (scripts / "webkitpy" / "llvm_profile_utils.py").write_text(textwrap.dedent(UTILS))
    (scripts / "pgo-profile").write_text(textwrap.dedent(UPSTREAM))
    return scripts


def collection(root, plans=("speedometer3", "jetstream3", "motionmark"), lib="WPEWebKit"):
    for plan in plans:
        raw = root / plan / "diagnose"
        raw.mkdir(parents=True)
        for pid in (101, 202):
            (raw / f"{lib}_{pid}.profraw").write_bytes(b"x")
    return root


def wkpgo(*args):
    return subprocess.run(["python3", str(WKPGO), *args], capture_output=True, text=True, timeout=120)


class TestTheMixingIsUpstreams(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.tmp = self._scratch.__enter__()
        self.scripts = stub_scripts(self.tmp)
        self.dir = collection(self.tmp / "pgo")

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_the_weights_come_from_the_checkout_and_not_from_here(self):
        cp = wkpgo("plans", "--scripts", str(self.scripts))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(),
                         ["speedometer3", "0.6", "jetstream3", "0.2", "motionmark", "0.2"])

    def test_a_glib_collection_mixes_into_one_library(self):
        cp = wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), str(self.dir / "output" / "WPEWebKit.profdata"))
        recorded = json.loads((self.dir / "output" / "combine.json").read_text())
        self.assertEqual(recorded["libs"], ["WPEWebKit"])
        self.assertEqual(recorded["groups"],
                         {plan: str(self.dir / plan)
                          for plan in ("speedometer3", "jetstream3", "motionmark")})

    def test_each_leg_is_merged_where_the_reader_looks_for_it(self):
        wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir), "--lib", "WPEWebKit")
        for plan in ("speedometer3", "jetstream3", "motionmark"):
            merged = self.dir / plan / "WPEWebKit.profdata"
            self.assertTrue(merged.exists(), merged)
            self.assertEqual(merged.read_text(), "merged 2")

    def test_a_leg_that_wrote_no_profile_is_refused(self):
        for stale in (self.dir / "motionmark" / "diagnose").glob("*.profraw"):
            os.remove(stale)
        cp = wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("motionmark", cp.stdout + cp.stderr)

    def test_a_benchmark_upstream_does_not_weigh_is_refused(self):
        """Naming the three is all this repo may decide, and a list that has
        drifted from the one upstream weighs has no ratio to be mixed at."""
        cp = wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit", "--plan", "speedometer2")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("carries no weight", cp.stdout + cp.stderr)

    def test_upstreams_xcrun_search_does_not_kill_a_linux_mix(self):
        """webkitpy's llvm-profdata finder runs /usr/bin/xcrun for each SDK, and
        off macOS that is not a program: subprocess.run raises rather than
        returning non-zero. The stub keeps that shape, so this fails if the
        blunting goes."""
        if os.path.exists("/usr/bin/xcrun"):
            self.skipTest("this host has xcrun, so the raise cannot happen here")
        cp = wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("FileNotFoundError", cp.stderr)

    def test_it_refuses_a_checkout_that_has_no_pgo_profile(self):
        os.remove(self.scripts / "pgo-profile")
        cp = wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("Tools/Scripts", cp.stdout + cp.stderr)


class TestTheGateReadsBothLayouts(WkTest):
    """One reader for the Apple lane's three frameworks and the boards' one
    library; a board profile has no compressed copy and is not asked for one."""

    def setUp(self):
        self._scratch = scratch_dir()
        self.tmp = self._scratch.__enter__()
        self.scripts = stub_scripts(self.tmp)
        self.dir = collection(self.tmp / "pgo")
        wkpgo("mix", "--scripts", str(self.scripts), "--dir", str(self.dir), "--lib", "WPEWebKit")

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def _check(self, *extra):
        return wkpgo("check", "--scripts", str(self.scripts), "--dir", str(self.dir),
                     "--lib", "WPEWebKit", *extra)

    def test_a_whole_board_collection_passes(self):
        cp = self._check()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WPEWebKit: functions=40000", cp.stdout)

    def test_it_does_not_ask_a_board_profile_for_a_compressed_copy(self):
        self.assertNotIn("compressed", self._check().stdout)

    def test_a_missing_leg_is_named(self):
        os.remove(self.dir / "jetstream3" / "WPEWebKit.profdata")
        cp = self._check()
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("jetstream3/WPEWebKit.profdata", cp.stdout + cp.stderr)

    def test_a_reading_can_be_reported_again_with_no_checkout(self):
        out = self.tmp / "reading.json"
        self._check("--json", str(out))
        cp = wkpgo("check", "--read", str(out))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WPEWebKit", cp.stdout)


class TestTheCollectionRunIsNotAMeasurement(WkTest):
    def test_pi_bench_refuses_pgo_together_with_an_ab(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn('[ -z "$ab$ab_systems$task" ]', body)

    def test_a_collection_run_creates_no_bench_task(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn('[ -n "$PI_PGO_DIR" ] || log "  task', body)

    def test_it_asks_for_one_iteration_the_way_upstream_does(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertRegex(body, r"count=1\s+#")

    def test_the_launch_points_the_runtime_at_the_boards_directory(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn("LLVM_PROFILE_FILE=$PGO_BOARD_FILE", body)

    def test_one_file_per_process_so_the_browser_and_the_web_process_are_peers(self):
        self.assertIn("_%p.profraw", (REPO / "build" / "pgo.sh").read_text())


class TestTheDriverPullsWhatTheBoardWrote(WkTest):
    def setUp(self):
        for name in ("webkitpy", "webkitpy.benchmark_runner",
                     "webkitpy.benchmark_runner.browser_driver"):
            sys.modules.setdefault(name, types.ModuleType(name))
        driver = types.ModuleType("webkitpy.benchmark_runner.browser_driver.browser_driver")
        driver.BrowserDriver = type("BrowserDriver", (), {"__init__": lambda self, a=None: None})
        sys.modules["webkitpy.benchmark_runner.browser_driver.browser_driver"] = driver
        spec = importlib.util.spec_from_file_location(
            "wk_board_driver_stub", REPO / "bench" / "wk_board_driver.py")
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_it_implements_the_two_hooks_upstream_calls(self):
        for hook in ("prepare_pgo_profile_collection", "collect_pgo_profile"):
            self.assertTrue(callable(getattr(self.module.WkBoardDriver, hook)), hook)

    def test_a_run_that_wrote_nothing_is_an_error_and_not_an_empty_profile(self):
        driver = self.module.WkBoardDriver.__new__(self.module.WkBoardDriver)
        driver._ssh = ["true"]
        with scratch_dir() as tmp:
            os.environ["WK_BOARD_PGO"] = "/var/wk/pgo"
            try:
                with self.assertRaises(RuntimeError):
                    driver.collect_pgo_profile(str(tmp / "dest"))
            finally:
                os.environ.pop("WK_BOARD_PGO", None)


class TestAnInstrumentedSlotIsNeverMeasured(WkTest):
    """It writes a profile as every process exits and runs several times
    slower for it, so a number from one is not this engine's."""

    def test_pi_bench_reads_the_slots_own_manifest(self):
        body = (REPO / "cmd" / "pi").read_text()
        self.assertIn("wkslot get \"$PI_TMP/slot-$1.json\" build_config", body)
        self.assertIn("pi_check_instrumented", body)

    def test_the_builder_records_which_config_built_a_slot(self):
        self.assertIn('build_config="${CROSS_CONFIG:-wpe-cross}"',
                      (REPO / "image" / "yocto-build.sh").read_text())

    def test_the_check_runs_on_every_slot_a_leg_will_use(self):
        body = func_body((REPO / "cmd" / "pi").read_text(), "pi_leg_prepare")
        self.assertIn("pi_check_instrumented", body)


class TestTheUnprofiledSlotIsDeliberate(WkTest):
    """A 2.52 slot has a profile by default and there is no --no-pgo. The one
    way to an unprofiled one is naming the plain cross config, because the
    only reason to want one is to measure it against a profiled one."""

    def _webkit(self, *args):
        return run("sysimage", "webkit", "webkit-2.52-yocto-rpi5-64",
                   "--commit", "a" * 40, *args, timeout=240)

    def test_only_the_plain_cross_config_is_accepted(self):
        cp = self._webkit("--slot", "plain", "--config", "wpe-cross-pgo-collect")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("takes 'wpe-cross' here", cp.stdout)

    def test_it_builds_the_slot_with_no_profile_and_says_so(self):
        cp = self._webkit("--slot", "plain", "--config", "wpe-cross", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("WITHOUT a profile", cp.stdout)
        self.assertIn("config      wpe-cross", cp.stdout)

    def test_the_default_is_still_the_three_phases(self):
        cp = self._webkit("--slot", "pgo", "--dry-run")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("profile-guided build", cp.stdout)
        self.assertIn("--slot pgo-instr", cp.stdout)

    def test_the_slot_records_which_of_the_two_it_is(self):
        """`wk pi bench` and `wk sysimage ls` read build_config off the
        manifest, so an A/B of the two arms can say which was which."""
        self.assertIn('build_config="${CROSS_CONFIG:-wpe-cross}"',
                      (REPO / "image" / "yocto-build.sh").read_text())
        self.assertIn('build_config="$(wkslot get "$json" build_config)"',
                      (REPO / "cmd" / "pi").read_text())


STATUS_STUBS = """
. {repo}/lib/common.sh
. {repo}/lib/store.sh
. {repo}/lib/target.sh
wk_ws_dir() {{ printf '%s/ws/%s' "{tmp}" "$1"; }}
ws_busy_reason() {{ return 1; }}
note() {{ printf 'NOTE %s\n' "$*"; }}
note_warn() {{ printf 'WARN %s\n' "$*"; }}
bump() {{ :; }}
sub_add() {{ :; }}
_jesc() {{ printf '%s' "$1"; }}
"""


def report_image_stage_body():
    """The function lifted out of cmd/status, so the test drives the real one."""
    text = (REPO / "cmd" / "status").read_text()
    start = text.index("report_image_stage() {")
    return text[start:text.index("\nreport_one()", start)]


class TestTheCycleSaysWhatItIsDoing(WkTest):
    """`wk status` reported build=none through a whole image build and a whole
    profile-guided cycle: it read only the build.status that `wk build`
    writes. Two of the three phases run on the board, so no workspace pid is
    alive through them -- the driver's own pid is what is live for the cycle,
    and that is what liveness is read from."""

    def _report(self, pid):
        with scratch_dir() as tmp:
            ws = tmp / "ws" / "yocto-p"
            ws.mkdir(parents=True)
            (ws / "pgo.status").write_text(
                "slot=pgo\nphase=2/3 collecting jetstream3 on rpi5\nprofile=p\n"
                "pid=%s\n" % pid)
            script = (STATUS_STUBS.format(repo=REPO, tmp=tmp)
                      + report_image_stage_body()
                      + "\nreport_image_stage yocto-p\n")
            cp = bash(script)
            return cp.stdout + cp.stderr

    def test_a_live_cycle_reports_its_phase(self):
        out = self._report(os.getpid())
        self.assertIn("2/3 collecting jetstream3 on rpi5", out)
        self.assertNotIn("WARN", out)

    def test_a_cycle_whose_driver_died_is_reported_as_stopped(self):
        out = self._report(999999999)
        self.assertIn("WARN", out)
        self.assertIn("stopped at", out)

    def test_the_driver_records_each_phase_with_its_own_pid(self):
        text = (REPO / "image" / "pgo.sh").read_text()
        self.assertIn('"pid=$$"', text)
        for phase in ("1/3 instrumented build", "2/3 collecting", "2/3 mixing",
                      "3/3 measured build"):
            self.assertIn(phase, text, phase)

    def test_the_record_is_removed_when_the_cycle_ends(self):
        text = (REPO / "image" / "pgo.sh").read_text()
        self.assertIn("wk_atexit _pgo_status_clear", text)
        self.assertIn("_pgo_status_clear()", text)

    def test_status_reads_liveness_from_the_pid_and_labels_from_the_record(self):
        fn = report_image_stage_body()
        self.assertIn("ws_busy_reason", fn)
        self.assertIn('kill -0 "$cyclepid"', fn)
        self.assertIn("yocto.status", fn)
        self.assertIn("pgo.status", fn)

    def test_a_workspace_row_asks_for_it(self):
        self.assertIn('report_image_stage "$ws"', (REPO / "cmd" / "status").read_text())


class TestTheMixRunsWhereTheProfileCanBeRead(WkTest):
    """A .profraw is readable only by the toolchain that wrote it, and that
    clang is the Yocto SDK's rather than the workstation's or the container's."""

    def test_the_stage_goes_through_the_cross_environment(self):
        text = (REPO / "image" / "yocto-build.sh").read_text()
        stage = text[text.index("    pgo-mix)"):text.index("    *)  fail \"unknown stage")]
        self.assertIn("--cross-toolchain-run-cmd", stage)
        self.assertIn("wkpgo.py mix", stage)
        self.assertIn("wkpgo.py check", stage)

    def test_it_refuses_without_a_collection_to_mix(self):
        text = (REPO / "image" / "yocto-build.sh").read_text()
        stage = text[text.index("    pgo-mix)"):text.index("    *)  fail \"unknown stage")]
        self.assertIn("--pgo-dir and --pgo-lib", stage)
        self.assertIn("no collection at", stage)

    def test_the_stage_is_one_the_driver_knows(self):
        self.assertIn("pgo-mix", (REPO / "image" / "yocto.sh").read_text())


if __name__ == "__main__":
    unittest.main()
