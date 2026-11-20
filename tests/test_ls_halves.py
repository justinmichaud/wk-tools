"""`wk ls` is one table: places with no wk of their own are read here, every other place's own `wk ls --json` is
merged in, and a place that was not asked is named."""
import json
import unittest

from tests import fakes
from tests.fakes import FakeRegistry, FlowTest, Result
from tests.test_wk_places import LINUX_PROBE
from tests.test_wk_workspace import WorkspaceTest

W1 = {"name": "w1", "place": "box", "state": "present", "base": "main", "arch": "native", "changes": "1G"}


class Fleet(fakes.World):
    """A workstation whose podman machine is not made yet, and its peers: `box` lists W1, `garbled` prints no JSON
    and `down` does not answer ssh."""
    LS = {"box": json.dumps({"workspaces": [W1]}), "garbled": "not json"}

    def __init__(self, tmp, peers=("box", "garbled", "down")):
        super().__init__(tmp)
        (self.tmp / "machines").mkdir()
        for peer in peers:
            (self.tmp / "machines" / (peer + ".conf")).write_text("kind=peer\nhost=%s.example\n" % peer)
        self.env["WK_MACHINES_DIR"] = str(self.tmp / "machines")
        self.answer(["podman", "machine", "inspect"], rc=125, err="Error: wk: VM does not exist\n")
        self.react(["ssh"], self._ssh)
        self.reg = FakeRegistry(self.env, self)

    def _ssh(self, argv, f):
        peer = next(a for a in argv if a.endswith(".example"))[:-len(".example")]
        if peer == "down":
            return Result(255, "", "ssh: connect to host down.example port 22: Operation timed out\n")
        if "uname -s" in argv[-1]:
            return Result(0, LINUX_PROBE)
        return Result(0, f.LS[peer] if "wk ls --json" in argv[-1] else "")


class TestOneTable(FlowTest):
    def test_every_answering_places_rows_are_merged_into_one_table(self):
        w = Fleet(self.tmp, peers=("box",))
        rc, out, _ = self.wk(w, "ls", "--json")
        self.assertEqual((rc, json.loads(out)), (0, {"workspaces": [W1]}))
        rc, out, _ = self.wk(w, "ls")
        self.assertEqual((rc, out.splitlines()[1].split()), (0, ["w1", "box", "present", "main", "native", "1G"]))

    def test_a_place_that_did_not_answer_or_answered_no_json_is_named_and_the_rest_listed(self):
        rc, out, err = self.wk(Fleet(self.tmp), "ls", "--json")
        self.assertEqual((rc, json.loads(out)), (0, {"workspaces": [W1]}))
        self.assertIn("warning: down: unreachable over ssh: connect to host down.example port 22: Operation timed out, so its "
                      "workspaces are not listed", err.splitlines())
        self.assertIn("warning: garbled: its 'wk ls --json' printed no JSON, so its workspaces are not listed", err.splitlines())


class TestInsideAWorkspace(WorkspaceTest):
    """From inside a workspace BASE and CHANGES are not applicable: the host holds them."""

    def test_base_and_changes_are_not_applicable(self):
        w = self.make_world(kinds={"fakebox": "local"})
        rc, out, _ = self.wk(w, "ls")
        self.assertEqual((rc, out.splitlines()[1].split()), (0, ["ws", "fakebox", "running", "n/a", "native", "n/a"]))


if __name__ == "__main__":
    unittest.main()
