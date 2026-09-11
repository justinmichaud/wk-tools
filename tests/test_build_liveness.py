"""What `wk status` may claim about a build that has gone quiet.

A full-LTO link writes nothing to the log for many minutes, so silence is not
death, and this machine's compiler count cannot say otherwise: a container
build's compilers reach the process table through `podman exec` and name their
workspace only in their cgroup, and a macOS guest's run in another kernel. So
the verdict (`task_verdict`, lib/task.sh) is the recorded pid plus the log's
age and nothing else, cmd/status reports `silent` rather than `stalled` for a
quiet running record, and `stalled` stays what cmd/build and cmd/test write
when a watchdog killed the job.

The one thing the log's age settles by itself is how long a build has been
quiet, so the record carries `abort_after` -- the deadline the watchdog arming
it gives up at. Past that deadline a live `wk build` would have killed the job
and written `stalled`, so a record still saying running is one whose watchdog
is gone too.

The exit codes that follow: `silent` is busy (2), so `wk status --wait` waits
through it; `stalled` is 3, and `--wait` returns; `silent` past `abort_after`
is 4, a record with no writer left.

Run: python3 -m unittest tests.test_build_liveness -v
"""
import json
import os
import time
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, stub_path

STATUS_VIEW = REPO / "lib" / "status-view.py"

# The stub `ssh` tests/test_fleet_walk.py uses for a fleet walk with no fleet:
# it runs the probe locally instead of reaching a machine.
ANSWERING_SSH = '''#!/bin/sh
for last; do :; done
exec bash -c "$last"
'''

# A pid above every default pid_max on both platforms: dead by construction.
DEAD_PID = 4194304

STUB_PS = '''ps() {
cat <<'PSOUT'
 99.5 cc1plus
 98.0 cc1plus
 97.0 ld
  0.1 bash
PSOUT
}
'''


def write_task(store, kind="build", name="ws1", pid=None, log=None,
               plan=("compile jsc-release with -j4",), end=None,
               abort_after=1800, config="jsc-release", kill=None):
    """One task record, written by lib/task.sh itself into <store>, the way
    every long-running command writes it. Returns the record's directory."""
    steps = " ".join("'%s'" % s for s in plan)
    kill = kill or "wk %s %s --kill" % (kind, name)
    lines = [
        '. "%s/lib/common.sh"' % REPO,
        '. "%s/lib/task.sh"' % REPO,
        "d=$(task_begin %s here %s '%s' '%s' %s)"
        % (kind, name, kill, log or "/dev/null", steps),
        'task_step "$d" 1',
        'task_set "$d" config %s' % config,
        'task_pid "$d" %s' % (pid if pid is not None else os.getpid()),
    ]
    if end is not None:
        lines.append('task_end "$d" %s' % end)
    lines.append('printf "%s" "$d"')
    cp = bash("\n".join(lines),
              env={"WK_STORE": str(store), "WK_ABORT_SECONDS": str(abort_after)})
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout.strip()


def age_log(path, seconds):
    past = time.time() - seconds
    os.utime(path, (past, past))


