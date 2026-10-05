"""lib/wk/record.py against a scratch record directory and a fake clock: the record's shape and its verdicts."""
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.store import Store  # noqa: E402
from wk import record, status, statusview  # noqa: E402


class RecordTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-record-"))
        self.clock = FakeClock()
        self.log = self.tmp / "build.log"
        self.log.write_text("started\n")
        os.utime(self.log, (self.clock.now(), self.clock.now()))
        self.answers = {}
        self.records = record.Records(self.tmp / "store", clock=self.clock,
                                      ask_place=lambda name, pid, cap: self.answers.get((name, pid)),
                                      env={"WK_STORE": str(self.tmp / "store")})

    def tearDown(self):
        shutil.rmtree(self.tmp, True)

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
        self.assertEqual([], list((t.path / "steps").glob("*")))

    def test_a_target_record_has_no_pid_until_the_job_announces_one(self):
        t = self.begin(where="place")
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
        with self.assertRaises(ValueError):
            t.step_named("link")


class TestTheVerdict(RecordTest):
    def test_a_live_local_pid_with_a_fresh_log_is_running(self):
        t = self.begin()
        self.assertEqual(t.verdict(), "running")

    def test_a_gone_pid_is_died(self):
        t = self.begin(pid=999999)
        self.assertEqual(t.verdict(), "died")

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
        t = self.begin(where="place")
        self.assertEqual(t.verdict(), "starting")
        t.pid(77)
        self.answers[("ws", 77)] = True
        self.assertEqual(t.verdict(), "running")
        self.answers[("ws", 77)] = False
        self.assertEqual(t.verdict(), "died")
        self.answers[("ws", 77)] = None
        self.assertEqual(t.verdict("capped"), "unanswered")


class TestToleratesCorruptAndOld(RecordTest):
    """A record missing a required field or holding one that cannot be read is unreadable; the rest still list."""

    def mixed(self, damage):
        good = self.begin(name="good")
        bad = self.begin(name="bad")
        damage(bad.path)
        return good, bad

    def assert_unreadable_beside_good(self, damage, missing):
        good, bad = self.mixed(damage)
        self.assertEqual(("running", "unreadable"), (good.verdict(), bad.verdict()))
        self.assertEqual(missing, bad.unreadable())
        rows, worst = status.task_records(self.records, clock=self.clock)
        self.assertEqual({good.id: "running", bad.id: "unreadable"}, {r["task"]: r["state"] for r in rows})
        self.assertEqual(4, worst)
        self.assertEqual(good.id, self.records.find("build", "good").id)
        buf = io.StringIO()
        statusview.render_text_stream(iter([{"kind": "machine", "name": "m"}] + rows + [{"kind": "exit", "code": worst}]),
                                      buf, False)
        self.assertEqual(1, buf.getvalue().count(" unreadable"))

    def test_a_record_missing_a_required_field_is_unreadable(self):
        for field in record.REQUIRED:
            with self.subTest(field=field):
                self.assert_unreadable_beside_good(lambda p: (p / field).unlink(), [field])
                shutil.rmtree(self.records.root)

    def test_a_where_no_reader_knows_is_unreadable(self):
        self.assert_unreadable_beside_good(lambda p: (p / "where").write_text("guest\n"), ["where"])

    def test_a_field_that_cannot_be_read_is_unreadable(self):
        def damage(p):
            (p / "kind").unlink()
            (p / "kind").mkdir()
            (p / "plan").unlink()
            (p / "plan").mkdir()
        self.assert_unreadable_beside_good(damage, ["kind", "plan"])

    def test_a_record_being_begun_is_never_seen_part_written(self):
        fake = Fake("m")
        seen = []
        recs = record.Records("/s", clock=self.clock, machine=fake, env={})
        real = fake.write_own
        fake.write_own = lambda path, text: (seen.append([t.unreadable() for t in recs.list()]), real(path, text))
        recs.begin("build", "here", "ws", "wk build ws --kill", "/l", ["compile"], pid=1)
        self.assertEqual([], [u for snap in seen for u in snap if u])
        self.assertEqual([], recs.list()[0].unreadable())


