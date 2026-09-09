"""What `wk logs` says about a build: `(none)` on a good one, and the
readings the build was collected under. The first is owed by
docs/HANDOFF-test-runner.md: "catches: `error:` matching inside message
text". `first_error` (lib/watchdog.sh) greps a build.log for lines that look
like a compiler/ninja failure; the risk this guards is a bare, unanchored
`error:` matching a line that merely *talks about* an error (a log message
whose own text is "-- no error: handling here") rather than reporting one.

Driven the way tests/test_logs.py drives cmd/logs directly (WK_NAME/WK_TARGET=
vm/WK_VM_STORE, bypassing ./wk so a leftover WK_TARGET in this shell cannot
point it elsewhere).

Run: python3 -m unittest tests.test_owed_logs -v
"""
import json
import unittest

from tests.support import REPO, WkTest, bash, fake_workspace

CMD_LOGS = REPO / "cmd" / "logs"

GOOD_BROWSER = {
    "accelerator": "IOAccelerator", "dpr": 2, "focused": True, "raf_hz": 59.7,
    "renderer": "Apple M1 Pro", "screen": [1512, 982], "webgl": "WebGL 2.0",
    "webkit_gpu_clients": {"913": "com.apple.WebKit.GPU"},
}
LIBRARIES = ("JavaScriptCore", "WebCore", "WebKit")
BENCHMARKS = ("speedometer3", "jetstream3", "motionmark")
GOOD_PROFILE = {
    "profile_dir": "/Users/wk/pgo", "arch": "arm64", "missing": [],
    "combined": {lib: {"total_functions": 40000, "maximum_function_count": 900000}
                 for lib in LIBRARIES},
    "compressed": {lib: 1234567 for lib in LIBRARIES},
    "benchmarks": {b: {lib: {"total_functions": 24000, "maximum_function_count": 4200}
                       for lib in LIBRARIES} for b in BENCHMARKS},
}


class TestLogsShowsNoneOnAGoodBuild(WkTest):
    def _run(self, name, store):
        env = {"WK_NAME": name, "WK_TARGET": "vm", "WK_VM_STORE": str(store)}
        return bash(f'exec "{CMD_LOGS}"', env=env)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        """No real failure line, but the log's own text contains the
        substring 'error:' inside an unrelated sentence -- first_error's
        anchored patterns (^error:, : error:, ...) must not fire on it, so
        `wk logs` reports '(none)', not that sentence as a build error."""
        name = "goodws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building target foo\n"
            "-- no error: handling here, everything is fine\n"
            "ninja: no work to do.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("(none)", out, out)
        # first_error must not have surfaced the sentence as an error line.
        errors_section = out.split("errors:", 1)[1].split("last output:", 1)[0]
        self.assertNotIn("no error: handling here", errors_section, out)

    def test_a_real_ninja_failure_still_reports_by_the_same_path(self):
        """Contrast: a genuine failure line is still caught, so '(none)'
        above is not the result of first_error being broken outright."""
        name = "badws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building target foo\n"
            "foo.cpp:10:5: error: use of undeclared identifier 'x'\n"
            "ninja: build stopped: subcommand failed.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertNotIn("(none)", out, out)
        self.assertIn("ninja: build stopped", out, out)


class TestTheReadingsTravelWithTheBuild(WkTest):
    """A staged arm carries the readings that justify it beside its products,
    and `wk logs --gates` is what reads them back: the browser check, the
    profile check, and the payload pins, each judged again as it is shown."""

    def _gates(self, browser=None, profile=None, pins=None):
        with fake_workspace() as ws:
            products = ws.ws_dir / "WebKit" / "WebKitBuild" / "Release-mac-release-pgo"
            products.mkdir(parents=True)
            if browser is not None:
                (products / "wk-browser-check.json").write_text(json.dumps(browser))
            if profile is not None:
                (products / "wk-profile-check.json").write_text(json.dumps(profile))
            if pins is not None:
                (products / "wk-payload-pins").write_text(pins)
            cp = bash(f'exec "{CMD_LOGS}" --gates',
                      env=ws.env({"WK_NAME": "selftest-ws", "WK_TARGET": "local"}))
            return cp, cp.stdout + cp.stderr

    def test_a_build_with_no_readings_says_which_configs_have_them(self):
        cp, out = self._gates()
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("no readings under", out, out)
        self.assertIn("profile-guided build", out, out)
        for lane in ("build/mac-pgo.sh", "image/pgo.sh"):
            self.assertIn(lane, out, out)

    def test_the_readings_are_shown_from_beside_the_products(self):
        cp, out = self._gates(browser=GOOD_BROWSER, profile=GOOD_PROFILE,
                              pins="speedometer3 9f3c1a\nmotionmark 44ab02\n")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("raf_hz=59.7", out, out)
        self.assertIn("Apple M1 Pro", out, out)
        self.assertIn("JavaScriptCore: functions=40000", out, out)
        self.assertIn("speedometer3 9f3c1a", out, out)

    def test_a_reading_that_did_not_justify_a_measurement_says_so_again(self):
        """The verdict is re-derived from the reading, so a throttled window
        is reported by `wk logs` in the same words the build refused it in."""
        bad = dict(GOOD_BROWSER, raf_hz=8.0, webgl=None, webkit_gpu_clients={})
        cp, out = self._gates(browser=bad)
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("this window is throttled", out, out)
        self.assertIn("no WebGL context", out, out)


if __name__ == "__main__":
    unittest.main()
