import unittest

from tests.world import World
from tests.ws import MIRROR, WIPE, git, inv, server
from wk.core import seams
from wk.core.errors import Refused
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.machine import Result
from wk.workspace import hooks as mine

IMAGE = "ghcr.io/igalia/wkdev-sdk:latest"
USERMOD = "on lin: sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 u"
PULL = "[lin] podman pull " + IMAGE


def linux(w, image=0):
    """Script lin as a server with podman, subordinate ids and (image=0) the SDK image."""
    m = server(w)
    m.on(("podman", "--version"), "podman version 5.4.0\n")
    m.on(("id", "-un"), "u\n")
    m.files["/etc/subuid"] = m.files["/etc/subgid"] = "other:100000:65536\nu:165536:65536\n"
    m.on(("podman", "image", "exists"), Result(image, "", "Error: storage is corrupt\n" if image > 1 else ""))
    return m


class TestStages(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.inv = inv(self.w)

    def stage(self, name, order):
        return next(s for s in mine.stages(self.inv, self.w.confs[name]) if s.order == order)

    def test_a_server_gets_the_mirror_a_container_server_its_build_environment_and_other_machines_get_neither(self):
        for name, want in (("lin", [(30, "mirror"), (40, "build environment")]), ("mac", [(30, "mirror")]), ("box", [(30, "mirror")]),
                           ("desk", []), ("vault", [])):
            self.assertEqual([(s.order, s.name) for s in mine.stages(self.inv, self.w.confs[name])], want, name)

    def test_the_mirror_stage_makes_and_fetches_it_on_the_server_and_then_reports_no_change(self):
        git(self.w.machines["lin"], fetched=False)
        self.assertTrue(self.stage("lin", 30).run())
        self.assertEqual(self.w.journal.effects()[0], "[lin] git init --bare --quiet %s" % MIRROR)
        self.assertFalse(self.stage("lin", 30).run())

    def test_a_linux_server_with_podman_its_id_ranges_and_the_image_changes_nothing(self):
        linux(self.w)
        self.assertEqual((self.stage("lin", 40).run(), self.w.journal.effects()), (False, []))

    def test_a_missing_sdk_image_is_pulled(self):
        linux(self.w, image=1)
        self.assertEqual((self.stage("lin", 40).run(), self.w.journal.effects()), (True, [PULL]))

    def test_a_pull_that_fails_is_refused_with_what_podman_said(self):
        linux(self.w, image=1).on(("podman", "pull"), Result(125, "", "Error: unauthorized\n"))
        self.assertRaisesRegex(Refused, r"\(exit 125\): Error: unauthorized\n    check that lin reaches the registry, then `wk machine setup lin` again",
                               self.stage("lin", 40).run)

    def test_no_podman_is_refused_naming_the_install_and_nothing_is_pulled(self):
        linux(self.w, image=127).on(("podman", "--version"), Result(127))
        self.assertRaisesRegex(Refused, r"^lin lacks podman\n    install podman on lin$", self.stage("lin", 40).run)
        self.assertEqual(self.w.journal.effects(), [])

    def test_no_subordinate_id_range_is_refused_naming_the_command_a_person_runs(self):
        linux(self.w, image=1).files["/etc/subgid"] = "other:100000:65536\n"
        self.assertRaisesRegex(Refused, r"^lin lacks subordinate ids: no range for u in /etc/subgid\n    %s$" % USERMOD, self.stage("lin", 40).run)
        self.assertEqual(self.w.journal.effects(), [])

    def test_an_image_podman_cannot_look_for_is_not_pulled_over(self):
        linux(self.w, image=125)
        self.assertRaisesRegex(Refused, r"^lin lacks sdk image: podman failed \(exit 125\): Error: storage is corrupt\n    wk machine setup lin$",
                               self.stage("lin", 40).run)


class TestDoctor(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.inv = inv(self.w)

    def rows(self, name, ws=None):
        return mine.doctor(self.inv, self.w.confs[name], ws)

    def test_a_provisioned_linux_server_is_all_ok(self):
        linux(self.w)
        self.assertEqual([(r.name, r.status) for r in self.rows("lin")],
                         [("mirror", OK), ("podman", OK), ("subordinate ids", OK), ("sdk image", OK)])

    def test_each_missing_thing_is_a_row_with_its_remedy(self):
        m = linux(self.w, image=1)
        del m.files[MIRROR + "/config"], m.files["/etc/subuid"]
        m.on(("podman", "--version"), Result(127))
        self.assertEqual(self.rows("lin"), [
            DoctorRow("mirror", MISSING, MIRROR, "wk machine setup lin"),
            DoctorRow("podman", MISSING, "", "install podman on lin"),
            DoctorRow("subordinate ids", MISSING, "no range for u in /etc/subuid", USERMOD),
            DoctorRow("sdk image", MISSING, IMAGE, "wk machine setup lin")])

    def test_a_mirror_never_fetched_is_missing_and_an_image_podman_cannot_look_for_is_unknown(self):
        m = linux(self.w, image=125)
        git(m, fetched=False)
        rows = self.rows("lin")
        self.assertEqual((rows[0].status, rows[3]), (MISSING, DoctorRow("sdk image", UNKNOWN, "podman failed (exit 125): Error: storage is corrupt",
                                                                        "wk machine setup lin")))

    def test_a_plain_server_is_its_mirror_alone(self):
        server(self.w, "mac")
        self.assertEqual([(r.name, r.status) for r in self.rows("mac")], [("mirror", OK)])

    def test_a_workspace_and_a_machine_that_holds_none_have_no_rows(self):
        self.assertEqual((self.rows("lin", self.w.workspace("x")), self.rows("desk"), self.rows("vault")), ([], [], []))


class TestGc(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.inv = inv(self.w)

    def rows(self, name="lin"):
        return mine.gc(self.inv, self.w.confs[name])

    def test_a_half_made_workspace_nothing_is_making_is_reclaimed_by_a_plain_run(self):
        self.w.workspace("x", state=seams.BROKEN)
        row, = self.rows()
        self.assertEqual((row.name, row.detail, row.flag), ("workspace x", "half-made on lin: no .ready, and no `wk new` is making it", ""))
        row.reclaim()
        self.assertEqual((self.w.journal.effects(), self.w.machines["lin"].files), (WIPE, {}))

    def test_one_being_made_a_ready_one_and_one_whose_container_is_down_are_not_rows(self):
        self.w.workspace("ready")
        self.w.workspace("down")
        self.w.made.discard(("lin", "down"))
        self.assertEqual(self.rows(), [])
        self.w.workspace("x", state=seams.BROKEN)
        self.w.machines["lin"].on(("pgrep",), "4242\n")
        self.assertEqual(self.rows(), [])

    def test_a_machine_that_holds_no_workspace_has_no_rows(self):
        self.assertEqual((self.rows("desk"), self.rows("vault")), ([], []))
