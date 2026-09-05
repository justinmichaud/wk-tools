"""INT/TERM handling: `on_interrupt`/`wk_sleep` (lib/common.sh) and the sites
built on them (lib/detach.sh's `detach_wait`, lib/watchdog.sh's
`run_watched`). Each docstring is the phrase of the behaviour it checks.

Every test here sends the signal to the bash process's own pid, not its
process group -- exactly what a supervisor tracking one pid does (an agent's
own tool cancellation, or `kill -INT <pid>` below), and the case default
process-group delivery from a real terminal does not exercise.

Run: python3 -m unittest tests.test_interrupt -v
"""
import os
import signal
import subprocess
import tempfile
import time
import unittest

from tests.support import REPO

PRELUDE = f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n'


def _env():
    env = dict(os.environ)
    env["WK_ROOT"] = str(REPO)
    return env


def _kill_group_and_drain(proc, grace=5):
    """Kill the process group and read what is left, without waiting forever.

    The group, not the process: a grandchild holding the stdout pipe is
    exactly why the read blocked. A second timeout still gives up rather than
    hanging -- a test that cannot clean up must fail, not stall the suite.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    try:
        out, _ = proc.communicate(timeout=grace)
    except subprocess.TimeoutExpired:
        proc.stdout.close()
        out = "(output unreadable: the pipe was still held open after the group was killed)"
    return out


def _run_and_interrupt(script, sig=signal.SIGINT, delay=1.0, timeout=10, ready_file=None):
    """Start `bash -c script`, wait until it is ready to be interrupted, signal
    the bash pid alone, and return (returncode, elapsed_seconds, stdout+stderr).

    "Ready" is <ready_file> appearing (polled, up to <timeout>s) when given --
    the script touches it right before entering the loop under test, which is
    what makes this robust under a loaded machine where a fixed sleep is not
    long enough for bash to even finish sourcing lib/common.sh yet. Otherwise
    a plain <delay>s sleep, matching "send SIGINT after ~1s".
    """
    # Its own session, so cleanup can kill the whole group. The signal below
    # still goes to the bash pid alone -- that is the behaviour under test --
    # but a script that spawns a grandchild (`wk logs` leaves a `tail -f`)
    # leaves it holding the stdout pipe when bash exits, and communicate()
    # then waits for an EOF that never comes. `wk selftest` hung there for
    # 2h52m with a defunct bash and an orphaned tail (2026-09-05).
    proc = subprocess.Popen(
        ["bash", "-c", script],
        cwd=str(REPO),
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    if ready_file:
        deadline = time.monotonic() + timeout
        while not os.path.exists(ready_file):
            if proc.poll() is not None:
                out = proc.stdout.read()
                raise AssertionError(f"process exited ({proc.returncode}) before becoming ready:\n{out}")
            if time.monotonic() > deadline:
                proc.kill()
                raise AssertionError("never became ready (ready_file never appeared)")
            time.sleep(0.02)
    else:
        time.sleep(delay)
    sent_at = time.monotonic()
    proc.send_signal(sig)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        out = _kill_group_and_drain(proc)
        raise AssertionError(f"did not exit within {timeout}s of the signal; output so far:\n{out}")
    return proc.returncode, time.monotonic() - sent_at, out


class TestDetachWaitInterrupt(unittest.TestCase):
    def test_sigint_during_detach_wait_exits_promptly_with_130_and_runs_cleanup(self):
        """SIGINT during `detach_wait` exits promptly with 130 and runs the registered cleanup"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            status = os.path.join(tmp, "status")
            log = os.path.join(tmp, "log")
            marker = os.path.join(tmp, "cleaned")
            ready = os.path.join(tmp, "ready")

            script = PRELUDE + f"""
. lib/detach.sh
printf 'state=running\\n' > {status!r}
: > {log!r}
sleep 1000 &
DUMMY_PID=$!
mark_cleaned() {{ : > {marker!r}; kill "$DUMMY_PID" 2>/dev/null || true; }}
on_interrupt mark_cleaned
: > {ready!r}
detach_wait {status!r} {log!r} 0 "$DUMMY_PID"
"""
            rc, elapsed, out = _run_and_interrupt(script, ready_file=ready)

            # Inside the `with`: the temp dir, and the marker in it, are gone
            # the moment it exits.
            self.assertEqual(rc, 130, f"exit code was {rc}, not 130 (SIGINT); output:\n{out}")
            self.assertLess(elapsed, 5, f"took {elapsed:.1f}s to exit after SIGINT")
            self.assertTrue(os.path.exists(marker), f"on_interrupt handler did not run; output:\n{out}")

    def test_sigterm_during_detach_wait_exits_promptly_with_143(self):
        """SIGTERM during `detach_wait` exits promptly with 143"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            status = os.path.join(tmp, "status")
            log = os.path.join(tmp, "log")
            ready = os.path.join(tmp, "ready")

            script = PRELUDE + f"""
