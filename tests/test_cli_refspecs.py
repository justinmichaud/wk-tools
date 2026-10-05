"""The refspecs follow the layout of whatever a checkout fetches from: a mirror or the upstreams."""
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import git, images  # noqa: E402

FIFTH = git.REMOTES + (("fifth", "https://example.com/fifth/WebKit.git"),)


def _namespaced(remote):
    return f"+refs/remotes/{remote}/*:refs/remotes/{remote}/*"


def _argvs(steps):
    return [tuple(s[0]) for s in steps if not isinstance(s, str)]


class TestFetchRefspecs(unittest.TestCase):

    def test_origin_is_narrowed_to_the_mirrored_branches_wherever_it_comes_from(self):
        for mirror in ("/mirror/WebKit.git", ""):
            self.assertEqual(git.fetch_refspecs("origin", mirror, ["main"]), ["+refs/heads/main:refs/remotes/origin/main"])

    def test_the_mirrored_branches_are_the_one_list(self):
        self.assertEqual(git.fetch_refspecs("origin", "/m", images.mirror_branches({"WK_MIRROR_BRANCHES": "main wpe-2.46"})),
                         ["+refs/heads/main:refs/remotes/origin/main",
                          "+refs/heads/wpe-2.46:refs/remotes/origin/wpe-2.46"])

    def test_every_other_upstream_from_a_mirror_is_namespaced_on_both_sides(self):
        for remote in ("wpe", "fork", "forkwpe", "fifth"):
            with self.subTest(remote=remote):
                self.assertEqual(git.fetch_refspecs(remote, "/m", ["main"]), [_namespaced(remote)])
                self.assertEqual(git.fetch_refspecs(remote, "", ["main"]), [f"+refs/heads/*:refs/remotes/{remote}/*"])


class TestTheWiringWritesThoseRefspecs(unittest.TestCase):

    def test_each_remote_gets_its_specs_and_no_tags(self):
        steps = git.fetch_config("/mirror/WebKit.git", ["main"], FIFTH)
        argvs = _argvs(steps)
        for remote, _ in FIFTH:
            with self.subTest(remote=remote):
                unset = argvs.index(("git", "config", "--unset-all", f"remote.{remote}.fetch"))
                adds = [a[-1] for a in argvs if a[:4] == ("git", "config", "--add", f"remote.{remote}.fetch")]
                self.assertEqual(adds, git.fetch_refspecs(remote, "/mirror/WebKit.git", ["main"]))
                self.assertLess(unset, argvs.index(("git", "config", "--add", f"remote.{remote}.fetch", adds[0])))
                self.assertIn(("git", "config", f"remote.{remote}.tagOpt", "--no-tags"), argvs)

    def test_an_unset_of_nothing_is_not_a_failure(self):
        for step in git.fetch_config("/m", ["main"]):
            if not isinstance(step, str) and "--unset-all" in step[0]:
                self.assertEqual(step[1], git.TOLERATE)

    def test_every_remote_is_rewritten_to_the_mirror_given(self):
        argvs = _argvs(git.fetch_config("/mirror/WebKit.git", ["main"]))
        for _, url in git.REMOTES:
            self.assertIn(("git", "config", "--add", "url./mirror/WebKit.git.insteadOf", url), argvs)

    def test_no_mirror_rewrites_nothing_and_asks_the_upstreams(self):
        argvs = _argvs(git.fetch_config("", ["main"]))
        self.assertFalse([a for a in argvs if "insteadOf" in " ".join(a)])
        self.assertIn(("git", "config", "--add", "remote.wpe.fetch", "+refs/heads/*:refs/remotes/wpe/*"), argvs)

    def test_the_rendered_script_quotes_what_the_shell_would_split(self):
        script = git.render("/src/Web Kit", git.fetch_config("/m/My Mirror.git", ["main"]))
        self.assertTrue(script.startswith("set -e\ncd '/src/Web Kit'\n"), script)
        self.assertIn("git config --add 'url./m/My Mirror.git.insteadOf' https://github.com/WebKit/WebKit.git", script)
        self.assertIn("git config --unset-all remote.origin.fetch 2>/dev/null || true", script)


if __name__ == "__main__":
    unittest.main()
