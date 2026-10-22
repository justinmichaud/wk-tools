"""The display a benchmark runs on: `lib/wk/mac.py displays` / `brightness`, the faults bench/mac-browser-check.py
raises when the screen is not the declared one, and a macOS guest's desktop and load as `wk doctor <guest>` reads them."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import platform
import plistlib
import re
import subprocess
import sys
import types
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from unittest import mock

from tests.fakes import BenchHere, Fake, FakeClock
from tests.support import REPO, WkTest, live_selected, repo_files

sys.path.insert(0, str(REPO / "lib"))
from wk import doctor, guest, places  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.store import Store  # noqa: E402


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


WKMAC = load(REPO / "lib" / "wk" / "mac.py", "wkmac")
BROWSER = load(REPO / "bench" / "mac-browser-check.py", "mac_browser_check")


def py(script, *argv):
    """`script` (lib/wk/mac.py or bench/mac-browser-check.py) run as its own process."""
    return subprocess.run([sys.executable, str(REPO / script)] + [str(a) for a in argv], capture_output=True, text=True)


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
                 has_als=True, als=False, als_sticks=True):
        self.value, self.can_change, self.set_rc, self.get_rc, self.sticks = value, can_change, set_rc, get_rc, sticks
        self.has_als, self.als, self.als_sticks = has_als, als, als_sticks

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
        out.contents.value = self.als
        return 0

    def DisplayServicesEnableAmbientLightCompensation(self, ident, enable):
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
        self.assertEqual((0, "1280x832\n"), self.mode(FakeCG([dict(PANEL, points=[1280, 832])])))

    def test_declaring_a_mode_rewrites_that_panels_size_in_every_row_and_nothing_else(self):
        config = self._config(wide=1470, high=956)
        rc, out = self.mode(FakeCG([PANEL]), declare=(1280, 832), config=config)
        self.assertEqual(0, rc, out)
        self.assertEqual("1280x832", out.strip())
        rows = self._rows(config, self.UUID)
        self.assertEqual(4, len(rows), rows)   # two Configs x CurrentInfo + UnmirrorInfo
        for row in rows:
            self.assertEqual((1280, 832, 2, 60.0, 8), (row["Wide"], row["High"], row["Scale"], row["Hz"], row["Depth"]))
        other = self._rows(config, "AN-EXTERNAL-PANEL")
        self.assertEqual(2, len(other), other)
        for row in other:
            self.assertEqual((1920, 1080, 1), (row["Wide"], row["High"], row["Scale"]))

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
        answer = json.loads(out)
        self.assertEqual((0, 1), (rc, answer["count"]))
        row = answer["displays"][0]
        self.assertEqual(ROW_KEYS, set(row))
        self.assertEqual(([1470, 956], True, False, 0.4375), (row["points"], row["builtin"], row["mirrored"], row["brightness"]))

    def test_a_second_display_is_listed_too_and_none_at_all_is_a_valid_answer(self):
        for displays, ids in (([PANEL, EXTERNAL], [1, 2]), ([], [])):
            with self.subTest(ids=ids):
                rc, out = self.displays(FakeCG(displays))
                answer = json.loads(out)
                self.assertEqual((0, len(ids), ids), (rc, answer["count"], [d["id"] for d in answer["displays"]]))

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
        cp = py("lib/wk/mac.py", "displays")
        self.assertEqual(1, cp.returncode)
        self.assertEqual("", cp.stdout)

    @unittest.skipUnless(platform.system() == "Darwin", "needs a Mac")
    def test_a_real_window_server_answers_with_that_shape(self):
        cp = py("lib/wk/mac.py", "displays")
        self.assertEqual(0, cp.returncode, cp.stderr)
        answer = json.loads(cp.stdout)
        self.assertEqual(answer["count"], len(answer["displays"]))
        for row in answer["displays"]:
            self.assertEqual(ROW_KEYS, set(row))
            self.assertEqual(2, len(row["points"]))


class WkmacBrightness(WkmacHandles):
    def test_it_reads_the_builtin_panel_and_a_set_prints_the_value_read_back(self):
        rc, out = self.brightness(FakeCG([PANEL]))
        self.assertEqual((0, 0.4375), (rc, float(out)))
        ds = FakeDS()
        rc, out = self.brightness(FakeCG([PANEL]), ds=ds, value=0.0)
        self.assertEqual((0, 0.0, 0.0), (rc, float(out), ds.value))

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
        cp = py("lib/wk/mac.py", "brightness", "--set", "2.0")
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
        return py("bench/mac-browser-check.py", "--read", path, *(["--expect-display", expect] if expect is not None else []))

    def assertFault(self, phrase, **overrides):
        cp = self.check(**overrides)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn(phrase, cp.stderr)

    def test_what_is_recorded_and_not_judged_raises_nothing(self):
        for why, over in (("the declared display", {}), ("brightness", {"brightness": 0.9, "displays": [dict(PANEL, brightness=0.9)]}),
                          ("the screen reading", {"screen": [0, 0]}),
                          ("a panel with no ambient-light sensor", {"displays": [dict(PANEL, auto_brightness=None)]}),
                          ("an external panel declared", {"expect": "external 1470x956", "displays": [dict(EXTERNAL, points=[1470, 956])]})):
            with self.subTest(why):
                cp = self.check(**over)
                self.assertEqual((0, ""), (cp.returncode, cp.stderr), cp.stdout)

    def test_a_window_that_is_not_frontmost_or_not_focused_is_refused_each_for_its_own_reason(self):
        self.assertFault("not org.webkit.MiniBrowser", frontmost="com.apple.Terminal")
        cp = self.check(focused=False)
        self.assertEqual(1, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("did not have the focus", cp.stderr)
        self.assertNotIn("org.webkit.MiniBrowser: ", cp.stderr)
        self.assertIn("focused=False", cp.stdout)

    def test_a_machine_without_pyobjc_is_refused(self):
        with mock.patch.dict(sys.modules, {"AppKit": None}):
            with self.assertRaises(SystemExit) as cm:
                BROWSER.frontmost_bundle()
        self.assertIn("pyobjc", str(cm.exception))

    def test_the_new_readings_reach_the_log(self):
        cp = self.check()
        self.assertIn("frontmost=org.webkit.MiniBrowser", cp.stdout)
        self.assertIn("auto_brightness=None", cp.stdout)
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
        src, out = self.tmp / "in.json", self.tmp / "out.json"
        src.write_text(json.dumps(dict(GOOD)))
        cp = py("bench/mac-browser-check.py", "--read", src, "--expect-display", EXPECT, "--json", out)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertEqual(EXPECT, json.loads(out.read_text())["expect_display"])
        again = py("bench/mac-browser-check.py", "--read", out)
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)
        self.assertNotIn("display=not judged", again.stdout)

    def test_a_malformed_expectation_is_refused(self):
        for spec in ("1470x956", "builtin", "builtin 1470",
                     "builtin 1470x", "builtin AxB", "builtin any", "any", "any 1470x956"):
            with self.subTest(spec=spec):
                cp = self.check(expect=spec)
                self.assertEqual(2, cp.returncode)
                self.assertIn("<kind> <w>x<h>", cp.stderr)

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
        cp = py("bench/mac-browser-check.py")
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


class AmbientLightControl(WkmacHandles):
    def test_the_verb_reads_it_and_with_off_holds_it_off_and_reads_it_back(self):
        for why, ds, off, rc, out, after in (("held off", FakeDS(als=True), True, 0, "off\n", False),
                                             ("ignored the write", FakeDS(als=True, als_sticks=False), True, 1, "on\n", True),
                                             ("no sensor", FakeDS(has_als=False), True, 0, "none\n", False),
                                             ("a read", FakeDS(als=True), False, 0, "on\n", True)):
            with self.subTest(why):
                self.assertEqual((rc, out), self.call(WKMAC.cmd_auto_brightness, FakeCG([PANEL]), ds, off=off))
                self.assertEqual(after, ds.als)


REBUILD = guest.BASE_BUILD + " --rebuild"


# A real reading of a settled guest with every table row in force.
PYOBJC_VERSION = re.search(
    r'WK_PYOBJC_VERSION="\$\{WK_PYOBJC_VERSION:-([^}]*)\}"',
    (REPO / "bench" / "mac-pyobjc.sh").read_text()).group(1)


SETTLED = f"""console_user=admin
pyobjc={PYOBJC_VERSION}
screenlock=off
widgets_desktop=1
widgets_stage=1
reduce_motion=1
reduce_transparency=1
appnap=1
window_anim=0
askforpassword=0
askforpassworddelay=0
idletime=0
desktop_icons=0
dock_launchanim=0
dock_recents=0
crash_dialog=none
quarantine=0
timemachine_offer=1
personalised_ads=0
widgets_agent=off
notifications=off
notification_daemon=off
spotlight_menu=off
siri=off
siri_knowledge=off
siri_inference=off
suggestions=off
spotlight_suggestions=off
knowledge=off
proactive=off
photo_analysis=off
media_analysis=off
icloud_drive=off
icloud_photos=off
music_library=off
screentime=off
usage_tracking=off
experiments=off
sharing=off
tips=off
spotlight_content=running
softwareupdate=running
softwareupdate_helper=running
malware_scan=absent
malware_service=absent
malware_daemon=running
timemachine=running
timemachine_helper=running
analytics_daemon=running
analytics_helper=running
diagnostics=absent
crash_reporter=running
hang_sampler=running
power_records=absent
process_stats=running
icloud=running
icloud_defaults=running
downloads=running
findmy=running
experiments_system=running
power_displaysleep=0
power_disksleep=0
power_sleep=0
power_disablesleep=1
power_lowpowermode=
power_highpowermode=
spotlight=Indexing disabled.
analytics=0
power_source=AC Power
cpu_speed_limit=
setupassistant_pending=
update_check=0
update_download=0
update_download_system=0
update_autoinstall_system=0
setupassistant_seen_product=26.4
os_product=26.4
frontapp=com.apple.Finder
windows=Notification Center:21:1024x768@0,0;Dock:20:1024x768@0,0;Terminal:0:863x499@40,51;
securityagent=down
user=admin
"""


# A real reading of a clone behind Setup Assistant with the screen saver armed; a key it lacks is unknown, not settled.
AS_FOUND = """console_user=admin
screenlock=off
widgets_desktop=?
widgets_stage=?
reduce_motion=?
reduce_transparency=?
appnap=?
askforpassword=0
askforpassworddelay=0
idletime=?
widgets_agent=on
notifications=on
spotlight=Indexing enabled.
displaysleep=0
setupassistant_pending=DidSeeTrueTone DidSeeSyncSetup
update_check=?
update_download=?
windows=Setup Assistant:0:800x600;Setup Assistant:-1:1417x805;Notification Center:21:1417x805;Terminal:0:863x499;
securityagent=down
user=admin
"""


LOGIN_WINDOW = SETTLED.replace("console_user=admin", "console_user=root")


# A base that never turned Software Update off, and a Setup Assistant that has not seen this macOS.
UPDATE_ON = (SETTLED
             .replace("update_download_system=0", "update_download_system=1")
             .replace("update_autoinstall_system=0", "update_autoinstall_system=1")
             .replace("setupassistant_seen_product=26.4",
                      "setupassistant_seen_product=26.3"))


def readings(probe):
    """What a start and `wk doctor <guest>` make of a captured probe: this tree's libraries run for real."""
    return guest.desktop_readings(str(REPO), BenchHere(), types.SimpleNamespace(run=lambda argv, input=None: Result(0, probe)))


