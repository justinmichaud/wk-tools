"""lib/wk/job.py: a watched job, the detach, the far-side start line and its poll; INT/TERM/HUP through job.Signals
and the kill_tree walk; job.kill through a place (TERM then KILL, the pid checked against its pattern, and the
patterns every job's pid is adopted under); what a silent build says for itself from `ps` in both platforms'
spellings; and lib/wk/lock.py's locked run."""
import contextlib
import io
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.fakes import Fake, FakeClock, FlowTest, WsDriver
from tests.support import REPO, glob_bait, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import act, build, job, record  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import Local, Result, here  # noqa: E402


class Scratch(FlowTest):
    def setUp(self):
        super().setUp()
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
        with contextlib.redirect_stderr(io.StringIO()):
            killed = job.watch_pid(lambda: None, 4242, str(log), self.fake, self.clock,
                                   {"WK_POLL_SECONDS": "5", "WK_STALL_SECONDS": "10", "WK_ABORT_SECONDS": "20"})
        self.assertTrue(killed)
        kills = [e for e in self.fake.effects if e[0] == "kill"]
        self.assertEqual([k[1] for k in kills[:2]], [4243, 4242], "descendants go before the job")

    def test_a_watched_job_returns_its_own_status_and_leaves_its_output(self):
        log = self.tmp / "run.log"
        rc = job.watch(["sh", "-c", "echo hi; exit 3"], str(log), Local(), Clock(), {"WK_POLL_SECONDS": "1"})
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
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(("7", True), job.wait_remote(ask, "/l", "/rc", self.clock, 200, env={"WK_STALL_SECONDS": "300"}))


class TestAStopThroughThePlace(Scratch):
    """job.kill: a workspace pid is signalled through the place, after its command line is checked."""

    def test_the_callers_own_pid_is_ended_without_a_signal(self):
        t = record.Records(self.tmp, clock=self.clock, machine=self.fake, env={}).begin(
            "pgo", "here", "p", "k", "/l", ["one"], pid=777)
        self.assertTrue(job.kill(None, "ws", t, "cancelled", self.fake, self.clock, {}, me=777))
        self.assertEqual("cancelled", t.field("exit"))
        self.assertFalse([e for e in self.fake.effects if e[0] == "kill"])

    def test_a_workspace_pid_with_no_pattern_to_match_is_a_refusal(self):
        t = record.Records(self.tmp / "store", clock=self.clock, env={}).begin(
            "build", "place", "ws", "k", "/l", ["one"])
        t.pid(4242)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(act.Refused):
            job.kill(object(), "ws", t, "cancelled", self.fake, self.clock, {})

    def test_the_descendants_are_walked_and_signalled_inside_the_workspace(self):
        seen = []

        class Driver:
            def exec(self, ws, argv, timeout=None):
                seen.append(("exec", ws, tuple(argv)))
                return Result(0, "12\n4242\n")

            def act_exec(self, ws, argv):
                seen.append(("act", ws, tuple(argv)))
                return Result(0)

        job.kill_tree_in(Driver(), "ws", 4242, job.sig.SIGTERM)
        self.assertEqual(("act", "ws", ("kill", "-TERM", "12", "4242")), seen[-1])


class TestTheLockedRun(Scratch):
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


PLACE_PID_MATCH = "*build-in-workspace.sh*"


def place(alive=False, args="bash /opt/wk-tools/build/build-in-workspace.sh --release"):
    """Workspace `ws`, where the job's pid is `alive` or not and `ps` names it with `args`."""
    fake = Fake("here")
    fake.answer(["exec", "ws"])
    fake.answer(["exec", "ws", "kill", "-0"], rc=0 if alive else 1)
    fake.answer(["exec", "ws", "ps", "-o", "args="], out=args + "\n")
    return WsDriver("place", str(REPO), {}, fake)


def execs(driver):
    return "\n".join(" ".join(e[1][2:]) for e in driver.machine.effects if e[0] == "run")


class KillTest(FlowTest):

    def begin(self, driver, pid=None):
        t = record.Records(str(self.tmp), env={}, ask_place=driver.pid_alive).begin(
            "build", "place", "ws", "wk build ws --kill", "/dev/null", ["compile"])
        t.set("pid_match", PLACE_PID_MATCH)
        if pid:
            t.pid(pid)
        return t

    def kill(self, driver, t):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            gone = job.kill(driver, "ws", t, "cancelled", here(), FakeClock(), {"WK_KILL_WAIT": "1"})
        return gone, err.getvalue()


