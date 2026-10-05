"""The plan as a graph, and the one scheduler that runs it (lib/wk/sched.py)."""
import sys
import threading
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import sched  # noqa: E402
from wk.act import RETRY_EXIT, Refused  # noqa: E402
from wk.machine import Fake  # noqa: E402


class FakeRuns:
    """Runs nothing; records what was asked, the peak in flight, and which pairs overlapped."""

    def __init__(self, codes=None, block=()):
        self.codes = dict(codes or {})
        self.block = set(block)
        self.started, self.together, self.live = [], set(), set()
        self.peak = 0
        self.lock = threading.Lock()
        self.gate = threading.Event()

    def __call__(self, sid):
        with self.lock:
            self.started.append(sid)
            self.live.add(sid)
            self.peak = max(self.peak, len(self.live))
            self.together |= {frozenset((sid, o)) for o in self.live if o != sid}
        if sid in self.block:
            self.gate.wait(10)
        with self.lock:
            self.live.discard(sid)
        code = self.codes.get(sid, 0)
        return (code.pop(0) if code else 0) if isinstance(code, list) else code

    def overlapped(self, a, b):
        return frozenset((a, b)) in self.together


def step(sid, needs="", holds="", done=None, runs=None):
    runs = runs or FakeRuns()
    return sched.Step(sid, "m", needs, holds, done, lambda: runs(sid), command="run " + sid)


def steps(runs, *specs, done=()):
    """(id, needs, holds) each; a step in `done` declares a predicate answering yes, every other one none."""
    return [step(i, n, h, (lambda: True) if i in done else None, runs) for i, n, h in specs]


class TestTheGraphIsChecked(unittest.TestCase):

    def refused(self, graph):
        with self.assertRaises(Refused):
            sched.validate(graph)

    def test_a_step_needing_one_nothing_declares_is_refused(self):
        self.refused([step("a", "ghost")])

    def test_an_id_names_one_step(self):
        self.refused([step("a"), step("a")])

    def test_steps_that_need_each_other_are_refused(self):
        self.refused([step("a", "b"), step("b", "a")])

    def test_needs_and_holds_are_lists(self):
        s = step("c", "a,b", "machine:x device:d")
        self.assertEqual((s.needs, s.holds), (("a", "b"), ("machine:x", "device:d")))


class TestWhatRunsAtOnce(unittest.TestCase):
    def waves(self, graph, done=()):
        return [[s.id for s in wave] for wave in sched.waves(graph, done)]

    def test_independent_steps_are_one_wave(self):
        self.assertEqual(self.waves([step("a"), step("b")]), [["a", "b"]])

    def test_a_need_is_a_later_wave(self):
        self.assertEqual(self.waves([step("a"), step("b", "a")]), [["a"], ["b"]])

    def test_one_resource_is_one_step_at_a_time(self):
        self.assertEqual(self.waves([step("a", holds="machine:x"), step("b", holds="machine:x")]), [["a"], ["b"]])

    def test_a_board_and_a_build_interleave(self):
        """While one arm is deployed to the board the other arm is still building on the machine."""
        graph = [step("build-a", holds="machine:x"), step("build-b", holds="machine:x"), step("deploy-a", "build-a", "device:d")]
        self.assertEqual(self.waves(graph), [["build-a"], ["build-b", "deploy-a"]])

    def test_a_step_already_done_is_in_no_wave(self):
        self.assertEqual(self.waves([step("a"), step("b", "a")], done={"a"}), [["b"]])


class TestWhatIsWorthRunning(unittest.TestCase):
    """When what a step feeds is already there it is not run again."""

    def test_a_phase_whose_result_is_already_there_is_not_run(self):
        graph = [step("instr"), step("collect", "instr"), step("slot", "collect")]
        self.assertEqual(sched.needed(graph, {"slot"}), set())

    def test_the_phases_of_an_unfinished_one_all_are(self):
        graph = [step("instr"), step("collect", "instr"), step("slot", "collect")]
        self.assertEqual(sched.needed(graph), {"instr", "collect", "slot"})

    def test_a_step_something_unfinished_still_needs_stays(self):
        self.assertEqual(sched.needed([step("slot"), step("deploy", "slot")], {"slot"}), {"deploy"})

    def test_the_scheduler_runs_none_of_a_pruned_branch(self):
        runs = FakeRuns()
        graph = steps(runs, ("instr", "", ""), ("collect", "instr", ""), ("slot", "collect", ""), ("deploy", "slot", ""), done={"slot"})
        s = sched.Scheduler(graph)
        self.assertEqual(s.run_all(), 0)
        self.assertEqual(runs.started, ["deploy"])
        self.assertEqual([x.id for x in s.unneeded], ["instr", "collect"])


