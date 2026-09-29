"""lib/wk/job.py: a watched job, the detach, the far-side start line and its poll, a job stopped through its
target, and lib/wk/lock.py's locked run. Against a fake machine and a fake clock, except where the behaviour
is a real process's.

Run: python3 tests/run.py -k tests.test_wk_job
"""
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, job, record  # noqa: E402
from wk.clock import Clock, FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-job-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.clock = FakeClock()
        self.fake = Fake()


class TestAWatchedPid(Scratch):
    """watch starts the job itself; watch_pid watches a pid it did not spawn."""

    def test_a_job_that_ends_is_not_killed(self):
        log = self.tmp / "log"
        log.write_text("")
        polls = iter([None, None, 0])
        with contextlib.redirect_stderr(io.StringIO()):
            killed = job.watch_pid(lambda: next(polls), 4242, str(log), self.fake, self.clock,
                                   {"WK_POLL_SECONDS": "1"})
        self.assertFalse(killed)
        self.assertFalse([e for e in self.fake.effects if e[0] == "kill"])

    def test_a_silent_job_is_killed_past_the_deadline_by_the_clock(self):
        log = self.tmp / "log"
        log.write_text("")
        self.fake.answer(["sh", "-c", job.TREE, "wk", "4242"], out="4243\n4242\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            killed = job.watch_pid(lambda: None, 4242, str(log), self.fake, self.clock,
                                   {"WK_POLL_SECONDS": "5", "WK_STALL_SECONDS": "10", "WK_ABORT_SECONDS": "20"})
        self.assertTrue(killed)
        self.assertIn("giving up and killing the job", err.getvalue())
        kills = [e for e in self.fake.effects if e[0] == "kill"]
        self.assertEqual([k[1] for k in kills[:2]], [4243, 4242], "descendants go before the job")

    def test_a_watched_job_returns_its_own_status_and_leaves_its_output(self):
        log = self.tmp / "run.log"
        rc = job.watch(["sh", "-c", "echo hi; exit 3"], str(log), self.fake, Clock(), {"WK_POLL_SECONDS": "1"})
        self.assertEqual(3, rc)
        self.assertEqual("hi\n", log.read_text())


class TestTheDetach(Scratch):
    def test_the_log_begins_empty_and_the_pid_comes_back(self):
        pid = job.detach(self.fake, ["wk", "build", "ws"], "/store/ws/ws/detached.log")
        self.assertIn(pid, self.fake.pids)
        self.assertEqual(self.fake.files["/store/ws/ws/detached.log"], "")
        self.assertIn(("spawn", ("wk", "build", "ws"), "/store/ws/ws/detached.log"), self.fake.effects)

    def test_a_detached_job_outlives_its_caller(self):
        log = self.tmp / "d" / "job.log"
        caller = ("import sys; sys.path.insert(0, %r)\nfrom wk import job\nfrom wk.machine import here\n"
                  "print(job.detach(here(), ['sh', '-c', 'sleep 1; echo done'], %r))" % (str(REPO / "lib"), str(log)))
        cp = subprocess.run([sys.executable, "-c", caller], capture_output=True, text=True, timeout=30)
        self.assertRegex(cp.stdout, r"^[0-9]+$", cp.stderr)
        for _ in range(50):
            if "done" in (log.read_text() if log.exists() else ""):
                break
            time.sleep(0.1)
        self.assertEqual("done\n", log.read_text(), "the job died with the shell that detached it")


