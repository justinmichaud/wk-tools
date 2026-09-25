"""lib/wk/job.py as the one implementation lib/watchdog.sh and lib/detach.sh shim over: a watched pid a bash
caller started, the detach, the far-side start line and its poll, and a job stopped through the caller's own
target. Against a fake machine and a fake clock, except where the behaviour is a real process's.

Run: python3 tests/run.py -k tests.test_wk_job
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import job, record, shell  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-job-"))
        self.addCleanup(record._rmtree, self.tmp)
        self.clock = FakeClock()
        self.fake = Fake()


class TestAWatchedPid(Scratch):
    """run_watched starts the job itself, so the watch is of a pid it did not spawn."""

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

    def test_the_shim_returns_the_jobs_own_status(self):
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/watchdog.sh"\n'
                  'WK_POLL_SECONDS=1\nrun_watched "%s" -- sh -c "echo hi; exit 3" && echo "rc=0" || echo "rc=$?"'
                  % (self.tmp / "run.log"))
        self.assertIn("rc=3", cp.stdout, cp.stderr)
        self.assertEqual("hi\n", (self.tmp / "run.log").read_text())


class TestTheDetach(Scratch):
    def test_the_log_begins_empty_and_the_pid_comes_back(self):
        pid = job.detach(self.fake, ["wk", "build", "ws"], "/store/ws/ws/detached.log")
        self.assertIn(pid, self.fake.pids)
        self.assertEqual(self.fake.files["/store/ws/ws/detached.log"], "")
        self.assertIn(("spawn", ("wk", "build", "ws"), "/store/ws/ws/detached.log"), self.fake.effects)

    def test_the_shim_outlives_its_caller(self):
        log = self.tmp / "d" / "job.log"
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/detach.sh"\n'
                  'detach_run "%s" -- sh -c "sleep 1; echo done"' % log)
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


class TestAStopThroughTheCallersTarget(Scratch):
    """job_kill/job_stop from bash: the workspace is reached through the caller's own t_exec."""

    def test_the_callers_own_pid_is_ended_without_a_signal(self):
        t = record.Records(self.tmp, clock=self.clock, machine=self.fake, env={}).begin(
            "pgo", "here", "p", "k", "/l", ["one"], pid=777)
        self.assertTrue(job.kill(None, "ws", t, "cancelled", self.fake, self.clock, {}, me=777))
        self.assertEqual("cancelled", t.field("exit"))
        self.assertFalse([e for e in self.fake.effects if e[0] == "kill"])

    def test_a_refusal_ends_the_bash_caller(self):
        d = record.Records(self.tmp / "store", clock=self.clock, env={}).begin(
            "build", "target", "ws", "k", "/l", ["one"])
        d.pid(4242)
        cp = bash('. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/watchdog.sh"\n'
                  't_exec() { return 0; }\n'
                  'job_kill ws "%s" cancelled || echo "RETURNED $?"\necho SURVIVED' % d.path,
                  env={"WK_STORE": str(self.tmp / "store")})
        self.assertNotEqual(0, cp.returncode, cp.stdout)
        self.assertNotIn("SURVIVED", cp.stdout)
        self.assertIn("no pattern its", cp.stderr)

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

    def test_the_caller_shell_reaches_its_own_functions(self):
        state = self.tmp / "state"
        subprocess.run(["bash", "-c", 'X=from-the-caller; t_exec() { shift; echo "$X $*"; }; '
                        '{ declare -p; declare -f; } > "%s" 2>/dev/null' % state], check=True)
        r = shell.CallerShell(str(state)).exec("ws", ["ps", "-o", "args="])
        self.assertEqual("from-the-caller ps -o args=\n", r.out)

    def test_a_workspace_that_does_not_answer_in_time_is_unanswered(self):
        state = self.tmp / "state"
        subprocess.run(["bash", "-c", 't_exec() { sleep 30; }; declare -f > "%s"' % state], check=True)
        self.assertIsNone(shell.CallerShell(str(state)).ask("ws", 1, 0.5))


class TestTheLockedRun(Scratch):
    """lib/lockrun.sh over lib/wk/lock.py: the command runs holding the lock, and its status comes back."""

    def test_the_command_runs_under_the_lock_and_the_lock_goes_with_it(self):
        env = dict(os.environ, XDG_STATE_HOME=str(self.tmp))
        env.pop("WK_LOCK_DIR", None)
        cp = subprocess.run([str(REPO / "lib" / "lockrun.sh"), "demo", "-w", "5", "--", "sh", "-c",
                             'ls "$XDG_STATE_HOME/wk/locks"; exit 7'], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(7, cp.returncode, cp.stderr)
        self.assertIn("demo@", cp.stdout)
        self.assertEqual([], os.listdir(self.tmp / "wk" / "locks"))


if __name__ == "__main__":
    unittest.main()
