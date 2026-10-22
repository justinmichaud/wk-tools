"""The commit wall: wall.commit_wall_prefix, proved live against a throwaway repo in the workspace image, and `git` in a
workspace (container/bin/ws/git), which runs the command exactly as it would have and names the remedy for a failure the
wall caused (a scratch repository with `chmod -w .git/objects` stands in for the bind)."""
import os
import shlex
import shutil
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, container_side, requires_container_place

sys.path.insert(0, str(REPO / "lib"))
from wk import wall  # noqa: E402


def prefix(src):
    return " ".join(wall.commit_wall_prefix(str(REPO), src))


def _workspace_image():
    cp = container_side("podman images --format '{{.Repository}}:{{.Tag}}' | grep -m1 wkdev")
    return cp.stdout.strip() if cp.returncode == 0 else ""


@requires_container_place()
class TestTheWallHolds(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.img = _workspace_image()
        if not cls.img:
            raise unittest.SkipTest("no wkdev image for the container place")
        cls.prefix = prefix("/tmp/r")

    def _run(self, script):
        img = shlex.quote(self.img)
        inner = ("set -e; d=/tmp/r; rm -rf $d; mkdir $d; cd $d; "
                 "git init -q; git config user.email a@b; git config user.name a; "
                 "echo hi>f; git add f; git commit -qm one >/dev/null; "
                 f"W='{self.prefix}'; " + script)
        cp = container_side(
            f"podman run --rm --userns keep-id --security-opt no-new-privileges "
            f"--entrypoint /bin/sh {img} -c {shlex.quote(inner)}",
            timeout=120)
        return cp.stdout

    def test_commit_blocked_write_allowed_status_ok(self):
        out = self._run(
            'cd $d; echo two>>f; '
            '($W sh -c "git add f && git commit -qm two" >/dev/null 2>&1 && echo COMMITTED || echo BLOCKED); '
            '($W sh -c "echo x>>f" && echo WROTE || echo NOWRITE); '
            '($W git status >/dev/null 2>&1 && echo STATUS-OK || echo STATUS-BROKE)')
        self.assertIn("BLOCKED", out)
        self.assertIn("WROTE", out)
        self.assertIn("STATUS-OK", out)

    def test_no_escape_lifts_it(self):
        out = self._run(
            'cd $d; '
            '($W sh -c "umount .git/refs" 2>/dev/null && echo UNMOUNT || echo NO-UNMOUNT); '
            'mkdir -p /tmp/w; ($W sh -c "mount --bind /tmp/w .git/refs" 2>/dev/null && echo SHADOW || echo NO-SHADOW); '
            '($W sh -c "unshare -Urm sh -c \\"umount .git/refs 2>/dev/null && echo NESTED || echo NO-NESTED\\"" 2>/dev/null)')
        self.assertIn("NO-UNMOUNT", out)
        self.assertIn("NO-SHADOW", out)
        self.assertNotIn("NESTED\n", out.replace("NO-NESTED", ""))

    def test_an_unwrapped_shell_commits(self):
        out = self._run('cd $d; echo two>>f; (git add f && git commit -qm two >/dev/null 2>&1 && echo OK || echo NO)')
        self.assertIn("OK", out)


class TestBuiltinsAreAskedThroughAShell(unittest.TestCase):
    """A place's exec runs argv, not a shell, so a builtin such as `command -v` goes through `sh -c` (machine.HAVE)."""

    def test_cmd_ai_asks_for_bwrap_through_a_shell(self):
        from tests.test_ai import AI, sim_registry, SimDriver
        from wk.machine import HAVE
        from tests.fakes import Fake
        fake = Fake()
        driver = SimDriver(fake, {})
        AI.Ai(str(REPO), {}, sim_registry({}, fake, driver), driver, "claude", "demo").wall_available()
        self.assertEqual([" ".join(HAVE + ("bwrap",))], driver.asked)

    def test_no_bare_builtin_reaches_a_places_exec(self):
        import re
        bad = re.compile(r'exec\([^,()]+, \[\s*"(command|type|alias|cd|builtin|source)"')
        files = [f for f in sorted((REPO / "cmd").iterdir()) if f.is_file()] + sorted((REPO / "lib" / "wk").rglob("*.py"))
        self.assertIsNotNone(bad.search('place.exec(ws, ["command", "-v", "bwrap"])'))
        found = ["%s:%d" % (f.name, n) for f in files
                 for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1) if bad.search(line)]
        self.assertEqual([], found)



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
        self.assertIn("wk enter", cp.stderr)
        self.assertLess(cp.stderr.index("insufficient permission"), cp.stderr.index("wk:"))

    def test_an_unwalled_checkout_is_silent(self):
        cp = self._git("commit", "--allow-empty", "-m", "two")
        self.assertEqual(cp.returncode, 0, cp.stderr)
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