class TestTheScheduler(unittest.TestCase):
    """What starts, what waits, what is never run at all, and what a failure or a refusal does to the rest."""

    def run_all(self, graph):
        s = sched.Scheduler(graph)
        return s, s.run_all()

    def in_background(self, s, until):
        thread = threading.Thread(target=s.run_all)
        thread.start()
        for _ in range(200):
            if until():
                break
            threading.Event().wait(0.01)
        return thread

    def test_everything_ready_starts_at_once(self):
        runs = FakeRuns(block=("a", "b"))
        t = self.in_background(sched.Scheduler(steps(runs, ("a", "", ""), ("b", "", ""))), lambda: runs.peak >= 2)
        runs.gate.set()
        t.join(20)
        self.assertEqual(runs.peak, 2)

    def test_a_held_resource_keeps_two_steps_apart(self):
        runs = FakeRuns()
        _, rc = self.run_all(steps(runs, ("a", "", "machine:x"), ("b", "", "machine:x")))
        self.assertEqual((rc, runs.overlapped("a", "b"), sorted(runs.started)), (0, False, ["a", "b"]))

    def test_a_step_that_answers_done_is_never_run(self):
        runs = FakeRuns()
        s, rc = self.run_all(steps(runs, ("a", "", ""), ("b", "a", ""), done={"a"}))
        self.assertEqual((rc, runs.started, [x.id for x in s.already]), (0, ["b"], ["a"]))

    def test_the_predicates_are_asked_all_at_once(self):
        gate, seen, guard = threading.Event(), [0, 0], threading.Lock()

        def done():
            with guard:
                seen[0] += 1
                seen[1] = max(seen)
            gate.wait(10)
            with guard:
                seen[0] -= 1
            return False

        graph = [step(i, done=done) for i in "abc"]
        t = self.in_background(sched.Scheduler(graph), lambda: seen[1] >= 3)
        gate.set()
        t.join(30)
        self.assertEqual(seen[1], 3)

    def test_a_predicate_is_asked_only_of_a_step_that_declares_one(self):
        asked = []
        sched.Scheduler([step("a"), step("b", done=lambda: asked.append("b"))]).run_all()
        self.assertEqual(asked, ["b"])

    def test_a_failure_stops_what_needed_it_and_nothing_else(self):
        runs = FakeRuns(codes={"a": 3})
        s, rc = self.run_all(steps(runs, ("a", "", ""), ("b", "a", ""), ("c", "", "")))
        self.assertEqual((rc, [x.id for x, _ in s.failed], [x.id for x in s.skipped]), (1, ["a"], ["b"]))
        self.assertIn("c", runs.started)

    def test_a_refusal_is_not_now_and_the_step_keeps_its_place(self):
        runs = FakeRuns(codes={"b": [RETRY_EXIT, 0]}, block=("a",))
        s = sched.Scheduler(steps(runs, ("a", "", ""), ("b", "", "")))
        t = self.in_background(s, lambda: runs.started.count("b") == 1)
        runs.gate.set()
        t.join(20)
        self.assertEqual((runs.started.count("b"), sorted(x.id for x in s.ran), s.failed), (2, ["a", "b"], []))

    def test_a_refusal_with_nothing_left_running_ends_the_run(self):
        runs = FakeRuns(codes={"a": RETRY_EXIT})
        s, rc = self.run_all(steps(runs, ("a", "", "")))
        self.assertEqual((rc, [x.id for x in s.left], runs.started), (1, ["a"], ["a"]))

    def test_the_announcements_follow_each_step(self):
        heard = []
        runs = FakeRuns(codes={"a": 1})
        sched.Scheduler(steps(runs, ("a", "", ""), ("b", "a", "")), lambda event, s, rc=0: heard.append((s.id, event))).run_all()
        self.assertEqual(heard, [("a", "start"), ("a", "failed"), ("b", "skipped")])


class TestAWkCommandStep(unittest.TestCase):

    def test_the_command_runs_through_the_machine_appending_to_its_log(self):
        f = Fake("here")
        f.answer(["sh", "-c", sched.LOGGED], rc=3)
        s = sched.wk_step(f, "/t/wk", lambda st: "/logs/" + st.id, "a", "m", (), ("device:b",), None, ["bench", "deploy", "l", "b"])
        self.assertEqual((s.command, s.holds, s.run()), ("wk bench deploy l b", ("device:b",), 3))
        self.assertEqual(f.effects, [("run", ("sh", "-c", sched.LOGGED, "sh", "/logs/a", "/t/wk", "bench", "deploy", "l", "b"))])

    def test_a_target_is_named_in_the_command_and_the_environment(self):
        f = Fake("here")
        f.answer(["sh", "-c", sched.LOGGED])
        s = sched.wk_step(f, "/t/wk", lambda st: "/l", "a", "m", (), (), None, ["x"], "moose")
        s.run()
        self.assertEqual(s.command, "WK_PLACE=moose wk x")
        self.assertEqual(f.effects[0][1][5:8], ("env", "WK_PLACE=moose", "/t/wk"))

    def test_the_verdict_is_the_last_line(self):
        f = Fake("here")
        f.answer(["wk", "yes"], out="the machine is stopped\nyes\n")
        f.answer(["wk", "no"], out="yes\nno\n")
        self.assertEqual((sched.wk_yes(f, ["wk", "yes"])(), sched.wk_yes(f, ["wk", "no"])()), (True, False))


if __name__ == "__main__":
    unittest.main()
