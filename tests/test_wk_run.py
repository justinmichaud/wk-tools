"""`wk run` (cmd/run): the jsc a build produced, direct or under lldb, once or until it crashes, with
`Driver.exec_argv` intercepted before it replaces the process."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import presets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402


def _load_cmd_run():
    path = str(REPO / "cmd" / "run")
    loader = importlib.machinery.SourceFileLoader("cmd_run", path)
    spec = importlib.util.spec_from_loader("cmd_run", loader, origin=path)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


os.environ.setdefault("WK_ROOT", str(REPO))
RUN = _load_cmd_run()


class TestFindsBinaryOnEveryPort(unittest.TestCase):
    """The library path variable differs by port, and the prelude prepends to whatever the shell already carries."""

    def test_each_port_prepends_to_its_own_search_path(self):
        for name, os_name, kind, var, jsc in (("jsc-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("gtk-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("wpe-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("mac-release", "macos", "vm", "DYLD_FRAMEWORK_PATH", "/jsc")):
            with self.subTest(config=name):
                cfg = presets.resolve(name, os_name, kind, {})
                self.assertEqual(cfg.run_var(), var)
                self.assertTrue(cfg.jsc_path("/src/WebKit").endswith(jsc))
                cp = subprocess.run(["sh", "-c", RUN.prelude(var, "/new") + '; printf %s "$' + var + '"'],
                                    env={var: "/old"}, capture_output=True, text=True)
                self.assertEqual(cp.stdout, "/new:/old")

    def test_the_direct_run_embeds_the_prelude_and_the_right_jsc_path(self):
        for name, os_name, kind in (("gtk-release", "linux", "container"), ("mac-release", "macos", "vm")):
            with self.subTest(config=name):
                driver = mock.Mock()
                driver.os.return_value = os_name
                driver.kind = kind
                driver.env = {}
                driver.src.return_value = "/src/WebKit"
                driver.exec_argv.return_value = (["true"], None)
                reg = mock.Mock()
                reg.load.return_value = driver
                cfg = presets.resolve(name, os_name, kind, {})
                with mock.patch.object(RUN.places, "Registry", return_value=reg), \
                        mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": name}):
                    RUN.main(["--", "x.js"])
                call_args = driver.exec_argv.call_args[0]
                cmd = call_args[1][2]
                self.assertIn('export %s="%s' % (cfg.run_var(), cfg.run_dir("/src/WebKit")), cmd)
                self.assertIn(cfg.jsc_path("/src/WebKit"), cmd)


class TestLldbGetsAPty(unittest.TestCase):
    """Whichever place answers, `--lldb` asks it for a tty and a plain run does not."""

    def _driver(self):
        driver = mock.Mock()
        driver.os.return_value = "linux"
        driver.kind = "container"
        driver.env = {}
        driver.src.return_value = "/src/WebKit"
        driver.home.return_value = "/home/u"
        driver.tools.return_value = "/opt/wk-tools"
        driver.lldb_opts.return_value = ""
        driver.exec_argv.return_value = (["true"], None)
        return driver

    def _run(self, driver, argv):
        reg = mock.Mock()
        reg.load.return_value = driver
        with mock.patch.object(RUN.places, "Registry", return_value=reg), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "gtk-release"}):
            RUN.main(argv)
        return driver.exec_argv.call_args

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        driver = self._driver()
        driver.exec_argv.return_value = (["ssh", "box", "bash -lc 'jsc x.js'"], None)
        reg = mock.Mock()
        reg.load.return_value = driver
        reg.machine = Fake()
        err = io.StringIO()
        with mock.patch.object(RUN.places, "Registry", return_value=reg), \
                mock.patch("os.execvp", side_effect=AssertionError("ran it")), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": "gtk-release", "WK_DRY_RUN": "1"}), \
                contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            RUN.main(["--", "x.js"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn("would run: ssh box", err.getvalue())

    def test_only_lldb_asks_for_a_tty(self):
        for argv, tty in ((["--lldb"], True), ([], False), (["--until-crash", "--lldb"], True), (["--until-crash"], False)):
            with self.subTest(argv=argv):
                self.assertEqual(self._run(self._driver(), argv + ["--", "x.js"])[1]["tty"], tty)


class TestMaxValidation(unittest.TestCase):
    def test_a_non_numeric_max_is_refused_before_anything_runs(self):
        with mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            with self.assertRaises(Refused):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    RUN.parse(["--until-crash", "--max", "abc", "--", "x.js"])
        self.assertIn("--max", err.getvalue())


if __name__ == "__main__":
    unittest.main()
