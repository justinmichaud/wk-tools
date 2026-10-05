"""The display readings a Mac benchmark is judged against: `lib/wk/mac.py
displays` / `brightness`, and the faults bench/mac-browser-check.py raises when
the screen it is measuring on is not the declared one."""
import argparse
import importlib.util
import io
import plistlib
import json
import platform
import subprocess
import sys
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from tests.support import REPO, WkTest


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WKMAC = load(REPO / "lib" / "wk" / "mac.py", "wkmac")
BROWSER = load(REPO / "bench" / "mac-browser-check.py", "mac_browser_check")

# tolken's built-in panel, as CGGetOnlineDisplayList reports it on the bench
# install: asleep and inactive is what a headless ssh session sees.
PANEL = {"id": 1, "builtin": True, "main": True, "active": False, "online": True,
         "mirrored": False, "asleep": True, "points": [1470, 956],
         "vendor": 1552, "model": 41058, "unit": 0}
EXTERNAL = dict(PANEL, id=2, builtin=False, main=False, points=[2560, 1440],
                vendor=4268, model=41000, unit=1)

UNSET = object()

ROW_KEYS = {"id", "builtin", "main", "active", "online", "mirrored", "asleep",
            "points", "vendor", "model", "unit", "brightness", "auto_brightness"}


class FakeCG:

    def __init__(self, displays, err=0):
        self.displays = displays
        self.err = err

    def CGGetOnlineDisplayList(self, limit, buffer, count):
        if self.err:
            return self.err
        for i, display in enumerate(self.displays[:limit]):
            buffer[i] = display["id"]
        count[0] = min(len(self.displays), limit)
        return 0

    def _row(self, ident):
        return next(d for d in self.displays if d["id"] == ident)

    def __getattr__(self, name):
        for key, call in WKMAC._FLAGS.items():
            if call == name:
                return lambda ident, key=key: int(bool(self._row(ident)[key]))
        for key, call in WKMAC._NUMBERS.items():
            if call == name:
                return lambda ident, key=key: self._row(ident)[key]
        for axis, call in enumerate(("CGDisplayPixelsWide", "CGDisplayPixelsHigh")):
            if call == name:
                return lambda ident, axis=axis: self._row(ident)["points"][axis]
        raise AttributeError(name)


class FakeDS:

    def __init__(self, value=0.4375, can_change=True, set_rc=0, get_rc=0, sticks=True,
                 has_als=True, als=False, als_rc=0, als_set_rc=0, als_sticks=True):
        self.value = value
        self.can_change = can_change
        self.set_rc = set_rc
        self.get_rc = get_rc
        self.sticks = sticks
        self.has_als = has_als
        self.als = als
        self.als_rc = als_rc
        self.als_set_rc = als_set_rc
        self.als_sticks = als_sticks

    def DisplayServicesGetBrightness(self, ident, out):
        if self.get_rc:
            return self.get_rc
        out[0] = self.value
        return 0

    def DisplayServicesSetBrightness(self, ident, value):
        if self.set_rc:
            return self.set_rc
        if self.sticks:
            self.value = value
        return 0

    def DisplayServicesCanChangeBrightness(self, ident):
        return self.can_change

    def DisplayServicesHasAmbientLightCompensation(self, ident):
        return self.has_als

    def DisplayServicesAmbientLightCompensationEnabled(self, ident, out):
        if self.als_rc:
            return self.als_rc
        out.contents.value = self.als
        return 0

    def DisplayServicesEnableAmbientLightCompensation(self, ident, enable):
        if self.als_set_rc:
            return self.als_set_rc
        if self.als_sticks:
            self.als = bool(enable)
        return 0