def rows(probe):
    return doctor.desktop_rows(readings(probe), "demo")


class TestTheDesktopRows(unittest.TestCase):
    def test_a_settled_guest_misses_nothing(self):
        self.assertEqual([], [r for r in rows(SETTLED) if r[0] == doctor.MISS], rows(SETTLED))

    def test_each_fault_is_a_miss(self):
        for probe, what in ([(AS_FOUND, w) for w in ("screen saver", "Setup Assistant will put a modal pane", "nothing wk runs put it there")]
                            + [(UPDATE_ON, w) for w in ("install themselves", "download themselves", "what is new in macOS")]
                            + [(LOGIN_WINDOW, "nobody is logged in")]):
            with self.subTest(what):
                self.assertTrue([r for r in rows(probe) if r[0] == doctor.MISS and what in r[1]], rows(probe))

    def test_what_the_guest_did_not_say_is_unknown_not_ok(self):
        self.assertEqual([doctor.UNK], [r[0] for r in rows(AS_FOUND) if r[1].startswith("Software Update")][:1])
        blind = "\n".join("windows=?" if l.startswith("windows=") else l for l in SETTLED.splitlines())
        self.assertEqual([doctor.UNK], [r[0] for r in rows(blind) if "window server" in r[1]])

    def test_a_pane_over_the_window_is_named_with_its_size_and_cleared_on_the_base(self):
        (named,) = [r for r in rows(AS_FOUND) if "nothing wk runs put it there" in r[1]]
        self.assertIn("Setup Assistant:0:800x600", named[1])
        self.assertNotIn("Notification Center", named[1])
        self.assertIn(guest.BASE_BUILD, named[2])

    def test_only_what_no_clone_can_clear_blocks_a_start(self):
        self.assertEqual([], guest.blockers(readings(SETTLED)))
        self.assertEqual([], guest.blockers(readings(SETTLED.replace("pyobjc=" + PYOBJC_VERSION, "pyobjc=9.0"))))
        self.assertEqual(1, len(guest.blockers(readings(LOGIN_WINDOW))))


