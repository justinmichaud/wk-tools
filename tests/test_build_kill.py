"""job.kill through a place: the descendant walk, TERM then KILL, the pid checked against its pattern, and the
patterns every job's pid is adopted under."""
import contextlib
import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, build, job, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Result, here  # noqa: E402

PLACE_PID_MATCH = "*build-in-workspace.sh*"


class StubDriver:
    def __init__(self, answer="dead", args="bash /opt/wk-tools/build/build-in-workspace.sh --release"):
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


class KillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def begin(self, driver, pid=None):
        t = record.Records(self.tmp.name, env={}, ask_place=driver.pid_alive).begin(
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
        driver = StubDriver()
        self.assertTrue(self.kill(driver, self.begin(driver))[0])
        self.assertEqual(driver.execs, [])

    def test_the_term_goes_through_the_place_with_the_descendant_walk(self):
        driver = StubDriver()
        t = self.begin(driver, 4242)
        self.assertTrue(self.kill(driver, t)[0])
        execs = "\n".join(driver.execs)
        self.assertIn("sh -c %s wk 4242" % job.TREE, execs)
        self.assertIn("kill -TERM", execs)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_one_that_outlives_term_and_kill_is_reported_and_the_record_still_converges(self):
        driver = StubDriver(answer="alive")
        t = self.begin(driver, 4242)
        self.assertFalse(self.kill(driver, t)[0])
        self.assertIn("kill -KILL", "\n".join(driver.execs))
        self.assertEqual(t.field("exit"), "cancelled")

    def test_a_kill_stays_cancelled_when_the_driver_it_stopped_ends_too(self):
        driver = StubDriver()
        t = self.begin(driver, 4242)
        term = driver.act_exec
        driver.act_exec = lambda ws, argv: t.end(1) or term(ws, argv)
        self.kill(driver, t)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_nothing_is_signalled_at_a_pid_that_is_not_the_job(self):
        driver = StubDriver(answer="alive", args="/usr/bin/sshd -D")
        t = self.begin(driver, 4242)
        with self.assertRaises(act.Refused):
            self.kill(driver, t)
        self.assertNotIn("kill -TERM", "\n".join(driver.execs))


class TestThePatternsCoverEveryShapeTheJobTakes(unittest.TestCase):
    def test_a_builds_shapes_match_and_nothing_else_does(self):
        for args in ("env WK_JOBS=8 /opt/wk-tools/build/build-in-workspace.sh --release",
                     "Tools/Scripts/build-webkit --release --export-compile-commands",
                     "linux32 Tools/Scripts/build-jsc --release"):
            self.assertTrue(job.match_any(args, build.PID_MATCH), args)
        for args in ("/sbin/init", "/usr/bin/sshd -D", "bash -lc sleep 300"):
            self.assertFalse(job.match_any(args, build.PID_MATCH), args)

    def test_a_test_runs_suite_matches_and_another_shell_does_not(self):
        loader = importlib.machinery.SourceFileLoader("wk_cmd_test_kill", str(REPO / "cmd" / "test"))
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(mod)
        self.assertTrue(job.match_any('bash -lc echo "wk: test pid $$" >&2 cd /src/WebKit && nice -n 10 '
                                      'Tools/Scripts/run-javascriptcore-tests --release wpe', mod.PID_MATCH))
        self.assertTrue(job.match_any("bash -lc cd /src/WebKit && Tools/Scripts/run-webkit-tests --release", mod.PID_MATCH))
        self.assertFalse(job.match_any("bash -lc sleep 300", mod.PID_MATCH))

    def test_the_image_builds_wrapper_matches_and_a_bare_bitbake_does_not(self):
        from wk.sysimage import yocto
        self.assertTrue(job.match_any("python3 /opt/wk-tools/lib/wk/sysimage/yocto_ws.py --target rpi5 --stage image", yocto.PATTERN))
        self.assertFalse(job.match_any("bitbake core-image-weston", yocto.PATTERN))


if __name__ == "__main__":
    unittest.main()
