"""`wk run` (cmd/run): the jsc a build produced, direct or under lldb, once or until it crashes, with
`Driver.exec_argv` intercepted before it replaces the process."""
import contextlib
import io
import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import presets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402

os.environ.setdefault("WK_ROOT", str(REPO))
RUN = load_cmd("run")


def a_driver(os_name="linux", kind="container"):
    driver = mock.Mock()
    driver.os.return_value = os_name
    driver.kind = kind
    driver.env = {}
    driver.src.return_value = "/src/WebKit"
    driver.home.return_value = "/home/u"
    driver.tools.return_value = "/opt/wk-tools"
    driver.lldb_opts.return_value = ""
    driver.exec_argv.return_value = (["true"], None)
    return driver


def run_on(driver, argv, preset="gtk-release"):
    """The (args, kwargs) `wk run <argv>` handed `exec_argv`."""
    reg = mock.Mock()
    reg.load.return_value = driver
    with mock.patch.object(RUN.places, "Registry", return_value=reg), \
            mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_PRESET": preset}):
        RUN.main(argv)
    return driver.exec_argv.call_args


class TestFindsBinaryOnEveryPort(unittest.TestCase):
    """The library path variable differs by port, and the prelude prepends to whatever the shell already carries."""

    def test_each_port_prepends_to_its_own_search_path(self):
        for name, os_name, kind, var, jsc in (("jsc-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("gtk-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("wpe-release", "linux", "container", "LD_LIBRARY_PATH", "/bin/jsc"),
                                              ("mac-release", "macos", "vm", "DYLD_FRAMEWORK_PATH", "/jsc")):
            with self.subTest(config=name):
                preset = presets.resolve(name, os_name, kind, {})
                self.assertEqual(preset.run_var(), var)
                self.assertTrue(preset.jsc_path("/src/WebKit").endswith(jsc))
                cp = subprocess.run(["sh", "-c", RUN.prelude(var, "/new") + '; printf %s "$' + var + '"'],
                                    env={var: "/old"}, capture_output=True, text=True)
                self.assertEqual(cp.stdout, "/new:/old")

    def test_the_direct_run_embeds_the_prelude_and_the_right_jsc_path(self):
        for name, os_name, kind in (("gtk-release", "linux", "container"), ("mac-release", "macos", "vm")):
            with self.subTest(config=name):
                preset = presets.resolve(name, os_name, kind, {})
                cmd = run_on(a_driver(os_name, kind), ["--", "x.js"], name)[0][1][2]
                self.assertIn('export %s="%s' % (preset.run_var(), preset.run_dir("/src/WebKit")), cmd)
                self.assertIn(preset.jsc_path("/src/WebKit"), cmd)


class TestLldbGetsAPty(unittest.TestCase):
    """Whichever place answers, `--lldb` asks it for a tty and a plain run does not."""

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        driver = a_driver()
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
                self.assertEqual(run_on(a_driver(), argv + ["--", "x.js"])[1]["tty"], tty)


class TestMaxValidation(unittest.TestCase):
    def test_a_non_numeric_max_is_refused_before_anything_runs(self):
        with mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            with self.assertRaises(Refused):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    RUN.parse(["--until-crash", "--max", "abc", "--", "x.js"])
        self.assertIn("--max", err.getvalue())


if __name__ == "__main__":
    unittest.main()
