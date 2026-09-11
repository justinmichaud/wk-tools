"""container/sdk-refresh.sh -- the one place the webkit-container-sdk checkout
is fetched and moved onto its remote's current default branch. The image tag
`wkdev-create` asks for is read out of that checkout (the SDK's own
`get_sdk_version`), so a checkout that never fetches pins every container this
machine makes to whatever commit `./setup` first cloned.

host/linux/sdk.sh, host/macos/vmtools.sh (over ssh) and targets/container.sh's
`t_sdk_refresh` (from `wk new`, before `wkdev-create`) all invoke this script
rather than each carrying a copy of the fetch.

Driven against real, disposable git repos: a temporary "upstream" and a
clone of it, never the real webkit-container-sdk or a workspace.

Run: python3 -m unittest tests.test_sdk_refresh -v
"""
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
    """Run the script. The default patcher is a stub, since a disposable git
    repo is not an SDK checkout; patcher=SCRIPT.parent / "sdk-patches" /
    "apply.sh" runs the real one."""
    if patcher is None:
        patcher = checkout.parent / "patcher.sh"
        patcher.write_text("#!/bin/sh\nexit 0\n")
    env = dict(os.environ, WK_SDK_PATCHER=str(patcher))
    return subprocess.run(
        ["bash", str(SCRIPT), str(checkout)],
        capture_output=True, text=True, timeout=timeout, env=env,
    )


class TestSdkRefresh(unittest.TestCase):
    def test_a_fresh_checkout_lands_on_the_default_branch_tip(self):
        with scratch_dir() as d:
            upstream = make_upstream(d / "upstream")
            git("clone", "-q", str(upstream), str(d / "checkout"), cwd=d)
            checkout = d / "checkout"
            tip = head(upstream)

            cp = refresh(checkout)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(head(checkout), tip)

    def test_the_checkout_moves_when_upstream_advances(self):
        with scratch_dir() as d:
            upstream = make_upstream(d / "upstream")
            git("clone", "-q", str(upstream), str(d / "checkout"), cwd=d)
            checkout = d / "checkout"
            old_tip = head(checkout)

            new_tip = commit_more(upstream, "two")
            self.assertNotEqual(old_tip, new_tip)

            cp = refresh(checkout)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(head(checkout), new_tip)

    def test_a_renamed_default_branch_is_followed(self):
        """The remote's own choice, re-read every time (`git remote set-head
        -a`), not the branch name the checkout happened to clone."""
        with scratch_dir() as d:
            upstream = make_upstream(d / "upstream")
            git("clone", "-q", str(upstream), str(d / "checkout"), cwd=d)
            checkout = d / "checkout"

            git("branch", "-m", "main", "master", cwd=upstream)
            new_tip = commit_more(upstream, "two")

            cp = refresh(checkout)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(head(checkout), new_tip)
            self.assertEqual(
                git("symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                    cwd=checkout).stdout.strip(),
                "origin/master",
            )

    def test_an_unreachable_remote_refuses_with_the_remedy(self):
        with scratch_dir() as d:
            upstream = make_upstream(d / "upstream")
            git("clone", "-q", str(upstream), str(d / "checkout"), cwd=d)
            checkout = d / "checkout"
            git("remote", "set-url", "origin", str(d / "no-such-remote"), cwd=checkout)
            tip_before = head(checkout)

            cp = refresh(checkout)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("nothing useful to fall back to", cp.stdout + cp.stderr)
            self.assertIn("Retry once the network is back", cp.stdout + cp.stderr)
            # Refused, not half-moved: the checkout is exactly where it was.
            self.assertEqual(head(checkout), tip_before)

    def test_the_patches_are_applied_after_the_reset(self):
        """A reset alone leaves a wkdev-create that refuses wk's options; the
        patcher runs last, on the reset tree."""
        text = SCRIPT.read_text()
        self.assertLess(text.index("git reset --hard"), text.index("sdk-patches/apply.sh"))
        with scratch_dir() as d:
            upstream = make_upstream(d / "upstream")
            git("clone", "-q", str(upstream), str(d / "checkout"), cwd=d)
            cp = refresh(d / "checkout",
                         patcher=SCRIPT.parent / "sdk-patches" / "apply.sh")
            self.assertNotEqual(0, cp.returncode)
            self.assertIn("not an SDK checkout", cp.stderr)

    def test_not_a_checkout_at_all_refuses_by_name(self):
        with scratch_dir() as d:
            cp = refresh(d)
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("not an SDK checkout", cp.stdout + cp.stderr)


if __name__ == "__main__":
    unittest.main()
