"""What a build's record may claim once it has gone quiet: the verdict reads the pid and the log, and the record carries
the deadline the watchdog is armed with. `wk status`'s rendering of it is tests/test_status.py's."""
import os
import sys
import time
import unittest

from tests.support import REPO, WkTest, rand_suffix

sys.path.insert(0, str(REPO / "lib"))
from wk import record  # noqa: E402


# A pid above every default pid_max on both platforms: dead by construction.
DEAD_PID = 4194304

def write_task(store, kind="build", name="ws1", pid=None, log=None,
               plan=("compile jsc-release with -j4",), end=None,
               abort_after=1800, preset="jsc-release", kill=None):
    t = record.Records(store, env={"WK_ABORT_SECONDS": str(abort_after)}).begin(
        kind, "here", name, kill or "wk %s %s --kill" % (kind, name), str(log or "/dev/null"), list(plan),
        pid=os.getpid() if pid is None else pid)
    t.step(1)
    t.set("preset", preset)
    if end is not None:
        t.end(end)
    return t


def age_log(path, seconds):
    past = time.time() - seconds
    os.utime(path, (past, past))


class TestTheVerdictReadsThePidAndTheLogAndNotTheProcessTable(WkTest):

    def _verdict(self, pid=None, log_age=0, end=None, stall=None):
        logf = self.tmp / f"build-{rand_suffix()}.log"
        logf.write_text("[1/4200] cc\n")
        age_log(logf, log_age)
        return write_task(self.tmp / "store", pid=pid, log=logf, end=end).verdict(stall_seconds=stall)

    def test_a_fresh_log_is_running(self):
        self.assertEqual(self._verdict(), "running")

    def test_a_stale_log_is_silent_even_with_three_compilers_running(self):
        self.assertEqual(self._verdict(log_age=4000), "silent")

    def test_a_pid_that_is_gone_died(self):
        self.assertEqual(self._verdict(pid=DEAD_PID), "died")

    def test_an_ended_record_is_its_own_outcome(self):
        self.assertEqual(self._verdict(end=0), "ok")
        self.assertEqual(self._verdict(end=1), "failed")
        self.assertEqual(self._verdict(end="cancelled"), "cancelled")

    def test_the_silence_threshold_is_wk_stall_seconds(self):
        self.assertEqual(self._verdict(log_age=2, stall=1),
                         "silent")
        self.assertEqual(self._verdict(log_age=2), "running")


class TestTheRecordCarriesTheDeadlineTheWatchdogIsArmedWith(WkTest):

    def _record(self, env=None):
        return write_task(self.tmp / "store", log=self.tmp / "build.log", **(env or {}))

    def test_a_running_record_carries_the_watchdogs_default_deadline(self):
        self.assertEqual(self._record().field("abort_after"), "1800")

    def test_a_per_run_override_is_what_the_record_says(self):
        self.assertEqual(self._record({"abort_after": 5400}).field("abort_after"), "5400")

    def test_the_record_and_the_watchdog_read_one_variable(self):
        records = record.Records(self.tmp / "s", env={"WK_ABORT_SECONDS": "77"})
        t = records.begin("build", "here", "ws", "k", "/l", ["one"])
        self.assertEqual("77", t.field("abort_after"))


class TestTheViewCallsSilenceBusy(unittest.TestCase):
    def test_severity_of_silent_is_busy(self):
        from wk import statusview
        self.assertEqual((statusview.severity("silent"), statusview.severity("stalled")), ("busy", "bad"))


if __name__ == "__main__":
    unittest.main()
