"""A macOS `wk ls` is one table from two halves -- this host's targets, then
the podman VM's containers -- and the "(no workspaces ...)" note may appear
only when both halves are empty. cmd/ls carries the two dispatcher-only flags
that make that one decision: --more-follows (first half: never call the
listing empty) and --empty-so-far (second half: the note is yours if you are
empty too).

Run: python3 -m unittest tests.test_ls_halves -v
"""
import unittest

from tests.support import REPO, temp_store
from tests.test_options import run_impl


class TestTheEmptyNoteIsDecidedOnce(unittest.TestCase):
    def _ls(self, *flags):
        with temp_store() as store:
            # A podman-free PATH: the container target lists real containers
            # through podman, and this table must be empty by construction.
            return run_impl("ls", *flags, env={
                "WK_STORE": store["WK_STORE"],
                "XDG_STATE_HOME": str(store["path"] / "state"),
                "WK_TARGET": "container",
                "PATH": "/usr/bin:/bin",
            })

    def test_the_first_half_never_prints_the_note(self):
        cp = self._ls("--more-follows")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("NAME", cp.stdout)
        self.assertNotIn("no workspaces", cp.stdout)

    def test_the_second_half_prints_it_only_when_nothing_came_before(self):
        cp = self._ls("--continued", "--empty-so-far")
        self.assertIn("no workspaces", cp.stdout)
        cp = self._ls("--continued")
        self.assertNotIn("no workspaces", cp.stdout)
        self.assertNotIn("NAME", cp.stdout, "the second half must not print a second header")

    def test_a_bare_ls_still_prints_the_note_alone(self):
        cp = self._ls()
        self.assertIn("no workspaces", cp.stdout)

    def test_the_dispatcher_owns_the_flags(self):
        text = (REPO / "wk").read_text()
        self.assertIn('"$impl" --more-follows', text)
        self.assertIn('--continued ${_ls_empty:+"$_ls_empty"}', text)


if __name__ == "__main__":
    unittest.main()