def _load_sample(shells=40, free_pct=6, swap_used="4096.00M"):
    """A guest a fortnight up with an editor attached."""
    rows = ["proc=24880 /sbin/launchd", "proc=1048576 /System/Library/Frameworks/WebKit.framework/jsc",
            "proc=512000 /Users/admin/.zed_server/stable-0.1/zed-remote-server",
            "proc=221000 /Users/admin/.local/share/claude/versions/2.0.1/claude",
            "proc=61000 /usr/sbin/sshd-session", "proc=61000 /usr/sbin/sshd-session"] + ["proc=8192 /bin/zsh"] * shells
    return "\n".join(["mem_total_mb=32768", "mem_free_pct=%d" % free_pct,
                      "swapusage=total = 8192.00M  used = %s  free = 4096.00M  (encrypted)" % swap_used] + rows) + "\n"


IDLE_SAMPLE = _load_sample(shells=2, free_pct=71, swap_used="0.00M")


class TestWhatIsResidentInThere(unittest.TestCase):
    def test_an_accumulation_low_memory_and_swap_are_reported_with_the_culprits(self):
        f = doctor.load_rows(_load_sample(), "demo")
        for state, what in ((doctor.MISS, "40 shells"), (doctor.MISS, "editor remote server"), (doctor.MISS, "6%"), (doctor.MISS, "jsc"),
                            (doctor.NOTE, "4096 MB of swap")):
            with self.subTest(what):
                self.assertTrue([x for x in f if x[0] == state and what in x[1]], f)

    def test_an_idle_guest_is_all_ok(self):
        f = doctor.load_rows(IDLE_SAMPLE, "demo")
        self.assertEqual({x[0] for x in f}, {doctor.OK}, f)
        self.assertTrue(any("2 shells resident" in x[1] for x in f))

    def test_nothing_wk_runs_in_a_guest_leaves_a_shell_behind(self):
        with mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"):
            argv = places.Vm("vm", str(REPO), {}, Fake()).guest_of("wk-demo").argv("true")
        self.assertEqual(["/t/tart", "exec"], argv[:2])
        self.assertNotIn("ControlPersist", " ".join(argv))


