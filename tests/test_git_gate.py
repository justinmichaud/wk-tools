"""`git` in a workspace: what the sandbox refuses, git says cryptically.

Two of the sandbox's rules land on git, and git reports neither of them. The
commit wall is read-only mounts, so `git commit` answers "insufficient
permission for adding an object to repository database .git/objects" -- which
reads as a broken repository. The push switch is an empty ssh-agent on the
host, so `git push` answers "Permission denied (publickey)". Both are the
arrangement working, and neither says so.

So `git` is on a workspace's PATH ahead of the real one
(container/bin/ws/git -> wk-build-wall, added by shell/path.sh only where the
workspace marker is). It is never a refusal: the command runs exactly as it
would have, with its own output, its own exit status and its own terminal, and
what is added is a line naming the rule and the remedy after a failure the
sandbox caused. Refusing up front would need a list of verbs that always write,
and `git tag`, `git branch` and `git stash list` are the same verbs reading.

No real remote and no real bwrap: the wall is driven with a scratch repository,
`chmod -w .git/objects` standing in for the read-only bind, and a real
`ssh-agent` (empty, then holding a throwaway key) for the switch.

Run: python3 -m unittest tests.test_git_gate -v
"""
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.support import REPO, WkTest

WALL = REPO / "container" / "bin" / "wk-build-wall"
SHIM = REPO / "container" / "bin" / "ws" / "git"
PATH_SH = (REPO / "shell" / "path.sh").read_text()


def _write_verbs():
    """The verbs read from the one place that lists them, never copied here."""
    m = re.search(r'^GIT_WRITE_VERBS="([^"]+)"', WALL.read_text(), re.M)
    assert m, "container/bin/wk-build-wall no longer defines GIT_WRITE_VERBS"
    return tuple(m.group(1).split())


class _Gate(WkTest):
    def setUp(self):
        super().setUp()
        if not shutil.which("git"):
            raise unittest.SkipTest("no git")
        self.bin = SHIM.parent   # the shipped gate, in the arrangement it ships in
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
        """A real ssh-agent, and the ssh config chain the gate follows to find
        it: ~/.ssh/config's own Include, as the workspace's is written."""
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
    """The property the whole design rests on: nothing that worked before
    stops working, and no exit status changes."""

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
        """`git tag`, `git branch` and `git stash list` read. They are in the
        watched list and must still work, wall or no wall."""
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

    def test_gits_own_report_still_comes_first(self):
        """The note is added after git has had its say, never instead of it."""
        self.wall_on(staged=True)
        cp = self._git("commit", "-m", "two")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("insufficient permission", cp.stderr)
        self.assertLess(cp.stderr.index("insufficient permission"),
                        cp.stderr.index("wk:"))


class TestTheCommitWallExplainsItself(_Gate):
    def test_a_commit_that_the_wall_blocked_names_the_wall_and_the_remedy(self):
        self.wall_on(staged=True)
        cp = self._git("commit", "-m", "two")
        self.assertIn("git history is read-only in this session", cp.stderr)
        self.assertIn("bwrap", cp.stderr)
        self.assertIn("wk push on", cp.stderr)
        self.assertIn("wk enter", cp.stderr)

    def test_an_unwalled_checkout_is_silent(self):
        cp = self._git("commit", "--allow-empty", "-m", "two")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertNotIn("read-only in this session", cp.stderr)


class TestThePushSwitchExplainsItself(_Gate):
    def test_a_failed_push_with_an_empty_agent_names_the_switch(self):
        self.agent(keys=0)
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git",
                  wrapped=False)
        cp = self._git("push", "fork", "HEAD")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no deploy key reaches this workspace", cp.stderr)
        self.assertIn("wk push on", cp.stderr)

    def test_an_agent_holding_a_key_is_not_the_switch_being_off(self):
        """Push is on: whatever this failure is, it is not the switch, and
        saying so would send a person after the wrong thing."""
        self.agent(keys=1)
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git",
                  wrapped=False)
        cp = self._git("push", "fork", "HEAD")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("no deploy key", cp.stderr)

    def test_an_agent_that_cannot_be_asked_says_nothing(self):
        """Unreachable is not "off": the note would be a guess."""
        (self.home / ".ssh" / "fork_config").write_text(
            f"Host github-webkit\n    IdentityAgent {self.tmp}/nothing.sock\n")
        (self.home / ".ssh" / "config").write_text(
            f"Host *\n    Include {self.home}/.ssh/fork_config\n")
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git",
                  wrapped=False)
        cp = self._git("push", "fork", "HEAD")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("no deploy key", cp.stderr)

    def test_no_ssh_config_at_all_says_nothing(self):
        self._git("remote", "add", "fork", "ssh://git@github-webkit/x/y.git",
                  wrapped=False)
        cp = self._git("push", "fork", "HEAD")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("no deploy key", cp.stderr)


class TestWhatAToolResolvesGitTo(_Gate):
    """The shape webkitcorepy uses, end to end: resolve `git` on PATH,
    follow it to a real file, and run that path for every git call after --
    which is what `git-webkit setup` (and so `wk new`'s verify) rests on."""

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


class TestWhereItSitsOnPath(unittest.TestCase):
    def test_the_shim_is_the_wall_under_a_name_a_realpath_can_land_on(self):
        """A symlink here is resolved away: webkitcorepy runs
        `os.path.realpath(shutil.which('git'))` once and every git call
        after it is that path (webkitscmpy's Scm.executable), so the wall
        arrived under its own name, refused, and left `git-webkit setup`
        reporting "No repository found" in every workspace. A file named
        `git`, holding no second copy of the gate: it sources the wall."""
        self.assertFalse(SHIM.is_symlink(), SHIM)
        lines = SHIM.read_text().splitlines()
        self.assertTrue(any(l.startswith("# wk-build-wall:") for l in lines[:5]),
                        "_real() skips a wall it can recognise; this one has to be one")
        body = [l for l in lines if l and not l.startswith("#") and l != "set -euo pipefail"]
        self.assertEqual(len(body), 1, body)
        self.assertTrue(body[0].startswith(". "), body[0])
        self.assertIn("/../wk-build-wall", body[0])

    def test_a_host_shell_does_not_pay_for_it(self):
        """The workstation sources the same path.sh, and `git` there has no
        workspace question to answer -- so the directory goes on PATH only
        where the workspace marker is."""
        self.assertIn('[ -f "$HOME/.wk-workspace" ]', PATH_SH)
        block = PATH_SH[PATH_SH.index('[ -f "$HOME/.wk-workspace" ]'):]
        self.assertIn("container/bin/ws", block[:block.index("fi")])

    def test_every_verb_it_watches_is_one_the_wall_paths_can_break(self):
        """A verb here that the commit wall cannot touch would be a note on a
        failure with another cause."""
        wall_paths = re.search(r'^WK_COMMIT_WALL_PATHS="([^"]+)"',
                               (REPO / "lib" / "common.sh").read_text(), re.M)
        self.assertTrue(wall_paths)
        self.assertIn("objects", wall_paths.group(1))
        self.assertIn("index.lock", wall_paths.group(1))
        for verb in ("commit", "add", "fetch", "push", "rebase"):
            self.assertIn(verb, _write_verbs())


if __name__ == "__main__":
    unittest.main()
