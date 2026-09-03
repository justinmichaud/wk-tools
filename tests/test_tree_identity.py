"""wk-tools' own identity across machines (cmd/version, targets/remote.sh's
peer branch of t_sync, lib/tools.sh's tools_committed): the commit, plus
`+dirty` for a *tracked* modification -- never a hash of file contents.

Two checkouts of one commit that differ only in untracked or ignored files
read identical -- a macOS checkout's .DS_Store and Zed prompt cache have no
Linux counterpart, and a comparison that hashes working-tree files reports
two clean checkouts of the same commit as different. A checkout that differs
by a *tracked* edit reads as `+dirty`.

Run: python3 -m unittest tests.test_tree_identity -v
"""
import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

CMD_VERSION = REPO / "cmd" / "version"


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _lift_func(path, func):
    return subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


T_SYNC = _lift_func(REPO / "targets" / "remote.sh", "t_sync")


def kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def version(root):
    """cmd/version, pointed at an arbitrary checkout via WK_ROOT -- the
    script itself always sources lib/common.sh from its own location
    (cmd/version's own fix), so a bare scratch clone with no lib/ of its
    own works as the tree reported on."""
    cp = subprocess.run([str(CMD_VERSION)], env={**os.environ, "WK_ROOT": str(root)},
                        capture_output=True, text=True, timeout=15)
    return kv(cp.stdout)


class TwoClonesCase(unittest.TestCase):
    """Two independent clones of one commit -- exactly the shape a
    workstation (`a`) and a peer (`b`) are in a converged `wk sync
    --tools`."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-treeid-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        origin = self.tmp / "origin"
        origin.mkdir()
        git(origin, "init", "-q", ".")
        (origin / "f").write_text("one\n")
        (origin / ".gitignore").write_text(
            "local.conf\n*.pyc\n.DS_Store\n__pycache__/\n")
        git(origin, "add", "-A")
        git(origin, "commit", "-qm", "one")
        self.sha = git(origin, "rev-parse", "HEAD").stdout.strip()
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        git(self.tmp, "clone", "-q", str(origin), str(self.a))
        git(self.tmp, "clone", "-q", str(origin), str(self.b))
        # Independent of this machine's own ~/.gitconfig: a `pull.rebase`
        # left on from the ambient environment would make `git pull
        # --ff-only` refuse a dirty tree for a reason that has nothing to
        # do with what this test is measuring.
        for d in (self.a, self.b):
            git(d, "config", "pull.rebase", "false")
        # t_sync's own `"$WK_ROOT/cmd/version"` call assumes WK_ROOT is a
        # full wk-tools checkout, not a bare scratch clone; symlinked in
        # (untracked, invisible to a --untracked-files=no dirty check) so
        # each clone can answer for itself, the same way a real peer's own
        # checkout carries its own cmd/ and lib/.
        for d in (self.a, self.b):
            (d / "cmd").symlink_to(REPO / "cmd")
            (d / "lib").symlink_to(REPO / "lib")

    def _peer_sync(self, mine_root, their_root):
        """t_sync's peer branch, lifted from targets/remote.sh: `mine` is
        this process's own `$WK_ROOT/cmd/version` (the real call the
        function makes); `their` is answered by a stubbed _rsh_q that runs
        `bash -c` against the other clone -- exactly the shape an ssh
        wrapper hands a remote command string, no ssh or machine involved."""
        stubs = f"""
