"""The refspecs follow the layout of whatever a checkout fetches from:
origin's branches are a wk mirror's own heads, every other upstream is
namespaced under its own name, and an upstream asked directly has only
refs/heads. A fifth upstream needs no change beyond `git.REMOTES`, which is
what the tests with one assert.

  - `REMOTES` (lib/wk/git.py): the one list of upstreams.
  - `fetch_refspecs`: what one remote is asked for, by the source it is asked of.
  - `fetch_config`: the one writer of those specs into a checkout's
    `remote.<r>.fetch`, as git argv steps.
  - `mirror_refresh_script`: the mirror side of the same layout.

Run: python3 tests/run.py -k tests.test_cli_refspecs
"""
import sys
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import git  # noqa: E402

FIFTH = git.REMOTES + (("fifth", "https://example.com/fifth/WebKit.git"),)


def _namespaced(remote):
    return f"+refs/remotes/{remote}/*:refs/remotes/{remote}/*"


def _argvs(steps):
    return [tuple(s[0]) for s in steps if not isinstance(s, str)]


class TestRemotesIsTheOneList(unittest.TestCase):
    """Today's four upstreams, in order -- pinned so a change to this list is a
    deliberate edit, not a silent drift the refspec tests below would mask."""

    def test_todays_four_remotes_in_order(self):
        self.assertEqual([n for n, _ in git.REMOTES], ["origin", "wpe", "fork", "forkwpe"])

    def test_the_bash_name_prints_the_same_list(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; wk_remotes')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual([tuple(l.split()) for l in cp.stdout.splitlines()], list(git.REMOTES))

    def test_the_mirror_fetches_every_one_and_a_fifth_with_no_other_change(self):
        script = git.mirror_refresh_script("/m", ["main"], FIFTH)
        self.assertIn("for r in origin wpe fork forkwpe fifth; do", script)
        self.assertIn("git -C \"$M\" config --replace-all remote.fifth.fetch '+refs/heads/*:refs/remotes/fifth/*'", script)


class TestFetchRefspecs(unittest.TestCase):
    """From a wk mirror, origin's branches are that mirror's own heads and every
    other upstream is namespaced under its own name; from an upstream itself,
    every remote's branches are refs/heads. Origin is narrowed to the mirrored
    branches either way -- WebKit/WebKit's 924 heads are what git's default
    refspec writes a remote-tracking ref for, one by one."""

    def test_origin_from_a_mirror_is_narrowed_to_the_mirrored_branches(self):
        self.assertEqual(git.fetch_refspecs("origin", "/mirror/WebKit.git", ["main"]),
                         ["+refs/heads/main:refs/remotes/origin/main"])

    def test_origin_from_the_upstream_itself_is_narrowed_the_same_way(self):
        self.assertEqual(git.fetch_refspecs("origin", "", ["main"]),
                         ["+refs/heads/main:refs/remotes/origin/main"])

    def test_the_mirrored_branches_are_the_one_list(self):
        self.assertEqual(git.fetch_refspecs("origin", "/m", git.mirror_branches({"WK_MIRROR_BRANCHES": "main wpe-2.46"})),
                         ["+refs/heads/main:refs/remotes/origin/main",
                          "+refs/heads/wpe-2.46:refs/remotes/origin/wpe-2.46"])

    def test_every_other_upstream_from_a_mirror_is_namespaced_on_both_sides(self):
        for remote in ("wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(git.fetch_refspecs(remote, "/m", ["main"]), [_namespaced(remote)])

    def test_every_other_upstream_from_itself_maps_its_heads(self):
        for remote in ("wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(git.fetch_refspecs(remote, "", ["main"]), [f"+refs/heads/*:refs/remotes/{remote}/*"])

    def test_a_fifth_upstream_needs_no_change_here(self):
        self.assertEqual(git.fetch_refspecs("fifth", "/m", ["main"]), [_namespaced("fifth")])
        self.assertEqual(git.fetch_refspecs("fifth", "", ["main"]), ["+refs/heads/*:refs/remotes/fifth/*"])


class TestTheWiringWritesThoseRefspecs(unittest.TestCase):
    """fetch_config puts exactly fetch_refspecs' answer into `remote.<r>.fetch`,
    one `--add` per spec after the old ones go, so a checkout's configuration
    and a `wk sync` fetch cannot disagree about what is asked for."""

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
        self.assertIn("git config --add 'url./m/My Mirror.git.insteadOf' 'https://github.com/WebKit/WebKit.git'", script)
        self.assertIn("git config --unset-all remote.origin.fetch 2>/dev/null || true", script)


if __name__ == "__main__":
    unittest.main()
