"""The macOS perf build: `mac-release-pgo`, the preset every macOS number is
taken from (lib/wk/presets.py, build/mac-pgo.sh, lib/wk/bench/mac_pgo.py's PgoCollect,
build/pgo-run-benchmark.py)."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, WkTest, run, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from tests.test_bench_mac import StubWatch  # noqa: E402
from wk import presets, screen as wkscreen  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import mac, mac_pgo, seed  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

PRESET = "mac-release-pgo"


def preset(name, os_name="macos"):
    return presets.resolve(name, os_name, "vm", {})


def pgo_dry_run(tmp):
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
    return subprocess.run(["bash", str(REPO / "build" / "build-in-workspace.sh")],
                          capture_output=True, text=True, env=env, timeout=60)


class TestTheConfig(WkTest):
    def test_it_is_listed_so_a_reader_can_find_it(self):
        cp = run("build", "--list")
        self.assertIn(PRESET, cp.stdout + cp.stderr)

    def test_it_is_an_xcode_config_that_asks_for_a_profile(self):
        c = preset(PRESET)
        self.assertEqual((c.buildsys, c.pgo, c.args), ("xcode", True, "--release"))

    def test_its_products_never_share_a_directory_with_the_plain_release(self):
        pgo_dir = preset(PRESET).build_dir()
        self.assertNotEqual(pgo_dir, preset("mac-release").build_dir())
        self.assertNotEqual(pgo_dir, preset("mac-release-asan").build_dir())
        self.assertTrue(pgo_dir.endswith("Release-pgo"), pgo_dir)

    def test_a_linux_workspace_is_told_it_cannot_build_it(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            preset(PRESET, "linux")
        self.assertIn("Xcode", err.getvalue())


class TestTheThreePhases(WkTest):

    @classmethod
    def setUpClass(cls):
        with scratch_dir() as tmp:
            cp = pgo_dry_run(tmp)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        cls.lines = [l for l in (cp.stdout + cp.stderr).splitlines() if l.strip()]

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
        self.assertIn("--run-benchmark-harness", line)
        self.assertIn("pgo-run-benchmark.py", line)
        self.assertIn("rm -rf", line)

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


def fake_guest(check_rc=0, drew=(), profile_rc=0, blocker="", console="admin", pyobjc=0, screen=0, browser_rc=0):
    """The guest a collection runs in, every gate passing unless told otherwise; `drew` is what the watch saw."""
    m = Fake("guest")
    m.drew = list(drew)
    m.answer(["stat", "-f"], out=console + "\n")
    m.answer(["id", "-un"], out="admin\n")
    m.answer(["/usr/bin/python3", "-c"], rc=screen)
    m.answer(["/usr/bin/python3", str(REPO / mac.CHECK)], rc=browser_rc)
    m.answer(["env", "WK_WEBKIT_SCRIPTS=/src/Tools/Scripts"], rc=check_rc)
    m.answer(["/usr/bin/python3", "-I"], rc=profile_rc)

    def lib(argv, fake):
        fn = argv[2].split(";")[1].split()[0]
        return {"wk_pyobjc_have": Result(pyobjc), "wk_window_probe": Result(0, "windows=MiniBrowser:Speedometer\n"),
                "wk_window_unexpected": Result(0, blocker)}.get(fn, Result(0))
    m.react(["bash", "-c"], lib)
    return m


def collect(m, pins=(("speedometer3", "/seed/s"), ("jetstream3", "/seed/j"), ("motionmark", "/seed/m"))):
    c = mac_pgo.PgoCollect(REPO, m, {"HOME": "/Users/admin"}, "/src")
    with mock.patch.object(mac_pgo.PgoCollect, "pins", lambda self: list(pins)), mock.patch.object(wkscreen, "Watch", StubWatch), \
            mock.patch.dict(os.environ), \
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
        if e[0] == "watch":
            out.append("watch_" + e[1])
        elif argv[:2] == ("bash", "-c") and "; " in argv[2]:
            out.append(argv[2].split(";")[1].split()[0])
        elif len(argv) > 1 and argv[1].endswith("mac-browser-check.py"):
            out.append("browser-check")
        elif e[0] == "run_tty":
            out.append("collect")
        elif "-I" in argv and "wk.pgo" in argv:
            out.append("profile-check")
    return out


class TestTheCollection(WkTest):
    """The instrumented browser, gated before it is profiled and its profile read back after."""

    def test_it_runs_in_the_one_order_that_makes_sense(self):
        m = fake_guest()
        rc, err = collect(m)
        self.assertEqual(rc, 0, err)
        self.assertEqual(["wk_pyobjc_have", "wk_window_probe", "wk_window_unexpected", "mac_raiser_on", "browser-check", "watch_start",
                          "collect", "watch_stop", "mac_raiser_off", "profile-check"],
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
        rc, err = collect(fake_guest(drew=["2026-09-27T12:00:00Z\tSoftware Update"]))
        self.assertIs(rc, Refused)
        self.assertIn("Software Update", err)

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
        c = mac_pgo.PgoCollect(REPO, m, {"HOME": "/Users/admin", "WK_STORE": "/store"}, "/src")
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
            mac_pgo.PgoCollect(REPO, m, {"HOME": "/Users/admin"}, "/src").evidence("/final", "/p")
        self.assertEqual(sorted(f for f in m.files if f.startswith("/final/")),
                         ["/final/wk-browser-check.json", "/final/wk-payload-pins", "/final/wk-profile-check.json"])


class TestItRefusesAThrottledCollection(WkTest):

    def test_it_names_every_reason_rather_than_the_first(self):
        faults = mac_pgo.PgoCollect(REPO, fake_guest(console="root", screen=1, blocker="Setup Assistant:Welcome"), {}, "/src").faults()
        self.assertEqual(len(faults), 3, faults)
        self.assertIn("nowhere to draw", faults[0])
        self.assertIn("no main screen", faults[1])
        self.assertIn("Setup Assistant", faults[2])

    def test_without_pyobjc_nothing_can_raise_the_browser(self):
        faults = mac_pgo.PgoCollect(REPO, fake_guest(pyobjc=1), {}, "/src").faults()
        self.assertEqual(["no pyobjc: run-benchmark cannot size the screen and no raiser can hold the browser in front"], faults)

    def test_a_fault_stops_the_collection_before_the_raiser(self):
        m = fake_guest(console="root")
        rc, err = collect(m)
        self.assertIs(rc, Refused)
        self.assertIn("cannot present an unthrottled browser", err)
        self.assertNotIn("mac_raiser_on", order(m))


class TestItIsThePolicyAndNotAnOption(WkTest):

    def test_the_instrumented_products_are_named_in_one_place(self):
        """The reclaim after a stage deletes them, so a second spelling deletes the wrong directory, or nothing."""
        cp = run_py("pgo-instr", "/x/Release-pgo")
        self.assertEqual(cp.stdout.strip(), "/x/Release-pgo-instr")

    @unittest.skipUnless(os.path.exists("/usr/bin/python3"), "the collection's python is the Mac's /usr/bin/python3")
    def test_the_collection_runs_wk_tools_whatever_the_working_directory_holds(self):
        """The build sources this in the checkout, whose files an agent writes; a wk/ there must not be imported."""
        with scratch_dir() as tmp:
            (tmp / "wk").mkdir()
            (tmp / "wk" / "__init__.py").write_text("raise SystemExit('the checkout was imported')\n")
            cp = subprocess.run(["bash", "-c", '. "$0"; _pgo_py pgo-instr /x/Release-pgo', str(REPO / "build" / "mac-pgo.sh")],
                                cwd=str(tmp), capture_output=True, text=True, timeout=30, env=dict(os.environ, PYTHONPATH=str(tmp)))
        self.assertEqual((0, "/x/Release-pgo-instr"), (cp.returncode, cp.stdout.strip()), cp.stderr)


def run_py(*args):
    return subprocess.run(["python3", "-m", "wk.bench.mac_pgo"] + list(args), capture_output=True, text=True, timeout=30,
                          env=dict(os.environ, PYTHONPATH=str(REPO / "lib")))


class TestTheHarnessWrapper(WkTest):
    def test_it_refuses_without_being_told_where_the_checkout_is(self):
        cp = subprocess.run(["python3", str(REPO / "build" / "pgo-run-benchmark.py")],
                            capture_output=True, text=True, timeout=30,
                            env={k: v for k, v in os.environ.items()
                                 if k != "WK_WEBKIT_SCRIPTS"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("WK_WEBKIT_SCRIPTS", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
