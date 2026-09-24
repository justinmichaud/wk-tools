"""`wk new` over a workspace with no `base-id` remakes it, owed (docs/PLAN.md): "catches: 'already exists'
answered about a half-made thing". The decision lives in `Target.state` (lib/wk/targets.py): a target that needs a
base snapshot but whose workspace directory has no `base-id` file reports `creating`, not `present` -- so `wk new`
resumes/remakes it instead of refusing "already exists". Driven against the decision with the environment's word
scripted, over a Fake machine. The creation driver's order -- the SDK refreshed under its lock, before the store lock
and the create -- is tests/test_wk_workspace.py's, read off a fake machine.

Run: python3 -m unittest tests.test_owed_new -v
"""
import os
import sys
import unittest

from tests.support import REPO, rand_suffix, requires_container_target, run

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402


class Up(targets.Target):
    def info(self, ws):
        return "running"


class TestWorkspaceWithNoBaseIdIsStillCreating(unittest.TestCase):
    def _state(self, needs_base, has_base_id):
        fake = Fake()
        t = Up("stub", str(REPO), {"WK_STORE": "/store", "HOME": "/home/u"}, fake)
        t.needs_base = needs_base
        fake.mkdir(t.store.ws_dir("somews"))
        if has_base_id:
            fake.write(os.path.join(t.store.ws_dir("somews"), "base-id"), "some-snapshot-id\n")
        return t.state("somews")

    def test_a_workspace_needing_a_base_with_none_recorded_is_creating(self):
        """The environment exists but the base-id pin was never written -- an interrupted `wk new`, not a
        finished one: `wk new` has to resume it, not refuse it."""
        self.assertEqual(self._state(needs_base=True, has_base_id=False), "creating")

    def test_the_same_workspace_once_base_id_is_recorded_is_present(self):
        self.assertEqual(self._state(needs_base=True, has_base_id=True), "present")

    def test_a_target_with_no_base_at_all_never_needs_the_file(self):
        """A remote target's base is a repository, not a pinned snapshot: present without one."""
        self.assertEqual(self._state(needs_base=False, has_base_id=False), "present")


class TestNewKillStopsTheCreation(unittest.TestCase):
    """A creation outlives its terminal, so its record names the command that
    stops it, `wk new <name> --kill` (the record itself is
    tests/test_wk_workspace.py's). What it leaves is half-made, which is `wk
    rm`'s to clear."""

    def test_the_help_block_says_what_it_does(self):
        cp = run("new", "-h")
        self.assertIn("--kill", cp.stdout)

    @requires_container_target()
    def test_it_takes_nothing_that_belongs_to_a_creation(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill", "--no-wait")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("stops the creation already running", cp.stdout)

    @requires_container_target()
    def test_with_no_creation_running_it_says_so_and_ends_well(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no new is running", cp.stdout)

if __name__ == "__main__":
    unittest.main()