class TestAStopAskedFor(RecordTest):
    def test_the_status_a_stop_caused_reads_as_the_stop(self):
        t = self.begin()
        t.set("stopping", "cancelled")
        t.end(143)
        self.assertEqual((t.field("exit"), t.verdict()), ("cancelled", "cancelled"))

    def test_a_job_that_finished_first_keeps_its_ok(self):
        t = self.begin()
        t.set("stopping", "cancelled")
        t.end(0)
        self.assertEqual(t.verdict(), "ok")


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


class TestHoldFollowsHolder(RecordTest):
    """A hold is released only when its holder is provably gone; an unreadable holder keeps it; no child inherits one."""

    def setUp(self):
        super().setUp()
        self.machine = Fake()
        self.records = record.Records(self.tmp / "store", clock=self.clock, machine=self.machine,
                                      env={"WK_STORE": str(self.tmp / "store")})

    def held(self, pid, name="ws", **kw):
        self.machine.pids.add(pid)
        return self.begin(name=name, holds="device:rpi3", pid=pid, **kw)

    def holders(self):
        return [r[0] for r in self.records.holders("device:rpi3")]

    def test_a_hold_is_released_only_when_the_machine_says_its_holder_is_gone(self):
        t = self.held(1001)
        self.assertEqual(self.holders(), [t.id])
        self.machine.pids.discard(1001)
        self.assertEqual(self.holders(), [])

    def test_an_ended_holder_holds_nothing(self):
        t = self.held(1001)
        t.end(0)
        self.assertEqual(self.holders(), [])

    def test_an_unreadable_pid_keeps_the_hold(self):
        t = self.held(1001)
        pid = str(t.path / "pid")
        self.machine.files[pid] = "10x1\n"
        self.assertEqual(self.holders(), [t.id])
        del self.machine.files[pid]
        self.machine.dirs.add(pid)
        self.assertEqual(self.holders(), [t.id])

    def test_an_unreadable_claim_keeps_the_hold(self):
        t = self.held(1001)
        holds = str(t.path / "holds")
        del self.machine.files[holds]
        self.machine.dirs.add(holds)
        self.assertEqual(self.holders(), [t.id])

    def test_a_hold_names_the_pid_that_took_it(self):
        t = self.held(1001)
        self.assertEqual(t.field("pid"), "1001")
        t = self.records.begin("bench", "here", "rpi3", "k", "/l", ["one"], holds="device:rpi3")
        self.assertEqual(t.field("pid"), str(os.getpid()))

    def test_a_workspace_pid_cannot_hold(self):
        with self.assertRaises(ValueError):
            self.begin(where="place", holds="device:rpi3")

    def test_a_target_record_given_a_hold_keeps_it_until_it_ends(self):
        t = self.begin(where="place")
        t.set("holds", "device:rpi3")
        t.pid(77)
        self.assertEqual(self.holders(), [t.id])
        t.end(0)
        self.assertEqual(self.holders(), [])

    def test_no_child_inherits_its_parents_hold(self):
        parent = self.held(1001)
        self.machine.pids.add(1002)
        records = record.Records(self.tmp / "store", clock=self.clock, machine=self.machine,
                                 env={"WK_STORE": str(self.tmp / "store"), "WK_DEVICE_HELD": "device:rpi3"})
        child = records.begin(kind="boot", where="here", name="rpi3", kill="wk boot --kill",
                              log=str(self.log), plan=["boot"], pid=1002)
        self.assertEqual(child.field("holds"), "")
        self.assertEqual(self.holders(), [parent.id])
        self.machine.pids.discard(1001)
        self.assertEqual(self.holders(), [], "the child is alive and still holds nothing")

    def test_wait_asks_the_machine_whether_the_driver_lives(self):
        self.assertEqual(self.records.wait("build", "nothing", str(self.log), pid=4242), "crashed")


