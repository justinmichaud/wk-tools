"""`wk enter` -- a shell in a workspace, or one command run there and exited."""
import importlib.machinery
import importlib.util
import io
import os
import unittest
from unittest import mock

from tests.support import REPO, WkTest, fake_workspace, run


def _load_enter():
    path = os.path.join(str(REPO), "cmd", "enter")
    loader = importlib.machinery.SourceFileLoader("wk_cmd_enter", path)
    spec = importlib.util.spec_from_loader("wk_cmd_enter", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


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

    def test_zed_delegates_to_cmd_zed_by_name_rather_than_running_a_command(self):
        with fake_workspace() as ws:
            cp = ws.run("enter", "--zed", "should-never-run")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no ssh route to itself", cp.stdout)   # cmd/zed's own refusal answered, not cmd/enter's
        self.assertNotIn("should-never-run", cp.stdout)


class TestNoTerminalAsksForNone(unittest.TestCase):

    def test_the_command_is_exec_argv_without_a_tty_and_replaces_this_process(self):
        enter = _load_enter()
        target = mock.Mock()
        target.info.return_value = "present"
        target.exec_argv.return_value = (["true"], None)
        reg = mock.Mock()
        reg.load.return_value = target
        with mock.patch.object(enter.targets, "Registry", return_value=reg), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}), \
                mock.patch("sys.stdin", io.StringIO("")):
            enter.main(["git", "status"])
        target.exec_argv.assert_called_once_with("ws", ["git", "status"], tty=False)
        reg.machine.exec.assert_called_once_with(["true"], None)


class TestNoSuchWorkspace(WkTest):
    def test_refuses_by_name_without_landing_anywhere(self):
        # WK_TARGET=vm: an absent name otherwise resolves to the container
        # target, which a macOS host forwards into the podman VM -- what is
        # under test here is the dispatcher's own resolution and refusal,
        # needing no machine at all (test_lifecycle.py's own comment on this).
        cp = run("enter", "no-such-workspace-abcxyz", "true", env={"WK_TARGET": "vm"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no such workspace", cp.stdout)


if __name__ == "__main__":
    unittest.main()
