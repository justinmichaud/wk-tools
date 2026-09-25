"""The boards' profile-guided build (lib/wk/pgo.py) against a Fake world: which profiles are profile-guided, the
board a profile collects on, the cycle's phases as one graph shared with `wk bench ab`, each phase by itself, the
dry run, the record the cycle keeps and its stop, a cycle killed after any effect and run again, and the mixing,
which is WebKit's own `Tools/Scripts/pgo-profile` told that a GLib port carries one library.

The world answers each step's `wk` command by recording what it would leave and `wk sysimage holds` from that; the
yocto stage a phase runs is a recorder, since a stage is tests/test_yocto_stage.py's, and the collection run is
tests/test_bench_board.py's. The mixer runs against a stubbed checkout of exactly upstream's shape.

Run: python3 tests/run.py -k test_board_pgo
"""
import concurrent.futures as futures
import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

from tests.killpoints import converges
from tests.support import REPO, WkTest, _clean_env, requires_machine, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import images, pgo, record as progress, sched, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402

PROFILE = "webkit-2.52-yocto-rpi5-64"
LANE = "yocto-" + PROFILE
SHA = "a" * 40
WK = str(REPO / "wk")
RES = "machine:tolken"


class Inline:
    """An executor that runs each step as it is submitted: one order of effects, so a kill point is one place."""

    def __init__(self, max_workers=None):
        pass

    def submit(self, fn, *args):
        f = futures.Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:   # noqa: B902 -- a Killed is what the kill-point test is after
            f.set_exception(e)
        return f

    def map(self, fn, items):
        return [fn(x) for x in items]

    def shutdown(self, wait=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Reg(targets.Registry):
    def __init__(self, env, fake):
        super().__init__(REPO, env=env, machine=fake)

    def ws_target(self, ws):
        return "container"


class World:
    """This host holding the lane, a board `rpi5` running `mode`, and every step's command answered by the state it leaves."""

    def __init__(self, tmp, mode="bench %s-0123abcd" % PROFILE, fail=""):
        self.tmp, self.mode, self.fail = Path(tmp), mode, fail
        self.env = {"WK_STORE": str(self.tmp / "store"), "XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"), "WK_MACHINES_DIR": str(self.tmp / "machines")}
        (self.tmp / "machines").mkdir(parents=True, exist_ok=True)
        (self.tmp / "machines" / "rpi5.conf").write_text("KIND=board\nNODE_SSH=rpi5-rescue\n")
        self.fake, self.clock, self.built = Fake("here"), FakeClock(), []
        self.fake.answer(["hostname", "-s"], out="tolken\n")
        self.fake.react(["sh", "-c", sched.LOGGED], self.logged)
        self.fake.react([WK, "sysimage", "holds"], self.holds)
        self.reg = Reg(self.env, self.fake)
        self.p = images.load(PROFILE, self.env)

    @staticmethod
    def key(words):
        w = lambda flag: words[words.index(flag) + 1] if flag in words else ""   # noqa: E731
        if words[:2] == ["sysimage", "build"]:
            return "mix/%s" % w("--slot")
        if words[:2] == ["sysimage", "webkit"]:
            return "slot/%s/%s/%s" % (w("--slot"), w("--commit"), w("--config"))
        if words[:2] == ["bench", "deploy"]:
            return "deploy/%s" % w("--slot")
        return "collect/%s/%s" % (w("--slot"), words[3])

    def logged(self, argv, fk):
        words = argv[6:]
        if self.fail and self.fail in " ".join(words):
            return Result(1)
        fk._set_file("/state/" + self.key(words), "1")
        return Result(0)

    def holds(self, argv, fk):
        w = lambda flag: argv[argv.index(flag) + 1] if flag in argv else ""   # noqa: E731
        config = w("--config") or pgo.USE
        return Result(0, "yes\n" if "/state/slot/%s/%s/%s" % (w("--slot"), w("--commit"), config) in fk.files else "no\n")

    def cycle(self, spec=PROFILE, p=None):
        def build(rest):
            self.built.append(rest)
            return 0
        return pgo.Cycle(self.reg, p or self.p, spec, self.clock, build=build, mode_of=lambda b: self.mode, pool=Inline)

    def recs(self):
        return progress.Records(self.reg.store.record_dir(), clock=self.clock, env=self.env, machine=self.fake)

    def state(self):
        return sorted(k for k in self.fake.files if k.startswith("/state/"))


class PgoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-pgo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_CONFIRMED"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        self.n = 0

    def world(self, **kw):
        self.n += 1
        return World(os.path.join(self.tmp, "w%d" % self.n), **kw)

    def quiet(self, fn, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            try:
                return fn(*args), err.getvalue()
            except Refused as e:
                return e, err.getvalue()

    def webkit(self, w, *rest):
        return self.quiet(w.cycle().webkit, list(rest))

    def refused(self, w, *rest):
        rc, err = self.webkit(w, *rest)
        self.assertIsInstance(rc, Refused, err)
        return err

    def graph(self, w, spec=PROFILE):
        return {s.id: s for s in w.cycle(spec).graph(LANE, SHA, "pr", "rpi5")}


class TestWhichProfilesAreProfileGuided(unittest.TestCase):
    """2.52 is where upstream's cmake support arrives; from it on there is no plain build to fall back to."""

    def wanted(self, glob):
        seen = {}
        for conf in sorted((REPO / "image" / "configs").glob(glob)):
            p = images.load(conf.stem)
            seen[conf.stem] = images.pgo_wanted(p["IMG_BUILDER"], p["CFG_RELEASE"])
        self.assertTrue(seen)
        return set(seen.values())

    def test_every_2_52_yocto_profile_is_and_no_earlier_release_is(self):
        self.assertEqual((self.wanted("webkit-2.52-yocto-*.conf"), self.wanted("wpewebkit-2.*-yocto-*.conf")), ({True}, {False}))

    def test_a_2_52_buildroot_profile_is_not_yet(self):
        self.assertEqual(self.wanted("webkit-2.52-buildroot-*.conf"), {False})


class TestTheBoardIsReadOffTheFleet(PgoTest):
    def test_a_profile_collects_on_its_own_img_machine(self):
        w = self.world()
        self.assertEqual(w.cycle().board(), "rpi5")

    def test_a_profile_whose_board_the_fleet_lacks_refuses_rather_than_building_plain(self):
        w = self.world()
        os.remove(w.tmp / "machines" / "rpi5.conf")
        self.assertIn("IMG_MACHINE", self.refused(w, "--commit", SHA, "--slot", "pr"))
        self.assertEqual(w.state(), [])

    def test_a_board_not_running_this_image_is_refused_naming_the_way_in(self):
        w = self.world(mode="rescue")
        err = self.refused(w, "--commit", SHA, "--slot", "pr")
        self.assertIn("wk sysimage write --from %s --disk rpi5:<device>" % PROFILE, err)
        self.assertEqual(w.state(), [])


class TestThePhasesAndTheirOrder(PgoTest):
    """Each phase is one `wk` command and one step of the graph `wk bench ab` also runs (ab.py's pgo_steps)."""

    def test_it_instruments_then_collects_every_benchmark_then_mixes_then_rebuilds(self):
        order = [s.id for s in sched.plan_order(list(self.graph(self.world()).values()))]
        self.assertEqual(order, ["instr:%s:pr" % LANE, "deploy:rpi5:pr-instr"] + ["collect:rpi5:pr:" + p for p in pgo.BENCHMARKS]
                         + ["mix:%s:pr" % LANE, "slot:%s:pr" % LANE])

    def test_the_board_is_held_for_the_collection_and_the_machine_for_the_builds(self):
        g = self.graph(self.world())
        self.assertEqual({g[i].holds for i in g if i.startswith(("deploy:", "collect:"))}, {("device:rpi5",)})
        self.assertEqual({g[i].holds for i in g if i.startswith(("instr:", "mix:", "slot:"))}, {(RES,)})

    def test_the_measured_build_needs_the_mix_which_needs_every_leg(self):
        g = self.graph(self.world())
        self.assertEqual(g["slot:%s:pr" % LANE].needs, ("mix:%s:pr" % LANE,))
        self.assertEqual(g["mix:%s:pr" % LANE].needs, tuple("collect:rpi5:pr:" + p for p in pgo.BENCHMARKS))

    def test_every_command_names_the_lane_and_each_leg_collects_from_the_instrumented_slot(self):
        g = self.graph(self.world())
        self.assertTrue(all(LANE in s.command for s in g.values()))
        self.assertIn("wk bench run %s jetstream3 --system rpi5 --slot pr-instr --collect" % LANE, g["collect:rpi5:pr:jetstream3"].command)
        self.assertIn("--slot pr-instr --config %s" % pgo.COLLECT, g["instr:%s:pr" % LANE].command)
        self.assertIn("--slot pr --config %s" % pgo.USE, g["slot:%s:pr" % LANE].command)

    def test_a_spec_naming_another_machine_routes_the_board_steps_there(self):
        g = self.graph(self.world(), PROFILE + "@moose")
        self.assertEqual(g["instr:%s:pr" % LANE].holds, ("machine:moose",))
        self.assertTrue(g["deploy:rpi5:pr-instr"].command.startswith("WK_TARGET=moose wk bench deploy"))

    def test_a_phase_already_done_is_asked_about_by_its_own_evidence(self):
        w = self.world()
        w.fake._set_file("/state/slot/pr-instr/%s/%s" % (SHA, pgo.COLLECT), "1")
        self.assertEqual(sched.done_ids(list(self.graph(w).values()), Inline), {"instr:%s:pr" % LANE})


class TestEachPhaseByItself(PgoTest):
    """`--config` is one phase of the cycle, or the unprofiled build, as one yocto webkit stage."""

    def phase(self, w, config, slot="pr"):
        rc, err = self.webkit(w, "--commit", SHA, "--slot", slot, "--config", config)
        self.assertEqual(rc, 0, err)
        return w.built[-1], err

    def test_the_measured_phase_builds_against_the_profile_the_mix_wrote(self):
        rest, _ = self.phase(self.world(), pgo.USE)
        self.assertEqual(rest[rest.index("--pgo-profile") + 1], "/src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata")
        self.assertEqual(rest[:2], ["--stage", "webkit"])

    def test_the_instrumented_phase_clears_the_collection_it_invalidates(self):
        w = self.world()
        stale = images.pgo_dir(LANE, "pr", w.env)
        w.fake._set_file(stale + "/jetstream3/diagnose/old.profraw", "x")
        rest, _ = self.phase(w, pgo.COLLECT, "pr-instr")
        self.assertFalse(w.fake.exists(stale))
        self.assertNotIn("--pgo-profile", rest)

    def test_the_unprofiled_build_is_taken_and_says_so(self):
        rest, err = self.phase(self.world(), "wpe-cross")
        self.assertIn("WITHOUT a profile", err)
        self.assertEqual(rest[rest.index("--config") + 1], "wpe-cross")

    def test_any_other_config_is_refused(self):
        self.assertIn("--config takes one of", self.refused(self.world(), "--commit", SHA, "--slot", "pr", "--config", "wpe-cross-pgo"))

    def test_a_slot_needs_a_full_sha(self):
        self.assertIn("40 hex digits", self.refused(self.world(), "--commit", "abc", "--slot", "pr"))


class TestTheDryRun(PgoTest):
    def test_it_prints_the_graph_and_does_nothing(self):
        w = self.world()
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.webkit(w, "--commit", SHA, "--slot", "pgo")
        self.assertEqual(rc, 0, err)
        self.assertIn("profile-guided build", err)
        self.assertIn("--slot pgo-instr", err)
        self.assertEqual((w.state(), w.built, w.recs().list()), ([], [], []))


class TestTheCycle(PgoTest):
    """One `pgo` record whose plan is the graph's steps in order, stepped as each runs, ended when the cycle ends."""

    def test_the_whole_cycle_runs_every_phase_once_into_one_record(self):
        w = self.world()
        rc, err = self.webkit(w, "--commit", SHA, "--slot", "pr")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(w.state()), 7, w.state())
        (t,) = w.recs().list()
        self.assertEqual(len(t.plan()), 7)
        self.assertIn("--config %s" % pgo.COLLECT, t.plan()[0])
        self.assertEqual((t.field("kill"), t.field("exit")), ("wk sysimage webkit %s --workspace %s --slot pr --stop" % (PROFILE, LANE), "0"))

    def test_a_failed_leg_stops_the_cycle_before_the_measured_build(self):
        w = self.world(fail="motionmark")
        self.assertIn("the cycle for 'pr' stopped", self.refused(w, "--commit", SHA, "--slot", "pr"))
        self.assertNotIn("/state/mix/pr", w.state())
        self.assertNotEqual(w.recs().list()[0].field("exit"), "0")

    def test_a_cycle_killed_after_any_effect_and_run_again_converges(self):
        """`unit killpoints[sysimage webkit]` for a profile-guided slot: a re-run takes up what is left."""
        base = os.path.join(self.tmp, "kill")

        def make():
            self.n += 1
            return World(os.path.join(base, str(self.n)))

        def once(w):
            rc, err = self.quiet(w.cycle().webkit, ["--commit", SHA, "--slot", "pr"])
            self.assertEqual(rc, 0, err)
        converges(self, make, once, World.state)

    def test_stop_with_no_cycle_running_says_so_and_exits_0(self):
        rc, err = self.webkit(self.world(), "--slot", "pr", "--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn("no pgo is running", err)

    def test_stop_takes_nothing_with_it(self):
        self.assertIn("takes nothing", self.refused(self.world(), "--slot", "pr", "--commit", SHA, "--stop"))

    def test_stop_ends_a_running_cycle_cancelled(self):
        w = self.world()
        w.fake.pids.add(4242)
        w.fake.answer(["sh", "-c"], out="")
        t = w.recs().begin("pgo", "here", LANE + "/pr", "k", "/l", ["a"], pid=4242)
        rc, err = self.webkit(w, "--slot", "pr", "--stop")
        self.assertEqual(rc, 0, err)
        self.assertEqual(t.field("exit"), "cancelled")


class TestTheFacts(unittest.TestCase):
    def test_a_collection_is_as_long_as_it_is_told_and_two_hours_otherwise(self):
        self.assertEqual((pgo.collect_timeout({"WK_PGO_COLLECT_TIMEOUT": "99"}), pgo.collect_timeout({})), ("99", "7200"))

    def test_a_board_writes_one_file_per_process_into_the_directory_the_build_bakes_in(self):
        self.assertTrue(pgo.BOARD_FILE.startswith(pgo.BOARD_DIR + "/") and pgo.BOARD_FILE.endswith("_%p.profraw"))


# A `pgo-profile` of exactly upstream's shape: the two names lib/wk/pgo.py reaches
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


def mix_cli(*args):
    return subprocess.run([sys.executable, "-m", "wk.pgo", *args], capture_output=True, text=True, timeout=120,
                          env=_clean_env({"PYTHONPATH": str(REPO / "lib")}))


class TestTheMixingIsUpstreams(WkTest):
    def setUp(self):
        self._scratch = scratch_dir()
        self.tmp = self._scratch.__enter__()
        self.scripts = stub_scripts(self.tmp)
        self.dir = collection(self.tmp / "pgo")

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def test_the_weights_come_from_the_checkout_and_not_from_here(self):
        cp = mix_cli("plans", "--scripts", str(self.scripts))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split(),
                         ["speedometer3", "0.6", "jetstream3", "0.2", "motionmark", "0.2"])

    def test_a_glib_collection_mixes_into_one_library(self):
        cp = mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), str(self.dir / "output" / "WPEWebKit.profdata"))
        recorded = json.loads((self.dir / "output" / "combine.json").read_text())
        self.assertEqual(recorded["libs"], ["WPEWebKit"])
        self.assertEqual(recorded["groups"],
                         {plan: str(self.dir / plan)
                          for plan in ("speedometer3", "jetstream3", "motionmark")})

    def test_each_leg_is_merged_where_the_reader_looks_for_it(self):
        mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir), "--lib", "WPEWebKit")
        for plan in ("speedometer3", "jetstream3", "motionmark"):
            merged = self.dir / plan / "WPEWebKit.profdata"
            self.assertTrue(merged.exists(), merged)
            self.assertEqual(merged.read_text(), "merged 2")

    def test_a_leg_that_wrote_no_profile_is_refused(self):
        for stale in (self.dir / "motionmark" / "diagnose").glob("*.profraw"):
            os.remove(stale)
        cp = mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("motionmark", cp.stdout + cp.stderr)

    def test_a_benchmark_upstream_does_not_weigh_is_refused(self):
        """Naming the three is all this repo may decide, and a list that has
        drifted from the one upstream weighs has no ratio to be mixed at."""
        cp = mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
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
        cp = mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
                   "--lib", "WPEWebKit")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("FileNotFoundError", cp.stderr)

    def test_it_refuses_a_checkout_that_has_no_pgo_profile(self):
        os.remove(self.scripts / "pgo-profile")
        cp = mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir),
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
        mix_cli("mix", "--scripts", str(self.scripts), "--dir", str(self.dir), "--lib", "WPEWebKit")

    def tearDown(self):
        self._scratch.__exit__(None, None, None)

    def _check(self, *extra):
        return mix_cli("check", "--scripts", str(self.scripts), "--dir", str(self.dir),
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
        cp = mix_cli("check", "--read", str(out))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("WPEWebKit", cp.stdout)


class TestTheDriverPullsWhatTheBoardWrote(WkTest):
    def setUp(self):
        for name in ("webkitpy", "webkitpy.benchmark_runner",
                     "webkitpy.benchmark_runner.browser_driver"):
            sys.modules.setdefault(name, types.ModuleType(name))
        driver = types.ModuleType("webkitpy.benchmark_runner.browser_driver.browser_driver")
        driver.BrowserDriver = type("BrowserDriver", (), {"__init__": lambda self, a=None: None})
        sys.modules["webkitpy.benchmark_runner.browser_driver.browser_driver"] = driver
        spec = importlib.util.spec_from_file_location(
            "wk_board_driver_stub", REPO / "lib" / "wk" / "bench" / "board_driver.py")
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




class TestARealCollection(unittest.TestCase):
    """`live bench.pgo_collection[<b>]`, the read-only half: the newest collection a board left in this store's lanes
    passes the gate and has every leg's run. Taking one reflashes nothing but deploys and runs on the board, so a
    collection itself is a person's `wk sysimage webkit <profile> --commit <sha> --slot <s>`."""

    def collection(self, board):
        found = sorted(Path(Store().ws_dir("x")).parent.glob("yocto-webkit-2.52-yocto-%s-*/build/wk-pgo/*/profile-check.json"
                                                                     % board), key=os.path.getmtime)
        if not found:
            self.skipTest("no collection from %s in this store" % board)
        reading = json.loads(found[-1].read_text())
        self.assertEqual(pgo.faults(reading), [], found[-1])
        for plan in pgo.BENCHMARKS:
            self.assertTrue((found[-1].parent / plan / "result.json").is_file(), plan)

    @requires_machine("root@rpi3-bench")
    def test_rpi3(self):
        self.collection("rpi3")

    @requires_machine("root@rpi4-bench")
    def test_rpi4(self):
        self.collection("rpi4")

    @requires_machine("root@rpi5-bench")
    def test_rpi5(self):
        self.collection("rpi5")


if __name__ == "__main__":
    unittest.main()
