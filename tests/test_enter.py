"""`wk enter` -- a shell in a workspace, or one command run there and exited."""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, WkTest, as_dispatched, fake_workspace, invocation, load_cmd, run

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402


class TestRunsCommand(WkTest):
    def test_it_runs_the_command_and_reports_both_streams_and_the_status(self):
        with fake_workspace() as ws:
            cp = ws.run("enter", "sh", "-c", "echo out; echo err >&2; exit 3")
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("out", cp.stdout)
        self.assertIn("err", cp.stdout)

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        with fake_workspace() as ws:
            marker = ws.ws_dir / "dry-run-ran"
            cp = ws.run("enter", "--dry-run", "touch", str(marker))
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("would run:", cp.stdout)
            self.assertIn("touch %s" % marker, cp.stdout)
            self.assertFalse(marker.exists(), "a dry run ran the command")

    def test_zed_is_wk_zed_by_name_rather_than_a_command(self):
        stub = tempfile.mkdtemp(prefix="wk-test-zed-")
        self.addCleanup(shutil.rmtree, stub, True)
        zed = os.path.join(stub, "zed")
        with open(zed, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(zed, 0o755)
        with fake_workspace() as ws:
            cp = ws.run("enter", "--zed", "should-never-run", env={"PATH": stub + os.pathsep + os.environ["PATH"]})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("'wk zed' acts on a host", cp.stdout)   # wk zed's own refusal answered, not wk enter's
        self.assertNotIn("should-never-run", cp.stdout)


class TestNoTerminalAsksForNone(unittest.TestCase):
    def setUp(self):
        self.enter = load_cmd("enter")
        self.driver = mock.Mock(**{"info.return_value": "present", "exec_argv.return_value": (["true"], None)})
        self.reg = mock.Mock(**{"present.return_value": self.driver})
        for p in (mock.patch.dict(os.environ, {"WK_NAME": "ws"}), mock.patch("sys.stdin", io.StringIO(""))):
            p.start()
            self.addCleanup(p.stop)

    def test_the_command_is_exec_argv_without_a_tty_and_replaces_this_process(self):
        self.enter.main(as_dispatched("enter", ["git", "status"]), invocation(self.reg))
        self.driver.exec_argv.assert_called_once_with("ws", ["git", "status"], tty=False, person=True)
        self.reg.machine.exec.assert_called_once_with(["true"], None)

    def test_own_runs_the_command_as_wks_own_not_a_persons_and_takes_no_shell(self):
        self.enter.main(as_dispatched("enter", ["--own", "--", "git", "status"]), invocation(self.reg))
        self.driver.exec_argv.assert_called_once_with("ws", ["git", "status"], tty=False, person=False)
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.enter.main(as_dispatched("enter", ["--own"]), invocation(self.reg))
        self.assertIn("--own takes a command", err.getvalue())


class TestNoSuchWorkspace(WkTest):
    def test_refuses_by_name_without_landing_anywhere_with_or_without_dash_dash(self):
        # WK_PLACE=vm: an absent name otherwise resolves to the container place, which a macOS host forwards to podman.
        for rest in (["true"], ["--", "true"]):
            with self.subTest(rest=rest):
                cp = run("enter", "no-such-workspace-abcxyz", *rest, env={"WK_PLACE": "vm"})
                self.assertNotEqual(cp.returncode, 0)
                self.assertIn("no such workspace", cp.stdout)
                self.assertNotIn("--: command not found", cp.stdout)


if __name__ == "__main__":
    unittest.main()
