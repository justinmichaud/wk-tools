"""The commit wall: while an agent holds a container workspace, nothing it runs
can write git history into the checkout the person will push -- the write-side
twin of `wk push` (only the person commits). `wk claude` runs the agent under
bwrap with the checkout's .git commit-parts read-only (commit_wall_prefix,
lib/common.sh); a human `wk enter` shell is not wrapped and commits normally.

The static half checks the wiring (no hardware). The functional half proves
the recipe against a throwaway repo inside the podman VM's image -- the same
image a container workspace runs -- and is skipped when that VM is not up.

Run: python3 -m unittest tests.test_commit_wall -v
"""
import shlex
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, bash, podman_vm_ssh, requires_podman_vm

COMMON = (REPO / "lib" / "common.sh").read_text()
CLAUDE = (REPO / "cmd" / "claude").read_text()
PUSH = (REPO / "cmd" / "push").read_text()
VERIFY = (REPO / "cmd" / "verify").read_text()


class TestWiring(unittest.TestCase):
    def test_the_recipe_lives_in_one_place(self):
        self.assertIn("commit_wall_prefix()", COMMON)
        self.assertIn("WK_COMMIT_WALL_PATHS=", COMMON)
        for p in ("objects", "refs", "logs", "HEAD", "packed-refs"):
            self.assertIn(p, COMMON.split("WK_COMMIT_WALL_PATHS=", 1)[1].split("\n", 1)[0])

    def test_prefix_binds_every_wall_path_read_only(self):
        out = bash(f'. "{REPO}/lib/common.sh"; commit_wall_prefix /src/WebKit')
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        line = out.stdout.strip()
        self.assertTrue(line.startswith("bwrap "), line)
        self.assertIn("--dev-bind / /", line)
        for p in ("objects", "refs", "logs", "HEAD", "packed-refs"):
            self.assertIn(f"--ro-bind-try /src/WebKit/.git/{p} /src/WebKit/.git/{p}", line)
        self.assertTrue(line.rstrip().endswith("--"), line)

    def test_claude_refuses_a_container_without_bwrap(self):
        # the refusal names bwrap and the remedy, and it is gated on the
        # container target (a vm/remote has no such bind).
        self.assertIn("command -v bwrap", CLAUDE)
        self.assertIn("the commit wall needs bwrap", CLAUDE)

    def test_claude_wraps_the_agent_exec_with_the_wall(self):
        self.assertIn("WALL=\"$(commit_wall_prefix", CLAUDE)
        self.assertIn("exec ${WALL}$(sh_quote \"$CLAUDE_BIN\")", CLAUDE)

    def test_push_on_is_a_forceable_barrier_while_a_session_runs(self):
        seg = PUSH.split("\non)\n", 1)[1]
        self.assertIn("agent_sessions", seg)
        self.assertIn("barrier ", seg)  # refuse, or --force past with a record
        self.assertNotIn('die "push stays OFF', seg)

    def test_verify_measures_the_wall(self):
        self.assertIn("commit wall", VERIFY)
        self.assertIn("did NOT block a commit", VERIFY)


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
        cls.prefix = bash(f'. "{REPO}/lib/common.sh"; commit_wall_prefix /tmp/r').stdout.strip()

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
