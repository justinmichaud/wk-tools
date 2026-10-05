"""`wk new`: a workspace with no `base-id` is still creating, and `--kill` stops only a creation."""
import os
import sys
import unittest

from tests.fakes import WsDriver
from tests.support import REPO, rand_suffix, requires_container_place, run

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestWorkspaceWithNoBaseIdIsStillCreating(unittest.TestCase):
    def _state(self, needs_base, has_base_id):
        fake = Fake()
        t = WsDriver("stub", str(REPO), {"WK_STORE": "/store", "HOME": "/home/u"}, fake)
        t.needs_base = needs_base
        fake.mkdir(t.store.ws_dir("somews"))
        if has_base_id:
            fake.write(os.path.join(t.store.ws_dir("somews"), "base-id"), "some-snapshot-id\n")
        return places.Driver.state(t, "somews")

    def test_a_workspace_needing_a_base_with_none_recorded_is_creating(self):
        self.assertEqual(self._state(needs_base=True, has_base_id=False), "creating")
        self.assertEqual(self._state(needs_base=True, has_base_id=True), "present")
        self.assertEqual(self._state(needs_base=False, has_base_id=False), "present")


class TestNewKillStopsTheCreation(unittest.TestCase):
    @requires_container_place()
    def test_it_takes_nothing_that_belongs_to_a_creation(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill", "--no-wait")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("stops the creation already running", cp.stdout)

    @requires_container_place()
    def test_with_no_creation_running_it_says_so_and_ends_well(self):
        cp = run("new", "kill-probe-%s" % rand_suffix(), "--kill")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no new is running", cp.stdout)


if __name__ == "__main__":
    unittest.main()
