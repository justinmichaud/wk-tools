"""The boards' profile-guided build: the two cross configs (build/configs.sh),
the phases (image/pgo.sh), the collection run (cmd/pi's --pgo) and
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

from tests.support import REPO, WkTest, bash, func_body, requires_podman_vm, run, run_here, scratch_dir

WKPGO = REPO / "lib" / "wkpgo.py"

PGO_LIBS = "\n".join('. "%s/%s"' % (REPO, f) for f in (
    "lib/common.sh", "lib/store.sh", "lib/target.sh", "lib/image.sh",
    "image/profiles.sh", "boot/machines.sh", "lib/bench.sh", "image/pgo.sh"))

PROFILE = "webkit-2.52-yocto-rpi5-64"
LANE = "yocto-" + PROFILE
# The machine the lane is on, which the steps are keyed on: stubbed, so this
# asks nothing of the fleet and no workspace has to exist.
ON = "abuilder"
RES = "machine:" + ON


def pgo_steps(slot="pr", machine="rpi5", commit="a" * 40, spec=PROFILE, lane=LANE):
    """The phases image/pgo.sh declares, one record each, as lib/sched.py reads
    them: id, machine, needs, holds, done-predicate, command."""
    with scratch_dir() as tmp:
        cp = bash('%s\nimage_lane_machine() { echo %s; }\n'
                  'sched_begin %s/steps\nimage_pgo_steps %s %s %s %s %s\ncat %s/steps\n'
                  % (PGO_LIBS, ON, tmp, spec, lane, commit, slot, machine, tmp))
        assert cp.returncode == 0, cp.stdout + cp.stderr
        rows = [line.split("\t") for line in cp.stdout.splitlines() if line.strip()]
    return {r[0]: {"machine": r[1], "needs": r[2], "holds": r[3],
                   "done": r[4], "command": r[5]} for r in rows}


def steps_holds(steps, id):
    return steps[id]["holds"]


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
        no lane collects for it; owed (docs/PLAN.md)."""
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
    """Each phase is one `wk` command and one step of the graph: what it needs,
    what it holds while it runs, and how to ask whether it is already done."""

    def test_it_instruments_then_collects_then_rebuilds(self):
        steps = pgo_steps()
        instr = steps["instr:%s:pr" % LANE]
        self.assertIn("--slot pr-instr --config wpe-cross-pgo-collect", instr["command"])
        self.assertEqual(instr["holds"], RES)
        self.assertEqual(steps["deploy:rpi5:pr-instr"]["needs"], "instr:%s:pr" % LANE)
        mix = steps["mix:%s:pr" % LANE]
        self.assertIn("--stage pgo-mix --slot pr", mix["command"])
        measured = steps["slot:%s:pr" % LANE]
        self.assertIn("--slot pr --config wpe-cross-pgo-use", measured["command"])
        self.assertEqual(measured["needs"], "mix:%s:pr" % LANE)

    def test_every_phase_runs_on_the_machine_holding_the_lane(self):
        """Including the collection: the board writes its profiles back into
        that lane's build directory, which only that machine can reach."""
        for step in pgo_steps().values():
            self.assertEqual(step["machine"], ON, step["command"])

    def test_every_command_names_the_lane_it_acts_in(self):
        """Two lanes of one profile -- one per arm, or one per machine -- are
        told apart by nothing else once the command has been routed."""
        for step in pgo_steps().values():
            self.assertIn("--workspace %s" % LANE, step["command"], step["command"])

    def test_a_build_is_held_by_its_machine_and_not_by_its_lane(self):
        """One machine builds one thing at a time, whichever lane it is for;
        two machines build at once."""
        self.assertEqual(steps_holds(pgo_steps(), "instr:%s:pr" % LANE), "machine:" + ON)
        self.assertNotIn(LANE, RES)

    def test_the_board_is_held_for_the_collection_and_the_lane_for_the_builds(self):
        """The whole point of the graph: the builder is not idle through the
        collection, so nothing may take the board or the lane from under one."""
        steps = pgo_steps()
        for phase in ("deploy:rpi5:pr-instr", "collect:rpi5:pr:speedometer3"):
            self.assertEqual(steps[phase]["holds"], "device:rpi5", phase)
        for phase in ("instr:%s:pr" % LANE, "mix:%s:pr" % LANE, "slot:%s:pr" % LANE):
            self.assertEqual(steps[phase]["holds"], RES, phase)

    def test_a_phase_already_done_is_asked_about_by_its_own_evidence(self):
        """The instrumented slot and the measured one each carry the commit and
        the config they were built with, which is what makes the cycle
        re-runnable."""
        steps = pgo_steps()
        # Asked of the machine holding the lane, not of the one that drew the
        # graph: a slot built there reads as missing here (`wk sysimage holds`).
        self.assertEqual(
            steps["instr:%s:pr" % LANE]["done"],
            '[ "$(wk sysimage holds %s --workspace %s --slot pr-instr --commit %s'
            ' --config wpe-cross-pgo-collect)" = yes ]' % (PROFILE, LANE, "a" * 40))
        self.assertEqual(
            steps["slot:%s:pr" % LANE]["done"],
            '[ "$(wk sysimage holds %s --workspace %s --slot pr --commit %s)" = yes ]'
            % (PROFILE, LANE, "a" * 40))

    def test_it_collects_every_benchmark_the_weights_name(self):
        steps = pgo_steps()
        collected = [id.rsplit(":", 1)[1] for id in steps if id.startswith("collect:")]
        self.assertEqual(collected, ["speedometer3", "jetstream3", "motionmark"])
        for id in [i for i in steps if i.startswith("collect:")]:
            self.assertIn("--slot pr-instr --pgo ", steps[id]["command"])

    def test_the_collection_lands_in_the_builders_own_bind_mount(self):
        """The host writes it and the cross toolchain reads it back, so it is
        one directory seen from two sides and never a copy."""
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/image.sh"
image_pgo_dir_in pr
''')
        self.assertEqual(cp.stdout.strip(), "/src/WebKit/WebKitBuild/wk-pgo/pr")
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/image.sh"
image_pgo_dir yocto-webkit-2.52-yocto-rpi5-64 pr
''')
        self.assertTrue(cp.stdout.strip().endswith(
            "ws/yocto-webkit-2.52-yocto-rpi5-64/build/wk-pgo/pr"), cp.stdout)

    def test_the_collection_belongs_to_the_lane_and_not_to_the_profile(self):
        """Two lanes of one profile collect into two directories: a profile
        taken in one arm's lane says nothing about the other's."""
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/lib/image.sh"
image_pgo_dir yocto-webkit-2.52-yocto-rpi5-64-base pr
''')
        self.assertTrue(cp.stdout.strip().endswith(
            "ws/yocto-webkit-2.52-yocto-rpi5-64-base/build/wk-pgo/pr"), cp.stdout)


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


@requires_podman_vm()
class TestTheUnprofiledSlotIsDeliberate(WkTest):
    """A 2.52 slot has a profile by default and there is no --no-pgo. The one
    way to an unprofiled one is naming the plain cross config, because the
    only reason to want one is to measure it against a profiled one."""

    def _webkit(self, *args):
        return run_here("sysimage", "webkit", "webkit-2.52-yocto-rpi5-64",
                        "--commit", "a" * 40, *args, timeout=240)

    def phase(self, slot, config, store):
        """`wk sysimage webkit <profile> --config <c>` with the build stubbed:
        one phase of the cycle, or the unprofiled build."""
        return bash('%s\nyocto_build() { printf \'%%s\\n\' "$*" > %s/called; }\n'
                    'image_profile_load %s >/dev/null 2>&1\n'
                    'image_pgo_webkit %s --commit %s --slot %s --config %s\n'
                    % (PGO_LIBS, store, PROFILE, PROFILE, "a" * 40, slot, config),
                    env={"WK_STORE": str(store)})

    def test_each_config_the_cycle_builds_with_is_taken_and_nothing_else(self):
        with scratch_dir() as tmp:
            for config in ("wpe-cross", "wpe-cross-pgo-collect", "wpe-cross-pgo-use"):
                cp = self.phase("s", config, tmp)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertIn("--config %s" % config, (tmp / "called").read_text())
            cp = self.phase("s", "wpe-cross-pgo", tmp)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("--config takes one of the configs", cp.stdout + cp.stderr)

    def test_the_measured_phase_builds_against_the_profile_the_mix_wrote(self):
        with scratch_dir() as tmp:
            self.phase("pr", "wpe-cross-pgo-use", tmp)
            self.assertIn("--pgo-profile /src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata",
                          (tmp / "called").read_text())

    def test_the_instrumented_phase_clears_the_collection_it_invalidates(self):
        """A collection is every leg of one run of one build: legs taken
        against the last instrumented build say nothing about this one."""
        with scratch_dir() as tmp:
            stale = tmp / "ws" / ("yocto-%s" % PROFILE) / "build" / "wk-pgo" / "pr"
            stale.mkdir(parents=True)
            (stale / "leg.profraw").write_text("old")
            cp = self.phase("pr-instr", "wpe-cross-pgo-collect", tmp)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertFalse(stale.exists(), "the previous collection is still there")

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


class TestTheCycleSaysWhatItIsDoing(WkTest):
    """The cycle declares its three phases through lib/task.sh and steps
    through them, so one renderer says which phase is running, which are done,
    and what stops it. Two of the three phases run on the board and no
    workspace pid is alive through them -- the driver's own pid is what is
    live for the cycle, which is why the record is `here`."""

    def test_the_driver_declares_the_graphs_steps_as_its_plan(self):
        """One record for the cycle, whose plan is the steps the scheduler
        reaches in order; `--on-event` keeps each step's state in the record."""
        cycle = func_body((REPO / "image" / "pgo.sh").read_text(), "image_pgo_slot")
        self.assertIn("image_pgo_graph", cycle)
        self.assertIn("$(sched_steps)", cycle)
        self.assertIn("task_begin pgo here", cycle)
        self.assertIn('sched_run --on-event "task_step_event', cycle)

    def test_the_record_it_writes_is_the_graph_it_then_runs(self):
        """Driven with the scheduler stubbed: one `pgo` record, whose plan is
        the steps in the order they will be reached, and a run told to step the
        record as each one starts."""
        with scratch_dir() as tmp:
            cp = bash('%s\nyocto_log() { echo %s/log; }\n'
                      'image_lane_machine() { echo %s; }\n'
                      'sched_run() { echo "RUN $*"; }\n'
                      'image_profile_load %s >/dev/null 2>&1\n'
                      'image_pgo_slot %s %s %s pr rpi5\n'
                      % (PGO_LIBS, tmp, ON, PROFILE, PROFILE, LANE, "a" * 40),
                      env={"WK_STORE": str(tmp)})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            records = list((tmp / "task").glob("pgo-*"))
            self.assertEqual(len(records), 1, records)
            plan = (records[0] / "plan").read_text().splitlines()
            self.assertEqual(len(plan), 7, plan)
            self.assertIn("--slot pr-instr --config wpe-cross-pgo-collect", plan[0])
            self.assertIn("--slot pr --config wpe-cross-pgo-use", plan[-1])
            self.assertIn("--on-event task_step_event '%s' {step} {event}" % records[0], cp.stdout)

    def test_the_record_names_a_command_a_person_types_to_stop_it(self):
        """Every task record names the command that stops its job, and this
        one's is real: `--stop` is an arm of the same command, through the one
        implementation (job_stop, lib/watchdog.sh)."""
        text = (REPO / "image" / "pgo.sh").read_text()
        self.assertIn('"wk sysimage webkit $spec --workspace $lane --slot $slot --stop"', text)
        self.assertNotIn("kill $$ on", text)
        self.assertIn('job_stop "$lane/$slot" pgo', text)

    def test_stop_with_no_cycle_running_says_so_and_exits_0(self):
        cp = run_here("sysimage", "webkit", "webkit-2.52-yocto-rpi5-64",
                      "--slot", "pgo", "--stop", timeout=240)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no pgo is running", cp.stdout)

    def test_stop_takes_nothing_with_it(self):
        cp = run_here("sysimage", "webkit", "webkit-2.52-yocto-rpi5-64", "--slot", "pgo",
                      "--commit", "a" * 40, "--stop", timeout=240)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("takes nothing with it", cp.stdout)

    def test_the_phase_is_a_step_of_the_declared_plan_and_not_a_second_record(self):
        text = (REPO / "image" / "pgo.sh").read_text()
        self.assertNotIn("status_write", text)
        self.assertNotIn("pgo.status", text)

    def test_the_record_ends_when_the_cycle_ends(self):
        text = (REPO / "image" / "pgo.sh").read_text()
        self.assertIn("wk_atexit _pgo_task_end", text)
        self.assertIn('task_end "$PGO_TASK" "${WK_EXIT_STATUS:-0}"', text)

    def test_status_asks_the_process_table_rather_than_believing_the_record(self):
        """A cycle whose driver is gone reads `died`, computed at read time:
        lib/task.sh holds no verdict and cmd/status stores none."""
        fn = (REPO / "lib" / "wk" / "status.py").read_text()
        self.assertIn('.verdict("capped")', fn)
        self.assertIn('"died"', fn)
        self.assertIn("_task_py verdict", (REPO / "lib" / "task.sh").read_text())
        lib = (REPO / "lib" / "wk" / "record.py").read_text()
        verdict = lib[lib.index("    def verdict("):lib.index("    def running(")]
        self.assertIn("self.alive(", verdict)
        self.assertIn('"died"', verdict)

    def test_a_workspace_walk_asks_for_it_once_per_store(self):
        text = (REPO / "lib" / "wk" / "status.py").read_text()
        self.assertIn("self.tasks(records, name)", text)
        self.assertIn("tasks_said", text)


class TestTheProfileGateStandsBeforeTheMeasuredBuild(WkTest):
    """A collection that died still writes files, so the measured build must
    not be reachable unless the profile was read back and accepted. The Mac
    lane pins the same ordering (tests/test_mac_gates.py)."""

    def test_the_mix_stage_checks_after_it_merges(self):
        text = (REPO / "image" / "yocto-build.sh").read_text()
        stage = text[text.index("    pgo-mix)"):text.index('    *)  fail "unknown stage')]
        self.assertLess(stage.index("wkpgo.py mix"), stage.index("wkpgo.py check"))

    def test_a_failed_check_fails_the_stage(self):
        """run_helper turns a non-zero helper into `fail`, which exits."""
        body = (REPO / "image" / "yocto-build.sh").read_text()
        fn = func_body(body, "run_helper")
        self.assertIn('|| fail "$what failed"', fn)

    def test_the_measured_build_comes_after_the_mix_stage(self):
        """It needs it, so the scheduler cannot reach it until the mix has
        finished -- and a step whose need failed is not run at all
        (tests/test_sched.py)."""
        steps = pgo_steps()
        self.assertEqual(steps["slot:%s:pr" % LANE]["needs"], "mix:%s:pr" % LANE)

    def test_a_failed_stage_stops_the_cycle(self):
        """yocto_build dies on a stage that did not finish, so the mix step
        exits non-zero and the measured build is never started."""
        self.assertIn('die "  full log:', (REPO / "image" / "yocto.sh").read_text())
        cycle = func_body((REPO / "image" / "pgo.sh").read_text(), "image_pgo_slot")
        line = [l.strip() for l in cycle.splitlines() if "sched_run" in l]
        self.assertEqual(len(line), 1, line)
        self.assertIn('rc=$?', line[0])


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
