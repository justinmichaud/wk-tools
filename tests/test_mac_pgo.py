"""The macOS perf build: `mac-release-pgo`, the config every macOS number is
taken from (build/configs.sh, build/mac-pgo.sh, lib/wk/bench/mac.py's PgoCollect,
build/pgo-run-benchmark.py).

Three phases with a benchmark run between them, so the shape of each phase --
and the fact that the instrumented one never lands in the measured one's
products directory -- is what these tests pin; the collection itself runs
against a Fake machine.

Run: python3 -m unittest tests.test_mac_pgo -v
"""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash, run, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import pgo  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import mac, seed  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

CONFIG = "mac-release-pgo"


def config_fields(config, os_name, kind="vm"):
    cp = bash(f'''
. "{REPO}/lib/common.sh"
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


def fake_guest(check_rc=0, watch_rc=0, profile_rc=0, blocker="", console="admin", pyobjc=0, screen=0, browser_rc=0):
    """The guest a collection runs in, every gate passing unless told otherwise."""
    m = Fake("guest")
    m.answer(["stat", "-f"], out=console + "\n")
    m.answer(["id", "-un"], out="admin\n")
    m.answer(["/usr/bin/python3", "-c"], rc=screen)
    m.answer(["/usr/bin/python3", str(REPO / mac.CHECK)], rc=browser_rc)
    m.answer(["env", "WK_WEBKIT_SCRIPTS=/src/Tools/Scripts"], rc=check_rc)
    m.answer(["env", "PYTHONPATH=" + str(REPO / "lib")], rc=profile_rc)

    def lib(argv, fake):
        fn = argv[2].split(";")[1].split()[0]
        return {"wk_pyobjc_have": Result(pyobjc), "screen_blocker": Result(0, blocker),
                "screen_watch_stop": Result(watch_rc, "a Software Update pane\n" if watch_rc else "")}.get(fn, Result(0))
    m.react(["bash", "-c"], lib)
    return m


def collect(m, pins=(("speedometer3", "/seed/s"), ("jetstream3", "/seed/j"), ("motionmark", "/seed/m"))):
    c = mac.PgoCollect(REPO, m, {"HOME": "/Users/admin"}, "/src")
    with mock.patch.object(mac.PgoCollect, "pins", lambda self: list(pins)), mock.patch.dict(os.environ), \
            contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
        os.environ.pop("WK_DRY_RUN", None)
        try:
            return c.run("/src/WebKitBuild/Release-pgo-instr", "/src/WebKitBuild/Release-pgo-profile", "arm64"), err.getvalue()
        except Refused:
            return Refused, err.getvalue()


def order(m):
    """Each step the collection took, named by what it ran."""
    out = []
    for e in m.effects:
        argv = e[1] if e[0] in ("run", "run_tty") else ()
        if argv[:2] == ("bash", "-c") and "; " in argv[2]:
            out.append(argv[2].split(";")[1].split()[0])
        elif len(argv) > 1 and argv[1].endswith("mac-browser-check.py"):
            out.append("browser-check")
        elif e[0] == "run_tty":
            out.append("collect")
        elif argv[:1] == ("env",) and "wk.pgo" in argv:
            out.append("profile-check")
    return out


class TestTheCollection(WkTest):
    """The instrumented browser, gated before it is profiled and its profile read back after."""

    def test_it_runs_in_the_one_order_that_makes_sense(self):
        m = fake_guest()
        rc, err = collect(m)
        self.assertEqual(rc, 0, err)
        self.assertEqual(["wk_pyobjc_have", "screen_blocker", "mac_raiser_on", "browser-check", "screen_watch_start", "collect",
                          "screen_watch_stop", "mac_raiser_off", "profile-check"],
                         [s for s in order(m)])

    def test_the_browser_is_checked_against_the_instrumented_build_and_no_display(self):
        """A collection trains a profile rather than producing a number, so it is compared with no display."""
        m = fake_guest()
        collect(m)
        check = [e[1] for e in m.effects if e[0] == "run" and len(e[1]) > 1 and e[1][1].endswith("mac-browser-check.py")][0]
        self.assertIn("/src/WebKitBuild/Release-pgo-instr", check)
        self.assertNotIn("--expect-display", check)

    def test_a_failed_browser_check_profiles_nothing_and_lets_the_raiser_go(self):
        m = fake_guest(browser_rc=1)
        rc, err = collect(m)
        self.assertIs(rc, Refused)
        self.assertNotIn("collect", order(m))
        self.assertEqual(order(m)[-1], "mac_raiser_off")

    def test_something_drawn_over_the_collection_refuses_it(self):
        rc, err = collect(fake_guest(watch_rc=1))
        self.assertIs(rc, Refused)
        self.assertIn("a Software Update pane", err)

    def test_a_profile_not_to_build_against_stops_the_build(self):
        self.assertIs(collect(fake_guest(profile_rc=1))[0], Refused)

    def test_a_collection_that_failed_is_not_read_back(self):
        m = fake_guest(check_rc=3)
        self.assertEqual(collect(m)[0], 3)
        self.assertNotIn("profile-check", order(m))

    def test_each_benchmark_is_handed_its_pinned_copy_and_the_pins_are_kept(self):
        m = fake_guest()
        collect(m)
        argv = [e[1] for e in m.effects if e[0] == "run_tty"][0]
        self.assertIn("local-copy:/seed/j", argv)
        self.assertEqual(m.files["/src/WebKitBuild/Release-pgo-profile/payload-pins"].splitlines()[0], "speedometer3\t/seed/s")
        self.assertIn(("remove", "/src/WebKitBuild/Release-pgo-profile"), m.effects, "collect-pgo-profiles refuses a full directory")

    def test_a_payload_it_could_not_pin_stops_the_collection(self):
        m = fake_guest()
        m.files["/src/Tools/Scripts/webkitpy/benchmark_runner/data/plans/speedometer3.plan"] = '{"git_repository": {"url": "u"}}'
        c = mac.PgoCollect(REPO, m, {"HOME": "/Users/admin", "WK_STORE": "/store"}, "/src")
        with mock.patch.object(seed.Seeder, "seed", return_value=""), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(Refused):
                c.pins()
        self.assertIn("could not pin the speedometer3 payload", err.getvalue())

    def test_the_readings_travel_beside_the_products(self):
        m = fake_guest()
        for f in ("/Users/admin/.local/state/wk/pgo/browser-check.json", "/Users/admin/.local/state/wk/pgo/profile-check.json",
                  "/p/payload-pins"):
            m.files[f] = "x"
        with mock.patch.dict(os.environ):
            os.environ.pop("WK_DRY_RUN", None)
            mac.PgoCollect(REPO, m, {"HOME": "/Users/admin"}, "/src").evidence("/final", "/p")
        self.assertEqual(sorted(f for f in m.files if f.startswith("/final/")),
                         ["/final/wk-browser-check.json", "/final/wk-payload-pins", "/final/wk-profile-check.json"])


class TestItRefusesAThrottledCollection(WkTest):
    """Measured on a Tart guest 2026-09-06: Setup Assistant is frontmost on
    every boot, killing it takes the console session with it, and the guest's
    /usr/bin/python3 has no pyobjc so no raiser can displace it. A collection
    there would look exactly like a good one."""

    def test_it_names_every_reason_rather_than_the_first(self):
        faults = mac.PgoCollect(REPO, fake_guest(console="root", screen=1, blocker="Setup Assistant"), {}, "/src").faults()
        self.assertEqual(len(faults), 3, faults)
        self.assertIn("nowhere to draw", faults[0])
        self.assertIn("no main screen", faults[1])
        self.assertIn("Setup Assistant", faults[2])

    def test_without_pyobjc_nothing_can_raise_the_browser(self):
        faults = mac.PgoCollect(REPO, fake_guest(pyobjc=1), {}, "/src").faults()
        self.assertEqual(["no pyobjc: run-benchmark cannot size the screen and no raiser can hold the browser in front"], faults)

    def test_a_fault_stops_the_collection_before_the_raiser(self):
        m = fake_guest(console="root")
        rc, err = collect(m)
        self.assertIs(rc, Refused)
        self.assertIn("cannot present an unthrottled browser", err)
        self.assertNotIn("mac_raiser_on", order(m))


class TestItIsThePolicyAndNotAnOption(WkTest):
    """Every macOS number this repo quotes comes from a profile-guided build,
    so the lane defaults to it rather than offering it."""

    def test_the_mac_ab_defaults_to_it(self):
        self.assertEqual(mac.AB_CONFIG, CONFIG)

    def test_the_benchmarks_are_named_once_and_the_weights_are_webkits_own(self):
        """0.6 / 0.2 / 0.2 lives in Tools/Scripts/pgo-profile; naming the three
        benchmarks is all this repo may decide, and lib/wk/pgo.py names them."""
        self.assertEqual(set(pgo.BENCHMARKS), {"speedometer3", "jetstream3", "motionmark"})
        self.assertNotIn("0.6", (REPO / "build" / "mac-pgo.sh").read_text())
        for rel in ("build/mac-pgo.sh", "lib/wk/bench/mac.py"):
            text = (REPO / rel).read_text()
            self.assertNotIn('"speedometer3", "jetstream3"', text, rel)
            self.assertNotIn("speedometer3 jetstream3", text, rel)

    def test_the_instrumented_products_are_named_in_one_place(self):
        """The reclaim after a stage deletes them, so a second spelling deletes the wrong directory, or nothing."""
        self.assertNotRegex((REPO / "build" / "mac-pgo.sh").read_text(), r"[=\"]-instr|final-instr")
        cp = run_py("pgo-instr", "/x/Release-pgo")
        self.assertEqual(cp.stdout.strip(), "/x/Release-pgo-instr")


def run_py(*args):
    return subprocess.run(["python3", "-m", "wk.bench.mac"] + list(args), capture_output=True, text=True, timeout=30,
                          env=dict(os.environ, PYTHONPATH=str(REPO / "lib")))


class TestTheProfileReachesTheMachineThatRunsIt(WkTest):
    """A dSYM is a product on this lane: the machine that profiles is the
    benchmark install, which never had the build tree."""

    def test_the_stage_no_longer_drops_them(self):
        text = (REPO / "lib" / "bench-arms.sh").read_text()
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
