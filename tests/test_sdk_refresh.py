"""container/sdk-refresh.sh: the webkit-container-sdk checkout is a clone of the fork at SDK_PIN, since the
wkdev-create it runs carries wk's edits only on the fork."""
import subprocess
import unittest

from tests.support import REPO, scratch_dir
from wk import project

SCRIPT = REPO / "container" / "sdk-refresh.sh"


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t.com", "PATH": "/usr/bin:/bin"},
    ).stdout.strip()


def commit(dirpath, text):
    (dirpath / "f").write_text(text + "\n")
    git("add", "f", cwd=dirpath)
    git("commit", "-qm", text, cwd=dirpath)
    return git("rev-parse", "HEAD", cwd=dirpath)


def pin(checkout, repo, sha, timeout=30):
    return subprocess.run(["bash", str(SCRIPT), str(checkout), str(repo), sha], capture_output=True, text=True, timeout=timeout)


class TestSdkRefresh(unittest.TestCase):
    def setUp(self):
        self.d = self.enterContext(scratch_dir())
        self.fork = self.d / "fork"
        self.fork.mkdir()
        git("init", "-q", "-b", "wk", ".", cwd=self.fork)
        self.one = commit(self.fork, "one")
        self.checkout = self.d / "checkout"

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.checkout)

    def test_a_missing_checkout_is_cloned_at_the_pin(self):
        two = commit(self.fork, "two")
        cp = pin(self.checkout, self.fork, self.one)
        self.assertEqual((cp.returncode, cp.stdout + cp.stderr), (0, ""))
        self.assertEqual(self.head(), self.one)
        self.assertNotEqual(self.head(), two)

    def test_a_moved_head_is_put_back_on_the_pin_and_stray_edits_go(self):
        pin(self.checkout, self.fork, self.one)
        two = commit(self.fork, "two")
        self.assertEqual(pin(self.checkout, self.fork, two).returncode, 0)
        self.assertEqual(self.head(), two)
        (self.checkout / "f").write_text("edited\n")
        (self.checkout / "stray").write_text("x\n")
        self.assertEqual(pin(self.checkout, self.fork, two).returncode, 0)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all", cwd=self.checkout), "")
        self.assertEqual(pin(self.checkout, self.fork, self.one).returncode, 0)
        self.assertEqual(self.head(), self.one)

    def test_a_checkout_at_the_pin_needs_no_network(self):
        pin(self.checkout, self.fork, self.one)
        self.fork.rename(self.d / "gone")
        cp = pin(self.checkout, self.fork, self.one)
        self.assertEqual((cp.returncode, cp.stdout + cp.stderr), (0, ""))

    def test_an_unreachable_fork_refuses_with_the_remedy(self):
        cp = pin(self.checkout, self.d / "no-such-fork", self.one)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("Retry once the network is back", cp.stderr)

    def test_a_pin_the_fork_lacks_is_refused_naming_the_setting(self):
        pin(self.checkout, self.fork, self.one)
        cp = pin(self.checkout, self.fork, "0" * 40)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("SDK_PIN in lib/wk/project.json", cp.stderr)
        self.assertEqual(self.head(), self.one)

    def test_a_checkout_of_another_origin_is_converged(self):
        other = self.d / "upstream"
        other.mkdir()
        git("init", "-q", "-b", "main", ".", cwd=other)
        commit(other, "up")
        git("clone", "-q", str(other), str(self.checkout), cwd=self.d)
        cp = pin(self.checkout, self.fork, self.one)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("origin moved to %s" % self.fork, cp.stdout)
        self.assertEqual((self.head(), git("remote", "get-url", "origin", cwd=self.checkout)), (self.one, str(self.fork)))
        again = pin(self.checkout, self.fork, self.one)
        self.assertEqual((again.returncode, again.stdout + again.stderr), (0, ""))

    def test_a_directory_that_is_not_a_checkout_is_refused_by_name(self):
        self.checkout.mkdir()
        (self.checkout / "x").write_text("x")
        cp = pin(self.checkout, self.fork, self.one)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not an SDK checkout", cp.stderr)

    def test_the_project_reader_prints_the_pin_the_stages_pass(self):
        out = subprocess.run(["python3", "-m", "wk.project", "SDK_PIN"], cwd=str(REPO), capture_output=True, text=True,
                             env={"PYTHONPATH": str(REPO / "lib"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(out.stdout.strip(), project.get("SDK_PIN"))
        self.assertRegex(project.get("SDK_PIN"), r"^[0-9a-f]{40}$")
        bad = subprocess.run(["python3", "-m", "wk.project", "NOPE"], cwd=str(REPO), capture_output=True, text=True,
                             env={"PYTHONPATH": str(REPO / "lib"), "PATH": "/usr/bin:/bin"})
        self.assertEqual(bad.returncode, 2)
        self.assertIn("no such key: NOPE", bad.stderr)


if __name__ == "__main__":
    unittest.main()