class TestJobKillReachesTheMachineThatRuns(KillTest):
    def test_a_record_with_no_pid_yet_is_just_converged(self):
        driver = place()
        self.assertTrue(self.kill(driver, self.begin(driver))[0])
        self.assertEqual(execs(driver), "")

    def test_the_term_goes_through_the_place_with_the_descendant_walk(self):
        driver = place()
        t = self.begin(driver, 4242)
        self.assertTrue(self.kill(driver, t)[0])
        self.assertIn("sh -c %s wk 4242" % job.TREE, execs(driver))
        self.assertIn("kill -TERM", execs(driver))
        self.assertEqual(t.field("exit"), "cancelled")

    def test_one_that_outlives_term_and_kill_is_reported_and_the_record_still_converges(self):
        driver = place(alive=True)
        t = self.begin(driver, 4242)
        self.assertFalse(self.kill(driver, t)[0])
        self.assertIn("kill -KILL", execs(driver))
        self.assertEqual(t.field("exit"), "cancelled")

    def test_a_kill_stays_cancelled_when_the_driver_it_stopped_ends_too(self):
        driver = place()
        t = self.begin(driver, 4242)
        term = driver.act_exec
        driver.act_exec = lambda ws, argv: t.end(1) or term(ws, argv)
        self.kill(driver, t)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_nothing_is_signalled_at_a_pid_that_is_not_the_job(self):
        driver = place(alive=True, args="/usr/bin/sshd -D")
        t = self.begin(driver, 4242)
        with self.assertRaises(act.Refused):
            self.kill(driver, t)
        self.assertNotIn("kill -TERM", execs(driver))


class TestThePatternsCoverEveryShapeTheJobTakes(unittest.TestCase):
    def test_a_builds_shapes_match_and_nothing_else_does(self):
        for args in ("env WK_JOBS=8 /opt/wk-tools/build/build-in-workspace.sh --release",
                     "Tools/Scripts/build-webkit --release --export-compile-commands",
                     "linux32 Tools/Scripts/build-jsc --release"):
            self.assertTrue(job.match_any(args, build.PID_MATCH), args)
        for args in ("/sbin/init", "/usr/bin/sshd -D", "bash -lc sleep 300"):
            self.assertFalse(job.match_any(args, build.PID_MATCH), args)

    def test_a_test_runs_suite_matches_and_another_shell_does_not(self):
        mod = load_cmd("test")
        self.assertTrue(job.match_any('bash -lc echo "wk: test pid $$" >&2 cd /src/WebKit && nice -n 10 '
                                      'Tools/Scripts/run-javascriptcore-tests --release wpe', mod.PID_MATCH))
        self.assertTrue(job.match_any("bash -lc cd /src/WebKit && Tools/Scripts/run-webkit-tests --release", mod.PID_MATCH))
        self.assertFalse(job.match_any("bash -lc sleep 300", mod.PID_MATCH))

    def test_the_image_builds_wrapper_matches_and_a_bare_bitbake_does_not(self):
        from wk.sysimage import yocto
        self.assertTrue(job.match_any("python3 /opt/wk-tools/lib/wk/sysimage/yocto_ws.py --target rpi5 --stage image", yocto.PATTERN))
        self.assertFalse(job.match_any("bitbake core-image-weston", yocto.PATTERN))


DRIVER = '''
import sys
sys.path.insert(0, %(lib)r)
from wk import job
from wk.clock import Clock
from wk.machine import Local
with job.Signals():
    try:
        job.watch(["sh", "-c", "echo $$ > %(pidfile)s; exec sleep 1000"], %(log)r, Local(), Clock(), {"WK_POLL_SECONDS": "600"})
    except job.Interrupted as e:
        open(%(marker)r, "w").close()
        sys.exit(job.EXIT_OF[e.signum])
'''


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_gone(pid):
    """Returns once `pid` is gone: one that outlives its kill is stopped by the runner's budget."""
    while _pid_alive(pid):
        time.sleep(0.05)


def _interrupt_driver(tmp, sig):
    """Run DRIVER until its watched child has written its pid, signal the driver alone; (rc, output, child pid, marker).
    The watch polls every 600s, so a signal that did not cut the poll short outlives the runner's budget."""
    pidfile, marker = os.path.join(tmp, "child.pid"), os.path.join(tmp, "cancelled")
    script = DRIVER % {"lib": str(REPO / "lib"), "pidfile": pidfile, "log": os.path.join(tmp, "build.log"), "marker": marker}
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, start_new_session=True)
    try:
        while not (os.path.exists(pidfile) and open(pidfile).read().strip()):
            if proc.poll() is not None:
                raise AssertionError("the driver exited (%d) before its child started:\n%s" % (proc.returncode, proc.stdout.read()))
            time.sleep(0.02)
        child = int(open(pidfile).read())
        proc.send_signal(sig)
        out, _ = proc.communicate()
        return proc.returncode, out, child, os.path.exists(marker)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()


