import shlex
import unittest

from tests.pueue import FakePueue
from tests.world import World
from wk.core import act
from wk.core.errors import Refused
from wk.core.fleet import Fleet
from wk.core.hooks import DoctorRow
from wk.core.inv import Inv
from wk.core.machine import Result
from wk.tasks import hooks as lane
from wk.tasks import tasks

BIN, DIR = "/home/u/wk/cache/pueue-4.0.5", "/home/u/wk/pueue"
URL = "https://github.com/Nukesor/pueue/releases/download/v4.0.5/"


class Bare(unittest.TestCase):
    """A server with nothing on it, whose files stand for their own digests, and the release as a table of them."""

    def setUp(self):
        act.begin(act.Flags())
        self.w = World(here="desk", seams={"tasks": tasks})
        self.m = self.w.machines["lin"]
        self.daemon = FakePueue(self.m, self.w.clock, up=False)
        self.release = {URL + "%s-%s" % (tool, target): digests[tool] for target, digests in lane.ASSETS.values() for tool in digests}
        self.m.on(["uname", "-sm"], "Linux x86_64\n")
        self.m.on(["python3", "-I", "-c", lane.SHA256], lambda call: self.m.files.get(call.argv[4]) or Result(1, "", "No such file"))
        self.m.on(["curl"], self.curl)
        self.m.on(["python3", "-I", "-c", lane.PLACE], self.place)
        self.m.on(["sh", "-c", lane.START], lambda call: self.daemon.start())
        fleet = Fleet(lambda: self.w.confs, "desk", self.w.machines.get, self.w.machines["desk"], workers=1)
        self.inv = Inv("/", {}, fleet, self.w.clock, None, {}, self.w.seams)

    def curl(self, call):
        self.m.files[call.argv[4]] = self.release[call.argv[5]]

    def place(self, call):
        part, path, want = call.argv[4:]
        if self.m.files[part] != want:
            return Result(1, "", "sha256 %s, pinned %s" % (self.m.files.pop(part), want))
        self.m.files[path] = self.m.files.pop(part)

    def stages(self, name="lin"):
        return lane.stages(self.inv, self.w.confs[name])

    def effects(self):
        return self.w.journal.effects()


