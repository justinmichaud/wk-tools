"""The mirror's refspecs follow the mirror's own layout: origin's branches
become the mirror's own heads, and every other upstream is namespaced --
owed by docs/HANDOFF-wk-cli.md: "the refspecs follow the mirror's own
layout (origin's branches are its heads; every other upstream is
namespaced); a fifth upstream needs no change here beyond `wk_remotes`
[needs a test]".

Two functions are lifted and driven directly, with sed (the
tests/test_wifi_seed.py idiom), so this tracks the exact code that ships:

  - `wk_remotes` (lib/store.sh): the one list of upstreams, a plain heredoc.
  - `wk_mirror_default_remotes` (lib/store.sh): `wk_remotes | awk ...`,
    read through the function rather than the heredoc directly.
  - `mirror_refspecs` (cmd/sync): builds the fetch refspec list from
    `wk_mirror_default_remotes`, one line per remote.

The last test overrides `wk_remotes` itself (a shell function definition
takes whatever is defined last) with a five-remote fake and re-derives
both `wk_mirror_default_remotes` and `mirror_refspecs` from it, proving the
refspec list is *generated* from the remotes list rather than hand-written
to match today's four -- a fifth upstream needs no change beyond
`wk_remotes`, exactly as the handoff item claims.

Run: python3 -m unittest tests.test_owed_cli_refspecs -v
"""
import subprocess
import unittest

from tests.support import REPO, WkTest

CMD_SYNC = REPO / "cmd" / "sync"
LIB_STORE = REPO / "lib" / "store.sh"


def _lift_func(path, name):
    text = subprocess.run(
        ["sed", "-n", f"/^{name}() {{/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{name}() not found in {path}"
    return text


ORIGIN_MAP = "+refs/heads/*:refs/remotes/origin/*"


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


class TestMirrorRefspecs(WkTest):
    """cmd/sync's mirror_refspecs: origin maps refs/heads to its own
    refs/remotes/origin (a plain clone's own layout, since origin's
    branches become the mirror's heads); every other remote is namespaced
    under its own name -- and the whole list is generated from
    wk_mirror_default_remotes, not hand-listed."""

    def _fn(self):
        return (
            _lift_func(LIB_STORE, "wk_remotes")
            + "\n"
            + _lift_func(LIB_STORE, "wk_mirror_default_remotes")
            + "\n"
            + _lift_func(CMD_SYNC, "mirror_refspecs")
        )

    def test_origin_maps_heads_to_its_own_remote_tracking_namespace(self):
        cp = self.bash(self._fn() + "\nmirror_refspecs")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        specs = cp.stdout.split()
        self.assertIn(ORIGIN_MAP, specs)
        # Exactly once: origin's branches are the mirror's own heads, not a
        # namespaced copy of itself as well.
        self.assertEqual(specs.count(ORIGIN_MAP), 1)
        self.assertNotIn(_namespaced("origin"), specs)

    def test_every_other_upstream_is_namespaced_under_its_own_name(self):
        cp = self.bash(self._fn() + "\nmirror_refspecs")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        specs = cp.stdout.split()
        for remote in ("wpe", "fork", "forkwpe"):
            self.assertIn(_namespaced(remote), specs, specs)

    def test_a_fifth_upstream_is_namespaced_with_no_change_here(self):
        """The handoff's exact claim: a fifth remote needs no change beyond
        wk_remotes. This overrides only wk_remotes (a fresh function
        definition, the same mechanism the previous class used) and
        re-derives mirror_refspecs -- unedited -- from it."""
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
        rest = (
            _lift_func(LIB_STORE, "wk_mirror_default_remotes")
            + "\n"
            + _lift_func(CMD_SYNC, "mirror_refspecs")
        )
        cp = self.bash(fake_remotes + rest + "\nmirror_refspecs")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        specs = cp.stdout.split()
        self.assertIn(_namespaced("fifth"), specs, specs)
        # origin is still the one exception, unaffected by the new arrival.
        self.assertIn(ORIGIN_MAP, specs)
        self.assertEqual(specs.count(ORIGIN_MAP), 1)

    def test_the_refspec_count_tracks_the_remote_count_not_a_fixed_number(self):
        """Generated, not hand-listed: N remotes produce exactly N
        refspecs (one origin mapping plus one namespaced entry per other
        remote) for both today's four and a fifth added on top."""
        base_fn = self._fn()
        cp4 = self.bash(base_fn + "\nmirror_refspecs")
        self.assertEqual(len(cp4.stdout.split()), 4, cp4.stdout)

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
        rest = (
            _lift_func(LIB_STORE, "wk_mirror_default_remotes")
            + "\n"
            + _lift_func(CMD_SYNC, "mirror_refspecs")
        )
        cp5 = self.bash(fake_remotes + rest + "\nmirror_refspecs")
        self.assertEqual(len(cp5.stdout.split()), 5, cp5.stdout)


if __name__ == "__main__":
    unittest.main()