WK_TARGET=peer
WK_REMOTE_HOST=peer
_remote_probe() {{ :; }}
_remote_peer() {{ return 0; }}
_peer_why_behind() {{ printf 'stubbed reason'; }}
t_tools() {{ printf %s {shlex.quote(str(their_root))}; }}
# WK_ROOT cleared, not inherited from this side's own: a real ssh would
# never carry it across, and cmd/version's own default (unset WK_ROOT falls
# back to wherever it was invoked from) is what points this call at the
# other clone instead of this one.
_rsh_q() {{ WK_ROOT= bash -c "$1"; }}
"""
        return bash(". lib/common.sh\n" + stubs + T_SYNC + "\nt_sync\n",
                    env={"WK_ROOT": str(mine_root)})


class TestMachineLocalFilesDoNotDiffer(TwoClonesCase):
    """The defect itself: a machine-local file that legitimately exists on
    one checkout and not the other (a .DS_Store, a __pycache__/*.pyc, a
    gitignored local.conf) must never make two clean checkouts of the same
    commit read as different."""

    def setUp(self):
        super().setUp()
        (self.a / ".DS_Store").write_bytes(b"\x00\x01")
        (self.a / "__pycache__").mkdir()
        (self.a / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (self.a / "local.conf").write_text("machine-local\n")

    def test_cmd_version_agrees_on_sha_and_dirty(self):
        va, vb = version(self.a), version(self.b)
        self.assertEqual(va["sha"], self.sha)
        self.assertEqual(vb["sha"], self.sha)
        self.assertEqual(va["dirty"], "no", va)
        self.assertEqual(vb["dirty"], "no", vb)

    def test_the_peer_branch_reports_in_sync(self):
        cp = self._peer_sync(self.a, self.b)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("in sync", cp.stderr)
        self.assertNotIn("DIFFERS", cp.stderr)


class TestATrackedModificationDiffers(TwoClonesCase):
    def test_cmd_version_reports_dirty(self):
        (self.b / "f").write_text("two\n")
        vb = version(self.b)
        self.assertEqual(vb["sha"], self.sha)
        self.assertEqual(vb["dirty"], "yes")

    def test_the_peer_branch_reports_differs_and_fails(self):
        (self.b / "f").write_text("two\n")
        cp = self._peer_sync(self.a, self.b)
        self.assertNotEqual(cp.returncode, 0, "a tracked edit is not in sync")
        self.assertIn("still DIFFERS", cp.stderr)
        self.assertIn("+dirty", cp.stderr)


class TestUntrackedNonIgnoredFileIsNotDirty(TwoClonesCase):
    """Dirtiness is tracked-only: a new file not yet `git add`-ed is not a
    change to this repository any more than an ignored one is."""

    def test_cmd_version_reports_clean(self):
        (self.a / "new.sh").write_text("not added yet\n")
        va = version(self.a)
        self.assertEqual(va["dirty"], "no", va)

    def test_tools_committed_accepts_it(self):
        (self.a / "new.sh").write_text("not added yet\n")
        cp = bash(
            f'WK_ROOT={REPO}\n'
            '. "$WK_ROOT/lib/common.sh"\n'
            '. "$WK_ROOT/lib/tools.sh"\n'
            f'WK_ROOT={self.a}\n'
            'tools_committed\n'
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestNoTreeHashMachinery(unittest.TestCase):
    """Identity is the commit, and there is no second answer anywhere: a
    hash of file contents needs an exclusion list that grows one ignored
    filename at a time. So its names -- the function, the flag, the kv field,
    the variable naming the far side's expected tree hash -- appear nowhere in
    the tree."""

    DIRS = ["cmd", "lib", "targets", "bench", "host"]

    def _grep(self, pattern):
        cp = subprocess.run(
            ["grep", "-rlE", pattern, *self.DIRS],
            cwd=str(REPO), capture_output=True, text=True,
        )
        return [l for l in cp.stdout.splitlines() if l]

    def test_tree_hash_function_is_gone(self):
        self.assertEqual(self._grep(r"\btree_hash\b"), [])

    def test_the_tree_flag_is_gone(self):
        self.assertEqual(self._grep(r"--tree\b"), [])

    def test_wk_expect_tree_is_gone(self):
        self.assertEqual(self._grep(r"\bWK_EXPECT_TREE\b"), [])

    def test_cmd_versions_own_sha256_machinery_is_gone(self):
        """cmd/version no longer names a SHA256 program at all -- not the
        bare `sha256sum`/`shasum` commands, which lib/image.sh, cmd/pi and
        the macOS host scripts still use for unrelated integrity checks
        (a downloaded image, a written card, an SDK patch), but the two
        names cmd/version itself defined to pick one."""
        text = (REPO / "cmd" / "version").read_text()
        for name in ("SHA256", "_sha256"):
            self.assertNotIn(name, text, f"{name} still in cmd/version")


if __name__ == "__main__":
    unittest.main()
