"""The two gates that stand between a PGO collection and a number nobody can
attribute (lib/wk/bench/mac.py's PgoCollect, bench/mac-browser-check.py, lib/wk/pgo.py)."""
import importlib.util
import os
import subprocess
import sys
import threading
import unittest

from tests.support import REPO, WkTest, bash, scratch_dir


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem.replace("-", "_"), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BROWSER = load(REPO / "bench" / "mac-browser-check.py")
sys.path.insert(0, str(REPO / "lib"))
from wk import pgo as PROFILE, presets, screen  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result, lib_argv  # noqa: E402

# What the Mac reads back: three frameworks and a compressed copy per
# arch. A board's one library goes through the same code from the other
# direction, in tests/test_board_pgo.py.
LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")

GOOD_DISPLAY = {"id": 1, "builtin": True, "main": True, "active": True,
                "online": True, "mirrored": False, "asleep": False,
                "points": [1024, 768], "vendor": 1552, "model": 41058,
                "unit": 0, "brightness": 0.0}
GOOD_READING = {"webgl": "WebGL 2.0", "renderer": "Apple GPU", "raf_hz": 57.2,
                "screen": [1024, 768], "dpr": 2, "focused": True,
                "frontmost": "org.webkit.MiniBrowser", "brightness": 0.0,
                "displays": [GOOD_DISPLAY]}
GOOD_CLIENTS = {1732: "com.apple.WebKit.GPU.Development"}
GOOD_EXPECT = ("builtin", [1024, 768])

# The guest that builds draws on a paravirtual panel, which is not a built-in one.
GUEST_DISPLAY = dict(GOOD_DISPLAY, builtin=False, points=[1920, 1080],
                     vendor=0, model=0, brightness=None)

_DEFAULT = object()


class TestTheBrowserGate(WkTest):
    # None is a real value for `expect` -- "no display to be comparable with" --
    # so the default has its own sentinel.
    def verdict(self, reading=None, clients=None, min_raf=45.0, expect=_DEFAULT):
        return BROWSER.faults(dict(GOOD_READING if reading is None else reading),
                              GOOD_CLIENTS if clients is None else clients,
                              "AppleParavirtGPU", min_raf,
                              GOOD_EXPECT if expect is _DEFAULT else expect)

    def test_an_accelerated_unthrottled_run_passes(self):
        self.assertEqual(self.verdict(), [])

    def test_each_fault_is_refused_by_name(self):
        for said, reading, clients in (("throttle", dict(GOOD_READING, raf_hz=8.0), None),
                                       ("no WebGL", dict(GOOD_READING, webgl=None), None), ("did not reach that device", None, {}),
                                       ("never reported", {}, {}),
                                       ("points, not [1024, 768]", dict(GOOD_READING, displays=[dict(GOOD_DISPLAY, points=[1470, 956])]), None)):
            with self.subTest(said):
                found = self.verdict(reading, clients)
                self.assertTrue(any(said in f for f in found), found)

    def test_a_run_compared_with_nothing_is_not_judged_on_its_display(self):
        guest = dict(GOOD_READING, displays=[GUEST_DISPLAY])
        self.assertEqual([], self.verdict(guest, expect=None))
        self.assertTrue(any("builtin panel" in f for f in self.verdict(guest)))

        throttled = dict(guest, raf_hz=8.0)
        found = self.verdict(throttled, expect=None)
        self.assertTrue(any("throttle" in f for f in found), found)
        self.assertEqual([], [f for f in found if "display" in f or "panel" in f])

    def test_the_bar_is_the_callers_to_set(self):
        self.assertEqual(self.verdict(min_raf=10.0), [])
        self.assertNotEqual(self.verdict(min_raf=59.0), [])

    def test_a_busy_but_focused_window_is_not_a_throttled_one(self):
        self.assertEqual(self.verdict(dict(GOOD_READING, raf_hz=44.4, focused=True), min_raf=BROWSER.MIN_RAF), [])


