"""The display readings a Mac benchmark is judged against: `lib/wkmac.py
displays` / `brightness`, and the faults bench/mac-browser-check.py raises when
the screen it is measuring on is not the declared one.

The CoreGraphics and DisplayServices calls are driven through fakes standing in
for the two library handles, so every refusal is exercised on any host; the two
tests that ask a real window server skip unless this machine is a Mac. Nothing
here sets a brightness on a real display.

Run: python3 -m unittest tests.test_mac_display -v
"""
import importlib.util
import io
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


WKMAC = load(REPO / "lib" / "wkmac.py", "wkmac")
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
            "points", "vendor", "model", "unit", "brightness"}


class FakeCG:
    """The CoreGraphics handle `_coregraphics()` returns, answering out of a
    list of display dicts. The symbol names come from wkmac's own maps, so a
    renamed call fails here rather than passing against a stale fake."""

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
    """The DisplayServices handle: a brightness that can be read, set, and made
    to refuse a set or to ignore one."""

    def __init__(self, value=0.4375, can_change=True, set_rc=0, get_rc=0, sticks=True):
        self.value = value
        self.can_change = can_change
        self.set_rc = set_rc
        self.get_rc = get_rc
        self.sticks = sticks

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

    def test_coregraphics_that_cannot_be_loaded_prints_nothing(self):
        rc, out = self.displays(None)
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_display_list_call_that_errors_prints_nothing(self):
        rc, out = self.displays(FakeCG([PANEL], err=1000))
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_brightness_is_null_when_display_services_cannot_be_loaded(self):
        rc, out = self.displays(FakeCG([PANEL]), ds=None)
        self.assertEqual(0, rc)
        self.assertIsNone(json.loads(out)["displays"][0]["brightness"])

    @unittest.skipIf(platform.system() == "Darwin",
                     "this machine is a Mac: CoreGraphics loads here")
    def test_the_subcommand_exits_1_printing_nothing_off_a_mac(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wkmac.py"), "displays"],
                            capture_output=True, text=True)
        self.assertEqual(1, cp.returncode)
        self.assertEqual("", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "needs a Mac")
    def test_a_real_window_server_answers_with_that_shape(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wkmac.py"), "displays"],
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

    def test_a_brightness_that_cannot_be_read_prints_nothing(self):
        rc, out = self.brightness(FakeCG([PANEL]), ds=FakeDS(get_rc=1))
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_more_than_one_online_display_is_refused(self):
        """Which display to dim is not a guess."""
        rc, out = self.brightness(FakeCG([PANEL, EXTERNAL]))
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_machine_with_no_builtin_display_is_refused(self):
        rc, out = self.brightness(FakeCG([EXTERNAL]))
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_no_display_at_all_is_refused(self):
        rc, out = self.brightness(FakeCG([]))
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_set_prints_the_value_read_back(self):
        ds = FakeDS()
        rc, out = self.brightness(FakeCG([PANEL]), ds=ds, value=0.0)
        self.assertEqual(0, rc)
        self.assertEqual(0.0, float(out))
        self.assertEqual(0.0, ds.value)

    def test_a_set_that_did_not_take_is_refused(self):
        """The autorun halts the machine on this: a panel still lit is a panel
        whose compositing the benchmark pays for."""
        rc, out = self.brightness(FakeCG([PANEL]), ds=FakeDS(sticks=False), value=0.0)
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_set_the_framework_rejected_is_refused(self):
        rc, out = self.brightness(FakeCG([PANEL]), ds=FakeDS(set_rc=-1), value=0.0)
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_panel_that_cannot_change_brightness_is_refused(self):
        rc, out = self.brightness(FakeCG([PANEL]), ds=FakeDS(can_change=False), value=0.0)
        self.assertEqual(1, rc)
        self.assertEqual("", out)

    def test_a_value_outside_0_to_1_is_a_usage_error(self):
        cp = subprocess.run([sys.executable, str(REPO / "lib" / "wkmac.py"),
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

    def test_a_second_online_display_is_refused(self):
        self.assertFault("displays are online, not one",
                         displays=[dict(PANEL), dict(EXTERNAL)])

    def test_the_one_display_not_being_the_builtin_one_is_refused(self):
        self.assertFault("is not the built-in panel", displays=[dict(EXTERNAL)])

    def test_a_mirror_set_is_refused(self):
        self.assertFault("mirror set", displays=[dict(PANEL, mirrored=True)])

    def test_a_display_mode_other_than_the_declared_one_is_refused(self):
        """The defect this exists for: run-benchmark sizes its window from the
        screen, so 1280x832 and 1470x956 are two different measurements."""
        self.assertFault("points, not [1470, 956]",
                         displays=[dict(PANEL, points=[1280, 832])])

    def test_a_display_list_that_could_not_be_read_is_refused(self):
        self.assertFault("display list could not be read", displays=None)

    def test_a_window_that_is_not_frontmost_is_refused(self):
        self.assertFault("not org.webkit.MiniBrowser", frontmost="com.apple.Terminal")

    def test_a_machine_without_pyobjc_is_refused(self):
        self.assertFault("pyobjc", frontmost="?")

    def test_brightness_is_recorded_and_not_judged(self):
        """`wkmac.py brightness --set 0` verifies its own read-back; a second
        judgement here could drift from that one."""
        cp = self.check(brightness=0.9, displays=[dict(PANEL, brightness=0.9)])
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)

    def test_a_page_that_does_not_have_the_focus_is_refused(self):
        """Measured over six browser-check readings on the bench install
        (2026-09-07): five read focused=True and one read False, so True is the
        healthy population and the raiser does take."""
        self.assertFault("did not have the focus", focused=False)

    def test_focus_and_frontmost_answer_different_questions(self):
        """Which application is active is not whether the measured window is
        key, so a reading can fail one and pass the other."""
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
        """One branch, not a third mode: a PGO collection trains a profile
        rather than producing a number, and a reading stored before the pin
        existed is history — both name no expectation, and the display is
        recorded for a reader without being compared against anything."""
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
        """What a reading was judged against travels in it, so re-deriving the
        verdict later reaches the same one with no argument."""
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
        for spec in ("1470x956", "builtin", "builtin 1470", "external 1470x956",
                     "builtin 1470x", "builtin AxB", "builtin any", "any", "any 1470x956"):
            with self.subTest(spec=spec):
                cp = self.check(expect=spec)
                self.assertEqual(2, cp.returncode)
                self.assertIn("builtin <w>x<h>", cp.stderr)

    def test_the_display_list_is_read_through_the_wkmac_subcommand(self):
        self.assertEqual(str(REPO / "lib" / "wkmac.py"), BROWSER.WKMAC)

    @unittest.skipIf(platform.system() == "Darwin",
                     "this machine is a Mac: CoreGraphics loads here")
    def test_a_display_list_nothing_could_answer_reads_as_none(self):
        self.assertIsNone(BROWSER.display_list())


if __name__ == "__main__":
    unittest.main()
