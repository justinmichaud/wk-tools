"""A task's record and log live on the machine that runs it and every reader reaches them through it; a build on a
box with a wk of its own is handed to that wk whole."""
import io
import os
import sys
import unittest
from unittest import mock

from tests import test_wk_places
from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import build, dispatch, job, record, status  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestAGuestsBuildShowsOnTheHost(test_wk_places.VmTest):
    def test_a_build_started_for_a_guest_is_the_hosts_own_record(self):
        """The guest is driven from the host, so its record sits in the vm store on the host: the reader `wk status`
        uses for the vm place finds it there, running."""
        clock = FakeClock()
        self.fake.pids.add(os.getpid())
        log = str(self.tmp / "vmstore" / "ws" / "mac" / "build.log")
        job.records_of(self.t, clock, self.fake).begin("build", "here", "mac", "wk build mac --kill", log, ["compile"])
        recs, worst = status.task_records(self.t.records(clock), "mac", clock)
        self.assertEqual([("mac", "running")], [(r["name"], r["state"]) for r in recs])
        self.assertEqual(2, worst)
        self.assertTrue(str(self.t.records(clock).root).startswith(str(self.tmp / "vmstore")))


class TestEveryReaderGoesThroughTheMachine(test_wk_places.DriversTest):
    def test_records_on_a_machine_are_listed_through_it(self):
        far = Fake("far")
        root = "/far/store/task/build-ws-20000101T000000Z-1"
        far.dirs.update({"/far/store/task", root})
        far.files[root + "/plan"] = "compile\n"
        far.files[root + "/name"] = "ws\n"
        recs = record.Records("/far/store", clock=FakeClock(), machine=far)
        self.assertEqual(["ws"], [t.field("name") for t in recs.list()])

    def test_a_record_is_written_through_its_machine(self):
        far = Fake("far")
        t = record.Records("/far/store", clock=FakeClock(), machine=far, env={}).begin(
            "build", "place", "ws", "wk build ws --kill", "/far/build.log", ["compile"])
        t.step(1)
        t.end(0)
        self.assertEqual((far.files[str(t.path / "plan")], far.files[str(t.path / "steps/1")], far.files[str(t.path / "exit")]),
                         ("compile\n", "running\n", "0\n"))
        self.assertFalse(os.path.exists(str(t.path)))


class TestNothingCopiesARecord(test_wk_places.RemoteTest):
    def test_a_box_build_is_read_through_the_boxs_own_wk_and_nothing_here_holds_one(self):
        """`live build.box_record`'s unit half: the host's `wk status` asks the box's wk for its records."""
        clock = FakeClock()
        self.env["WK_PLACE"] = "box"
        walk = status.Walk(REPO, name="a", fleet=False, devices=False, env=self.env, reg=self.reg, clock=clock)
        with mock.patch.object(status.Walk, "reach", return_value=("", "")):
            recs = [r for r in walk.records(markers=False) if r.get("kind") == "task"]
        self.assertEqual([], recs)
        self.assertTrue(self.fake.ssh_calls("status --no-fleet --records a"))
        self.assertEqual([], self.t.records(clock).list())


class TestTheLogIsReadThroughItsMachine(unittest.TestCase):
    """The record's machine holds its log: nothing reads the log off this machine's own filesystem."""

    LOG = "/far/ws/build.log"

    def setUp(self):
        self.far = Fake("far")
        self.clock = FakeClock()
        self.far.pids.add(4242)
        self.recs = record.Records("/far/store", clock=self.clock, machine=self.far,
                                   env={"WK_ABORT_SECONDS": "1800", "WK_STALL_SECONDS": "300"})
        self.t = self.recs.begin("build", "here", "ws", "wk build ws --kill", self.LOG, ["compile"], pid=4242)
        self.far.files[self.LOG] = "[1/9] CXX a.o\r[7/9] CXX g.o\n"
        self.far.mtimes[self.LOG] = self.clock.now() - 10
        self.assertFalse(os.path.exists(self.LOG))

    def notes(self):
        return [n["text"] for n in status.task_records(self.recs, "ws", self.clock)[0][0]["notes"]]

    def test_a_running_row_reads_its_progress_and_age_off_the_far_log(self):
        self.assertEqual("running", self.t.verdict())
        self.assertEqual(("[7/9]", 10), (record.progress_line(self.LOG, self.far), record.log_age(self.LOG, self.clock, self.far)))
        self.assertTrue(any("[7/9]" in n and "10s" in n for n in self.notes()), self.notes())

    def test_the_far_logs_age_is_what_makes_it_silent(self):
        self.far.mtimes[self.LOG] = self.clock.now() - 400
        self.assertEqual("silent", self.t.verdict())
        self.assertEqual("400", status.task_records(self.recs, "ws", self.clock)[0][0]["log_age"])

    def test_a_failed_row_names_the_far_logs_first_errors(self):
        self.far.files[self.LOG] = "[1/9] CXX a.o\na.cpp:1: error: no\n"
        self.t.end(1)
        self.assertEqual(["2:a.cpp:1: error: no"], record.first_error(self.LOG, self.far))
        self.assertIn("  2:a.cpp:1: error: no", self.notes())

    def test_wait_streams_the_far_log_as_it_grows(self):
        far, log = self.far, self.LOG

        class Grows(FakeClock):
            def sleep(self, seconds):
                super().sleep(seconds)
                far.files[log] += "[9/9] Linking jsc\n"

        self.recs.clock = Grows()
        out = io.StringIO()
        with mock.patch.object(record.Task, "verdict", side_effect=["running", "ok"]):
            self.assertEqual("ok", self.recs.wait("build", "ws", log, stream=out))
        self.assertEqual("[1/9] CXX a.o\r[7/9] CXX g.o\n[9/9] Linking jsc\n", out.getvalue())


