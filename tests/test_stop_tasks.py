"""`wk stop` in process: `--tasks` ends what is still running through the kill command each record names, and reads the tasks
again afterwards for its exit status."""
import contextlib
import io
import os
from unittest import mock
import shlex
import subprocess
import sys
import unittest

from tests.fakes import FlowTest
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import places, record, stop  # noqa: E402
from wk.act import Refused  # noqa: E402

STOP = load_cmd("stop")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class TestStopTasks(FlowTest):
    """Each kill command runs for real on this host against a real pid; the command is run in process."""

    def setUp(self):
        super().setUp()
        self.env = {"WK_STORE": str(self.tmp / "store")}

    def spawn(self):
        """A pid this test is not the parent of, so a killed one is not read alive as a zombie."""
        cp = subprocess.run(["bash", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
                            capture_output=True, text=True, timeout=30)
        pid = int(cp.stdout.strip())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
        return pid

    def make_task(self, kind, name, kill, pid=None, ended=None):
        log = self.tmp / (name + ".log")
        log.write_text("")
        t = record.Records(env=self.env).begin(kind, "here", name, kill, str(log), ["step-one"],
                                               pid=os.getpid() if pid is None else pid)
        if ended is not None:
            t.end(ended)
        return t.path

    def a_live_task(self, kind="build", name="ws1"):
        """(pid, the flag its kill command touches)."""
        pid = self.spawn()
        flag = self.tmp / (name + ".killed")
        self.make_task(kind, name, "touch %s && kill %d" % (shlex.quote(str(flag)), pid), pid=pid)
        return pid, flag

    def stop(self, *flags, **env):
        """(exit or the refusal's status, what it said), run as `wk stop --tasks <flags>`."""
        os.environ.update(env)
        with contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out, \
                mock.patch.object(sys, "stdin", io.StringIO()):
            try:
                rc = STOP.main(as_dispatched("stop", ["--tasks"] + list(flags), os.environ), None)
            except Refused as e:
                rc = e.status
        return rc, out.getvalue() + err.getvalue()

    def test_each_task_is_ended_by_the_command_its_record_names(self):
        one, flag_one = self.a_live_task("build", "ws1")
        two, flag_two = self.a_live_task("test", "ws2")
        rc, said = self.stop(WK_YES="1", WK_STORE=self.env["WK_STORE"])
        self.assertEqual(0, rc, said)
        self.assertIn("build ws1", said)
        self.assertIn("test ws2", said)
        self.assertTrue(flag_one.exists() and flag_two.exists(), said)
        self.assertFalse(alive(one) or alive(two), said)

    def test_it_asks_before_it_acts(self):
        pid, flag = self.a_live_task()
        rc, said = self.stop(WK_STORE=self.env["WK_STORE"])
        self.assertNotEqual(0, rc, said)
        self.assertFalse(flag.exists(), said)
        self.assertTrue(alive(pid), said)

    def test_a_dry_run_names_every_kill_and_runs_none(self):
        pid, flag = self.a_live_task()
        rc, said = self.stop(WK_DRY_RUN="1", WK_STORE=self.env["WK_STORE"])
        self.assertEqual(0, rc, said)
        self.assertIn("would run", said)
        self.assertFalse(flag.exists(), said)
        self.assertTrue(alive(pid), said)

    def test_a_task_still_there_afterwards_is_the_exit_status(self):
        pid = self.spawn()
        self.make_task("build", "stubborn", "true", pid=pid)
        rc, said = self.stop(WK_YES="1", WK_STORE=self.env["WK_STORE"])
        self.assertEqual(1, rc, said)
        self.assertIn("build stubborn", said)
        self.assertTrue(alive(pid), said)

    def test_a_record_that_ended_is_left_alone(self):
        flag = self.tmp / "ended.killed"
        self.make_task("build", "done1", "touch %s" % shlex.quote(str(flag)), pid=os.getpid(), ended=0)
        rc, said = self.stop(WK_YES="1", WK_STORE=self.env["WK_STORE"])
        self.assertEqual(0, rc, said)
        self.assertFalse(flag.exists(), said)

    def test_the_machine_flag_is_refused_with_it(self):
        rc, said = self.stop("--keep-vm", WK_YES="1", WK_STORE=self.env["WK_STORE"])
        self.assertNotEqual(0, rc, said)
        self.assertIn("--keep-vm", said)


class TestEveryMachine(unittest.TestCase):
    """`--all` asks each machine's own wk to stop its tasks; one that cannot be asked is a line and exit 3."""

    def test_each_machine_answers_for_itself(self):
        from wk import fleet
        reg = mock.Mock(machines=lambda: ["box", "here", "far"])
        answers = [fleet.Answer("box", None, "answering", 1, "build ws -- left\n", ""), fleet.Answer("here", None, "none", None, "", ""),
                   fleet.Answer("far", None, "unreachable", None, "", "unreachable over ssh")]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(stop, "confirm", return_value=True), mock.patch.object(stop, "stop_tasks_here", return_value=0), \
                mock.patch.object(stop.fleet, "each", return_value=answers) as each, mock.patch.dict(os.environ, {}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(3, stop.stop_tasks_everywhere(reg, None))
        self.assertEqual(each.call_args[0][1:], (["stop", "--tasks"], ["box", "here", "far"]))
        self.assertIn("box", out.getvalue())
        self.assertIn("far                    unreachable over ssh", err.getvalue())
        self.assertNotIn("here", out.getvalue() + err.getvalue())


class TestWhichVerdictsAreStillGoing(unittest.TestCase):
    def test_only_a_task_with_no_exit_recorded_is_still_going(self):
        going = ("starting", "running", "silent", "unanswered")
        over = ("ok", "failed", "died", "cancelled", "stopped", "oom", "stalled", "refused")
        self.assertEqual([w for w in going + over if w in record.RUNNING], list(going))


class TestStopWorkspace(unittest.TestCase):
    """`wk stop <ws>` is the driver's stop and nothing else, on every kind: no session of its own to end first."""

    class Driver:
        def __init__(self, kind):
            self.kind, self.calls = kind, []

        def info(self, ws):
            return "running"

        def stop(self, ws):
            self.calls.append(("stop", ws))
            return True

        def __getattr__(self, name):
            raise AssertionError("wk stop <ws> asked the place for %s" % name)

    class Reg:
        def __init__(self, driver):
            self.driver = driver

        def present(self, name):
            return self.driver

    def test_each_kind_is_stopped_by_its_driver_alone(self):
        for kind in ("container", "vm", "remote"):
            with self.subTest(kind=kind):
                t = self.Driver(kind)
                with mock.patch.object(places, "Registry", lambda root: self.Reg(t)), mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
                    self.assertEqual(0, STOP.main(as_dispatched("stop", []), None))
                self.assertEqual([("stop", "ws")], t.calls)