class TestStages(Bare):
    def test_a_bare_server_gets_the_pinned_binaries_the_config_a_daemon_and_the_build_group(self):
        stages = self.stages()
        self.assertEqual([(s.order, s.name) for s in stages], [(20, "pueue"), (20, "pueued"), (20, "build group")])
        self.assertEqual([s.run() for s in stages], [True, True, True])
        asset = URL + "%s-x86_64-unknown-linux-musl"
        self.assertEqual(self.effects(), [
            "[lin] curl -fsSL --create-dirs -o %s/pueue.part %s" % (BIN, asset % "pueue"),
            "[lin] python3 -I -c %s %s/pueue.part %s/pueue %s" % (shlex.quote(lane.PLACE), BIN, BIN, lane.ASSETS["Linux x86_64"][1]["pueue"]),
            "[lin] curl -fsSL --create-dirs -o %s/pueued.part %s" % (BIN, asset % "pueued"),
            "[lin] python3 -I -c %s %s/pueued.part %s/pueued %s" % (shlex.quote(lane.PLACE), BIN, BIN, lane.ASSETS["Linux x86_64"][1]["pueued"]),
            "[lin] write %s/pueue.yml" % DIR,
            "[lin] sh -c %s %s/pueued %s/pueue.yml %s/pueue" % (shlex.quote(lane.START), BIN, DIR, BIN),
            "[lin] %s/pueue --config %s/pueue.yml group add build --parallel 1" % (BIN, DIR)])
        self.assertEqual(self.m.files[DIR + "/pueue.yml"], "shared:\n  pueue_directory: %s\n  runtime_directory: %s\n" % (DIR, DIR))
        self.assertEqual(self.daemon.state["groups"]["build"], {"status": "Running", "parallel_tasks": 1})

    def test_a_second_run_changes_nothing(self):
        for s in self.stages():
            s.run()
        before = len(self.effects())
        self.assertEqual(([s.run() for s in self.stages()], len(self.effects())), ([False, False, False], before))

    def test_each_platform_fetches_its_own_binary(self):
        for platform, target in (("Linux aarch64", "aarch64-unknown-linux-musl"), ("Darwin arm64", "aarch64-apple-darwin")):
            self.m.on(["uname", "-sm"], platform + "\n")
            self.m.files.clear()
            self.stages()[0].run()
            self.assertIn("%spueued-%s" % (URL, target), self.effects()[-2])

    def test_a_platform_pueue_does_not_publish_for_is_refused(self):
        self.m.on(["uname", "-sm"], "Linux riscv64\n")
        self.assertRaisesRegex(Refused, r"pueue 4.0.5 has no pinned binary for lin \(Linux riscv64\): wk's servers are Linux x86_64, Linux aarch64, Darwin arm64",
                               self.stages()[0].run)

    def test_a_download_with_another_digest_is_not_installed(self):
        self.release[URL + "pueue-x86_64-unknown-linux-musl"] = "feed"
        with self.assertRaisesRegex(Refused, "cannot verify pueue 4.0.5 on lin: sha256 feed, pinned 5bf543b1.*\n    nothing is installed"):
            self.stages()[0].run()
        self.assertEqual(self.m.files, {})

    def test_a_failed_download_is_refused(self):
        self.m.on(["curl"], Result(22, "", "curl: (22) The requested URL returned error: 404"))
        self.assertRaisesRegex(Refused, r"cannot download pueue 4.0.5 on lin: curl: \(22\)", self.stages()[0].run)

    def test_a_stale_binary_is_replaced_and_the_other_left(self):
        self.stages()[0].run()
        self.m.files[BIN + "/pueued"] = "an older build"
        before = len(self.effects())
        self.assertEqual((self.stages()[0].run(), len(self.effects()) - before, self.m.files[BIN + "/pueued"]),
                         (True, 2, lane.ASSETS["Linux x86_64"][1]["pueued"]))

    def test_a_daemon_that_never_answers_is_refused_naming_how_to_see_why(self):
        self.m.on(["sh", "-c", lane.START], Result(1, "", "Address already in use"))
        self.assertRaisesRegex(Refused, "pueued does not answer on lin after starting it: Address already in use\n    run %s/pueued --config %s/pueue.yml there"
                               % (BIN, DIR), self.stages()[1].run)

    def test_a_running_daemon_with_its_config_gone_gets_the_config_back_and_is_left_running(self):
        self.daemon.start()
        self.assertEqual((self.stages()[1].run(), self.effects()), (True, ["[lin] write %s/pueue.yml" % DIR]))

    def test_a_build_group_with_another_limit_is_set_back(self):
        self.daemon.start()
        tasks(self.inv, self.m).limit("build", 4)
        self.assertEqual((self.stages()[2].run(), self.daemon.state["groups"]["build"]["parallel_tasks"]), (True, 1))

    def test_a_machine_that_holds_no_workspace_has_no_stage_and_no_row(self):
        for name in ("desk", "vault"):
            self.assertEqual((self.stages(name), lane.doctor(self.inv, self.w.confs[name], None)), ([], []))


class TestDoctor(Bare):
    def rows(self):
        return lane.doctor(self.inv, self.w.confs["lin"], None)

    def test_a_provisioned_server_is_ok_on_every_row(self):
        for s in self.stages():
            s.run()
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

    def test_a_server_that_does_not_answer_is_unknown_on_every_row(self):
        self.m.down = True
        self.assertEqual(self.rows(), [DoctorRow(name, "unknown", "lin did not answer", "wk machine setup lin") for name in ("pueue", "pueued", "build group")])

    def test_a_workspace_has_no_row_of_its_own(self):
        self.assertEqual(lane.doctor(self.inv, self.w.confs["lin"], self.w.workspace("x")), [])

