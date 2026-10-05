"""What `wk status <ws> --log` says about a build: `(none)` on a good one, and the"""
import json
import unittest

from tests.support import REPO, WkTest, bash, fake_workspace

CMD_STATUS = REPO / "cmd" / "status"

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
        env = {"WK_NAME": name, "WK_PLACE": "vm", "WK_VM_STORE": str(store)}
        return bash(f'exec "{CMD_STATUS}" --log', env=env)

    def test_a_message_containing_the_word_error_mid_sentence_is_not_reported(self):
        name = "goodws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building place foo\n"
            "-- no error: handling here, everything is fine\n"
            "ninja: no work to do.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("(none)", out, out)
        errors_section = out.split("errors:", 1)[1].split("last output:", 1)[0]
        self.assertNotIn("no error: handling here", errors_section, out)

    def test_a_real_ninja_failure_still_reports_by_the_same_path(self):
        name = "badws"
        wsdir = self.tmp / "ws" / name
        wsdir.mkdir(parents=True)
        (wsdir / "build.log").write_text(
            "Building place foo\n"
            "foo.cpp:10:5: error: use of undeclared identifier 'x'\n"
            "ninja: build stopped: subcommand failed.\n"
        )
        cp = self._run(name, self.tmp)
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertNotIn("(none)", out, out)
        self.assertIn("ninja: build stopped", out, out)


class TestTheReadingsTravelWithTheBuild(WkTest):

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
            cp = bash(f'exec "{CMD_STATUS}" --log --gates',
                      env=ws.env({"WK_NAME": "selftest-ws", "WK_PLACE": "local"}))
            return cp, cp.stdout + cp.stderr

    def test_a_build_with_no_readings_says_which_configs_have_them(self):
        cp, out = self._gates()
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("no readings under", out, out)

    def test_the_readings_are_shown_from_beside_the_products(self):
        cp, out = self._gates(browser=GOOD_BROWSER, profile=GOOD_PROFILE,
                              pins="speedometer3 9f3c1a\nmotionmark 44ab02\n")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("raf_hz=59.7", out, out)
        self.assertIn("JavaScriptCore: functions=40000", out, out)
        self.assertIn("speedometer3 9f3c1a", out, out)

    def test_a_reading_that_did_not_justify_a_measurement_says_so_again(self):
        bad = dict(GOOD_BROWSER, raf_hz=8.0, webgl=None, webkit_gpu_clients={})
        cp, out = self._gates(browser=bad)
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("this window is throttled", out, out)
        self.assertIn("no WebGL context", out, out)


if __name__ == "__main__":
    unittest.main()