. lib/detach.sh
printf 'state=running\\n' > {status!r}
: > {log!r}
sleep 1000 &
DUMMY_PID=$!
noop() {{ kill "$DUMMY_PID" 2>/dev/null || true; }}
on_interrupt noop
: > {ready!r}
detach_wait {status!r} {log!r} 0 "$DUMMY_PID"
"""
            rc, elapsed, out = _run_and_interrupt(script, sig=signal.SIGTERM, ready_file=ready)

        self.assertEqual(rc, 143, f"exit code was {rc}, not 143 (SIGTERM); output:\n{out}")
        self.assertLess(elapsed, 5, f"took {elapsed:.1f}s to exit after SIGTERM")


class TestRunWatchedInterrupt(unittest.TestCase):
    def test_sigint_during_run_watched_kills_the_watched_child(self):
        """SIGINT during `run_watched` kills the child it is watching, not just the watcher"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            log = os.path.join(tmp, "build.log")
            pidfile = os.path.join(tmp, "child.pid")

            script = PRELUDE + f"""
. lib/watchdog.sh
WK_POLL_SECONDS=30
run_watched {log!r} -- bash -c 'echo $$ > {pidfile!r}; exec sleep 1000'
"""
            # The watched child writing its own pidfile is close enough to
            # "run_watched has registered its cleanup and entered the poll
            # loop" -- both happen within a few bash instructions of the
            # fork, well before a slow machine could plausibly take 1.5s.
            rc, elapsed, out = _run_and_interrupt(script, ready_file=pidfile)

            self.assertEqual(rc, 130, f"exit code was {rc}, not 130 (SIGINT); output:\n{out}")
            self.assertLess(elapsed, 8, f"took {elapsed:.1f}s to exit after SIGINT")
            self.assertTrue(os.path.exists(pidfile), f"the watched child never started; output:\n{out}")
            with open(pidfile) as f:
                child_pid = int(f.read().strip())

        # A moment for the TERM/KILL in run_watched's cleanup to land -- fine
        # to check after the `with` exits, since this reads the process
        # table, not the temp dir it just deleted.
        for _ in range(30):
            if not _pid_alive(child_pid):
                break
            time.sleep(0.2)
        self.assertFalse(_pid_alive(child_pid), "the watched child (sleep 1000) is still running")


class TestWkSleepInterruptible(unittest.TestCase):
    def test_wk_sleep_is_interrupted_within_its_one_second_chunk(self):
        """`wk_sleep` notices INT within its 1s chunk, not after the full duration"""
        with tempfile.TemporaryDirectory(prefix="wk-interrupt-test-") as tmp:
            ready = os.path.join(tmp, "ready")
            script = PRELUDE + f"""
noop() {{ :; }}
on_interrupt noop
: > {ready!r}
wk_sleep 30
"""
            rc, elapsed, out = _run_and_interrupt(script, timeout=8, ready_file=ready)
        self.assertEqual(rc, 130, f"exit code was {rc}, not 130 (SIGINT); output:\n{out}")
        self.assertLess(elapsed, 3, f"took {elapsed:.1f}s to notice SIGINT during a 30s wk_sleep")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


if __name__ == "__main__":
    unittest.main()
