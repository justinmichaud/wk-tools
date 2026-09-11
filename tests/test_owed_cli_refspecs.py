"""The refspecs follow the layout of whatever a checkout fetches from:
origin's branches are a wk mirror's own heads, every other upstream is
namespaced under its own name, and an upstream asked directly has only
refs/heads. A fifth upstream needs no change beyond `wk_remotes`, which is
what the last test here asserts.

The functions are lifted and driven directly, with sed (the
tests/test_wifi_seed.py idiom), so this tracks the exact code that ships:

  - `wk_remotes` (lib/store.sh): the one list of upstreams, a plain heredoc.
  - `wk_mirror_default_remotes` (lib/store.sh): `wk_remotes | awk ...`,
    read through the function rather than the heredoc directly.
  - `wk_fetch_refspecs` (lib/store.sh): what one remote is asked for, by the
    source it is asked of.
  - `wk_fetch_config` (lib/store.sh): the one writer of those specs into a
    checkout's `remote.<r>.fetch`.

A remote nothing here has heard of ("fifth") is answered for anyway: that is
what "no change beyond `wk_remotes`" means.

Run: python3 -m unittest tests.test_owed_cli_refspecs -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest

LIB_STORE = REPO / "lib" / "store.sh"


def _lift_func(path, name):
    text = subprocess.run(
        ["sed", "-n", f"/^{name}() {{/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


def _namespaced(remote):
    return f"+refs/remotes/{remote}/*:refs/remotes/{remote}/*"


class TestWkRemotesIsTheOneList(WkTest):
    """The heredoc itself: today's four upstreams, in order -- pinned so a
    change to this list is a deliberate edit, not a silent drift the
    refspec tests below would otherwise mask."""

    def test_todays_four_remotes_in_order(self):
        fn = _lift_func(LIB_STORE, "wk_remotes")
        cp = self.bash(fn + "\nwk_remotes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        names = [line.split()[0] for line in cp.stdout.splitlines() if line.strip()]
        self.assertEqual(names, ["origin", "wpe", "fork", "forkwpe"])


class TestMirrorDefaultRemotes(WkTest):
    """wk_mirror_default_remotes: a space-joined summary of wk_remotes'
    first column, read through the function -- not the heredoc directly --
    so a change to how the summary is built is what this tracks."""

    def _fn(self):
        return _lift_func(LIB_STORE, "wk_remotes") + "\n" + _lift_func(LIB_STORE, "wk_mirror_default_remotes")

    def test_matches_wk_remotes_names_in_order(self):
        cp = self.bash(self._fn() + "\nwk_mirror_default_remotes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "origin wpe fork forkwpe")

    def test_a_fifth_remote_appears_with_no_change_to_this_function(self):
        # wk_remotes overridden -- a plain shell function definition takes
        # whatever was defined last -- with a fifth upstream added.
        fake_remotes = (
            'wk_remotes() { cat <<'"'"'EOF'"'"'\n'
            'origin   https://github.com/WebKit/WebKit.git\n'
            'wpe      https://github.com/WebPlatformForEmbedded/WPEWebKit.git\n'
            'fork     https://github.com/justinmichaud/WebKit.git\n'
            'forkwpe  https://github.com/justinmichaud/WPEWebKit.git\n'
            'fifth    https://example.com/fifth/WebKit.git\n'
            'EOF\n'
            '}\n'
        )
        mdr = _lift_func(LIB_STORE, "wk_mirror_default_remotes")
        cp = self.bash(fake_remotes + mdr + "\nwk_mirror_default_remotes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "origin wpe fork forkwpe fifth")


class TestFetchRefspecs(WkTest):
    """lib/store.sh's wk_fetch_refspecs: what one remote is asked for, and by
    the source it is asked of. From a wk mirror, origin's branches are that
    mirror's own heads and every other upstream is namespaced under its own
    name; from an upstream itself, every remote's branches are refs/heads.
    Origin is narrowed to wk_mirror_branches either way -- the whole point of
    the pair, since WebKit/WebKit's 924 heads are what git's default refspec
    writes a remote-tracking ref for, one by one."""

    def _fn(self):
        return (
            _lift_func(LIB_STORE, "wk_remotes")
            + "\n"
            + _lift_func(LIB_STORE, "wk_mirror_branches")
            + "\n"
            + _lift_func(LIB_STORE, "wk_fetch_refspecs")
        )

    def _specs(self, remote, mirror="/mirror/WebKit.git", env=None):
        cp = self.bash(self._fn() + f"\nwk_fetch_refspecs {remote!r} {mirror!r}", env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.split()

    def test_origin_from_a_mirror_is_narrowed_to_the_mirrored_branches(self):
        self.assertEqual(self._specs("origin"),
                         ["+refs/heads/main:refs/remotes/origin/main"])

    def test_origin_from_the_upstream_itself_is_narrowed_the_same_way(self):
        self.assertEqual(self._specs("origin", mirror=""),
                         ["+refs/heads/main:refs/remotes/origin/main"])

    def test_wk_mirror_branches_is_the_one_list(self):
        self.assertEqual(
            self._specs("origin", env={"WK_MIRROR_BRANCHES": "main wpe-2.46"}),
            ["+refs/heads/main:refs/remotes/origin/main",
             "+refs/heads/wpe-2.46:refs/remotes/origin/wpe-2.46"])

    def test_every_other_upstream_from_a_mirror_is_namespaced_on_both_sides(self):
        for remote in ("wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(self._specs(remote), [_namespaced(remote)])

    def test_every_other_upstream_from_itself_maps_its_heads(self):
        for remote in ("wpe", "fork", "forkwpe"):
            with self.subTest(remote=remote):
                self.assertEqual(self._specs(remote, mirror=""),
                                 [f"+refs/heads/*:refs/remotes/{remote}/*"])

    def test_a_fifth_upstream_needs_no_change_here(self):
        """A fifth remote needs no change beyond wk_remotes. Nothing about this
        function names a remote, so the proof is that it answers for one it has
        never heard of."""
        self.assertEqual(self._specs("fifth"), [_namespaced("fifth")])
        self.assertEqual(self._specs("fifth", mirror=""),
                         ["+refs/heads/*:refs/remotes/fifth/*"])


class TestTheWiringWritesThoseRefspecs(WkTest):
    """And the one caller: wk_fetch_config puts exactly wk_fetch_refspecs'
    answer into `remote.<r>.fetch`, one `--add` per spec, so a checkout's
    configuration and a `wk sync` fetch cannot disagree about what is asked
    for."""

    def _script(self, mirror):
        cp = self.bash(
            '. lib/common.sh\n. lib/store.sh\n'
            f'wk_fetch_config {mirror!r}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_each_remote_gets_its_specs_and_no_tags(self):
        out = self._script("/mirror/WebKit.git")
        self.assertIn("git config --add remote.origin.fetch "
                      "'+refs/heads/main:refs/remotes/origin/main'", out)
        for remote in ("wpe", "fork", "forkwpe"):
            self.assertIn(f"git config --add remote.{remote}.fetch "
                          f"'{_namespaced(remote)}'", out)
            self.assertIn(f"git config remote.{remote}.tagOpt --no-tags", out)
        self.assertIn("git config remote.origin.tagOpt --no-tags", out)

    def test_every_remote_is_rewritten_to_the_mirror_given(self):
        out = self._script("/mirror/WebKit.git")
        for url in ("https://github.com/WebKit/WebKit.git",
                    "https://github.com/WebPlatformForEmbedded/WPEWebKit.git",
                    "https://github.com/justinmichaud/WebKit.git",
                    "https://github.com/justinmichaud/WPEWebKit.git"):
            self.assertIn(
                f"git config --add 'url./mirror/WebKit.git.insteadOf' '{url}'", out)

    def test_no_mirror_rewrites_nothing_and_asks_the_upstreams(self):
        out = self._script("")
        self.assertNotIn("insteadOf", out)
        self.assertIn("git config --add remote.wpe.fetch "
                      "'+refs/heads/*:refs/remotes/wpe/*'", out)


if __name__ == "__main__":
    unittest.main()