class TestABoxBuildIsHandedToTheBox(test_wk_places.RemoteTest):
    """A build on a box that runs its own wk is that wk's, record and all: the dispatcher hands every command about
    it over, and nothing here drives one or records one."""

    def setUp(self):
        super().setUp()
        self.env["WK_PLACE"] = "box"

    def build(self):
        return build.Build(self.reg, "a", {"config": "jsc-release"}, clock=FakeClock())

    def made_here(self, t):
        """What this end changed beyond the ssh control directory every far call makes."""
        return t.records().list(), [e for e in t.here.effects if e[0] != "run" and e != ("mkdir", t.ssh_dir())]

    def test_every_command_about_a_box_workspace_goes_to_the_boxs_wk(self):
        with mock.patch.object(dispatch, "_registry", self.reg):
            self.assertEqual("box", dispatch.delegate_driver("box").name)

    def test_the_hand_over_is_the_whole_command_and_changes_nothing_here(self):
        def world():
            w = test_wk_places.RemoteTest()
            w.setUp()
            w.env["WK_PLACE"] = "box"
            w.ran = []
            self.addCleanup(w.tearDown)
            return w

        def run_once(w):
            with mock.patch.object(dispatch, "_registry", w.reg), mock.patch.object(dispatch.os, "execvp", lambda f, a: w.ran.append(a)):
                dispatch.delegate_run(dispatch.delegate_driver("box"), "build", ["a", "jsc-release", "--detach"])

        def final(w):
            return self.made_here(w.reg.load("box")), [a[-1] for a in w.ran]

        converges(self, world, run_once, final)
        w = world()
        run_once(w)
        (argv,) = w.ran
        self.assertEqual(argv[0], "ssh")
        self.assertIn("/home/u/wk/tools/wk build a jsc-release --detach", argv[-1])
        self.assertEqual(([], []), self.made_here(w.reg.load("box")))

    def test_a_box_without_a_wk_of_its_own_is_refused_and_nothing_is_recorded(self):
        self.fake.answer_remote("test -f $HOME/.wk-remote", rc=1)
        self.assertIn("wk machine setup box", self.refused(self.build))
        self.assertEqual(([], []), self.made_here(self.t))

    def test_a_box_that_does_not_answer_is_refused_and_nothing_is_recorded(self):
        self.fake.answer_remote("uname -s", rc=255, err="ssh: connect to host box.example port 22: Operation timed out")
        self.refused(self.build)
        self.assertEqual(([], []), self.made_here(self.t))

    def test_a_build_reaching_this_end_with_the_boxs_wk_answering_is_refused(self):
        self.assertIn("wk build a", self.refused(self.build))
        self.assertEqual(([], []), self.made_here(self.t))

    def test_on_the_box_the_build_is_recorded_in_the_boxs_own_store(self):
        root = self.tmp / "rr"
        self.conf("me", "local=1\nroot=%s\n" % root)
        self.env["WK_PLACE"] = "me"
        b = build.Build(self.reg, "a", {"config": "jsc-release"}, clock=FakeClock())
        self.assertTrue(str(b.recs.root).startswith(str(root)), b.recs.root)