class TestWatchInterrupt(unittest.TestCase):
    def test_a_signal_during_watch_runs_the_cancel_path_kills_the_watched_child_and_exits_by_it(self):
        for sig, exit_code in ((signal.SIGINT, 130), (signal.SIGHUP, 129)):   # HUP is what a supervisor with no tty sends
            with self.subTest(sig=sig), tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
                rc, out, child, cancelled = _interrupt_driver(tmp, sig)
                self.assertEqual(rc, exit_code, out)
                self.assertTrue(cancelled, "the cancel path did not run:\n" + out)
                _wait_gone(child)


class TestKillingAJobKillsWhatItStarted(unittest.TestCase):
    def test_a_grandchild_does_not_outlive_the_job(self):
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            marker = os.path.join(tmp, "kid")
            p = subprocess.Popen(["sh", "-c", "sleep 300 & echo $! > %s; sleep 300" % marker], start_new_session=True)
            try:
                while not (os.path.exists(marker) and open(marker).read().strip()):
                    time.sleep(0.02)
                kid = int(open(marker).read())
                job.kill_tree(Local(), p.pid, signal.SIGTERM)
                p.wait()
                _wait_gone(kid)
            finally:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGKILL)
                    p.wait()

    def test_it_never_signals_the_process_asking(self):
        m = Fake()
        me = os.getpid()
        m.answer(["sh", "-c", job.TREE, "wk", "10"], out="%d\n11\n10\n" % me)
        m.pids.update({me, 10, 11})
        job.kill_tree(m, 10, signal.SIGTERM)
        self.assertEqual([11, 10], [e[1] for e in m.effects if e[0] == "kill"])


DARWIN_PS = """\
 99.5 /Applications/Xcode.app/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld
  0.4 /usr/libexec/xpcproxy
  0.0 /bin/bash
"""


LINUX_PS = """\
 12.0 cc1plus
 11.5 cc1plus
  9.0 clang++
  0.1 bash
  0.0 sshd
"""


IDLE_PS = """\
  0.3 /bin/bash
  0.0 /usr/sbin/sshd
"""


def _machine(ps_out):
    m = Fake()
    m.answer(["ps", "-A", "-o", "pcpu=,comm="], out=ps_out)
    return m


class TestTheProcessReadings(unittest.TestCase):
    def test_every_compiler_and_linker_counts_busiest_first_by_either_spelling(self):
        self.assertEqual(job.build_processes(_machine(DARWIN_PS)), [(99.5, "ld")])
        self.assertEqual(job.build_processes(_machine(LINUX_PS)), [(12.0, "cc1plus"), (11.5, "cc1plus"), (9.0, "clang++")])
        self.assertEqual(job.build_processes(_machine(IDLE_PS)), [])

    def test_the_reading_never_names_its_own_reader(self):
        names = [n for _, n in job.build_processes(here())]
        for tool in ("ps", "python3", "sh"):
            self.assertNotIn(tool, names)


class TestWhatTheReportClaims(unittest.TestCase):
    def _report(self, ps_out):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            job.stall_report(_machine(ps_out), "/dev/null", 301)
        return err.getvalue()

    def test_a_working_linker_is_not_reported_as_a_stall(self):
        out = self._report(DARWIN_PS)
        self.assertIn("301s", out)
        self.assertIn("ld at 99.5% CPU", out, out)

    def test_a_machine_doing_nothing_is_reported_as_doing_nothing(self):
        out = self._report(IDLE_PS)
        self.assertNotIn("% CPU", out, out)


WANT = "*build-in-workspace.sh* *Tools/Scripts/build-*"


ARGS = "perl Tools/Scripts/build-webkit --jsc-only --debug --makeargs=-j75 "


class TestMatchAnyIsIndependentOfTheCwd(unittest.TestCase):
    def test_a_declared_pattern_matches_from_inside_a_checkout(self):
        here = os.getcwd()
        with glob_bait(WANT) as cwd:
            os.chdir(str(cwd))
            try:
                self.assertTrue(job.match_any(ARGS, WANT))
            finally:
                os.chdir(here)

    def test_an_unrelated_command_line_never_matches(self):
        for args, want in (("bash -lc sleep 300", WANT), ("", WANT), (ARGS, "")):
            with self.subTest(args=args, want=want):
                self.assertFalse(job.match_any(args, want))
