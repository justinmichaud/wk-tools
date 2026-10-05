"""container/sdk-refresh.sh: the webkit-container-sdk checkout is fetched and moved onto its remote's default
branch, since the image tag `wkdev-create` asks for is read out of that checkout."""
import os
import subprocess
import unittest

from tests.support import REPO, scratch_dir

SCRIPT = REPO / "container" / "sdk-refresh.sh"


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        check=True, env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
                          "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com",
                          "PATH": "/usr/bin:/bin"},
    )


def head(cwd):
    return git("rev-parse", "HEAD", cwd=cwd).stdout.strip()


def make_upstream(dirpath, branch="main"):
    dirpath.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", branch, ".", cwd=dirpath)
    (dirpath / "f").write_text("one\n")
    git("add", "f", cwd=dirpath)
    git("commit", "-qm", "one", cwd=dirpath)
    return dirpath


def commit_more(dirpath, text):
    (dirpath / "f").write_text(text + "\n")
    git("add", "f", cwd=dirpath)
    git("commit", "-qm", text, cwd=dirpath)
    return head(dirpath)


def refresh(checkout, timeout=30, patcher=None):
    if patcher is None:
        patcher = checkout.parent / "patcher.sh"
        patcher.write_text("#!/bin/sh\nexit 0\n")
    env = dict(os.environ, WK_SDK_PATCHER=str(patcher))
    return subprocess.run(
        ["bash", str(SCRIPT), str(checkout)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class TestSdkRefresh(unittest.TestCase):
    def setUp(self):
        self.d = self.enterContext(scratch_dir())
        self.upstream = make_upstream(self.d / "upstream")
        self.checkout = self.d / "checkout"
        git("clone", "-q", str(self.upstream), str(self.checkout), cwd=self.d)

    def test_the_checkout_moves_when_upstream_advances(self):
        new_tip = commit_more(self.upstream, "two")
        cp = refresh(self.checkout)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(head(self.checkout), new_tip)

    def test_a_renamed_default_branch_is_followed(self):
        git("branch", "-m", "main", "master", cwd=self.upstream)
        new_tip = commit_more(self.upstream, "two")
        cp = refresh(self.checkout)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(head(self.checkout), new_tip)
        self.assertEqual(git("symbolic-ref", "--short", "refs/remotes/origin/HEAD", cwd=self.checkout).stdout.strip(),
                         "origin/master")

    def test_an_unreachable_remote_refuses_with_the_remedy_and_moves_nothing(self):
        git("remote", "set-url", "origin", str(self.d / "no-such-remote"), cwd=self.checkout)
        tip_before = head(self.checkout)
        cp = refresh(self.checkout)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("nothing useful to fall back to", cp.stdout + cp.stderr)
        self.assertIn("Retry once the network is back", cp.stdout + cp.stderr)
        self.assertEqual(head(self.checkout), tip_before)

    def test_the_patches_are_applied_after_the_reset(self):
        cp = refresh(self.checkout, patcher=SCRIPT.parent / "sdk-patches" / "apply.sh")
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("not an SDK checkout", cp.stderr)

    def test_a_refresh_that_changes_nothing_says_nothing_about_the_patches(self):
        patcher = self.d / "patcher.sh"
        patcher.write_text('#!/bin/sh\necho patched > "$1/f"\necho "added --x" >&2\n')
        first = refresh(self.checkout, patcher=patcher)
        again = refresh(self.checkout, patcher=patcher)
        self.assertIn("added --x", first.stdout + first.stderr)
        self.assertEqual((0, ""), (again.returncode, again.stdout + again.stderr))
        commit_more(self.upstream, "two")
        moved = refresh(self.checkout, patcher=patcher)
        self.assertIn("added --x", moved.stdout + moved.stderr)
        patcher.write_text('#!/bin/sh\necho patched > "$1/f"\necho "verify failed: x" >&2\nexit 1\n')
        failed = refresh(self.checkout, patcher=patcher)
        self.assertNotEqual(0, failed.returncode)
        self.assertIn("verify failed: x", failed.stdout + failed.stderr)

    def test_not_a_checkout_at_all_refuses_by_name(self):
        empty = self.d / "empty"
        empty.mkdir()
        cp = refresh(empty)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not an SDK checkout", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
