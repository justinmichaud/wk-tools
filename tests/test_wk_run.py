"""`wk run` -- the jsc binary a build produced, from the workspace's own
build tree, direct or under lldb, once or in an until-crash loop.

`docs/PLAN.md`'s owed rows: `unit run.finds_binary[<port>]` (the
`LD_LIBRARY_PATH`/`DYLD_FRAMEWORK_PATH` prelude is right for every port, and
prepended rather than replacing whatever the shell already carries) and the
unit half of `--lldb gets a pty on every target` (`wk run` always asks its
target for a tty under `--lldb`, whichever target answers). Nothing here
starts a real container, guest or build: `cmd/run` execs into
`Target.exec_argv`'s result, which this file intercepts before it replaces
the process.

Run: python3 -m unittest tests.test_wk_run -v
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
    """`run_var()`/`run_dir()` differ by port, and the prelude prepends
    rather than replaces: `${VAR:+:${VAR}}` keeps whatever the image already
    put on the search path (the wkdev jhbuild prefix)."""

    def test_cmake_ports_use_ld_library_path(self):
        for name in ("jsc-release", "gtk-release", "wpe-release"):
            with self.subTest(config=name):
                cfg = buildconf.resolve(name, "linux", "container", {})
                self.assertEqual(cfg.run_var(), "LD_LIBRARY_PATH")
                self.assertTrue(cfg.jsc_path("/src/WebKit").endswith("/bin/jsc"))
                text = RUN.prelude(cfg.run_var(), cfg.run_dir("/src/WebKit"))
                self.assertEqual(text, 'export LD_LIBRARY_PATH="%s${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"'
                                       % cfg.run_dir("/src/WebKit"))

    def test_the_apple_port_guest_uses_dyld_framework_path(self):
        cfg = buildconf.resolve("mac-release", "macos", "vm", {})
        self.assertEqual(cfg.run_var(), "DYLD_FRAMEWORK_PATH")
        self.assertTrue(cfg.jsc_path("/src/WebKit").endswith("/jsc"))
        text = RUN.prelude(cfg.run_var(), cfg.run_dir("/src/WebKit"))
        self.assertEqual(text, 'export DYLD_FRAMEWORK_PATH="%s${DYLD_FRAMEWORK_PATH:+:${DYLD_FRAMEWORK_PATH}}"'
                               % cfg.run_dir("/src/WebKit"))

    def test_the_direct_run_embeds_the_prelude_and_the_right_jsc_path(self):
        """End to end through `main`: the composed command jsc actually runs
        under carries the same prelude and path, for a CMake and an Xcode port."""
        for name, os_name, kind in (("gtk-release", "linux", "container"), ("mac-release", "macos", "vm")):
            with self.subTest(config=name):
                target = mock.Mock()
                target.os.return_value = os_name
                target.kind = kind
                target.env = {}
                target.src.return_value = "/src/WebKit"
                target.exec_argv.return_value = (["true"], None)
                reg = mock.Mock()
                reg.load.return_value = target
                cfg = buildconf.resolve(name, os_name, kind, {})
                with mock.patch.object(RUN.targets, "Registry", return_value=reg), \
                        mock.patch.object(RUN, "exec_into"), \
                        mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
                    RUN.main(["--config", name, "--", "x.js"])
                call_args = target.exec_argv.call_args[0]
                cmd = call_args[1][2]
                self.assertIn('export %s="%s' % (cfg.run_var(), cfg.run_dir("/src/WebKit")), cmd)
                self.assertIn(cfg.jsc_path("/src/WebKit"), cmd)


class TestLldbGetsAPty(unittest.TestCase):
    """Whichever target answers, `--lldb` asks it for a tty and a plain run does not."""

    def _target(self):
        target = mock.Mock()
        target.os.return_value = "linux"
        target.kind = "container"
        target.env = {}
        target.src.return_value = "/src/WebKit"
        target.home.return_value = "/home/u"
        target.tools.return_value = "/opt/wk-tools"
        target.lldb_opts.return_value = ""
        target.exec_argv.return_value = (["true"], None)
        return target

    def _run(self, target, argv):
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(RUN.targets, "Registry", return_value=reg), \
                mock.patch.object(RUN, "exec_into"), \
                mock.patch.object(RUN.shell, "lldb_prelude", return_value=""), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            RUN.main(["--config", "gtk-release"] + argv)
        return target.exec_argv.call_args

    def test_direct_lldb_asks_for_a_tty(self):
        target = self._target()
        _, kw = self._run(target, ["--lldb", "--", "x.js"])
        self.assertTrue(kw["tty"])

    def test_direct_without_lldb_asks_for_none(self):
        target = self._target()
        _, kw = self._run(target, ["--", "x.js"])
        self.assertFalse(kw["tty"])

    def test_until_crash_lldb_asks_for_a_tty(self):
        target = self._target()
        _, kw = self._run(target, ["--until-crash", "--lldb", "--", "x.js"])
        self.assertTrue(kw["tty"])

    def test_until_crash_without_lldb_asks_for_none(self):
        target = self._target()
        _, kw = self._run(target, ["--until-crash", "--", "x.js"])
        self.assertFalse(kw["tty"])


class TestMaxValidation(unittest.TestCase):
    def test_a_non_numeric_max_is_refused_before_anything_runs(self):
        with mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
            with self.assertRaises(Refused):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    RUN.parse(["--until-crash", "--max", "abc", "--", "x.js"])
        self.assertIn("--max needs a positive integer", err.getvalue())


if __name__ == "__main__":
    unittest.main()
