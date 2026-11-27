import unittest

from tests.pueue import FakePueue
from tests.world import World
from wk.core import act
from wk.core.errors import Unreachable
from wk.core.fleet import Fleet
from wk.core.hooks import DoctorRow
from wk.core.inv import Inv
from wk.tasks import hooks as lane
from wk.tasks import tasks

BIN = "/home/u/wk/cache/pueue-4.0.5"


class TestDoctor(unittest.TestCase):
    def setUp(self):
        act.begin(act.Flags())
        self.w = World(here="desk", seams={"tasks": tasks})
        self.m = self.w.machines["lin"]
        self.daemon = FakePueue(self.m, self.w.clock, up=False)
        fleet = Fleet(lambda: self.w.confs, "desk", self.w.machines.get, self.w.machines["desk"])
        self.inv = Inv("/", {}, fleet, self.w.clock, None, {}, self.w.seams)

    def rows(self):
        return lane.doctor(self.inv, self.w.confs["lin"], None)

    def test_a_provisioned_server_is_ok_on_every_row(self):
        self.m.files.update({BIN + "/pueue": "", BIN + "/pueued": ""})
        self.daemon.start()
        tasks(self.inv, self.m).limit("build", 1)
        self.assertEqual(self.rows(), [DoctorRow("pueue", "ok", "4.0.5"), DoctorRow("pueued", "ok"), DoctorRow("build group", "ok", "one build at a time")])

    def test_a_bare_server_misses_pueue_and_its_daemon_and_the_group_is_unknown(self):
        remedy = "wk machine setup lin"
        self.assertEqual(self.rows(), [DoctorRow("pueue", "missing", "4.0.5 is not in " + BIN, remedy),
                                       DoctorRow("pueued", "missing", "the daemon does not answer", remedy),
                                       DoctorRow("build group", "unknown", "the daemon does not answer", remedy)])

    def test_a_build_group_that_is_absent_or_wide_is_missing(self):
        self.daemon.start()
        self.assertEqual(self.rows()[2], DoctorRow("build group", "missing", "its parallel limit is None, not 1", "wk machine setup lin"))
        tasks(self.inv, self.m).limit("build", 2)
        self.assertEqual(self.rows()[2].detail, "its parallel limit is 2, not 1")

    def test_a_server_that_does_not_answer_is_the_collectors_to_report(self):
        self.m.down = True
        self.assertRaises(Unreachable, self.rows)

    def test_a_workspace_has_no_row_of_its_own(self):
        self.assertEqual(lane.doctor(self.inv, self.w.confs["lin"], self.w.workspace("x")), [])

    def test_the_pinned_version_is_the_playbooks_and_its_download_is_kept_by_gc(self):
        self.assertEqual(lane.pins(self.inv, self.w.confs["lin"]), ["pueue-4.0.5"])
