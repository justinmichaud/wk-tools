import contextlib
import io
import unittest

from wk.core import act, seams
from wk.core.clock import Clock
from wk.core.errors import Unreachable
from wk.core.fakes import Fake, FakeClock, FakeDriver, FakeSecrets, FakeTasks, Journal
from wk.core.machine import Result


class Wet(unittest.TestCase):
    def setUp(self):
        act.begin(act.Flags())
        self.journal = Journal()
        self.m = Fake("srv", self.journal)


class TestFake(Wet):
    def test_a_written_file_reads_back_and_its_parents_exist_and_list(self):
        self.m.write("/a/b/f", "text", mode="600")
        self.m.write("/a/g", "")
        self.assertEqual((self.m.read("/a/b/f"), self.m.modes["/a/b/f"]), ("text", "600"))
        self.assertEqual((self.m.exists("/a/b"), self.m.exists("/a/b/f"), self.m.listdir("/a"), self.m.listdir("/a/")), (True, True, ["b", "g"], ["b", "g"]))

    def test_an_absent_path_reads_none_does_not_exist_and_lists_nothing(self):
        self.assertEqual((self.m.read("/x"), self.m.exists("/x"), self.m.listdir("/x")), (None, False, []))

    def test_remove_takes_the_tree_and_not_its_neighbour(self):
        for path in ("/a/f", "/a/b/g", "/ab"):
            self.m.write(path, "")
        self.m.remove("/a")
        self.assertEqual(self.m.files, {"/ab": ""})

    def test_copy_writes_the_bytes_on_the_other_machine(self):
        other = Fake("other", self.journal)
        self.m.write("/t", "text")
        self.m.write("/b", b"\xff")
        self.m.copy("/t", other, "/t2")
        self.m.copy("/b", other, "/b2", mode="755")
        self.assertEqual((other.files, other.modes["/b2"], other.read("/b2")), ({"/t2": b"text", "/b2": b"\xff"}, "755", "�"))
        self.assertEqual(self.journal.effects()[2:], ["[other] write /t2", "[other] write /b2"])

    def test_spawn_is_recorded(self):
        self.m.spawn(["pueued", "-d"], "/log")
        self.assertEqual((self.m.spawned, self.journal.effects()), ([(("pueued", "-d"), "/log")], ["[srv] spawn pueued -d"]))

    def test_an_unscripted_read_fails_the_test_and_names_the_fix(self):
        with self.assertRaisesRegex(AssertionError, "an unscripted read on srv: git status\n    script it"):
            self.m.run(["git", "status"])

    def test_an_unscripted_act_succeeds_and_is_recorded_with_its_input_cwd_and_env(self):
        r = self.m.act(["git", "clone", "a b"], input="i", cwd="/w", env={"K": "v"})
        call = self.m.calls[0]
        self.assertEqual((r.rc, r.out, call.argv, call.input, call.cwd, call.env), (0, "", ("git", "clone", "a b"), "i", "/w", {"K": "v"}))
        self.assertEqual(self.journal.effects(), ["[srv] git clone 'a b'"])

    def test_the_longest_scripted_prefix_answers_with_text_a_result_or_a_function(self):
        self.m.on(("git",), "any git\n")
        self.m.on(("git", "status"), Result(1, "", "not a repo"))
        self.m.on(("git", "clone"), lambda call: self.m.files.update({call.argv[2] + "/.git": ""}))
        self.assertEqual(self.m.run(["git", "log"]).out, "any git\n")
        self.assertEqual((self.m.run(["git", "status"]).rc, self.m.run(["git", "status"]).err), (1, "not a repo"))
        self.assertEqual((self.m.act(["git", "clone", "/src"]).ok, self.m.exists("/src")), (True, True))

    def test_reads_and_effects_are_journalled_in_order_and_only_effects_count(self):
        self.m.on(("ls",), "")
        self.m.run(["ls"])
        self.m.write("/f", "")
        self.assertEqual(([line for line, _ in self.journal.entries], self.journal.effects()), (["[srv] ls", "[srv] write /f"], ["[srv] write /f"]))

    def test_a_machine_that_is_down_raises_unreachable_from_every_method(self):
        self.m.down = True
        for ask in (lambda: self.m.run(["true"]), lambda: self.m.act(["true"]), lambda: self.m.read("/x"), lambda: self.m.exists("/x"),
                    lambda: self.m.listdir("/x"), lambda: self.m.write("/x", ""), lambda: self.m.remove("/x")):
            self.assertRaises(Unreachable, ask)

    def test_under_dry_run_nothing_changes_and_nothing_is_journalled(self):
        self.m.write("/f", "")
        act.begin(act.Flags(dry_run=True))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.m.remove("/f")
            self.assertEqual((self.m.act(["rm", "/f"]).rc, self.m.files, len(self.journal.effects())), (0, {"/f": ""}, 1))
        self.assertEqual(err.getvalue(), "would: [srv] remove /f\nwould: [srv] rm /f\n")

    def test_it_makes_its_own_journal_when_given_none(self):
        self.assertEqual(Fake("solo").journal.entries, [])


