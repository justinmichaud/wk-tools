"""`wk stop --tasks` ends what is still running, through the kill command each
record names (lib/task.sh) -- so nothing here knows how to end a build, a
creation or an agent session, and a kind that grows a new way of stopping is
stopped the new way without this command changing.

The verdict decides what is acted on: a record with an exit in it is over, and
its kill command has nothing to stop. What ran is not believed either -- the
tasks are read again afterwards, and one still there is the exit status.

Run: python3 -m unittest tests.test_stop_tasks -v
"""
import os
import shlex
import shutil
import subprocess
import unittest
from pathlib import Path

from tests.support import WkTest, bash


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
        """A pid this test is not the parent of: a child of the test process
        would answer `kill -0` as a zombie until it was reaped, and read as
        running after its kill command had ended it. The shell that starts it
        exits at once, so the sleep is reparented and this process is not it."""
        cp = subprocess.run(["bash", "-c", "sleep 300 >/dev/null 2>&1 & echo $!"],
                            capture_output=True, text=True, timeout=30)
        pid = int(cp.stdout.strip())
        self.addCleanup(lambda: alive(pid) and os.kill(pid, 9))
        return pid

    def make_task(self, kind, name, kill, pid=None, ended=None):
        log = self.tmp / (name + ".log")
        log.write_text("")
        script = ['. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/task.sh"',
                  'dir=$(task_begin %s here %s %s %s step-one)'
                  % (kind, name, shlex.quote(kill), shlex.quote(str(log)))]
        if pid is not None:
            script.append('task_pid "$dir" %d' % pid)
        if ended is not None:
            script.append('task_end "$dir" %s' % ended)
        script.append('printf "%s" "$dir"')
        cp = bash("\n".join(script), env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return Path(cp.stdout.strip())

    def a_live_task(self, kind="build", name="ws1"):
        """(pid, the flag its kill command touches) -- the flag is how a test
        tells "the kill command ran" from "the process died on its own"."""
        pid = self.spawn()
        flag = self.tmp / (name + ".killed")
        self.make_task(kind, name, "touch %s && kill %d" % (shlex.quote(str(flag)), pid),
                       pid=pid)
        return pid, flag

    def test_each_task_is_ended_by_the_command_its_record_names(self):
        one, flag_one = self.a_live_task("build", "ws1")
        two, flag_two = self.a_live_task("rc", "ws2")
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("build ws1", cp.stdout)
        self.assertIn("rc ws2", cp.stdout)
        self.assertTrue(flag_one.exists(), cp.stdout)
        self.assertTrue(flag_two.exists(), cp.stdout)
        self.assertFalse(alive(one), cp.stdout)
        self.assertFalse(alive(two), cp.stdout)

    def test_it_asks_before_it_acts(self):
        """Declared destructive: with no terminal to ask in it declines, and
        the task is where it was."""
        pid, flag = self.a_live_task()
        cp = self.run_wk("stop", "--tasks", env=self.env)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("nothing stopped", cp.stdout)
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
        """A kill command that exits 0 having stopped nothing: the report is
        read from the task, not from that exit status."""
        pid = self.spawn()
        self.make_task("build", "stubborn", "true", pid=pid)
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(1, cp.returncode, cp.stdout)
        self.assertIn("still running", cp.stdout)
        self.assertIn("build stubborn", cp.stdout)
        self.assertTrue(alive(pid), cp.stdout)

    def test_a_record_that_ended_is_left_alone(self):
        flag = self.tmp / "ended.killed"
        self.make_task("build", "done1", "touch %s" % shlex.quote(str(flag)),
                       pid=os.getpid(), ended=0)
        cp = self.run_wk("stop", "--tasks", "--yes", env=self.env)
        self.assertEqual(0, cp.returncode, cp.stdout)
        self.assertIn("no task is running", cp.stdout)
        self.assertFalse(flag.exists(), cp.stdout)

    def test_the_machine_flag_is_refused_with_it(self):
        """--keep-vm is about the podman machine, which this leaves running."""
        cp = self.run_wk("stop", "--tasks", "--keep-vm", "--yes", env=self.env)
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertIn("--keep-vm", cp.stdout)


class TestWhichVerdictsAreStillGoing(WkTest):
    """task_running() is the one place that decides it, so `wk stop --tasks`
    and anything else that acts on a task agree on what is over."""

    def verdicts(self, *words):
        script = ['. "$WK_ROOT/lib/task.sh"']
        for w in words:
            script.append('task_running %s && echo "%s yes" || echo "%s no"' % (w, w, w))
        cp = bash("\n".join(script))
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        return dict(line.split() for line in cp.stdout.split("\n") if line)

    def test_a_task_with_no_exit_recorded_is_still_going(self):
        got = self.verdicts("starting", "running", "silent", "unanswered")
        self.assertEqual({"starting": "yes", "running": "yes",
                          "silent": "yes", "unanswered": "yes"}, got)

    def test_an_ended_record_is_not(self):
        got = self.verdicts("ok", "failed", "died", "cancelled", "stopped",
                            "oom", "stalled", "refused")
        self.assertEqual(["no"] * 8, list(got.values()), got)