class WkmacHandles(WkTest):
    """Both subcommands, driven against the two fake handles."""

    def call(self, func, cg, ds, **kwargs):
        with mock.patch.object(WKMAC, "_coregraphics", lambda: cg), \
             mock.patch.object(WKMAC, "_display_services", lambda: ds):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = func(Namespace(**kwargs))
        return rc, out.getvalue()

    def displays(self, cg, ds=UNSET):
        return self.call(WKMAC.cmd_displays, cg, FakeDS() if ds is UNSET else ds)

    def brightness(self, cg, ds=UNSET, value=None):
        return self.call(WKMAC.cmd_brightness, cg, FakeDS() if ds is UNSET else ds,
                         set=value)


class WkmacDisplayMode(WkmacHandles):

    UUID = "37D8832A-2D66-02CA-B9F7-8F30A301B230"

    def mode(self, cg, declare=None, config=None):
        with mock.patch.object(WKMAC, "_colorsync",
                               lambda: (object(), object())), \
             mock.patch.object(WKMAC, "_builtin_uuid",
                               lambda cs, cf, ident: self.UUID), \
             mock.patch.object(WKMAC, "WINDOWSERVER_CONFIG",
                               str(config) if config else "/nonexistent"):
            return self.call(WKMAC.cmd_display_mode, cg, FakeDS(), declare=declare)

    def _config(self, wide=1470, high=956, scale=2, uuid=None):
        def row(w, h, sc, ident):
            info = {"Wide": w, "High": h, "Scale": sc, "Hz": 60.0, "Depth": 8}
            return {"UUID": ident, "Rotation": 0,
                    "CurrentInfo": dict(info), "UnmirrorInfo": dict(info)}
        panel = uuid or self.UUID
        doc = {"DisplayAnyUserSets": {"Configs": [
            {"DisplayConfig": [row(wide, high, scale, panel)]},
            {"DisplayConfig": [row(wide, high, scale, panel),
                               row(1920, 1080, 1, "AN-EXTERNAL-PANEL")]}]}}
        path = self.tmp / "windowserver.plist"
        with open(path, "wb") as handle:
            plistlib.dump(doc, handle)
        return path

    @staticmethod
    def _rows(path, uuid):
        with open(path, "rb") as handle:
            doc = plistlib.load(handle)
        return WKMAC._mode_rows(doc, uuid)

    def test_it_reads_the_running_mode_in_points(self):
        rc, out = self.mode(FakeCG([dict(PANEL, points=[1280, 832])]))
        self.assertEqual(0, rc)
        self.assertEqual("1280x832", out.strip())

    def test_declaring_a_mode_rewrites_every_row_of_that_panel(self):
        config = self._config(wide=1470, high=956)
        rc, out = self.mode(FakeCG([PANEL]), declare=(1280, 832), config=config)
        self.assertEqual(0, rc, out)
        self.assertEqual("1280x832", out.strip())
        rows = self._rows(config, self.UUID)
        self.assertEqual(4, len(rows), rows)   # two Configs x CurrentInfo + UnmirrorInfo
        for row in rows:
            self.assertEqual((1280, 832, 2), (row["Wide"], row["High"], row["Scale"]))

    def test_it_leaves_another_panels_rows_alone(self):
        config = self._config()
        self.mode(FakeCG([PANEL]), declare=(1280, 832), config=config)
        other = self._rows(config, "AN-EXTERNAL-PANEL")
        self.assertEqual(2, len(other), other)
        for row in other:
            self.assertEqual((1920, 1080, 1), (row["Wide"], row["High"], row["Scale"]))

    def test_it_keeps_what_it_was_not_asked_about(self):
        config = self._config()
        self.mode(FakeCG([PANEL]), declare=(1280, 832), config=config)
        for row in self._rows(config, self.UUID):
            self.assertEqual(60.0, row["Hz"])
            self.assertEqual(8, row["Depth"])

    def test_a_mode_it_cannot_place_is_refused_printing_nothing(self):
        self.assertEqual((1, ""), self.mode(FakeCG([PANEL, EXTERNAL])))
        self.assertEqual((1, ""), self.mode(FakeCG([PANEL]), declare=(1280, 832)))
        self.assertEqual((1, ""), self.mode(FakeCG([PANEL]), declare=(1280, 832),
                                            config=self._config(uuid="SOME-OTHER-PANEL")))

    def test_the_declared_mode_is_parsed_before_anything_is_written(self):
        for spec in ("1280", "1280x", "x832", "1280x832x2", "", "wide x high"):
            with self.subTest(spec=spec):
                with self.assertRaises(argparse.ArgumentTypeError):
                    WKMAC._points(spec)
        self.assertEqual((1280, 832), WKMAC._points("1280x832"))


