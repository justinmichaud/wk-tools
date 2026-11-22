import unittest

from tests.world import World
from tests.ws import MIRROR, WIPE, git, inv, server
from wk.core import seams
from wk.core.hooks import MISSING, OK, UNKNOWN, DoctorRow
from wk.core.machine import Result
from wk.workspace import hooks as mine

IMAGE = "ghcr.io/igalia/wkdev-sdk:latest"
USERMOD = "on lin: sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 u"


def linux(w, image=0):
    """Script lin as a server with podman, subordinate ids and (image=0) the SDK image."""
    m = server(w)
    m.on(("podman", "--version"), "podman version 5.4.0\n")
    m.on(("id", "-un"), "u\n")
    m.files["/etc/subuid"] = m.files["/etc/subgid"] = "other:100000:65536\nu:165536:65536\n"
    m.on(("podman", "image", "exists"), Result(image, "", "Error: storage is corrupt\n" if image > 1 else ""))
    return m


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
            DoctorRow("mirror", MISSING, MIRROR, "wk sync"),
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

    def test_a_workspace_has_no_row_of_its_own(self):
        self.assertEqual(self.rows("lin", self.w.workspace("x")), [])


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
