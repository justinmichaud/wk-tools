"""`wk ls` is one table: places with no wk of their own are read here, every other place's own `wk ls --json` is
merged in, and a place that was not asked is named."""
import contextlib
import io
import json
import sys
import unittest
from unittest import mock

from tests.fakes import FakeDriver, FakeRegistry
from tests.support import REPO, fake_workspace, load_cmd
from tests.test_options import run_impl

sys.path.insert(0, str(REPO / "lib"))
from tests.fakes import Fake  # noqa: E402

ls = load_cmd("ls")


class TestOneTable(unittest.TestCase):
    def listed(self, drivers, *argv):
        by_name = {d.name: d for d in drivers}
        reg = FakeRegistry({}, Fake(), lambda name, env: by_name[name], names=list(by_name))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(ls.places, "Registry", lambda root: reg), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            ls.main(list(argv))
        return out.getvalue(), err.getvalue()

    def test_every_answering_place_s_rows_are_merged_and_a_silent_one_named(self):
        far = {"name": "w1", "place": "box", "state": "present", "base": "main", "snap": "-", "arch": "native", "changes": "1G"}
        box = FakeDriver("box", out=json.dumps({"workspaces": [far]}))
        down = FakeDriver("down", side="unreachable", why="timed out")
        out, err = self.listed([box, down], "--json")
        self.assertEqual(json.loads(out), {"workspaces": [far]})
        self.assertEqual(box.asked[0][0], ("ls", "--json"))
        self.assertIn("down: unreachable over ssh: timed out, so its workspaces are not listed", err)
        out, _ = self.listed([box])
        self.assertEqual(out.splitlines()[1].split(), ["w1", "box", "present", "main", "-", "native", "1G"])

    def test_an_answer_that_is_no_json_is_named(self):
        _, err = self.listed([FakeDriver("box", out="not json")], "--json")
        self.assertIn("box: its 'wk ls --json' printed no JSON", err)


class TestInsideAWorkspace(unittest.TestCase):
    """From inside a workspace BASE and CHANGES are not applicable: the host holds them."""

    def test_base_and_changes_are_not_applicable(self):
        with fake_workspace() as ws:
            table = run_impl("ls", env=ws.env()).stdout.splitlines()
            doc = json.loads(run_impl("ls", "--json", env=ws.env(), split=True).stdout)
        self.assertEqual(table[1].split(), ["selftest-ws", "local", "running", "n/a", "-", "native", "n/a"])
        (row,) = doc["workspaces"]
        self.assertEqual((row["base"], row["changes"]), ("n/a", "n/a"))


if __name__ == "__main__":
    unittest.main()
