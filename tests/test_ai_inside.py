"""`wk ai claude` typed *inside* a workspace."""
import contextlib
import io
import os
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash
from tests.test_ai import AI, _Flow
from tests.test_doctor_wall import _Wall
from wk import wall
from wk.act import Refused


class _Inside(_Flow, _Wall):
    kind = "local"

    def setUp(self):
        _Wall.setUp(self)
        self.setUpFlow()
        self.env.update(WK_NAME="demo", WK_PLACE="local")
        self.fake.answer(["ssh", "-G", "github-webkit"], out="user me\n")

    def checks(self, force=False):
        if force:
            os.environ["WK_FORCE"] = "1"
        err = io.StringIO()
        refused = False
        with contextlib.redirect_stderr(err):
            try:
                AI.Ai(AI.ROOT, self.env, self.reg, self.driver, "claude", "demo").checks()
            except Refused:
                refused = True
        os.environ.pop("WK_FORCE", None)
        return refused, err.getvalue()


class TestTheCommandRunsInAWorkspace(_Inside):
    def test_the_local_target_runs_the_in_workspace_checks(self):
        refused, err = self.checks()
        self.assertFalse(refused, err)
        self.assertIn("checking workspace 'demo' from inside it", err)
        self.assertIn("the agent holds nothing", err)
        self.assertIn("sandbox intact", err)

    def test_the_commit_wall_covers_a_session_started_from_inside(self):
        with mock.patch.object(self.driver, "os", return_value="linux"):
            self.assertTrue(wall.commit_walled(self.driver))
        with mock.patch.object(self.driver, "os", return_value="macos"):
            self.assertFalse(wall.commit_walled(self.driver))
        self.assertTrue(wall.commit_walled(self.reg.load("container")))
        self.assertFalse(wall.commit_walled(self.reg.load("remote")))


class TestWhatEachKindOfFailureDoes(_Inside):
    def test_a_sandbox_failure_is_a_barrier(self):
        self.set("rest/version", "000")
        refused, err = self.checks()
        self.assertTrue(refused, err)
        self.assertIn("the sandbox around 'demo' is not intact", err)
        self.assertIn("--force", err)
        refused, err = self.checks(force=True)
        self.assertFalse(refused, err)

    def test_a_way_to_publish_is_not_forceable(self):
        self.set("/pulls", "422")
        for force in (False, True):
            with self.subTest(force=force):
                refused, err = self.checks(force=force)
                self.assertTrue(refused, err)
                self.assertIn("could publish", err)
                self.assertIn("wk key push off", err)


class TestTheFunctionDoesNotShadowTheRealCli(WkTest):

    def _what_claude_is(self, interactive, workspace=True):
        home = self.tmp / f"home-{interactive}-{workspace}"
        (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
        if workspace:
            (home / ".wk-workspace").write_text("name=demo\nsrc=/src\n")
        real = home / ".local" / "bin" / "claude"
        real.write_text("#!/bin/sh\necho REAL-CLI\n")
        real.chmod(0o755)
        flags = "-ic" if interactive else "-c"
        cp = bash(
            f'bash {flags} \'. "{REPO}/shell/bashrc" >/dev/null 2>&1; '
            f'type -t claude; type claude\' 2>/dev/null',
            env={"HOME": str(home), "NO_ZSH": "1", "TERM": "dumb",
                 "PATH": f"{home}/.local/bin:/usr/bin:/bin"})
        return cp.stdout

    def test_only_an_interactive_shell_in_a_workspace_gets_the_measured_start(self):
        for interactive, workspace in ((True, True), (False, True), (True, False)):
            with self.subTest(interactive=interactive, workspace=workspace):
                out = self._what_claude_is(interactive, workspace)
                if interactive and workspace:
                    self.assertIn("function", out)
                    self.assertIn("wk ai claude", out)
                else:
                    self.assertIn("file", out)
                    self.assertNotIn("wk ai claude", out)


if __name__ == "__main__":
    unittest.main()
