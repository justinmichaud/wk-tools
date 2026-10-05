"""rr: `wk run --rr` records jsc and `wk gui --rr` MiniBrowser into the workspace's ~/wk-rr, `wk run --replay`
replays the latest recording under lldb; a non-Linux target, a missing rr and closed perf events are refused.
cmd/run and cmd/gui exec into `Target.exec_argv`'s result, intercepted here before it replaces the process."""
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
from wk import ldpath  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.act import Refused  # noqa: E402


def _load(cmd):
    path = str(REPO / "cmd" / cmd)
    loader = importlib.machinery.SourceFileLoader("cmd_" + cmd, path)
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader, origin=path))
    mod.__file__ = path
    loader.exec_module(mod)
    return mod


os.environ.setdefault("WK_ROOT", str(REPO))
RUN, GUI = _load("run"), _load("gui")


def _target(os_name="linux", have_rr=True, paranoid="1"):
    target = mock.Mock()
    target.os.return_value = os_name
    target.kind, target.env = "container", {}
    target.src.return_value = "/src/WebKit"
    target.home.return_value = "/home/u"
    target.lldb_opts.return_value = ""
    target.exec_argv.return_value = (["true"], None)

    def exec_(ws, argv, **kw):
        if argv[-1] == "rr":
            return mock.Mock(ok=have_rr, out="")
        return mock.Mock(ok=True, out=paranoid + "\n")
    target.exec.side_effect = exec_
    return target


def uname(sysname, machine):
    real = os.uname()
    return lambda: os.uname_result((sysname, real.nodename, real.release, real.version, machine))


class RrTest(unittest.TestCase):
    """On a Linux x86_64 host, whatever the suite runs on: a container on an Apple Silicon Mac is refused."""

    def setUp(self):
        p = mock.patch("os.uname", uname("Linux", "x86_64"))
        p.start()
        self.addCleanup(p.stop)

    def go(self, mod, target, argv, config="gtk-release", **env):
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(mod.targets, "Registry", return_value=reg), \
                mock.patch.dict(os.environ, dict({"WK_NAME": "ws", "WK_CONFIG": config, "WK_QUIET": "1"}, **env)):
            if mod is GUI:
                with mock.patch.object(GUI, "is_macos", return_value=False), mock.patch.object(GUI, "session_env", return_value=""):
                    mod.main(argv)
            else:
                mod.main(argv)
        args, kw = target.exec_argv.call_args
        return args[1][2], kw["tty"]

    def refused(self, mod, target, argv, config="gtk-release"):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            self.go(mod, target, argv, config)
        target.exec_argv.assert_not_called()
        return err.getvalue()


class TestRecordAndReplay(RrTest):
    def test_run_rr_records_jsc_into_the_workspace_trace_directory(self):
        cmd, tty = self.go(RUN, _target(), ["--rr", "--", "x.js"])
        self.assertIn("export _RR_TRACE_DIR=/home/u/wk-rr", cmd)
        self.assertRegex(cmd, r"exec rr record \S+/bin/jsc --validateOptions=1 x.js$")
        self.assertIn("export LD_LIBRARY_PATH=", cmd)
        self.assertFalse(tty)

    def test_replay_runs_the_latest_trace_under_lldb_with_the_tail_for_rr(self):
        cmd, tty = self.go(RUN, _target(), ["--replay", "--", "-p", "1234"])
        self.assertIn("export _RR_TRACE_DIR=/home/u/wk-rr", cmd)
        self.assertTrue(cmd.endswith('exec rr replay -d "$LLDB" -p 1234'), cmd)
        self.assertTrue(tty)

    def test_gui_rr_prefixes_minibrowser_with_the_recorder(self):
        cmd, tty = self.go(GUI, _target(), ["--rr"])
        self.assertIn("export _RR_TRACE_DIR=/home/u/wk-rr", cmd)
        self.assertIn("WEBKIT_MINI_BROWSER_PREFIX='rr record' Tools/Scripts/run-minibrowser", cmd)

    def test_a_dry_run_reads_nothing_and_prints_the_recording(self):
        target = _target(have_rr=False, paranoid="3")
        target.exec_argv.return_value = (["ssh", "box", "rr record"], None)
        reg = mock.Mock()
        reg.load.return_value = target
        reg.machine = Fake()
        with mock.patch.object(RUN.targets, "Registry", return_value=reg), \
                mock.patch("os.execvp", side_effect=AssertionError("ran it")), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws", "WK_CONFIG": "gtk-release", "WK_DRY_RUN": "1"}), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            RUN.main(["--rr", "--", "x.js"])
        self.assertIn("would run: ssh box 'rr record'", err.getvalue())
        target.exec.assert_not_called()


class TestRefusals(RrTest):
    def test_a_macos_target_is_refused_naming_why(self):
        for mod, argv in ((RUN, ["--rr"]), (RUN, ["--replay"]), (GUI, ["--rr"])):
            with self.subTest(argv=argv):
                err = self.refused(mod, _target(os_name="macos"), argv, config="mac-release")
                self.assertIn("rr records Linux processes only", err)

    def test_a_container_on_an_apple_silicon_mac_is_refused_and_one_on_an_arm_server_is_not(self):
        for sysname, machine, env, refused in (("Darwin", "arm64", {}, True), ("Linux", "aarch64", {"WK_IN_VM": "1"}, True),
                                               ("Linux", "aarch64", {}, False), ("Darwin", "x86_64", {}, False)):
            with self.subTest(sysname=sysname, machine=machine, env=env), mock.patch("os.uname", uname(sysname, machine)):
                target = _target()
                target.env = env
                if refused:
                    self.assertIn("rr does not support Apple Silicon CPUs", self.refused(RUN, target, ["--rr"]))
                else:
                    self.assertIn("exec rr record", self.go(RUN, target, ["--rr", "--", "x.js"])[0])

    def test_rr_missing_is_refused(self):
        self.assertIn("rr is not installed in 'ws'", self.refused(RUN, _target(have_rr=False), ["--rr"]))

    def test_closed_perf_events_are_refused_with_the_remedy(self):
        with mock.patch.object(ldpath, "is_linux", return_value=False):
            err = self.refused(RUN, _target(paranoid="2"), ["--rr"])
        self.assertIn("perf_event_paranoid is 2 in 'ws', and rr needs 1 or less", err)
        self.assertIn("echo 1 | sudo tee /proc/sys/kernel/perf_event_paranoid", err)

    def test_rr_runs_on_its_own(self):
        for argv in (["--rr", "--lldb"], ["--replay", "--until-crash"], ["--rr", "--replay"]):
            with self.subTest(argv=argv), mock.patch.dict(os.environ, {"WK_NAME": "ws"}), \
                    contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
                RUN.parse(argv)
            self.assertIn("runs on its own", err.getvalue())
        with mock.patch.dict(os.environ, {"WK_NAME": "ws"}), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(Refused):
            GUI.parse(["--rr", "--lldb", "ui"])
        self.assertIn("pick one", err.getvalue())


if __name__ == "__main__":
    unittest.main()
