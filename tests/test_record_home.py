"""`unit record.home_is_the_machine`: a task's record and log live on the machine that runs it, every reader
reaches them through that machine, nothing copies them, a build started in a guest shows on the host, and
`wk build <ws> --kill` finds a build the host drives even once the far side has a wk of its own.

Run: python3 tests/run.py -k tests.test_record_home
"""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests import test_wk_targets
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import build, dispatch, record, status  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402


class TestAGuestsBuildShowsOnTheHost(test_wk_targets.VmTest):
    def test_a_build_started_for_a_guest_is_the_hosts_own_record(self):
        """The guest is driven from the host, so its record sits in the vm store on the host: the reader `wk status`
        uses for the vm target finds it there, running."""
        clock = FakeClock()
        self.fake.pids.add(os.getpid())
        log = str(self.tmp / "vmstore" / "ws" / "mac" / "build.log")
        build.records_of(self.t, clock, self.fake).begin("build", "here", "mac", "wk build mac --kill", log, ["compile"])
        recs, worst = status.task_records(self.t.records(clock), "mac", clock)
        self.assertEqual([("mac", "running")], [(r["name"], r["state"]) for r in recs])
        self.assertEqual(2, worst)
        self.assertTrue(str(self.t.records(clock).root).startswith(str(self.tmp / "vmstore")))


class TestEveryReaderGoesThroughTheMachine(test_wk_targets.TargetsTest):
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
            "build", "target", "ws", "wk build ws --kill", "/far/build.log", ["compile"])
        t.step(1)
        t.end(0)
        self.assertEqual((far.files[str(t.path / "plan")], far.files[str(t.path / "steps/1")], far.files[str(t.path / "exit")]),
                         ("compile\n", "running\n", "0\n"))
        self.assertFalse(os.path.exists(str(t.path)))


class TestNothingCopiesARecord(test_wk_targets.RemoteTest):
    def test_a_far_build_is_the_hosts_own_record_and_shows_while_the_far_wk_answers(self):
        """The driver runs here, so its record is written here and nowhere else; the host's `wk status` shows it
        beside what the far machine's own wk answers."""
        clock = FakeClock()
        self.fake.pids.add(os.getpid())
        self.fake.answer_remote("tools/wk", out="")
        t = build.records_of(self.t, clock, self.fake).begin("build", "here", "a", "wk build a --kill", "/nolog", ["compile"])
        t.step(1)
        self.assertEqual([], self.fake.ssh_calls("/task/"))
        self.env["WK_TARGET"] = "box"
        walk = status.Walk(REPO, name="a", fleet=False, devices=False, env=self.env, reg=self.reg, clock=clock)
        with mock.patch.object(status.Walk, "reach", return_value=("", "")):
            recs = [r for r in walk.records(markers=False) if r.get("kind") == "task"]
        self.assertEqual([("a", "running")], [(r["name"], r["state"]) for r in recs])
        self.assertTrue(self.fake.ssh_calls("status --no-fleet --records a"))


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


class TestKillFindsTheHostsOwnBuild(test_wk_targets.RemoteTest):
    """A build the host drives on a box is the host's record. Once a tools sync gives the box a wk, a command
    about another workspace there goes to that wk, and `--kill` of this one is still answered here."""

    def setUp(self):
        super().setUp()
        self.env["WK_TARGET"] = "box"
        self.clock = FakeClock()
        self.fake.pids.add(4242)
        self.task = build.records_of(self.t, self.clock, self.fake).begin(
            "build", "here", "a", "wk build a --kill", "/nolog", ["compile"], pid=4242)
        self.assertTrue(self.t.delegates())

    def test_the_host_driving_a_workspace_keeps_it_from_the_far_wk(self):
        with mock.patch.object(dispatch, "_registry", self.reg):
            self.assertIsNone(dispatch.delegate_target("box", "a"))
            self.assertEqual("box", dispatch.delegate_target("box", "b").name)
            self.fake.pids.discard(4242)
            self.assertEqual("box", dispatch.delegate_target("box", "a").name)

    def test_the_kill_lands_on_the_hosts_pid_and_ends_the_hosts_record(self):
        with contextlib.redirect_stderr(io.StringIO()):
            rc = build.Build(self.reg, "a", {"kill": True}, clock=self.clock).front()
        self.assertEqual(0, rc)
        self.assertIn(("kill", 4242, 15), self.fake.effects)
        self.assertEqual(("cancelled", "cancelled"), (self.task.field("exit"), self.task.field("stopping")))
        self.assertEqual([], self.fake.ssh_calls("--kill"))
