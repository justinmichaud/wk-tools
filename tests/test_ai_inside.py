"""`wk ai claude` typed *inside* a workspace.

A session in a workspace starts the same way whether it is driven from the host
or from in there, so `claude` in a workspace shell is a function that calls this
command (shell/bashrc) and the command does not refuse the `local` target.
What differs is that the host's push switch cannot be thrown from inside, so
`wk doctor`'s inside half measures it beside the sandbox checks
(lib/wk/wall.py, tests/test_doctor_wall.py) and says which kind of failure it
found: the sandbox (a barrier) or a way to publish (a refusal).

The checks are driven here against the healthy workspace
tests/test_doctor_wall.py answers for, one answer taken away at a time.

Run: python3 -m unittest tests.test_ai_inside -v
"""
import contextlib
import io
import os
import re
import unittest
from unittest import mock

from tests.support import REPO, WkTest, bash
from tests.test_ai import AI, _Flow
from tests.test_doctor_wall import _Wall
from wk import wall
from wk.act import Refused

AI_TEXT = (REPO / "cmd" / "ai").read_text()
BASHRC = (REPO / "shell" / "bashrc").read_text()


class _Inside(_Flow, _Wall):
    kind = "local"

    def setUp(self):
        _Wall.setUp(self)
        self.setUpFlow()
        self.env.update(WK_NAME="demo", WK_TARGET="local")
        self.fake.answer(["ssh", "-G", "github-webkit"], out="user me\n")

    def checks(self, force=False):
        """(refused, stderr) of the checks a session in here runs first."""
        if force:
            os.environ["WK_FORCE"] = "1"
        err = io.StringIO()
        refused = False
        with contextlib.redirect_stderr(err):
            try:
                AI.Ai(AI.ROOT, self.env, self.reg, self.target, "claude", "demo").checks()
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
        """The wall is a property of the container, not of which side the
        command was typed on, and bwrap -- Linux's -- is what applies it."""
        with mock.patch.object(self.target, "os", return_value="linux"):
            self.assertTrue(wall.commit_walled(self.target))
        with mock.patch.object(self.target, "os", return_value="macos"):
            self.assertFalse(wall.commit_walled(self.target))
        self.assertTrue(wall.commit_walled(self.reg.load("container")))
        self.assertFalse(wall.commit_walled(self.reg.load("remote")))


class TestWhatEachKindOfFailureDoes(_Inside):
    def test_a_sandbox_failure_is_a_barrier(self):
        """The same verdict `wk ai claude <ws>` reaches from the host: it
        refuses, and an explicit --force crosses it."""
        self.set("rest/version", "000")
        refused, err = self.checks()
        self.assertTrue(refused, err)
        self.assertIn("the sandbox around 'demo' is not intact", err)
        self.assertIn("--force", err)
        refused, err = self.checks(force=True)
        self.assertFalse(refused, err)

    def test_a_way_to_publish_is_not_forceable(self):
        """A session that starts with a working push is the failure the whole
        arrangement exists to prevent, and nothing in here could fix it."""
        self.set("/pulls", "422")
        for force in (False, True):
            with self.subTest(force=force):
                refused, err = self.checks(force=force)
                self.assertTrue(refused, err)
                self.assertIn("could publish", err)
                self.assertIn("wk push off", err)

    def test_the_checks_are_doctors_and_not_a_copy(self):
        for asked in ("wall.from_inside(", "wall.from_host("):
            self.assertIn(asked, AI_TEXT)
        self.assertNotIn("api.github.com", AI_TEXT)
        self.assertNotIn("bugs.webkit.org", AI_TEXT)


class TestTypingClaudeInAWorkspaceGoesThroughIt(unittest.TestCase):
    def _fn(self):
        m = re.search(r"claude\(\) \{ wk ai claude \"\$@\"; \}", BASHRC)
        self.assertIsNotNone(m, "shell/bashrc defines no claude function")
        start = BASHRC.rindex("case $- in", 0, m.start())
        return BASHRC[start:BASHRC.index("esac", m.end())]

    def test_it_is_defined_only_in_a_workspace(self):
        self.assertIn('[ -f "$HOME/.wk-workspace" ]', self._fn())

    def test_it_is_defined_only_for_an_interactive_shell(self):
        """The start itself runs the CLI from `bash -lc`, where this must not
        be defined or the command would call itself."""
        self.assertTrue(self._fn().startswith("case $- in"), self._fn())
        self.assertIn("*i*)", self._fn())

    def test_a_shell_with_no_wk_still_gets_a_claude(self):
        self.assertIn("command -v wk", self._fn())


class TestTheFunctionDoesNotShadowTheRealCli(WkTest):
    """Driven for real: the function answers an interactive shell and nothing
    else, so cmd/ai's own `bash -lc ... exec claude` finds the CLI rather than
    calling this command again."""

    def _what_claude_is(self, interactive, workspace=True):
        home = self.tmp / "home"
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

    def test_an_interactive_shell_in_a_workspace_gets_the_measured_start(self):
        out = self._what_claude_is(interactive=True)
        self.assertIn("function", out)
        self.assertIn("wk ai claude", out)

    def test_a_non_interactive_shell_gets_the_cli(self):
        out = self._what_claude_is(interactive=False)
        self.assertIn("file", out)
        self.assertNotIn("wk ai claude", out)

    def test_an_interactive_shell_that_is_not_a_workspace_gets_the_cli(self):
        """This workstation sources the same rc, and `claude` here is a
        session on this machine."""
        out = self._what_claude_is(interactive=True, workspace=False)
        self.assertIn("file", out)
        self.assertNotIn("wk ai claude", out)


if __name__ == "__main__":
    unittest.main()