class TestClock(unittest.TestCase):
    def test_wait_until_is_true_once_the_condition_holds(self):
        clock = FakeClock()
        clock.after(25, lambda: seen.append(clock.now()))
        seen = []
        self.assertTrue(clock.wait_until(lambda: seen, timeout=60, interval=10))
        self.assertEqual((seen, clock.now()), ([1030.0], 1030.0))

    def test_wait_until_is_false_at_the_timeout(self):
        clock = FakeClock()
        self.assertFalse(clock.wait_until(lambda: False, timeout=30, interval=10))
        self.assertEqual(clock.now(), 1030.0)

    def test_scheduled_events_run_once_in_time_order(self):
        clock, seen = FakeClock(), []
        clock.after(2, lambda: seen.append("b"))
        clock.after(1, lambda: seen.append("a"))
        clock.sleep(5)
        clock.sleep(5)
        self.assertEqual(seen, ["a", "b"])

    def test_the_real_clock_tells_the_time_and_does_not_sleep_for_a_condition_that_holds(self):
        clock = Clock()
        clock.sleep(0)
        self.assertTrue(clock.wait_until(lambda: True, timeout=None))
        self.assertGreater(clock.now(), 1.7e9)


class TestSeamFakes(Wet):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.ws = seams.Workspace("x", "srv", "/home/u/wk/ws/x", "container", seams.READY)

    def test_a_driver_creates_reports_and_destroys_idempotently(self):
        d = FakeDriver("container", self.m, self.journal, set())
        self.assertEqual(d.state(self.ws), seams.ABSENT)
        d.create(self.ws)
        d.create(self.ws)
        self.assertEqual((d.kind, d.state(self.ws)), ("container", seams.READY))
        d.destroy(self.ws)
        d.destroy(self.ws)
        self.assertEqual((d.state(self.ws), self.journal.effects()[0]), (seams.ABSENT, "[srv] driver create x"))

    def test_a_driver_gives_the_argv_that_runs_or_enters_the_workspace(self):
        d = FakeDriver("plain", self.m, self.journal, set())
        self.assertEqual(d.exec_argv(self.ws, ["make", "-j4"]), ["fake-exec", "x", "/home/u/wk/ws/x/src", "make", "-j4"])
        self.assertEqual((d.exec_argv(self.ws, ["ls"], cwd="/c")[2], d.enter_argv(self.ws)), ("/c", ["fake-enter", "x"]))

    def test_a_task_runs_until_finished_and_keeps_its_log(self):
        t = FakeTasks(self.m, self.clock, self.journal)
        a, b = t.add(["make", "a b"], "x", group="build"), t.add(["false"], "y")
        self.assertEqual(t.status()[a], seams.Task(0, "x", "build", seams.RUNNING, "make 'a b'"))
        t.finish(a, log="built\n")
        t.finish(b, rc=2)
        self.assertEqual([(k.state, k.rc, k.ended) for k in t.status()], [("done", 0, True), ("failed", 2, True)])
        self.assertEqual((t.log(a), t.log(b), self.journal.effects()[0]), ("built\n", "", "[srv] task add x: make 'a b'"))

    def test_kill_ends_a_running_task_and_leaves_an_ended_one(self):
        t = FakeTasks(self.m, self.clock, self.journal)
        a = t.add(["sleep", "9"], "x")
        t.kill(a)
        t.kill(a)
        self.assertEqual((t.status()[a].state, self.journal.effects()[1:]), (seams.KILLED, ["[srv] task kill 0"]))

    def test_wait_returns_the_ended_task_or_the_running_one_at_the_timeout(self):
        t = FakeTasks(self.m, self.clock, self.journal)
        a = t.add(["make"], "x")
        self.assertEqual(t.wait(a, timeout=5).state, seams.RUNNING)
        self.clock.after(30, lambda: t.finish(a, rc=1))
        self.assertEqual(t.wait(a).state, seams.FAILED)

    def test_a_dry_run_adds_no_task_and_returns_the_id_it_would_get(self):
        t = FakeTasks(self.m, self.clock, self.journal)
        act.begin(act.Flags(dry_run=True))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual((t.add(["make"], "x"), t.status()), (0, []))
        self.assertEqual(err.getvalue(), "would: [srv] task add x: make\n")

    def test_secrets_put_get_and_list(self):
        s = FakeSecrets(self.journal)
        s.put("github-token", "v")
        s.put("bugzilla-key", "w")
        self.assertEqual((s.get("github-token"), s.get("absent"), s.list()), ("v", None, ["bugzilla-key", "github-token"]))
        self.assertEqual(self.journal.effects(), ["[store] secret put github-token", "[store] secret put bugzilla-key"])

    def test_the_contracts_refuse_to_run_unimplemented(self):
        for call in (lambda: seams.Driver().create(self.ws), lambda: seams.Driver().destroy(self.ws), lambda: seams.Driver().state(self.ws),
                     lambda: seams.Driver().exec_argv(self.ws, []), lambda: seams.Driver().enter_argv(self.ws),
                     lambda: seams.Tasks().add([], "x"), lambda: seams.Tasks().status(), lambda: seams.Tasks().log(0),
                     lambda: seams.Tasks().kill(0), lambda: seams.Tasks().wait(0),
                     lambda: seams.Secrets().get("n"), lambda: seams.Secrets().put("n", "v"), lambda: seams.Secrets().list()):
            self.assertRaises(NotImplementedError, call)