class TestOneStorePerPlace(unittest.TestCase):
    """The vm place's store is WK_VM_STORE or this host's record directory, never the container's."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-vmstore-")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, True))
        self.base = {"HOME": self.tmp, "XDG_STATE_HOME": self.tmp + "/state"}

    def envs(self):
        yield "the default store", dict(self.base)
        yield "a scratch store", dict(self.base, WK_STORE=self.tmp + "/store")
        yield "a scratch store beside a vm store", dict(self.base, WK_STORE=self.tmp + "/store",
                                                       WK_VM_STORE=self.tmp + "/vm")
        yield "a vm store named as the scratch store", dict(self.base, WK_STORE=self.tmp + "/store",
                                                           WK_VM_STORE=self.tmp + "/store")
        yield "in the podman VM", dict(self.base, WK_IN_VM="1", WK_STORE="/var/lib/wk")

    def test_the_vm_store_is_never_the_container_store(self):
        for label, env in self.envs():
            with self.subTest(env=label):
                store = Store(env)
                if not store.vm_store_apart():
                    continue
                vm = store.vm_store_dir()
                self.assertNotEqual(os.path.realpath(vm), os.path.realpath(store.store_dir()))
                vm_records = Store(dict(env, WK_STORE=vm)).records_dir()
                self.assertNotEqual(os.path.realpath(vm_records), os.path.realpath(store.store_dir()))

    @unittest.skipUnless(sys.platform == "darwin", "a guest exists only on a macOS host")
    def test_a_macos_host_gives_the_vm_its_own_store_or_none(self):
        want = {"the default store": self.tmp + "/state/wk",
                "a scratch store": None,
                "a scratch store beside a vm store": self.tmp + "/vm",
                "a vm store named as the scratch store": None,
                "in the podman VM": None}
        for label, env in self.envs():
            with self.subTest(env=label):
                s = Store(env)
                self.assertEqual(s.vm_store_dir() if s.vm_store_apart() else None, want[label])

    @unittest.skipIf(sys.platform == "darwin", "a guest exists only on a macOS host")
    def test_off_macos_there_is_no_vm_store(self):
        for label, env in self.envs():
            with self.subTest(env=label):
                self.assertFalse(Store(env).vm_store_apart())


class TestTheMachineName(unittest.TestCase):
    def test_the_name_is_hostname_lowered_and_here_where_it_answers_none(self):
        m = Fake()
        m.answer(["hostname", "-s"], out="Tolken\n")
        self.assertEqual(record.host_name(m), "tolken")
        self.assertEqual(record.machine_name({}, m), "tolken")
        self.assertEqual(record.machine_name({}, Fake()), "here")

    def test_in_the_vm_the_forwarding_workstation_names_the_row(self):
        self.assertEqual(record.machine_name({"WK_IN_VM": "1", "WK_ROW_LABEL": "mbp"}, Fake()), "mbp")

    def test_a_row_label_names_the_machine_only_inside_the_vm(self):
        m = Fake()
        m.answer(["hostname", "-s"], out="Tolken\n")
        self.assertEqual(record.machine_name({"WK_ROW_LABEL": "container"}, m), "tolken")
        self.assertEqual(record.machine_name({"WK_IN_VM": "1"}, m), "tolken")


class TestWatchdogSettings(unittest.TestCase):
    def test_the_deadlines_may_be_fractional(self):
        env = {"WK_STALL_SECONDS": "1.5", "WK_ABORT_SECONDS": "2.5", "WK_TASK_ASK_SECONDS": "0.5"}
        self.assertEqual((record.watchdog_stall(env), record.watchdog_abort(env), record.task_ask_seconds(env)), (1.5, 2.5, 0.5))

    def test_a_runs_own_deadlines_never_override_the_ones_set(self):
        env = {"WK_STALL_SECONDS": "60"}
        record.default_watchdog(env, 900, 5400)
        self.assertEqual(env, {"WK_STALL_SECONDS": "60", "WK_ABORT_SECONDS": "5400"})


if __name__ == "__main__":
    unittest.main()
