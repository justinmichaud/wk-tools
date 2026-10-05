"""wk-tools' identity across machines (`wk doctor --probe-tools`, Remote.sync's peer arm, tools.committed): the commit, plus
`+dirty` for a tracked modification only -- untracked and ignored files never make two checkouts differ."""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Local  # noqa: E402

CMD_DOCTOR = REPO / "cmd" / "doctor"


def git(cwd, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True, text=True, timeout=60, check=check,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
    )


def kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def version(root):
    """`doctor --probe-tools`, pointed at an arbitrary checkout via WK_ROOT."""
    cp = subprocess.run([str(CMD_DOCTOR), "--probe-tools"], env={**os.environ, "WK_ROOT": str(root)},
                        capture_output=True, text=True, timeout=15)
    return kv(cp.stdout)


class TwoClonesCase(unittest.TestCase):
    """Two clones of one commit: a workstation (`a`) and a peer (`b`) after `wk sync --tools`."""

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
        for d in (self.a, self.b):
            git(d, "config", "pull.rebase", "false")
        # Untracked, so invisible to the dirty check; each clone runs its own cmd/doctor.
        for d in (self.a, self.b):
            (d / "cmd").symlink_to(REPO / "cmd")
            (d / "lib").symlink_to(REPO / "lib")
            (d / "wk").symlink_to(REPO / "wk")

    def _peer_sync(self, mine_root, their_root):
        """Remote.sync's peer arm with no ssh (WK_REMOTE_LOCAL)."""
        env = {k: v for k, v in os.environ.items() if k != "WK_ROOT"}
        t = places.Remote("peer", str(mine_root), dict(env, WK_REMOTE_PEER="1", WK_REMOTE_LOCAL="1",
                                                        WK_REMOTE_HOST="peer", WK_REMOTE_TOOLS=str(their_root)), Local())
        with mock.patch.dict(os.environ, {"WK_ROOT": ""}), contextlib.redirect_stderr(io.StringIO()) as err:
            os.environ.pop("WK_ROOT")
            ok = t.sync()
        return ok, err.getvalue()


class TestMachineLocalFilesDoNotDiffer(TwoClonesCase):

    def setUp(self):
        super().setUp()
        (self.a / ".DS_Store").write_bytes(b"\x00\x01")
        (self.a / "__pycache__").mkdir()
        (self.a / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (self.a / "local.conf").write_text("machine-local\n")
        (self.a / "new.sh").write_text("untracked, not ignored\n")

    def test_cmd_version_agrees_on_sha_and_dirty(self):
        va, vb = version(self.a), version(self.b)
        self.assertEqual(va["sha"], self.sha)
        self.assertEqual(vb["sha"], self.sha)
        self.assertEqual(va["dirty"], "no", va)
        self.assertEqual(vb["dirty"], "no", vb)

    def test_the_peer_branch_reports_in_sync(self):
        ok, err = self._peer_sync(self.a, self.b)
        self.assertTrue(ok, err)


class TestATrackedModificationDiffers(TwoClonesCase):
    def test_cmd_version_reports_dirty(self):
        (self.b / "f").write_text("two\n")
        vb = version(self.b)
        self.assertEqual(vb["sha"], self.sha)
        self.assertEqual(vb["dirty"], "yes")

    def test_the_peer_branch_reports_differs_and_fails(self):
        (self.b / "f").write_text("two\n")
        ok, err = self._peer_sync(self.a, self.b)
        self.assertFalse(ok)
        self.assertIn("+dirty", err)


if __name__ == "__main__":
    unittest.main()
