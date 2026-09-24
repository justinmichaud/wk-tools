"""The commit wall: while an agent holds a container workspace, nothing it runs
can write git history into the checkout the person will push -- the write-side
twin of `wk push` (only the person commits). `wk ai claude` runs the agent under
bwrap with the checkout's .git commit-parts read-only (commit_wall_prefix,
lib/wk/wall.py); a human `wk enter` shell is not wrapped and commits normally.
tests/test_ai.py drives the session that is wrapped and the refusal without bwrap.

The static half checks the wiring (no hardware). The functional half proves
the recipe against a throwaway repo inside the podman VM's image -- the same
image a container workspace runs -- and is skipped when that VM is not up.

Run: python3 -m unittest tests.test_commit_wall -v
"""
import shlex
import sys
import unittest

from tests.support import REPO, podman_vm_ssh, requires_podman_vm

sys.path.insert(0, str(REPO / "lib"))
from wk import wall  # noqa: E402

PUSH = (REPO / "cmd" / "push").read_text()
WALL = (REPO / "lib" / "wk" / "wall.py").read_text()


def prefix(src):
    return " ".join(wall.commit_wall_prefix(str(REPO), src))


class TestWiring(unittest.TestCase):
    def test_the_paths_live_in_one_place(self):
        for p in ("objects", "refs", "logs", "HEAD", "packed-refs"):
            self.assertIn(p, wall.COMMIT_WALL_PATHS)

    def test_prefix_binds_every_wall_path_read_only(self):
        line = prefix("/src/WebKit")
        self.assertTrue(line.startswith("bwrap "), line)
        self.assertIn("--dev-bind / /", line)
        for p in ("objects", "refs", "logs", "HEAD", "packed-refs"):
            self.assertIn(f"--ro-bind-try /src/WebKit/.git/{p} /src/WebKit/.git/{p}", line)
        self.assertTrue(line.rstrip().endswith("--"), line)

    def test_the_probe_measures_the_prefix_a_session_runs_under(self):
        """One recipe: `wk doctor`'s commit-wall probe builds its wrapper with
        the same function, so what it proves is what the agent gets."""
        self.assertIn('commit_wall_prefix(self.root, "$D")', WALL)

    def test_push_on_ends_a_running_session_before_it_loads_the_keys(self):
        on = PUSH.split("\n    def switch_on(self):", 1)[1]
        self.assertLess(on.index("self.end_sessions_first(self.agent_sessions())"), on.index("agent_load"))
        gate = PUSH.split("\n    def end_sessions_first(self, sessions):", 1)[1].split("\n    def ", 1)[0]
        self.assertIn("end_agent_sessions", gate)   # the wall goes with the session
        self.assertIn("act.confirm(", gate)

    def test_doctor_measures_the_wall(self):
        self.assertIn("commit wall", WALL)
        self.assertIn("did NOT block a commit", WALL)


def _vm_image():
    cp = podman_vm_ssh("podman images --format '{{.Repository}}:{{.Tag}}' | grep -m1 wkdev")
    return cp.stdout.strip() if cp.returncode == 0 else ""


@requires_podman_vm()
class TestTheWallHolds(unittest.TestCase):
    """The recipe run for real inside the workspace image: a commit is blocked,
    an ordinary write is not, and no unmount/shadow/nested-namespace escape
    lifts it -- while an unwrapped shell commits fine."""

    @classmethod
    def setUpClass(cls):
        cls.img = _vm_image()
        if not cls.img:
            raise unittest.SkipTest("no wkdev image in the podman VM")
        # the exact prefix production emits, for a repo at /tmp/r
        cls.prefix = prefix("/tmp/r")

    def _run(self, script):
        img = shlex.quote(self.img)
        inner = ("set -e; d=/tmp/r; rm -rf $d; mkdir $d; cd $d; "
                 "git init -q; git config user.email a@b; git config user.name a; "
                 "echo hi>f; git add f; git commit -qm one >/dev/null; "
                 f"W='{self.prefix}'; " + script)
        cp = podman_vm_ssh(
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


if __name__ == "__main__":
    unittest.main()


class TestBuiltinsAreAskedThroughAShell(unittest.TestCase):
    """A target's exec hands argv to an exec, not a shell, so a shell builtin
    such as `command -v` cannot be the program: measured live, the bare form
    failed in every container and no session started. cmd/ai asks
    through `sh -c`, and nothing under cmd/ or lib/ uses the bare form (`test`
    is a real program in every image and is fine)."""

    def test_cmd_ai_asks_for_bwrap_through_a_shell(self):
        from tests.test_ai import AI, SimRegistry, SimTarget
        from wk.machine import Fake
        fake = Fake()
        target = SimTarget(fake, {})
        AI.Ai(str(REPO), {}, SimRegistry({}, fake, target), target, "claude", "demo").wall_available()
        self.assertEqual(["sh -c command -v bwrap >/dev/null 2>&1"], target.asked)

    def test_no_bare_builtin_reaches_t_exec(self):
        import re
        bad = re.compile(r't_exec "\$[A-Za-z_]+" (command|type|alias|cd|builtin|source) ')
        for d in ("cmd", "lib", "targets"):
            for f in sorted((REPO / d).iterdir()):
                if not f.is_file() or f.suffix in (".py", ".pyc"):
                    continue
                for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
                    with self.subTest(file=f.name, line=n):
                        self.assertIsNone(bad.search(line), line.strip())
