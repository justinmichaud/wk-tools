"""What `wk status` may claim about a build that has gone quiet."""
import json
import os
import re
import shutil
import sys
import time
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import job, record  # noqa: E402
from wk.machine import Fake  # noqa: E402


# The stub `ssh` tests/test_fleet_walk.py uses for a fleet walk with no fleet:
# it runs the probe locally instead of reaching a machine.
ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''

# A pid above every default pid_max on both platforms: dead by construction.
DEAD_PID = 4194304

PS_OUT = " 99.5 cc1plus\n 98.0 cc1plus\n 97.0 ld\n  0.1 bash\n"


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

    def test_the_reading_is_available_and_says_three(self):
        m = Fake()
        m.answer(["ps", "-A", "-o", "pcpu=,comm="], out=PS_OUT)
        self.assertEqual(3, len(job.build_processes(m)))

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


class _FakeWalk(WkTest):

    def setUp(self):
        super().setUp()
        self.xdg = self.tmp / "xdg"
        self.store = self.xdg / "wk" / "remote" / "remote"
        self.machdir = self.tmp / "machines"
        self.machdir.mkdir(parents=True)

    def task(self, kind="build", log="[1/4200] cc\n", log_age=0, **kw):
        name = kw.pop("name", f"wsl-{rand_suffix()}")
        logf = self.tmp / f"{name}.log"
        if log == "missing":
            logf = self.tmp / f"{name}-gone.log"
        else:
            logf.write_text(log)
            if log_age:
                age_log(logf, log_age)
        write_task(self.store, kind=kind, name=name, log=logf, **kw)
        return name

    def walk(self, *args, timeout=90, env=None, ssh=ANSWERING_SSH):
        with stub_path({"ssh": ssh}) as binp:
            e = {
                "XDG_STATE_HOME": str(self.xdg),
                "WK_REMOTE_ROOT": str(self.tmp / "remote-root"),
                "WK_MACHINES_DIR": str(self.machdir),
                "WK_PLACE": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                # The remote place's probe cap; the stub answers at once.
                "WK_PROBE_SECONDS": "1",
            }
            e.update(env or {})
            return run("status", *args, env=e, timeout=timeout)

    def rec(self, cp, name):
        for line in cp.stdout.splitlines():
            if not line.startswith("{"):
                continue
            r = json.loads(line)
            if r.get("kind") == "task" and r.get("name") == name:
                return r
        raise AssertionError(f"no task record for {name}:\n{cp.stdout}")

    def notes(self, rec):
        return "\n".join(n["text"] for n in rec.get("notes", []))

    def each(self, cases, *args, **kw):
        for case in cases:
            shutil.rmtree(self.store, ignore_errors=True)
            self.task(**case)
            yield case, self.walk(*args, **kw)


class TestAQuietRunningBuildIsSilentNotStalled(_FakeWalk):
    def test_a_quiet_running_record_reads_silent_and_busy(self):
        name = self.task(log_age=600)
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("no log output for", self.notes(rec))
        self.assertIn("counted as busy", self.notes(rec))
        self.assertIn("tail -f", self.notes(rec))


    def test_a_moving_log_reads_running_and_says_what_it_reached(self):
        name = self.task(log="[9/4200] cc\n")
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "running", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("[9/4200]", self.notes(rec))
        self.assertEqual(rec["kill"], f"wk build {name} --kill", rec)


    def test_a_running_record_whose_log_is_gone_is_still_running(self):
        name = self.task(log="missing")
        cp = self.walk("--records")
        self.assertEqual(self.rec(cp, name)["state"], "running", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)


class TestTheRecordedDeadlineTellsSilenceFromADeadWatchdog(_FakeWalk):

    def test_silence_past_the_recorded_deadline_says_the_watchdog_is_gone(self):
        name = self.task(log_age=4000, abort_after=1800)
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 4, cp.stdout)
        text = self.notes(rec)
        self.assertIn("past the 1800s", text)
        self.assertIn("watchdog is gone", text)
        self.assertNotIn("counted as busy", text)

    def test_wait_returns_on_a_build_past_its_deadline(self):
        self.task(log_age=4000, abort_after=1800)
        cp = self.walk("--wait", "--timeout=2",
                       env={"WK_WAIT_INTERVAL": "1"}, timeout=180)
        self.assertNotIn("wk status says busy", cp.stdout)
        self.assertEqual(cp.returncode, 4, cp.stdout)


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


