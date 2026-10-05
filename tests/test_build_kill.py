"""job.kill through a place: the descendant walk, TERM then KILL, the pid checked against its pattern, and the
patterns every job's pid is adopted under."""
import contextlib
import io
import sys
import tempfile
import unittest

from tests.fakes import WsDriver
from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import act, build, job, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, here  # noqa: E402

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


if __name__ == "__main__":
    unittest.main()
