"""Stopping a build, and refusing to start a second one."""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shlex
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import act, build, job, record  # noqa: E402
from wk.clock import Clock, FakeClock  # noqa: E402
from wk.machine import Result, here  # noqa: E402


def _load(rel):
    """A `cmd/*` file, which carries no `.py` suffix for the loader to infer from."""
    loader = importlib.machinery.SourceFileLoader(rel.replace("/", "_"), str(REPO / rel))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# The record the fake workspace's own `local` target resolves to.
def store_of(ws):
    return Path(ws.state_dir) / "wk"


# What the pid's command line inside the target must match, recorded by
# job.adopt with the pid itself: nothing signals a pid without it.
TARGET_PID_MATCH = "*build-in-target.sh*"
TARGET_PID_ARGS = "bash /opt/wk-tools/build/build-in-target.sh --release"
# A pid above every default pid_max on both platforms: dead by construction.
DEAD_PID = 4194304


class StubTarget:
    def __init__(self, answer="dead", args=TARGET_PID_ARGS):
        self.answer, self.args, self.execs = answer, args, []

    def exec(self, ws, argv, tty=False, timeout=None):
        self.execs.append(" ".join(argv))
        if argv[:2] == ["kill", "-0"]:
            return Result(0 if self.answer == "alive" else 1)
        if argv[:3] == ["ps", "-o", "args="]:
            return Result(0, self.args + "\n")
        return Result(0)

    def act_exec(self, ws, argv):
        return self.exec(ws, argv)

    def pid_alive(self, ws, pid, cap=None):
        return self.exec(ws, ["kill", "-0", str(pid)]).rc == 0


def records(store, target=None):
    return record.Records(store, env={}, ask_target=target.pid_alive if target else None)


def begin(store, kind="build", name="selftest-ws", pid=None, log="/dev/null",
          where="here", kill=None, plan="compile jsc-release",
          pid_match=TARGET_PID_MATCH, target=None):
    t = records(store, target).begin(kind, where, name, kill or f"wk {kind} {name} --kill", log, [plan],
                                     pid=DEAD_PID if pid is None and where == "here" else pid)
    t.step(1)
    if where == "target":
        t.set("pid_match", pid_match)
        if pid:
            t.pid(pid)
    return t


def spawn_orphan(marker=None):
    inner = "exec sleep 300"
    if marker:
        inner = "sleep 300 & echo $! > %s; exec sleep 300" % marker
    cp = subprocess.run(
        ["bash", "-c", "nohup bash -c %s >/dev/null 2>&1 & echo $!" % shlex.quote(inner)],
        capture_output=True, text=True, check=True)
    pid = int(cp.stdout.strip())
    if marker:
        for _ in range(50):
            if Path(marker).exists():
                break
            time.sleep(0.1)
    return pid


def reap(pid):
    if alive(pid):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def said(fn):
    """(fn's value, what it wrote to stdout and stderr)."""
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
        rv = fn()
    return rv, out.getvalue() + err.getvalue()


def refusal(case, fn):
    """What `fn` said as it was refused."""
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err, \
            case.assertRaises(act.Refused):
        fn()
    return out.getvalue() + err.getvalue()


class TestJobKillStopsWhatTheJobStarted(WkTest):
    """`job.kill` with the record's `where` at `here`: the local process tree."""

    def test_a_recorded_pid_and_its_children_are_gone_and_the_record_converges(self):
        marker = self.tmp / "kid"
        pid = spawn_orphan(marker=marker)
        kid = int(marker.read_text().strip())
        try:
            t = begin(self.tmp / "store", pid=pid)
            gone, out = said(lambda: job.kill(None, "selftest-ws", t, "cancelled", here(), Clock(), {}))
            self.assertTrue(gone, out)
            self.assertEqual(t.field("exit"), "cancelled")
            self.assertFalse(alive(kid), "the child outlived the job it belonged to")
            self.assertFalse(alive(pid))
        finally:
            reap(kid)
            reap(pid)

    def test_a_record_with_no_pid_yet_is_just_converged(self):
        target = StubTarget()
        t = begin(self.tmp / "store", where="target", target=target)
        self.assertTrue(job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(), {}))
        self.assertEqual(t.field("exit"), "cancelled")
        self.assertEqual(target.execs, [], "nothing was signalled")


class TestJobKillReachesTheMachineThatRuns(WkTest):

    def test_the_term_goes_through_the_target_with_the_descendant_walk(self):
        target = StubTarget()
        t = begin(self.tmp / "store", where="target", pid=4242, target=target)
        self.assertTrue(job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(), {}))
        execs = "\n".join(target.execs)
        self.assertIn("sh -c %s wk 4242" % job.TREE, execs,
                      "the far side gets the same descendants-first walk")
        self.assertIn("kill -TERM", execs)
        self.assertNotIn("pkill", execs,
                         "a container shares the host's PID namespace, so no pattern kill")
        self.assertEqual(t.field("exit"), "cancelled")

    def test_one_that_outlives_term_and_kill_is_reported_not_claimed_stopped(self):
        target = StubTarget(answer="alive")
        t = begin(self.tmp / "store", where="target", pid=4242, target=target)
        gone, out = said(lambda: job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(),
                                          {"WK_KILL_WAIT": "1"}))
        self.assertFalse(gone, out)
        self.assertIn("kill -KILL", "\n".join(target.execs))
        self.assertEqual(t.field("exit"), "cancelled",
                         "the record still converges: nothing is left saying running")

    def test_a_kill_stays_cancelled_when_the_driver_it_stopped_ends_too(self):
        target = StubTarget()
        t = begin(self.tmp / "store", where="target", pid=4242, target=target)
        term = target.act_exec

        def driver_ends_first(ws, argv):
            t.end(1)
            return term(ws, argv)

        target.act_exec = driver_ends_first
        job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(), {})
        self.assertEqual(t.field("exit"), "cancelled")


