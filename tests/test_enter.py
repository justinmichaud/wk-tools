"""`wk enter` -- a shell in a workspace, or one command run there and exited."""
import os
import shutil
import tempfile
import unittest

from tests.support import WkTest, fake_workspace
from tests.test_wk_workspace import WorkspaceTest


class TestRunsCommand(WkTest):
    def test_it_runs_the_command_and_reports_both_streams_and_the_status(self):
        with fake_workspace() as ws:
            cp = ws.run("enter", "sh", "-c", "echo out; echo err >&2; exit 3")
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("out", cp.stdout)
        self.assertIn("err", cp.stdout)

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


BRIDGE = "/opt/wk-tools/container/proxy/ensure-bridge.sh"


class TestEnterFlows(WorkspaceTest):
    """`wk enter ws ...` typed on the machine holding container workspace `ws`."""

    def setUp(self):
        super().setUp()
        self.w.make("ws")
        self.w.effects = []

    def entered(self, *argv):
        rc, _, err = self.wk(self.w, "enter", *argv)
        last = self.w.effects[-1] if self.w.effects else (None, ())
        return rc, err, list(last[1]) if last[0] == "exec" else None

    def test_a_command_replaces_this_process_with_the_sdk_enter_asking_no_tty_as_a_person(self):
        rc, _, argv = self.entered("ws", "git", "status")
        self.assertEqual((rc, argv[argv.index("--name"):]), (0, ["--name", "wk-ws", "--no-tty", "--exec", "--", BRIDGE,
                                                                  "/opt/wk-tools/container/bin/wk-person", "git", "status"]))

    def test_own_runs_the_command_as_wks_own_not_a_persons_and_takes_no_shell(self):
        rc, _, argv = self.entered("ws", "--own", "--", "git", "status")
        self.assertEqual((rc, argv[argv.index("--exec"):]), (0, ["--exec", "--", BRIDGE, "git", "status"]))
        self.assertEqual(self.entered("ws", "--own"), (1, "error: --own takes a command: a shell is always your own\n", None))

    def test_a_dry_run_prints_the_command_and_replaces_nothing(self):
        rc, err, argv = self.entered("ws", "--dry-run", "touch", "x")
        self.assertEqual((rc, argv), (0, None))
        self.assertRegex(err, r"^would run: env .* touch x\n$")

    def test_no_such_workspace_is_refused_by_name_with_or_without_dash_dash(self):
        for rest in (["true"], ["--", "true"]):
            rc, err, argv = self.entered("nope", *rest)
            self.assertEqual((rc, argv, err.splitlines()[0]), (2, None, "warning: no such workspace: nope -- 'wk ls' lists them"))


if __name__ == "__main__":
    unittest.main()
