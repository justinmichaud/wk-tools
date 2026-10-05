"""`wk gui` (cmd/gui): MiniBrowser in the benchmark seat, its refusals, and the fullscreen flag per port, with
`Driver.exec_argv` intercepted before it replaces the process."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import presets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402

os.environ.setdefault("WK_ROOT", str(REPO))
GUI = load_cmd("gui")


def _driver(kind="container", os_name="linux"):
    driver = mock.Mock()
    driver.os.return_value = os_name
    driver.kind = kind
    driver.env = {}
    driver.src.return_value = "/src/WebKit"
    driver.exec.return_value = mock.Mock(ok=True)
    driver.exec_argv.return_value = (["true"], None)
    driver.lldb_opts.return_value = ""
    driver.tools.return_value = "/opt/wk-tools"
    return driver


def _registry(driver):
    reg = mock.Mock()
    reg.load.return_value = driver
    return reg


def _refused(case, fn):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        with case.assertRaises(Refused):
            fn()
    return err.getvalue()


class TestRefusesARemotePlace(unittest.TestCase):
    def test_a_remote_target_is_refused_before_anything_else(self):
        driver = _driver(kind="remote")
        with mock.patch.object(GUI.places, "Registry", return_value=_registry(driver)), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            err = _refused(self, lambda: GUI.main([]))
        self.assertIn("remote place", err)
        self.assertIn("ws", err)
        driver.exec_argv.assert_not_called()

    def test_a_dry_run_prints_the_launch_and_runs_nothing(self):
        driver = _driver(kind="container")
        driver.exec_argv.return_value = (["wkdev-enter", "--exec", "--", "run-minibrowser"], None)
        reg = _registry(driver)
        reg.machine = Fake()
        err = io.StringIO()
        with mock.patch.object(GUI.places, "Registry", return_value=reg), \
                mock.patch("os.execvp", side_effect=AssertionError("ran it")), \
                mock.patch.object(GUI, "is_macos", return_value=False), \
                mock.patch.object(GUI, "session_env", return_value=""), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "gtk-release", "WK_DRY_RUN": "1"}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            GUI.main([])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("would run: wkdev-enter --exec -- run-minibrowser", err.getvalue())


class TestJscOnlyAndMissingBrowserRefusals(unittest.TestCase):
    def test_a_jsc_only_config_is_refused(self):
        driver = _driver()
        with mock.patch.object(GUI.places, "Registry", return_value=_registry(driver)), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "jsc-release"}):
            _refused(self, lambda: GUI.main([]))

    def test_no_minibrowser_built_is_refused_naming_the_build_command(self):
        driver = _driver()
        driver.exec.return_value = mock.Mock(ok=False)
        with mock.patch.object(GUI.places, "Registry", return_value=_registry(driver)), \
                mock.patch.object(GUI, "is_macos", return_value=False), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "gtk-release"}):
            err = _refused(self, lambda: GUI.main([]))
        self.assertIn("wk build ws gtk-release", err)


class TestMacosContainerHasNoDisplay(unittest.TestCase):
    def test_a_container_target_on_a_macos_host_is_refused(self):
        driver = _driver(kind="container")
        with mock.patch.object(GUI.places, "Registry", return_value=_registry(driver)), \
                mock.patch.object(GUI, "is_macos", return_value=True), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "gtk-release"}):
            _refused(self, lambda: GUI.main([]))
        driver.exec_argv.assert_not_called()


class TestFullscreenFlagByPort(unittest.TestCase):
    def test_wpe_gtk_and_the_apple_port(self):
        cases = (("wpe-release", "linux", "container", "--fullscreen"),
                 ("gtk-release", "linux", "container", "--full-screen"),
                 ("mac-release", "macos", "vm", ""))
        for name, os_name, kind, want in cases:
            with self.subTest(config=name):
                preset = presets.resolve(name, os_name, kind, {})
                self.assertEqual(GUI.fullscreen_flag(preset), want)

    def test_a_config_with_no_browser_is_refused(self):
        preset = presets.resolve("ios-sim-release", "macos", "vm", {})
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()):
                GUI.fullscreen_flag(preset)


if __name__ == "__main__":
    unittest.main()