class WkmacDisplays(WkmacHandles):
    def test_the_display_list_carries_every_field_consumers_read(self):
        rc, out = self.displays(FakeCG([PANEL]))
        self.assertEqual(0, rc)
        answer = json.loads(out)
        self.assertEqual(1, answer["count"])
        row = answer["displays"][0]
        self.assertEqual(ROW_KEYS, set(row))
        self.assertEqual([1470, 956], row["points"])
        self.assertTrue(row["builtin"])
        self.assertFalse(row["mirrored"])
        self.assertEqual(0.4375, row["brightness"])

    def test_a_second_display_is_listed_too(self):
        rc, out = self.displays(FakeCG([PANEL, EXTERNAL]))
        self.assertEqual(0, rc)
        answer = json.loads(out)
        self.assertEqual(2, answer["count"])
        self.assertEqual([1, 2], [d["id"] for d in answer["displays"]])

    def test_no_displays_at_all_is_a_valid_answer(self):
        rc, out = self.displays(FakeCG([]))
        self.assertEqual(0, rc)
        self.assertEqual({"count": 0, "displays": []}, json.loads(out))

    def test_a_display_list_it_cannot_read_prints_nothing(self):
        self.assertEqual((1, ""), self.displays(None))
        self.assertEqual((1, ""), self.displays(FakeCG([PANEL], err=1000)))

    def test_brightness_is_null_when_display_services_cannot_be_loaded(self):
        rc, out = self.displays(FakeCG([PANEL]), ds=None)
        self.assertEqual(0, rc)
        self.assertIsNone(json.loads(out)["displays"][0]["brightness"])

    @unittest.skipIf(platform.system() == "Darwin",
                     "this machine is a Mac: CoreGraphics loads here")
    def test_the_subcommand_exits_1_printing_nothing_off_a_mac(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wk" / "mac.py"), "displays"],
                            capture_output=True, text=True)
        self.assertEqual(1, cp.returncode)
        self.assertEqual("", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "needs a Mac")
    def test_a_real_window_server_answers_with_that_shape(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wk" / "mac.py"), "displays"],
                            capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)
        answer = json.loads(cp.stdout)
        self.assertEqual(answer["count"], len(answer["displays"]))
        for row in answer["displays"]:
            self.assertEqual(ROW_KEYS, set(row))
            self.assertEqual(2, len(row["points"]))


class WkmacBrightness(WkmacHandles):
    def test_it_reads_the_builtin_panel(self):
        rc, out = self.brightness(FakeCG([PANEL]))
        self.assertEqual(0, rc)
        self.assertEqual(0.4375, float(out))

    def test_a_set_prints_the_value_read_back(self):
        ds = FakeDS()
        rc, out = self.brightness(FakeCG([PANEL]), ds=ds, value=0.0)
        self.assertEqual(0, rc)
        self.assertEqual(0.0, float(out))
        self.assertEqual(0.0, ds.value)

    def test_each_reading_or_set_it_cannot_stand_behind_is_refused_printing_nothing(self):
        for name, displays, ds, value in (("unreadable", [PANEL], FakeDS(get_rc=1), None),
                                          ("two displays", [PANEL, EXTERNAL], UNSET, None),
                                          ("no builtin", [EXTERNAL], UNSET, None), ("none", [], UNSET, None),
                                          ("did not take", [PANEL], FakeDS(sticks=False), 0.0),
                                          ("rejected", [PANEL], FakeDS(set_rc=-1), 0.0),
                                          ("fixed panel", [PANEL], FakeDS(can_change=False), 0.0)):
            with self.subTest(case=name):
                self.assertEqual((1, ""), self.brightness(FakeCG(displays), ds=ds, value=value))

    def test_a_value_outside_0_to_1_is_a_usage_error(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wk" / "mac.py"),
                             "brightness", "--set", "2.0"],
                            capture_output=True, text=True)
        self.assertEqual(2, cp.returncode)
        self.assertIn("fraction from 0.0 to 1.0", cp.stderr)


