"""lib/wk/record.py against a scratch record directory and a fake clock:
the record's shape on disk, what the verdict says under each condition,
and that a bash reader (lib/task.sh) reads what the Python writer wrote.

Run: python3 tests/run.py -k tests.test_wk_record
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import FakeClock  # noqa: E402
from wk import record  # noqa: E402


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-record-"))
        self.clock = FakeClock()
        self.log = self.tmp / "build.log"
        self.log.write_text("started\n")
        os.utime(self.log, (self.clock.now(), self.clock.now()))
        self.answers = {}
        self.records = record.Records(self.tmp / "store", clock=self.clock,
                                      ask_target=lambda name, pid, cap: self.answers.get((name, pid)),
                                      env={"WK_STORE": str(self.tmp / "store")})

    def tearDown(self):
        record._rmtree(self.tmp)

    def begin(self, **kw):
        args = dict(kind="build", where="here", name="ws", kill="wk build ws --kill",
                    log=str(self.log), plan=["configure", "compile"], pid=os.getpid())
        args.update(kw)
        return self.records.begin(**args)


class TestTheShapeOnDisk(RecordTest):
    def test_one_directory_one_file_per_field(self):
        t = self.begin(holds="device:rpi3")
        self.assertTrue(t.id.startswith("build-ws-%s-" % self.clock.stamp()))
        for f in ("kind", "where", "name", "kill", "log", "machine", "pid", "argv", "started", "plan", "holds"):
            self.assertTrue((t.path / f).is_file(), f)
        self.assertEqual(t.field("kind"), "build")
        self.assertEqual(t.plan(), ["configure", "compile"])
        self.assertEqual(t.field("holds"), "device:rpi3")
        self.assertTrue((t.path / "steps").is_dir())

    def test_a_target_record_has_no_pid_until_the_job_announces_one(self):
        t = self.begin(where="target")
        self.assertEqual(t.field("pid"), "")
        t.pid(4242, "moose")
        self.assertEqual((t.field("pid"), t.field("machine")), ("4242", "moose"))

    def test_where_plan_and_kill_are_required(self):
        with self.assertRaises(ValueError):
            self.begin(where="elsewhere")
        with self.assertRaises(ValueError):
            self.begin(plan=[])
        with self.assertRaises(ValueError):
            self.begin(kill="")

    def test_a_dead_record_of_the_same_kind_and_name_is_pruned_by_the_next(self):
        old = self.begin(pid=999999)   # no such process
        self.clock.sleep(1)
        new = self.begin()
        self.assertFalse(old.path.exists())
        self.assertTrue(new.path.exists())
        self.assertEqual([t.id for t in self.records.list()], [new.id])

    def test_another_name_with_the_same_prefix_is_not_this_task(self):
        self.assertIsNone(self.records.stamp_of("new-foo-bar-20260101T000000Z-1", "new", "foo"))
        self.assertEqual(self.records.stamp_of("new-foo-20260101T000000Z-1", "new", "foo"), "20260101T000000Z")
        self.assertEqual(self.records.stamp_of("new-foo-20260101T000000Z", "new", "foo"), "20260101T000000Z")


class TestSteps(RecordTest):
    def test_steps_read_pending_until_written(self):
        t = self.begin()
        self.assertEqual(t.steps(), [(1, "pending"), (2, "pending")])
        t.step(2)
        self.assertEqual(t.steps(), [(1, "done"), (2, "running")])
        self.assertEqual(t.step_now(), 2)
        self.assertEqual(t.stage(), ["compile"])

    def test_a_scheduler_event_maps_to_one_state(self):
        t = self.begin()
        t.step_event(1, "refused")
        self.assertEqual(t.steps()[0], (1, "pending"))
        t.step_event(1, "already")
        self.assertEqual(t.steps()[0], (1, "done"))
        with self.assertRaises(ValueError):
            t.step_event(1, "exploded")

    def test_a_step_by_name(self):
        t = self.begin()
        t.step_named("compile")
        self.assertEqual(t.step_now(), 2)
        with self.assertRaises(ValueError):
            t.step_named("link")


class TestTheVerdict(RecordTest):
    def test_a_live_local_pid_with_a_fresh_log_is_running(self):
        t = self.begin()
        self.assertEqual(t.verdict(), "running")
        self.assertTrue(t.running())

    def test_a_gone_pid_is_died(self):
        t = self.begin(pid=999999)
        self.assertEqual(t.verdict(), "died")
        self.assertFalse(t.running())

    def test_the_first_exit_stands(self):
        t = self.begin()
        t.end("cancelled")
        t.end(1)
        self.assertEqual(t.verdict(), "cancelled")
        t2 = self.begin(name="other")
        t2.end(0)
        self.assertEqual(t2.verdict(), "ok")
        t3 = self.begin(name="third")
        t3.end(2)
        self.assertEqual(t3.verdict(), "failed")

    def test_silence_is_a_verdict_only_against_a_declared_deadline(self):
        t = self.begin()
        self.clock.sleep(1000)
        self.assertEqual(t.verdict(), "running")
        t.set("abort_after", "600")
        self.assertEqual(t.verdict(stall_seconds=300), "silent")
        self.assertEqual(t.verdict(stall_seconds=2000), "running")

    def test_a_target_pid_is_asked_of_the_workspace(self):
        t = self.begin(where="target")
        self.assertEqual(t.verdict(), "starting")
        t.pid(77)
        self.answers[("ws", 77)] = True
        self.assertEqual(t.verdict(), "running")
        self.answers[("ws", 77)] = False
        self.assertEqual(t.verdict(), "died")
        self.answers[("ws", 77)] = None
        self.assertEqual(t.verdict("capped"), "unanswered")
        self.assertTrue(t.running())

    def test_the_jobs_own_exit_file_is_read_and_never_copied(self):
        t = self.begin()
        exit_file = self.tmp / "exit"
        exit_file.write_text("3\n")
        t.set("exit_file", str(exit_file))
        self.assertEqual(t.verdict(), "failed")
        self.assertFalse((t.path / "exit").exists())


class TestFindHoldersAndWait(RecordTest):
    def test_find_returns_the_newest_at_or_after_the_floor(self):
        a = self.begin()
        floor = self.clock.stamp()
        self.clock.sleep(1)
        b = self.begin()
        self.assertEqual(self.records.find("build", "ws").id, b.id)
        self.assertIsNone(self.records.find("build", "ws", floor="29990101T000000Z"))
        self.assertEqual(self.records.find("build", "ws", floor=floor).id, b.id)
        self.assertTrue(a.path.exists(), "a live record of the same kind and name is kept")
        self.assertEqual([t.id for t in self.records.list()], [a.id, b.id])

    def test_holders_are_the_live_records_holding_the_resource(self):
        held = self.begin(holds="device:rpi3")
        self.begin(name="other", holds="device:rpi4")
        dead = self.begin(name="gone", holds="device:rpi3", pid=999999)
        rows = self.records.holders("device:rpi3")
        self.assertEqual([r[0] for r in rows], [held.id])
        self.assertEqual(rows[0][2:], ("build ws", "wk build ws --kill"))
        self.assertNotIn(dead.id, [r[0] for r in rows])

    def test_wait_ends_on_the_verdict_and_streams_the_log(self):
        t = self.begin()
        out = []

        class Stream:
            def write(self, s):
                out.append(s)

            def flush(self):
                pass
        self.log.write_text("started\nline two\n")
        t.end(0)
        st = self.records.wait("build", "ws", str(self.log), stream=Stream())
        self.assertEqual(st, "ok")
        self.assertIn("line two", "".join(out))

    def test_wait_times_out_by_the_clock_not_the_wall(self):
        self.begin()
        st = self.records.wait("build", "ws", str(self.log), timeout=5)
        self.assertEqual(st, "timeout")
        self.assertEqual(sum(self.clock.slept), 5)

    def test_a_driver_that_died_before_writing_reads_crashed(self):
        st = self.records.wait("build", "nothing", str(self.log), pid=999999)
        self.assertEqual(st, "crashed")


class TestBashReadsWhatPythonWrote(RecordTest):
    def test_the_bash_library_agrees_on_fields_steps_and_verdict(self):
        t = self.begin()
        t.step(2)
        cp = bash('''
. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; . "$WK_ROOT/lib/task.sh"
d="%s"
printf '%%s|%%s|%%s\\n' "$(task_field "$d" kind)" "$(task_steps "$d" | tr '\\t\\n' ':,')" "$(task_verdict "$d")"
task_end "$d" 0
''' % t.path, env={"WK_STORE": str(self.tmp / "store"), "XDG_STATE_HOME": str(self.tmp / "state")})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.strip(), "build|1:done,2:running,|running")
        self.assertEqual(t.verdict(), "ok")

    def test_python_reads_what_the_bash_library_wrote(self):
        cp = bash('''
. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; . "$WK_ROOT/lib/task.sh"
d=$(task_begin --holds device:rpi3 build here ws "wk build ws --kill" "%s" configure compile)
task_step_named "$d" compile
printf '%%s' "$d"
''' % self.log, env={"WK_STORE": str(self.tmp / "store"), "XDG_STATE_HOME": str(self.tmp / "state")})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        t = record.Task(cp.stdout.strip(), clock=self.clock)
        self.assertEqual(t.field("holds"), "device:rpi3")
        self.assertEqual(t.steps(), [(1, "done"), (2, "running")])
        self.assertEqual(t.verdict(), "died")   # the bash subshell that begun it is gone
        self.assertEqual(self.records.holders("device:rpi3"), [])


if __name__ == "__main__":
    unittest.main()
