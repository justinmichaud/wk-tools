"""`git` in a workspace (container/bin/ws/git): the command runs exactly as it would have, and a failure the sandbox
caused (the read-only commit wall, an empty push agent) gains a line naming the remedy. A scratch repository with
`chmod -w .git/objects` stands in for the bind, and a real ssh-agent for the switch."""
import os
import re
import shutil
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest

SHIM = REPO / "container" / "bin" / "ws" / "git"


class _Gate(WkTest):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            raise unittest.SkipTest("no git")
        self.bin = SHIM.parent
        self.home = self.tmp / "home"
        (self.home / ".ssh").mkdir(parents=True)
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        self._git("init", "-q", ".", wrapped=False)
        self._git("config", "user.email", "a@b", wrapped=False)
        self._git("config", "user.name", "a", wrapped=False)
        (self.repo / "f").write_text("hi\n")
        self._git("add", "f", wrapped=False)
        self._git("commit", "-qm", "one", wrapped=False)

    def _git(self, *args, wrapped=True):
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        if wrapped:
            env["PATH"] = f"{self.bin}:{env['PATH']}"
        else:
            env.pop("GIT_DIR", None)
        return subprocess.run(["git", *args], cwd=str(self.repo), env=env,
                              capture_output=True, text=True)

    def wall_on(self, staged=False):
        if staged:
            (self.repo / "f").write_text("two\n")
            self._git("add", "f", wrapped=False)
        (self.repo / ".git" / "objects").chmod(0o555)
        self.addCleanup(lambda: (self.repo / ".git" / "objects").chmod(0o755))

    def agent(self, keys=0):
        """A real ssh-agent behind ~/.ssh/config's Include, as a workspace's is written."""
        sock = self.tmp / "agent.sock"
        cp = subprocess.run(["ssh-agent", "-a", str(sock)],
                            capture_output=True, text=True)
        if cp.returncode != 0:
            raise unittest.SkipTest("no ssh-agent")
        pid = re.search(r"SSH_AGENT_PID=(\d+)", cp.stdout)
        self.addCleanup(subprocess.run,
                        ["kill", pid.group(1)] if pid else ["true"],
                        capture_output=True)
        (self.home / ".ssh" / "fork_config").write_text(
            f"Host github-webkit\n    IdentityAgent {sock}\n")
        (self.home / ".ssh" / "config").write_text(
            f"Host *\n    Include {self.home}/.ssh/fork_config\n")
        for i in range(keys):
            key = self.tmp / f"k{i}"
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                            "-f", str(key)], check=True, capture_output=True)
            subprocess.run(["ssh-add", str(key)], capture_output=True,
                           env={**os.environ, "SSH_AUTH_SOCK": str(sock)})
        return sock


class TestItNeverChangesWhatTheCommandDoes(_Gate):

    def test_a_verb_it_watches_still_succeeds_untouched(self):
        (self.repo / "f").write_text("two\n")
        cp = self._git("add", "f")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("wk:", cp.stderr)

    def test_a_verb_it_does_not_watch_is_exec_d_straight_through(self):
        cp = self._git("log", "--oneline")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("one", cp.stdout)
        self.assertNotIn("wk:", cp.stderr)

    def test_a_read_that_shares_a_verb_with_a_write_is_not_refused(self):
        self.wall_on()
        for args in (("tag",), ("branch", "--list"), ("stash", "list")):
            with self.subTest(args=args):
                cp = self._git(*args)
                self.assertEqual(cp.returncode, 0, cp.stderr)
                self.assertNotIn("wk:", cp.stderr)

    def test_a_failure_the_sandbox_did_not_cause_gets_no_note(self):
        cp = self._git("checkout", "no-such-branch")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("wk:", cp.stderr)


class TestTheCommitWallExplainsItself(_Gate):
    def test_a_commit_the_wall_blocked_names_the_remedy_after_gits_own_report(self):
        self.wall_on(staged=True)
        cp = self._git("commit", "-m", "two")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("wk key push on", cp.stderr)
        self.assertLess(cp.stderr.index("insufficient permission"), cp.stderr.index("wk:"))

    def test_an_unwalled_checkout_is_silent(self):
        cp = self._git("commit", "--allow-empty", "-m", "two")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("wk:", cp.stderr)


class TestThePushSwitchExplainsItself(_Gate):
    def test_a_failed_push_with_an_empty_agent_names_the_switch(self):
        self.agent(keys=0)
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git",
                  wrapped=False)
        cp = self._git("push", "fork", "HEAD")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("wk key push on", cp.stderr)

    def test_a_push_whose_agent_holds_a_key_cannot_be_asked_or_is_not_configured_says_nothing(self):
        def unreachable_agent():
            (self.home / ".ssh" / "fork_config").write_text(f"Host github-webkit\n    IdentityAgent {self.tmp}/nothing.sock\n")
            (self.home / ".ssh" / "config").write_text(f"Host *\n    Include {self.home}/.ssh/fork_config\n")
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git", wrapped=False)
        for case, arrange in (("no ssh config", lambda: None), ("an agent with a key", lambda: self.agent(keys=1)),
                              ("an agent that cannot be asked", unreachable_agent)):
            with self.subTest(case):
                arrange()
                cp = self._git("push", "fork", "HEAD")
                self.assertNotEqual(cp.returncode, 0)
                self.assertNotIn("wk:", cp.stderr)


class TestWhatAToolResolvesGitTo(_Gate):
    """webkitcorepy runs `realpath(which('git'))` once and uses that path for every git call after it."""

    def test_the_resolved_path_is_named_git_and_is_still_git(self):
        env = dict(os.environ, HOME=str(self.home),
                   PATH=f"{self.bin}:{os.environ['PATH']}")
        resolved = subprocess.run(
            [sys.executable, "-c",
             "import os, shutil; print(os.path.realpath(shutil.which('git')))"],
            env=env, capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(os.path.basename(resolved), "git", resolved)
        cp = subprocess.run([resolved, "log", "--oneline"], cwd=str(self.repo),
                            env=env, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("one", cp.stdout)


if __name__ == "__main__":
    unittest.main()
