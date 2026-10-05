"""INT/TERM/HUP: job.Signals and watch, and the kill_tree walk the stall and interrupt paths share. Each signal goes
to the driver's own pid, as a supervisor tracking one pid sends it."""
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import job  # noqa: E402
from wk.machine import Fake, Local  # noqa: E402

# A watched job as lib/wk/build.py runs one: under Signals, an Interrupted exits by its signal.
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
    def test_sigint_during_watch_kills_the_watched_child(self):
        """SIGINT during `watch` kills the child it is watching, not just the watcher, and exits 130"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            rc, out, child, _ = _interrupt_driver(tmp, signal.SIGINT)
        self.assertEqual(rc, 130, out)
        _wait_gone(child)

    def test_sighup_during_watch_runs_the_cancel_path_and_exits_129(self):
        """SIGHUP -- what a supervisor with no tty sends -- arrives as Interrupted, runs the cancel path and exits 129"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            rc, out, child, cancelled = _interrupt_driver(tmp, signal.SIGHUP)
        self.assertEqual(rc, 129, out)
        self.assertTrue(cancelled, "the cancel path did not run on HUP:\n" + out)
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
        """The one walk skips its own pid."""
        m = Fake()
        me = os.getpid()
        m.answer(["sh", "-c", job.TREE, "wk", "10"], out="%d\n11\n10\n" % me)
        m.pids.update({me, 10, 11})
        job.kill_tree(m, 10, signal.SIGTERM)
        self.assertEqual([11, 10], [e[1] for e in m.effects if e[0] == "kill"])


if __name__ == "__main__":
    unittest.main()