class TestJobStopHasOneExitCodePerOutcome(WkTest):
    def _stop(self, target, env=None):
        return said(lambda: job.stop(target, records(self.tmp / "store", target), "selftest-ws", "build",
                                     here(), FakeClock(), env or {}))

    def test_nothing_running_is_2_and_says_so(self):
        rc, out = self._stop(StubTarget())
        self.assertEqual(rc, 2, out)
        self.assertIn("no build is running", out)

    def test_a_stopped_job_is_0_and_names_what_it_stopped(self):
        pid = spawn_orphan()
        try:
            begin(self.tmp / "store", pid=pid)
            rc, out = self._stop(StubTarget())
            self.assertEqual(rc, 0, out)
            self.assertIn("stopping the build in 'selftest-ws'", out)
            self.assertIn("cancelled", out)
        finally:
            reap(pid)

    def test_one_that_outlives_the_kill_is_1(self):
        target = StubTarget(answer="alive")
        begin(self.tmp / "store", where="target", pid=4242, target=target)
        rc, out = self._stop(target, {"WK_KILL_WAIT": "1"})
        self.assertEqual(rc, 1, out)


class TestThePidComesBackDownTheLog(WkTest):
    """The one channel that reaches the driver from every target kind."""

    def test_nothing_is_signalled_at_a_pid_that_is_not_the_job(self):
        target = StubTarget(answer="alive", args="/usr/bin/sshd -D")
        t = begin(self.tmp / "store", where="target", pid=4242, target=target)
        out = refusal(self, lambda: job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(),
                                             {"WK_KILL_WAIT": "1"}))
        self.assertIn("refusing to send", out)
        self.assertNotIn("kill -TERM", "\n".join(target.execs))

    def test_a_target_record_with_no_pattern_at_all_is_a_refusal(self):
        target = StubTarget(answer="alive")
        t = begin(self.tmp / "store", where="target", pid=4242, target=target)
        (t.path / "pid_match").unlink()
        out = refusal(self, lambda: job.kill(target, "selftest-ws", t, "cancelled", here(), FakeClock(),
                                             {"WK_KILL_WAIT": "1"}))
        self.assertIn("job.adopt", out)
        self.assertEqual(target.execs, [], "it refused before asking the target anything")


class TestThePatternsCoverEveryShapeTheJobTakes(WkTest):

    def _declared(self, path, label):
        if path == "lib/wk/build.py":
            return build.PID_MATCH
        if path == "cmd/test":
            return _load(path).PID_MATCH
        self.fail(f"no PID_MATCH source known for {label} in {path}")

    def _matches(self, args, want):
        return job.match_any(args, want)

    def test_a_builds_two_shapes_both_match_and_nothing_else_does(self):
        want = self._declared("lib/wk/build.py", "build")
        for args in ("env WK_JOBS=8 /opt/wk-tools/build/build-in-target.sh --release",
                     "Tools/Scripts/build-webkit --release --export-compile-commands",
                     "linux32 Tools/Scripts/build-jsc --release"):
            self.assertTrue(self._matches(args, want), args)
        for args in ("/sbin/init", "/usr/bin/sshd -D", "bash -lc sleep 300"):
            self.assertFalse(self._matches(args, want), args)

    def test_a_test_runs_suite_matches_and_another_shell_does_not(self):
        want = self._declared("cmd/test", "test")
        self.assertTrue(self._matches(
            'bash -lc echo "wk: test pid $$" >&2 cd /src/WebKit && nice -n 10 '
            'Tools/Scripts/run-javascriptcore-tests --release wpe', want))
        self.assertTrue(self._matches(
            "bash -lc cd /src/WebKit && Tools/Scripts/run-webkit-tests --release", want))
        self.assertFalse(self._matches("bash -lc sleep 300", want))

    def test_the_image_builds_wrapper_matches_and_a_bare_bitbake_does_not(self):
        from wk.sysimage import yocto
        self.assertTrue(self._matches(
            "python3 /opt/wk-tools/lib/wk/sysimage/yocto_target.py --target rpi5 --stage image", yocto.PATTERN))
        self.assertFalse(self._matches("bitbake core-image-weston", yocto.PATTERN))


class TestKillFromTheOutside(WkTest):


    def test_kill_stops_a_recorded_build_and_records_it_cancelled(self):
        with fake_workspace() as ws:
            pid = spawn_orphan()
            try:
                t = begin(store_of(ws), pid=pid)
                cp = ws.run("build", "--kill", timeout=120)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("stopping the build", cp.stdout)
                self.assertIn("resumes rather than starts over", cp.stdout)
                self.assertEqual(t.field("exit"), "cancelled")
                self.assertFalse(alive(pid))
            finally:
                reap(pid)

    def test_a_test_run_is_stopped_the_same_way(self):
        with fake_workspace() as ws:
            pid = spawn_orphan()
            try:
                t = begin(store_of(ws), kind="test", pid=pid,
                          kill="wk test --kill", plan="jsc/jsc-release")
                cp = ws.run("test", "--kill", timeout=120)
                self.assertEqual(cp.returncode, 0, cp.stdout)
                self.assertIn("stopping the test", cp.stdout)
                self.assertEqual(t.field("exit"), "cancelled")
                self.assertFalse(alive(pid))
            finally:
                reap(pid)

    def test_the_record_a_running_build_writes_names_the_kill_command(self):
        self.assertEqual(build.kill_cmd(False, "ws"), "wk build ws --kill")
        self.assertEqual(build.kill_cmd(True, "ws"), "wk build --kill")


if __name__ == "__main__":
    unittest.main()