class GuestAt(BenchHere):
    """This host with one guest behind tart exec, answering each probe streamed in with the capture it is given."""

    def __init__(self, desktop, load=None):
        super().__init__()
        self.desktop, self.load, self.inputs = desktop, load, []
        self.react(["/t/tart", "exec"], self._guest)

    def run(self, argv, input=None, timeout=None):
        self.last = input or ""
        return super().run(argv, input=input, timeout=timeout)

    def _guest(self, argv, _):
        if argv[-1] != "bash -s":
            return Result(0)
        if "mem_total_mb" in self.last:
            return Result(0, self.load) if self.load else Result(255, "", "Connection closed")
        return Result(0, self.desktop) if self.desktop else Result(255, "", "Connection closed")


class ReportTest(unittest.TestCase):
    def setUp(self):
        for p in (mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True),
                  mock.patch.object(places.Vm, "tart", lambda s: "/t/tart"), mock.patch.dict(os.environ, {}, clear=False)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("WK_DRY_RUN", None)
        self.env = {"HOME": "/h", "WK_STORE": "/st", "WK_VM_STORE": "/vs", "XDG_STATE_HOME": "/h/st"}

    def vm(self, here):
        return places.Registry(str(REPO), env=self.env, machine=here).load("vm")

    def report(self, sample):
        """(started, stdout, stderr) of the start's last step against a guest answering `sample`."""
        vm = self.vm(GuestAt(sample))
        g = guest.Guest(guest.Host(vm, FakeClock()), "demo", vm.guest_of("wk-demo"))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ok = g.report_desktop()
            except Refused:
                ok = False
        return ok, out.getvalue(), err.getvalue()