GOOD = {
    "accelerator": "AGXAcceleratorG14X",
    "renderer": "Apple M3 Pro",
    "webgl": "WebGL 2.0",
    "raf_hz": 57.2,
    "screen": [1470, 956],
    "dpr": 2,
    "focused": True,
    "frontmost": "org.webkit.MiniBrowser",
    "brightness": 0.0,
    "displays": [dict(PANEL, brightness=0.0)],
    "webkit_gpu_clients": {"1732": "com.apple.WebKit.GPU"},
}
EXPECT = "builtin 1470x956"


class TheScreenTheReadingWasTakenOn(WkTest):
    def check(self, expect=EXPECT, **overrides):
        reading = dict(GOOD, **overrides)
        path = self.tmp / "reading.json"
        path.write_text(json.dumps(reading))
        argv = [sys.executable, str(REPO / "bench" / "mac-browser-check.py"),
                "--read", str(path)]
        if expect is not None:
            argv += ["--expect-display", expect]
        return subprocess.run(argv, capture_output=True, text=True)

    def assertFault(self, phrase, **overrides):
        cp = self.check(**overrides)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn(phrase, cp.stderr)

    def test_a_reading_on_the_declared_display_raises_nothing(self):
        cp = self.check()
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual("", cp.stderr)

    def test_a_window_that_is_not_frontmost_is_refused(self):
        self.assertFault("not org.webkit.MiniBrowser", frontmost="com.apple.Terminal")

    def test_a_machine_without_pyobjc_is_refused(self):
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            with self.assertRaises(SystemExit) as cm:
                BROWSER.frontmost_bundle()
        self.assertIn("pyobjc", str(cm.exception))

    def test_brightness_is_recorded_and_not_judged(self):
        cp = self.check(brightness=0.9, displays=[dict(PANEL, brightness=0.9)])
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_a_page_that_does_not_have_the_focus_is_refused(self):
        self.assertFault("did not have the focus", focused=False)

    def test_focus_and_frontmost_answer_different_questions(self):
        cp = self.check(focused=False)
        self.assertNotIn("org.webkit.MiniBrowser: ", cp.stderr)
        self.assertIn("focused=False", cp.stdout)

    def test_the_screen_reading_alone_decides_nothing(self):
        cp = self.check(screen=[0, 0])
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_the_new_readings_reach_the_log(self):
        cp = self.check()
        self.assertIn("frontmost=org.webkit.MiniBrowser", cp.stdout)
        self.assertIn("brightness=0.0", cp.stdout)
        self.assertIn("displays=count=1 builtin=1 points=[1470, 956] "
                      "mirrored=False asleep=True", cp.stdout)

    def test_no_expectation_records_the_display_and_judges_nothing_on_it(self):
        cp = self.check(expect=None,
                        displays=[dict(EXTERNAL, mirrored=True, points=[800, 600])])
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("displays=count=1 builtin=None points=None mirrored=True", cp.stdout)

    def test_no_expectation_still_judges_everything_else(self):
        cp = self.check(expect=None, focused=False, frontmost="com.apple.Terminal",
                        raf_hz=8.0, displays=[dict(EXTERNAL, points=[800, 600])])
        self.assertEqual(1, cp.returncode)
        for phrase in ("did not have the focus", "not org.webkit.MiniBrowser", "throttle"):
            self.assertIn(phrase, cp.stderr)
        self.assertNotIn("built-in panel", cp.stderr)

    def test_a_stored_reading_that_records_one_is_judged_against_it(self):
        script = str(REPO / "bench" / "mac-browser-check.py")
        src, out = self.tmp / "in.json", self.tmp / "out.json"
        src.write_text(json.dumps(dict(GOOD)))
        cp = subprocess.run([sys.executable, script, "--read", str(src),
                             "--expect-display", EXPECT, "--json", str(out)],
                            capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(EXPECT, json.loads(out.read_text())["expect_display"])
        again = subprocess.run([sys.executable, script, "--read", str(out)],
                               capture_output=True, text=True)
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertNotIn("display=not judged", again.stdout)

    def test_a_malformed_expectation_is_refused(self):
        for spec in ("1470x956", "builtin", "builtin 1470",
                     "builtin 1470x", "builtin AxB", "builtin any", "any", "any 1470x956"):
            with self.subTest(spec=spec):
                cp = self.check(expect=spec)
                self.assertEqual(2, cp.returncode)
                self.assertIn("<kind> <w>x<h>", cp.stderr)

    def test_a_panel_that_is_not_a_built_in_one_is_declarable(self):
        self.assertEqual(("external", [1470, 956]),
                         BROWSER.parse_expect_display("external 1470x956"))
        cp = self.check(expect="external 1470x956",
                        displays=[dict(EXTERNAL, points=[1470, 956])])
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_the_built_in_panel_is_refused_where_an_external_one_is_declared(self):
        """The discriminating half: the kind is judged, not merely recorded."""
        cp = self.check(expect="external 1470x956", displays=[dict(PANEL)])
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("is not the external panel", cp.stderr)

    @unittest.skipIf(platform.system() == "Darwin",
                     "this machine is a Mac: CoreGraphics loads here")
    def test_a_display_list_nothing_could_answer_reads_as_none(self):
        self.assertIsNone(BROWSER.display_list())


class TakeReadingNeverLeavesTheBrowserRunning(WkTest):

    def args(self, timeout=0):
        return argparse.Namespace(build_directory="/nonexistent", timeout=timeout)

    def test_no_pyobjc_refuses_before_the_browser_is_ever_launched(self):
        with mock.patch.dict(sys.modules, {"AppKit": None}), \
             mock.patch.object(BROWSER, "accelerator_clients", return_value=(None, {})), \
             mock.patch.object(BROWSER, "launch") as launch:
            with self.assertRaises(SystemExit) as cm:
                BROWSER.take_reading(self.args())
        launch.assert_not_called()
        self.assertIn("pyobjc", str(cm.exception))

    def test_a_refusal_after_launch_still_terminates_the_browser(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.dict(sys.modules, {"AppKit": mock.MagicMock()}), \
             mock.patch.object(BROWSER, "accelerator_clients", return_value=(None, {})), \
             mock.patch.object(BROWSER, "launch", return_value=proc), \
             mock.patch.object(BROWSER, "frontmost_bundle", side_effect=SystemExit(BROWSER.PYOBJC_MISSING)), \
             mock.patch.object(BROWSER, "display_list", return_value=None):
            with self.assertRaises(SystemExit):
                BROWSER.take_reading(self.args())
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()


class TheDisplayRuleAskedOnItsOwn(WkTest):

    def test_it_needs_neither_a_build_nor_a_reading(self):
        cp = subprocess.run([sys.executable, str(REPO / "bench" / "mac-browser-check.py")],
                            capture_output=True, text=True)
        self.assertEqual(2, cp.returncode)
        self.assertIn("--displays-only", cp.stderr)

    def test_it_judges_the_display_and_nothing_about_a_browser(self):
        found = BROWSER.display_faults([dict(PANEL)],
                                       BROWSER.parse_expect_display(EXPECT), True)
        self.assertEqual([], found)
        full = BROWSER.faults({"displays": [dict(PANEL)]}, {}, None, 30.0,
                              BROWSER.parse_expect_display(EXPECT))
        self.assertTrue([f for f in full if "WebGL" in f], full)

    def test_the_same_faults_are_raised_as_with_a_browser(self):
        expect = BROWSER.parse_expect_display(EXPECT)
        for name, displays, phrase in (
                ("two panels", [dict(PANEL), dict(EXTERNAL)], "online, not one"),
                ("not builtin", [dict(EXTERNAL)], "not the builtin panel"),
                ("mirrored", [dict(PANEL, mirrored=True)], "mirror set"),
                ("wrong mode", [dict(PANEL, points=[1280, 832])], "points, not"),
                ("unreadable", None, "could not be read")):
            with self.subTest(case=name):
                found = BROWSER.display_faults(displays, expect, True)
                self.assertTrue([f for f in found if phrase in f], found)

    def test_ambient_light_is_judged_with_no_expectation_at_all(self):
        lit = [dict(PANEL, auto_brightness=True)]
        for expect in (None, BROWSER.parse_expect_display(EXPECT)):
            with self.subTest(expect=expect):
                found = BROWSER.display_faults(lit, expect, expect is not None)
                self.assertTrue([f for f in found if "ambient-light" in f], found)


class AmbientLightControl(WkTest):

    def check(self, auto, **overrides):
        panel = dict(PANEL, auto_brightness=auto)
        reading = dict(GOOD, displays=[panel], **overrides)
        path = self.tmp / "reading.json"
        path.write_text(json.dumps(reading))
        return subprocess.run(
            [sys.executable, str(REPO / "bench" / "mac-browser-check.py"),
             "--read", str(path), "--expect-display", EXPECT],
            capture_output=True, text=True)

    def test_auto_brightness_on_is_refused(self):
        cp = self.check(True)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("ambient-light control", cp.stderr)

    def test_auto_brightness_off_raises_nothing(self):
        cp = self.check(False)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_a_panel_with_no_sensor_to_ask_is_reported_not_refused(self):
        cp = self.check(None)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("auto_brightness=None", cp.stdout)

    def test_the_verb_that_holds_it_off_reads_it_back(self):
        ds = FakeDS(als=True)
        rc, out = WkmacHandles.call(self, WKMAC.cmd_auto_brightness,
                                    FakeCG([PANEL]), ds, off=True)
        self.assertEqual(0, rc, out)
        self.assertEqual("off\n", out)
        self.assertFalse(ds.als)

    def test_a_panel_that_ignores_the_write_exits_nonzero(self):
        rc, out = WkmacHandles.call(self, WKMAC.cmd_auto_brightness,
                                    FakeCG([PANEL]), FakeDS(als=True, als_sticks=False),
                                    off=True)
        self.assertEqual(1, rc, out)
        self.assertEqual("on\n", out)

    def test_a_panel_with_no_sensor_answers_none_and_is_not_a_refusal(self):
        rc, out = WkmacHandles.call(self, WKMAC.cmd_auto_brightness,
                                    FakeCG([PANEL]), FakeDS(has_als=False), off=True)
        self.assertEqual(0, rc, out)
        self.assertEqual("none\n", out)

    def test_reading_it_without_off_changes_nothing(self):
        ds = FakeDS(als=True)
        rc, out = WkmacHandles.call(self, WKMAC.cmd_auto_brightness,
                                    FakeCG([PANEL]), ds, off=False)
        self.assertEqual(0, rc, out)
        self.assertEqual("on\n", out)
        self.assertTrue(ds.als, "a read turned it off")

if __name__ == "__main__":
    unittest.main()