class TestATestRunKeepsTheSameRecordAsABuild(_FakeWalk):

    def test_a_silent_test_past_its_deadline_names_wk_test(self):
        name = self.task(kind="test", log_age=4000, abort_after=1800,
                         plan=("jsc/jsc-release in ws1",))
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        text = self.notes(rec)
        self.assertIn("past the 1800s", text)
        self.assertIn("watchdog is gone", text)
        self.assertIn("wk test", text)
        self.assertNotIn("wk build", text)
        self.assertEqual(cp.returncode, 4, cp.stdout)


class TestTheStalledStateIsOnlyAKill(_FakeWalk):
    def test_a_written_stalled_record_stays_stalled_and_exits_3(self):
        name = self.task(log_age=4000, end="stalled")
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "stalled", cp.stdout)
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("killed after no output", self.notes(rec))


class TestOneExitCodePerRecordedState(_FakeWalk):
    def test_each_recorded_state_has_its_code(self):
        cases = ((dict(end=0), 0), (dict(end="cancelled"), 0), (dict(log="error: no\n", end=1), 1),
                 (dict(log="wk: MEMORY LIMIT hit\n", end="oom"), 3), (dict(pid=DEAD_PID), 4))
        for (case, cp), (_, want) in zip(self.each([c for c, _ in cases], "--records"), cases):
            self.assertEqual(cp.returncode, want, (case, cp.stdout))


class TestWaitWaitsThroughSilence(_FakeWalk):

    def _wait(self, **kw):
        self.task(**kw)
        cp = self.walk("--wait", "--timeout=2",
                       env={"WK_WAIT_INTERVAL": "1"}, timeout=180)
        return cp

    def test_a_silent_build_is_waited_through_until_the_timeout(self):
        cp = self._wait(log="[1/4200] cc\n", log_age=600)
        self.assertIn("wk status says busy", cp.stdout)
        self.assertRegex(cp.stdout, r"still busy after \d+s")
        self.assertEqual(cp.returncode, 2, cp.stdout)

    def test_a_stalled_or_finished_build_ends_the_wait_at_once(self):
        cases = (dict(end="stalled"), dict(end=0))
        for (case, cp), want in zip(self.each(cases, "--wait", "--timeout=2", env={"WK_WAIT_INTERVAL": "1"}, timeout=180), (3, 0)):
            self.assertNotIn("wk status says busy", cp.stdout)
            self.assertEqual(cp.returncode, want, (case, cp.stdout))

    def test_the_timeout_is_elapsed_time_and_the_report_says_how_long(self):
        self.task(log="[1/4200] cc\n", log_age=600)
        # Only the first probe is slow; its cap (WK_PROBE_SECONDS) outlasts the sleep.
        slow_probe = ('#!/bin/sh\nfor last; do :; done\n'
                      'case "$last" in *.wk-remote*)\n'
                      f'    [ -e "{self.tmp}/probed" ] || {{ : > "{self.tmp}/probed"; sleep 2; }} ;;\n'
                      'esac\n'
                      'exec bash -c "$last"\n')
        cp = self.walk("--wait", "--timeout=1", timeout=180,
                       env={"WK_WAIT_INTERVAL": "1", "WK_PROBE_SECONDS": "3"},
                       ssh=slow_probe)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        waited = re.search(r"still busy after (\d+)s", cp.stdout)
        self.assertTrue(waited, f"the wait did not report stopping: {cp.stdout}")
        self.assertGreaterEqual(int(waited.group(1)), 2,
                                f"the timeout counts sleeps, not elapsed time: {cp.stdout}")


class TestTheViewCallsSilenceBusy(unittest.TestCase):
    def test_severity_of_silent_is_busy(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk import statusview
        self.assertEqual((statusview.severity("silent"), statusview.severity("stalled")), ("busy", "bad"))


if __name__ == "__main__":
    unittest.main()