class TestTheVerdictReadsThePidAndTheLogAndNotTheProcessTable(WkTest):
    """`task_verdict` (lib/task.sh): a record with no outcome is running while
    its pid answers and its log moved within WK_STALL_SECONDS, silent when the
    log went quiet, died when the pid is gone. A machine mid-link is the case
    that made someone want the process count in here, so it is stubbed present
    in every one of these and changes no answer."""

    def _verdict(self, pid=None, log_age=0, end=None, stall=""):
        store = self.tmp / "store"
        logf = self.tmp / f"build-{rand_suffix()}.log"
        logf.write_text("[1/4200] cc\n")
        age_log(logf, log_age)
        d = write_task(store, pid=pid, log=logf, end=end)
        cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/task.sh"
{STUB_PS}
{stall}task_verdict "{d}"
''', env={"WK_STORE": str(store)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_the_reading_is_available_and_says_three(self):
        cp = bash(f'. "{REPO}/lib/detach.sh"\n{STUB_PS}\nbuild_processes')
        self.assertEqual(cp.stdout.strip(), "3", cp.stdout + cp.stderr)

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
        self.assertEqual(self._verdict(log_age=2, stall="WK_STALL_SECONDS=1 "),
                         "silent")
        self.assertEqual(self._verdict(log_age=2), "running")


class _FakeWalk(WkTest):
    """One `wk status` walk over a faked store, with no hardware: WK_TARGET
    =remote with an answering stub `ssh` (so nothing is delegated and the
    local records are reported), an empty WK_MACHINES_DIR so the device walk
    finds nothing, and the task records under the scratch XDG_STATE_HOME where
    targets/remote.sh's per-target store resolves."""

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

    def walk(self, *args, timeout=90, env=None):
        with stub_path({"ssh": ANSWERING_SSH}) as binp:
            e = {
                "XDG_STATE_HOME": str(self.xdg),
                "WK_REMOTE_ROOT": str(self.tmp / "remote-root"),
                "WK_MACHINES_DIR": str(self.machdir),
                "WK_TARGET": "remote",
                "WK_REMOTE_HOST": "fake-reachable-machine",
                "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            }
            e.update(env or {})
            return run("status", *args, env=e, timeout=timeout)

    def rec(self, cp, name):
        """The task record `wk status --records` wrote for one job."""
        for line in cp.stdout.splitlines():
            if not line.startswith("{"):
                continue
            r = json.loads(line)
            if r.get("kind") == "task" and r.get("name") == name:
                return r
        raise AssertionError(f"no task record for {name}:\n{cp.stdout}")

    def notes(self, rec):
        return "\n".join(n["text"] for n in rec.get("notes", []))


class TestAQuietRunningBuildIsSilentNotStalled(_FakeWalk):
    def test_a_quiet_running_record_reads_silent_and_busy(self):
        name = self.task(log_age=600)
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("no log output for", self.notes(rec))

    def test_the_note_says_it_keeps_waiting_and_names_the_log(self):
        name = self.task(log_age=600)
        rec = self.rec(self.walk("--records"), name)
        text = self.notes(rec)
        self.assertIn("counted as busy", text)
        self.assertIn("tail -f", text)

    def test_a_moving_log_reads_running_and_says_what_it_reached(self):
        name = self.task(log="[9/4200] cc\n")
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "running", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("[9/4200]", self.notes(rec))

    def test_the_record_names_the_command_that_stops_it(self):
        name = self.task(log="[9/4200] cc\n")
        rec = self.rec(self.walk("--records"), name)
        self.assertEqual(rec["kill"], f"wk build {name} --kill", rec)

    def test_a_running_record_whose_log_is_gone_is_still_running(self):
        """No log is no evidence of silence: the pid answers, so this is a
        build that has not written its first line yet."""
        name = self.task(log="missing")
        cp = self.walk("--records")
        self.assertEqual(self.rec(cp, name)["state"], "running", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)


class TestTheRecordedDeadlineTellsSilenceFromADeadWatchdog(_FakeWalk):
    def test_silence_inside_the_recorded_deadline_is_still_busy(self):
        name = self.task(log_age=600, abort_after=1800)
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("counted as busy", self.notes(rec))
        self.assertNotIn("watchdog is gone", self.notes(rec))

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
    """One writer for the deadline: lib/task.sh stamps `abort_after` from
    WK_ABORT_SECONDS, the same variable lib/watchdog.sh aborts on, and
    cmd/build declares its record through it rather than writing one of its
    own."""

    def _record(self, env=None):
        store = self.tmp / "store"
        d = write_task(store, log=self.tmp / "build.log", **(env or {}))
        return d

    def test_a_running_record_carries_the_watchdogs_default_deadline(self):
        d = self._record()
        self.assertEqual((__import__("pathlib").Path(d) / "abort_after").read_text().strip(),
                         "1800")

    def test_a_per_run_override_is_what_the_record_says(self):
        d = self._record({"abort_after": 5400})
        self.assertEqual((__import__("pathlib").Path(d) / "abort_after").read_text().strip(),
                         "5400")

    def test_cmd_build_declares_its_record_through_the_task_lib(self):
        text = (REPO / "cmd" / "build").read_text()
        self.assertIn("task_begin build here", text)
        self.assertNotIn("build.status", text)
        self.assertNotIn("write_status", text)

    def test_the_record_and_the_watchdog_read_one_variable(self):
        self.assertIn('_task_put "$dir/abort_after" "$WK_ABORT_SECONDS"',
                      (REPO / "lib" / "task.sh").read_text())
        self.assertIn('[ "$idle" -ge "$WK_ABORT_SECONDS" ]',
                      (REPO / "lib" / "watchdog.sh").read_text())


class TestATestRunKeepsTheSameRecordAsABuild(_FakeWalk):
    """`wk test` writes the same record, so a test run that lost its watchdog
    has to be tellable from a quiet one, and the note names `wk test`."""

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

    def test_a_silent_test_inside_its_deadline_is_still_busy(self):
        name = self.task(kind="test", log_age=600, abort_after=1800)
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "silent", cp.stdout)
        self.assertIn("counted as busy", self.notes(rec))
        self.assertEqual(cp.returncode, 2, cp.stdout)

    def test_the_record_cmd_test_writes_carries_the_deadline(self):
        text = (REPO / "cmd" / "test").read_text()
        self.assertIn("task_begin test here", text)
        self.assertIn("lib/watchdog.sh", text)


class TestTheStalledStateIsOnlyAKill(_FakeWalk):
    def test_a_written_stalled_record_stays_stalled_and_exits_3(self):
        name = self.task(log_age=4000, end="stalled")
        cp = self.walk("--records")
        rec = self.rec(cp, name)
        self.assertEqual(rec["state"], "stalled", cp.stdout)
        self.assertEqual(cp.returncode, 3, cp.stdout)
        self.assertIn("killed after no output", self.notes(rec))

    def test_nothing_in_cmd_status_manufactures_a_state(self):
        text = (REPO / "cmd" / "status").read_text()
        self.assertNotIn("state=stalled", text)
        self.assertIn("task_verdict", text)


class TestOneExitCodePerRecordedState(_FakeWalk):
    def _code(self, **kw):
        self.task(**kw)
        return self.walk("--records").returncode

    def test_ok_is_0(self):
        self.assertEqual(self._code(end=0), 0)

    def test_cancelled_is_0(self):
        """A build a person stopped is not a build that needs attention."""
        self.assertEqual(self._code(end="cancelled"), 0)

    def test_failed_is_1(self):
        self.assertEqual(self._code(log="error: no\n", end=1), 1)

    def test_running_is_2(self):
        self.assertEqual(self._code(log="[9/9] cc\n"), 2)

    def test_silent_is_2(self):
        self.assertEqual(self._code(log="[1/9] cc\n", log_age=600), 2)

    def test_stalled_is_3(self):
        self.assertEqual(self._code(end="stalled"), 3)

    def test_oom_is_3(self):
        self.assertEqual(self._code(log="wk: MEMORY LIMIT hit\n", end="oom"), 3)

    def test_a_dead_pid_with_no_outcome_is_4(self):
        self.assertEqual(self._code(pid=DEAD_PID), 4)


class TestWaitWaitsThroughSilence(_FakeWalk):
    """`--wait` waits on exit 2 and on nothing else (cmd/status's one
    `[ "$_rc" = 2 ] || break`), so this drives the states rather than the
    loop: a silent build blocks until --timeout, a killed one returns."""

    def _wait(self, **kw):
        self.task(**kw)
        cp = self.walk("--wait", "--timeout=2",
                       env={"WK_WAIT_INTERVAL": "1"}, timeout=180)
        return cp

    def test_a_silent_build_is_waited_through_until_the_timeout(self):
        cp = self._wait(log="[1/4200] cc\n", log_age=600)
        self.assertIn("wk status says busy", cp.stdout)
        self.assertIn("still busy after 2s", cp.stdout)
        self.assertEqual(cp.returncode, 2, cp.stdout)

    def test_a_stalled_build_ends_the_wait_at_once(self):
        cp = self._wait(end="stalled")
        self.assertNotIn("wk status says busy", cp.stdout)
        self.assertEqual(cp.returncode, 3, cp.stdout)

    def test_a_finished_build_ends_the_wait_at_once(self):
        cp = self._wait(end=0)
        self.assertNotIn("wk status says busy", cp.stdout)
        self.assertEqual(cp.returncode, 0, cp.stdout)


class TestTheViewCallsSilenceBusy(unittest.TestCase):
    def test_severity_of_silent_is_busy(self):
        cp = bash(f'''python3 - <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sv", "{STATUS_VIEW}")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(m.severity("silent"), m.severity("stalled"))
PY''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "busy bad", cp.stdout + cp.stderr)

    def test_exit_2_is_explained_the_same_way_in_both_files(self):
        """The page prints the exit code in cmd/status's own words, so the
        two say the same thing about a silent build or one of them is
        wrong."""
        header = (REPO / "cmd" / "status").read_text()
        view = STATUS_VIEW.read_text()
        for text in ("a build is running or silent",
                     "a build stalled and was killed by its own watchdog"):
            self.assertIn(text, header, text)
            self.assertIn(text, view, text)


if __name__ == "__main__":
    unittest.main()
