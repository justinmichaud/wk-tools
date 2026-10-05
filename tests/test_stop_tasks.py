"""`wk stop --tasks` ends what is still running through the kill command each record names, and reads the tasks
again afterwards for its exit status."""
import os
from unittest import mock
import shlex
import shutil
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import record  # noqa: E402


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@unittest.skipUnless(shutil.which("podman"), "`wk stop` declares `needs podman`")
class TestStopTasks(WkTest):
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
        self.make_task(kind, name, "touch %s && kill %d" % (shlex.quote(str(flag)), pid),
                       pid=pid)
        return pid, flag

    def test_each_task_is_ended_by_the_command_its_record_names(self):
        one, flag_one = self.a_live_task("build", "ws1")
        two, flag_two = self.a_live_task("test", "ws2")
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("build ws1", cp.stdout)
        self.assertIn("test ws2", cp.stdout)
        self.assertTrue(flag_one.exists(), cp.stdout)
        self.assertTrue(flag_two.exists(), cp.stdout)
        self.assertFalse(alive(one), cp.stdout)
        self.assertFalse(alive(two), cp.stdout)

    def test_it_asks_before_it_acts(self):
        pid, flag = self.a_live_task()
        cp = self.run_wk("stop", "--tasks", env=self.env)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertFalse(flag.exists(), cp.stdout)
        self.assertTrue(alive(pid), cp.stdout)

    def test_a_dry_run_names_every_kill_and_runs_none(self):
        pid, flag = self.a_live_task()
        cp = self.run_wk("stop", "--tasks", "--dry-run", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("would run", cp.stdout)
        self.assertFalse(flag.exists(), cp.stdout)
        self.assertTrue(alive(pid), cp.stdout)

    def test_a_task_still_there_afterwards_is_the_exit_status(self):
        pid = self.spawn()
        self.make_task("build", "stubborn", "true", pid=pid)
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("build stubborn", cp.stdout)
        self.assertTrue(alive(pid), cp.stdout)

    def test_a_record_that_ended_is_left_alone(self):
        flag = self.tmp / "ended.killed"
        self.make_task("build", "done1", "touch %s" % shlex.quote(str(flag)),
                       pid=os.getpid(), ended=0)
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertFalse(flag.exists(), cp.stdout)

    def test_the_machine_flag_is_refused_with_it(self):
        cp = self.run_wk("stop", "--tasks", "--keep-vm", "--yes", env=self.env)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("--keep-vm", cp.stdout)


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
        stop = load_cmd("stop")
        for kind in ("container", "vm", "remote"):
            with self.subTest(kind=kind):
                t = self.Driver(kind)
                with mock.patch.object(stop.places, "Registry", lambda root: self.Reg(t)), mock.patch.dict(os.environ, {"WK_NAME": "ws"}):
                    self.assertEqual(0, stop.main([]))
                self.assertEqual([("stop", "ws")], t.calls)