def profile_tree(root, benchmarks=BENCHMARKS, libraries=LIBRARIES,
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
        super().setUp()
        self.root = self.tmp

    def read(self, summaries):
        def summary(path):
            key = "/".join(str(path).split(os.sep)[-2:])
            return summaries.get(key, {"total_functions": 40000,
                                       "maximum_function_count": 900000})
        return PROFILE.collect(str(self.root), LIBRARIES, BENCHMARKS, "arm64", summary)

    def real_reading(self):
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

    def test_each_unusable_profile_is_refused_by_name(self):
        profile_tree(self.root)
        for said, reading in (
                ("every counter in it is zero", lambda: self.read({f"output/{lib}.profdata": {"total_functions": 40000, "maximum_function_count": 0}
                                                                  for lib in LIBRARIES})),
                ("nothing ran long enough", lambda: self.read({"output/JavaScriptCore.profdata": {"total_functions": 12, "maximum_function_count": 3}})),
                ("llvm-profdata cannot read", lambda: self.read({"output/WebKit.profdata": {"error": ["not a profile"]}})),
                ("jetstream3 touched 900", lambda: self.read_real({"jetstream3": {"JavaScriptCore": (900, 238390050)}})),
                ("no counter above zero", lambda: self.read_real({"motionmark": {"WebCore": (19266, 0)}}))):
            with self.subTest(said):
                found = PROFILE.faults(reading())
                self.assertTrue(any(said in f for f in found), found)

    def test_a_real_collection_passes(self):
        profile_tree(self.root)
        self.assertEqual(PROFILE.faults(self.read_real()), [])


class TestAProfileGuidedBuildDoesNotCacheCompilations(WkTest):

    def test_only_the_pgo_config_turns_it_off(self):
        for preset, off in (("mac-release-pgo", True), ("mac-release", False)):
            with self.subTest(preset):
                env = presets.build_env(presets.resolve(preset, "macos", "vm", {}), "/src/WebKit", 4, 10, "native", "/ccache", {})
                self.assertEqual(off, "WK_NO_COMPILATION_CACHE=1" in env, env)


class TestNothingMayDrawOverAMeasuredRun(WkTest):

    WITH_A_DIALOG = ("Control Center:25:42x30@837,0;Window Server:24:1024x30@0,0;"
                     "Dock:20:1024x768@0,0;UserNotificationCenter:8:260x364@382,119;"
                     "Terminal:0:877x499@40,51;")
    CLEAN = ("Control Center:25:42x30@837,0;Window Server:24:1024x30@0,0;"
             "Dock:20:1024x768@0,0;Terminal:0:877x499@40,51;")

    def _uninvited(self, reading):
        cp = bash('. "$WK_ROOT/bench/mac-window-probe.sh"; wk_window_unexpected "%s"' % reading)
        return cp.stdout.strip()

    def test_an_alert_above_the_ordinary_layer_is_reported_and_the_screens_own_furniture_is_not(self):
        self.assertIn("UserNotificationCenter", self._uninvited(self.WITH_A_DIALOG))
        self.assertEqual(self._uninvited(self.CLEAN), "")

    def _watch(self, appears):
        m, samples, sampled = Fake("mac"), [], threading.Event()

        def probe(argv, fake):
            samples.append(1)
            if len(samples) >= 5:
                sampled.set()
            dialog = "UserNotificationCenter:8:260x364@382,119;" if appears and len(samples) >= 3 else ""
            return Result(0, "windows=Dock:20:1x1@0,0;%sTerminal:0:8x8@0,0;\n" % dialog)

        m.react(lib_argv(str(REPO), screen.WINDOWS, "wk_window_probe"), probe)
        m.react(lib_argv(str(REPO), screen.WINDOWS, "wk_window_unexpected"),
                lambda argv, fake: Result(0, subprocess.run(argv, capture_output=True, text=True).stdout))
        m.answer(["bash", "-c"])
        m.answer(["ps"])
        m.answer(["uname", "-s"], out="Darwin\n")
        watch = screen.Watch(m, REPO, FakeClock(), {"WK_SCREEN_WATCH_SECONDS": "0.01"})
        watch.start()
        self.assertTrue(sampled.wait(30), "the watch never sampled five times")
        return watch.stop()

    def test_the_blocker_names_each_owner_once_and_an_unasked_server_is_not_a_clear_screen(self):
        m = Fake("mac")
        m.answer(lib_argv(str(REPO), screen.WINDOWS, "wk_window_unexpected"), out="Zed:8:1x1@0,0;Alert:8:1x1@0,0;Zed:9:1x1@0,0;")
        for said, want in (("windows=Dock:20:1x1@0,0;\n", "Alert,Zed"), ("windows=?\n", "?"), ("", "?")):
            with self.subTest(said=said):
                m.answer(lib_argv(str(REPO), screen.WINDOWS, "wk_window_probe"), out=said)
                self.assertEqual(screen.blocker(m, REPO), want)

    def test_a_window_that_appears_mid_run_is_caught(self):
        self.assertEqual([l.split("\t", 1)[1] for l in self._watch(True)], ["UserNotificationCenter"])

    def test_a_run_nothing_drew_over_passes(self):
        self.assertEqual(self._watch(False), [])


class TestPyobjcIsProvisionedNotAssumed(WkTest):

    def test_the_probe_wants_the_pinned_version_of_the_running_interpreter(self):
        for said, want in (("11.1", "HAVE"), ("9.0", "MISSING")):
            with self.subTest(version=said), scratch_dir() as tmp:
                fake = tmp / "python3"
                fake.write_text('#!/bin/sh\necho %s\n' % said)
                fake.chmod(0o755)
                cp = bash(f'. "$WK_ROOT/bench/mac-pyobjc.sh"; '
                          f'WK_PYOBJC_PYTHON={fake}; wk_pyobjc_have && echo HAVE || echo MISSING')
                self.assertIn(want, cp.stdout)


if __name__ == "__main__":
    unittest.main()
