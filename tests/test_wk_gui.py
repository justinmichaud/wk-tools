"""`wk gui` -- MiniBrowser in the benchmark seat.

`docs/PLAN.md`'s owed row `unit gui.refuses_remote`: a remote target is an
arbitrary machine reached over ssh, with no seat of its own to draw into, so
`wk gui` refuses one before touching it -- new behaviour this port adds
(the bash original had no such check). Also covers the jsc-only, no-browser
and macOS-container-has-no-display refusals, and the fullscreen-flag table.
Nothing here starts a real container, guest or browser: `cmd/gui` execs into
`Target.exec_argv`'s result, which this file intercepts before it replaces
the process.

Run: python3 -m unittest tests.test_wk_gui -v
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import buildconf  # noqa: E402
from wk.act import Refused  # noqa: E402


def _load_cmd_gui():
    path = str(REPO / "cmd" / "gui")
    loader = importlib.machinery.SourceFileLoader("cmd_gui", path)
    spec = importlib.util.spec_from_loader("cmd_gui", loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


os.environ.setdefault("WK_ROOT", str(REPO))
GUI = _load_cmd_gui()


def _target(kind="container", os_name="linux"):
    target = mock.Mock()
    target.os.return_value = os_name
    target.kind = kind
    target.env = {}
    target.src.return_value = "/src/WebKit"
    target.exec.return_value = mock.Mock(ok=True)
    target.exec_argv.return_value = (["true"], None)
    target.lldb_opts.return_value = ""
    target.tools.return_value = "/opt/wk-tools"
    return target


def _refused(case, fn):
    with contextlib.redirect_stderr(io.StringIO()) as err:
        with case.assertRaises(Refused):
            fn()
    return err.getvalue()


class TestRefusesARemoteTarget(unittest.TestCase):
    """A remote target is a bare ssh machine wk gives no seat of its own --
    refused before any config is resolved or any probe is made."""

    def test_a_remote_target_is_refused_before_anything_else(self):
        target = _target(kind="remote")
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(GUI.targets, "Registry", return_value=reg), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            err = _refused(self, lambda: GUI.main([]))
        self.assertIn("remote target", err)
        self.assertIn("ws", err)
        target.exec_argv.assert_not_called()

    def test_a_container_target_is_not_refused_by_this_check(self):
        target = _target(kind="container")
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(GUI.targets, "Registry", return_value=reg), \
                mock.patch.object(GUI, "exec_into"), \
                mock.patch.object(GUI, "is_macos", return_value=False), \
                mock.patch.object(GUI, "session_env", return_value=""), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_QUIET": "1"}):
            GUI.main(["--gtk"])
        target.exec_argv.assert_called_once()


class TestJscOnlyAndMissingBrowserRefusals(unittest.TestCase):
    def test_a_jsc_only_config_is_refused(self):
        target = _target()
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(GUI.targets, "Registry", return_value=reg), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            err = _refused(self, lambda: GUI.main(["--config", "jsc-release"]))
        self.assertIn("there is no browser in it", err)

    def test_no_minibrowser_built_is_refused_naming_the_build_command(self):
        target = _target()
        target.exec.return_value = mock.Mock(ok=False)
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(GUI.targets, "Registry", return_value=reg), \
                mock.patch.object(GUI, "is_macos", return_value=False), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            err = _refused(self, lambda: GUI.main(["--gtk"]))
        self.assertIn("wk build ws gtk-release", err)


class TestMacosContainerHasNoDisplay(unittest.TestCase):
    def test_a_container_target_on_a_macos_host_is_refused(self):
        target = _target(kind="container")
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(GUI.targets, "Registry", return_value=reg), \
                mock.patch.object(GUI, "is_macos", return_value=True), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            err = _refused(self, lambda: GUI.main(["--gtk"]))
        self.assertIn("no display", err)


class TestFullscreenFlagByPort(unittest.TestCase):
    def test_wpe_gtk_and_the_apple_port(self):
        cases = (("wpe-release", "linux", "container", "--fullscreen"),
                 ("gtk-release", "linux", "container", "--full-screen"),
                 ("mac-release", "macos", "vm", ""))
        for name, os_name, kind, want in cases:
            with self.subTest(config=name):
                cfg = buildconf.resolve(name, os_name, kind, {})
                self.assertEqual(GUI.fullscreen_flag(cfg), want)

    def test_a_config_with_no_browser_is_refused(self):
        cfg = buildconf.resolve("ios-sim-release", "macos", "vm", {})
        with self.assertRaises(Refused):
            with contextlib.redirect_stderr(io.StringIO()):
                GUI.fullscreen_flag(cfg)


if __name__ == "__main__":
    unittest.main()
