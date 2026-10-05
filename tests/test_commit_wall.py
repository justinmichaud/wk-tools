"""The commit wall (wall.commit_wall_prefix), proved live against a throwaway repo in the workspace image."""
import shlex
import sys
import unittest

from tests.support import REPO, container_side, requires_container_place

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
        from wk.machine import HAVE, Fake
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


if __name__ == "__main__":
    unittest.main()
