"""lib/wk/record.py against a scratch record directory and a fake clock: the record's shape and its verdicts."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from tests.fakes import FakeClock  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.store import Store  # noqa: E402
from wk import record, status, statusview  # noqa: E402
from wk.act import Refused  # noqa: E402


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
        t = self.begin(holds=["device:rpi3", "ws:ws"])
        self.assertTrue(t.id.startswith("build-ws-%s-" % self.clock.stamp()))
        for f in ("kind", "where", "name", "kill", "log", "machine", "pid", "argv", "started", "plan", "holds"):
            self.assertTrue((t.path / f).is_file(), f)
        self.assertEqual(t.field("kind"), "build")
        self.assertEqual(t.plan(), ["configure", "compile"])
        self.assertEqual(t.holds(), ["device:rpi3", "ws:ws"])
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


class TestHolds(RecordTest):
    """A hold is a resource on a live record: it ends with its holder, a claim of a held one is refused under one lock,
    and a parent's holds are its child's."""

    def setUp(self):
        super().setUp()
        self.machine = Fake()
        self.records = record.Records(self.tmp / "store", clock=self.clock, machine=self.machine,
                                      ask_place=lambda name, pid, cap: self.answers.get((name, pid)),
                                      env={"WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks")})

    def held(self, pid, name="ws", **kw):
        self.machine.pids.add(pid)
        return self.begin(name=name, holds=["device:rpi3"], pid=pid, **kw)

    def holders(self, records=None):
        return [t.id for t in (records or self.records).holders("device:rpi3")]

    def test_a_hold_lasts_while_its_holder_lives_and_names_the_holder(self):
        t = self.held(1001)
        self.held(1002, name="other").set("holds", "device:rpi4")
        self.assertEqual(self.holders(), [t.id])
        self.assertEqual(self.records.holders("device:rpi3")[0].line().split("\t")[2:], ["build ws", "wk build ws --kill"])
        self.machine.pids.discard(1001)
        self.assertEqual(self.holders(), [])
        t = self.held(1003, name="ended")
        t.end(0)
        self.assertEqual(self.holders(), [])

    def test_what_cannot_be_read_keeps_the_hold(self):
        t = self.held(1001)
        self.machine.files[str(t.path / "pid")] = "10x1\n"
        self.assertEqual(self.holders(), [t.id], "an unreadable pid")
        self.machine.files[str(t.path / "pid")] = "1001\n"
        holds = str(t.path / "holds")
        del self.machine.files[holds]
        self.machine.dirs.add(holds)
        self.assertEqual(self.holders(), [t.id], "an unreadable claim holds everything")

    def test_a_workspace_pid_holds_while_the_workspace_says_it_lives(self):
        with self.assertRaises(ValueError):
            self.begin(where="place", holds=["device:rpi3"])
        t = self.begin(where="place")
        t.set("holds", "device:rpi3")
        t.pid(77)
        self.answers[("ws", 77)] = None
        self.assertEqual(self.holders(), [t.id], "no answer is no evidence it is gone")
        self.answers[("ws", 77)] = False
        self.assertEqual(self.holders(), [])

    def test_a_parents_holds_are_its_childs(self):
        parent = self.held(1001)
        child = record.Records(self.tmp / "store", clock=self.clock, machine=self.machine,
                               env={"WK_STORE": str(self.tmp / "store"), "WK_TASK_PARENT": str(parent.path)})
        self.assertEqual(self.holders(child), [])
        self.assertEqual(self.holders(), [parent.id])

    def test_a_claim_of_a_held_resource_is_refused_and_one_of_a_free_one_holds_it(self):
        refused = []
        t = self.records.claim("bench", "rpi3", "k", "/l", ["one"], ["device:rpi3"], lambda r, held: refused.append(r))
        self.assertEqual((refused, t.holds(), t.field("pid")), ([], ["device:rpi3"], str(os.getpid())))
        self.machine.pids.add(os.getpid())

        def refuse(resource, held):
            raise Refused(1)
        with self.assertRaises(Refused):
            self.records.claim("boot", "rpi3", "k", "/l", ["two"], ["device:rpi3"], refuse)
        self.assertEqual(self.holders(), [t.id])

    def test_a_dry_run_claim_refuses_the_same_and_takes_nothing(self):
        with unittest.mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
            self.assertIsNone(self.records.claim("bench", "rpi3", "k", "/l", ["one"], ["device:rpi3"], None))
        self.assertEqual(self.records.list(), [])

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

    def test_a_row_label_names_the_machine_only_inside_the_vm(self):
        m = Fake()
        m.answer(["hostname", "-s"], out="Tolken\n")
        for env, want in (({"WK_IN_VM": "1", "WK_ROW_LABEL": "mbp"}, "mbp"), ({"WK_ROW_LABEL": "container"}, "tolken"),
                          ({"WK_IN_VM": "1"}, "tolken")):
            with self.subTest(env=env):
                self.assertEqual(record.machine_name(env, m), want)


class TestWatchdogSettings(unittest.TestCase):
    def test_the_deadlines_may_be_fractional(self):
        env = {"WK_STALL_SECONDS": "1.5", "WK_ABORT_SECONDS": "2.5"}
        self.assertEqual((record.watchdog_stall(env), record.watchdog_abort(env)), (1.5, 2.5))

    def test_a_runs_own_deadlines_never_override_the_ones_set(self):
        env = {"WK_STALL_SECONDS": "60"}
        record.default_watchdog(env, 900, 5400)
        self.assertEqual(env, {"WK_STALL_SECONDS": "60", "WK_ABORT_SECONDS": "5400"})


if __name__ == "__main__":
    unittest.main()


class TestTheWorkspacesLog(RecordTest):
    """`wk status <ws> --log` shows the newest log anything wrote for the workspace."""

    def driver(self):
        st = Store({"WK_STORE": str(self.tmp / "store")})
        return type("D", (), {"store": st, "env": {"WK_STORE": str(self.tmp / "store")}, "pid_alive": lambda *a: None})()

    def write(self, path, age):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
        os.utime(path, (1000 - age, 1000 - age))
        return str(path)

    def test_a_creation_before_any_build_is_its_log_and_a_later_build_wins(self):
        made = self.write(self.tmp / "store" / "log" / "new-ws.log", 50)
        self.begin(kind="new", log=made, pid=999999)
        self.begin(kind="new", name="other", log=self.write(self.tmp / "store" / "log" / "new-other.log", 1), pid=999999)
        self.assertEqual(made, record.workspace_log(self.driver(), "ws"))
        built = self.write(self.tmp / "store" / "ws" / "ws" / "build.log", 10)
        self.assertEqual(built, record.workspace_log(self.driver(), "ws"))
        staged = self.write(self.tmp / "store" / "ws" / "ws" / "home" / "yocto-image.log", 5)
        self.assertEqual(staged, record.workspace_log(self.driver(), "ws"))

    def test_nothing_written_is_refused_naming_what_writes_one(self):
        err = io.StringIO()
        with self.assertRaises(Refused), unittest.mock.patch("sys.stderr", err):
            record.workspace_log(self.driver(), "ws")
        self.assertIn("'wk new', 'wk build', 'wk sysimage build'", err.getvalue())