class TestTheStartReport(ReportTest):
    def test_every_finding_reaches_the_console_with_its_remedy(self):
        _, _, err = self.report(AS_FOUND)
        for what in ("the screen saver is disarmed", "Setup Assistant", "nothing wk runs put it there", "wk doctor demo", REBUILD):
            self.assertIn(what, err)

    def test_a_settled_guest_is_handed_over_and_still_gets_the_report_on_stderr(self):
        ok, out, err = self.report(SETTLED)
        self.assertTrue(ok, err)
        self.assertEqual("", out, "a start's stdout is the guest's address")
        self.assertIn("screen lock off", err)
        self.assertIn("com.apple.Finder has the focus", err)

    def test_a_guest_that_does_not_answer_does_not_fail_the_start(self):
        self.assertTrue(self.report(None)[0])

    def test_a_guest_behind_a_pane_or_with_nobody_at_the_window_is_refused(self):
        for sample, why in ((AS_FOUND, "Setup Assistant"), (LOGIN_WINDOW, "nobody is logged in")):
            ok, _, err = self.report(sample)
            self.assertFalse(ok)
            self.assertIn("not usable", err)
            self.assertIn(why, err)
            self.assertIn(REBUILD, err)

    def test_the_refusal_is_crossed_by_a_variable_that_says_so(self):
        self.env["WK_VM_FORCE"] = "1"
        ok, _, err = self.report(AS_FOUND)
        self.assertTrue(ok)
        self.assertIn("WK_VM_FORCE=1", err)

    def test_the_report_follows_the_settle(self):
        steps = [s[0] for s in guest.STEPS]
        self.assertLess(steps.index("settle_desktop"), steps.index("report_desktop"))

    def test_one_renderer_for_every_report(self):
        self.assertEqual([REPO / "lib" / "common.sh"],
                         [f for f in sorted(repo_files()) if f.suffix != ".py" and "render_findings() {" in f.read_text(errors="replace")])


class TestTheDoctorOfAGuest(ReportTest):
    def rows(self, desktop_sample, load=None):
        here = GuestAt(desktop_sample, load)
        here.answer(["/t/tart", "list"], out='[{"Name": "wk-demo", "Source": "local", "State": "running"}]')
        here.answer(["/t/tart", "ip"], out="10.0.0.2\n")
        return doctor.guest_rows(self.vm(here), "demo")

    def test_a_good_guest_has_all_three_reports(self):
        text = "\n".join(r[1] for r in self.rows(SETTLED, IDLE_SAMPLE))
        for what in ("golden base", "screen lock off", "shells resident"):
            self.assertIn(what, text)

    def test_a_guest_out_of_memory_misses_and_names_what_stayed(self):
        misses = [r for r in self.rows(SETTLED, _load_sample()) if r[0] == doctor.MISS]
        self.assertTrue([r for r in misses if "shells are resident" in r[1] and "editor remote server" in r[1]])
        self.assertTrue([r for r in misses if "macOS calls that pressure" in r[1]])
        self.assertTrue(all("<name>" not in r[2] for r in misses), "a remedy names the guest it is about")

    def test_a_guest_that_will_not_say_what_is_resident_says_so(self):
        self.assertTrue([r for r in self.rows(SETTLED) if r[0] == doctor.UNK and "did not answer the load probe" in r[1]])


class TestTheLiveDesktop(unittest.TestCase):
    wk_tier = "live"

    def test_vm_desktop(self):
        if not live_selected() or sys.platform != "darwin":
            self.skipTest("live tier not selected, or not a macOS host")
        vm = places.Registry(str(REPO)).load("vm")
        up = [n for n, state in vm.list() if state == "running"] if vm.tart() else []
        if not up:
            self.skipTest("no macOS guest is running on this host")
        rd = guest.desktop_readings(vm.root, vm.machine, vm.guest(up[0]))
        self.assertEqual([], guest.blockers(rd))
        self.assertEqual("off", rd["screenlock"])