class TestTheFarSide(Scratch):
    def test_the_start_line_writes_the_status_beside_the_log(self):
        log, rc = self.tmp / "far.log", self.tmp / "far.rc"
        rc.write_text("9\n")
        subprocess.run(["bash", "-c", job.remote_line(["sh", "-c", "echo far; exit 4"], str(log), str(rc))], timeout=30)
        for _ in range(50):
            if rc.exists() and rc.read_text().strip():
                break
            time.sleep(0.1)
        self.assertEqual(("far\n", "4"), (log.read_text(), rc.read_text().strip()))

    def far(self, answers):
        def ask(line):
            for key, out in answers:
                if line.startswith(key):
                    return Result(0 if out is not None else 1, out or "")
            return Result(1)
        return ask

    def test_the_poll_ends_on_the_status_and_streams_the_tail(self):
        ask = self.far([("wc -c", "3\n"), ("tail -c +1", "ok\n"), ("cat", "0\n")])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(("0", True), job.wait_remote(ask, "/l", "/rc", self.clock, 5, stream=True))
        self.assertIn("ok", err.getvalue())

    def test_a_timeout_and_an_abort_are_words_not_statuses(self):
        self.assertEqual(("timeout", False), job.wait_remote(self.far([]), "/l", "/rc", self.clock, 5, timeout=10))
        self.assertEqual(("aborted", False),
                         job.wait_remote(self.far([("grep -qiE", "")]), "/l", "/rc", self.clock, 5, abort_re="Traceback"))

    def test_silence_is_reported_and_the_job_left_alone(self):
        replies = iter(["", "", "", "7\n"])
        ask = lambda line: Result(0, next(replies)) if line.startswith("cat") else Result(0, "0\n")  # noqa: E731
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(("7", True), job.wait_remote(ask, "/l", "/rc", self.clock, 200, env={"WK_STALL_SECONDS": "300"}))
        self.assertIn("not stopping it", err.getvalue())


class TestAStopThroughTheTarget(Scratch):
    """job.kill: a workspace pid is signalled through the target, after its command line is checked."""

    def test_the_callers_own_pid_is_ended_without_a_signal(self):
        t = record.Records(self.tmp, clock=self.clock, machine=self.fake, env={}).begin(
            "pgo", "here", "p", "k", "/l", ["one"], pid=777)
        self.assertTrue(job.kill(None, "ws", t, "cancelled", self.fake, self.clock, {}, me=777))
        self.assertEqual("cancelled", t.field("exit"))
        self.assertFalse([e for e in self.fake.effects if e[0] == "kill"])

    def test_a_workspace_pid_with_no_pattern_to_match_is_a_refusal(self):
        t = record.Records(self.tmp / "store", clock=self.clock, env={}).begin(
            "build", "target", "ws", "k", "/l", ["one"])
        t.pid(4242)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            job.kill(object(), "ws", t, "cancelled", self.fake, self.clock, {})
        self.assertIn("no pattern its", err.getvalue())

    def test_the_descendants_are_walked_and_signalled_inside_the_workspace(self):
        seen = []

        class Target:
            def exec(self, ws, argv, timeout=None):
                seen.append(("exec", ws, tuple(argv)))
                return Result(0, "12\n4242\n")

            def act_exec(self, ws, argv):
                seen.append(("act", ws, tuple(argv)))
                return Result(0)

        job.kill_tree_in(Target(), "ws", 4242, job.sig.SIGTERM)
        self.assertEqual(("act", "ws", ("kill", "-TERM", "12", "4242")), seen[-1])


class TestTheLockedRun(Scratch):
    """`python3 -m wk.lock run`: the command runs holding the lock, and its status comes back."""

    def test_the_command_runs_under_the_lock_and_the_lock_goes_with_it(self):
        env = dict(os.environ, XDG_STATE_HOME=str(self.tmp))
        env.pop("WK_LOCK_DIR", None)
        env["PYTHONPATH"] = str(REPO / "lib")
        cp = subprocess.run([sys.executable, "-m", "wk.lock", "run", "demo", "-w", "5", "--", "sh", "-c",
                             'ls "$XDG_STATE_HOME/wk/locks"; exit 7'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(7, cp.returncode, cp.stderr)
        self.assertIn("demo@", cp.stdout)
        self.assertEqual([], os.listdir(self.tmp / "wk" / "locks"))



class TestJobSettings(unittest.TestCase):
    def test_the_kill_wait_and_pid_tries_come_from_the_env_else_the_default(self):
        self.assertEqual((job.kill_wait({}), job.kill_wait({}, 120), job.pid_tries({})), (15, 120, 900))
        env = {"WK_KILL_WAIT": "2", "WK_JOB_PID_TRIES": "0"}
        self.assertEqual((job.kill_wait(env, 120), job.pid_tries(env)), (2, 0))


if __name__ == "__main__":
    unittest.main()
