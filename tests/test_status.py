"""`wk status`: the walk (lib/wk/status.py) and the renderer (lib/wk/statusview.py), driven in process over fakes."""
import contextlib
import io
import json
import os
import sys
import unittest
from unittest import mock

from tests.fakes import FakeDriver, FakeRegistry
from tests.support import REPO, WkTest, clean_env, load_cmd, run

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet, status, statusview  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.record import Records  # noqa: E402


def render(records):
    buf = io.StringIO()
    statusview.render_text_stream(iter(records), buf, False)
    return buf.getvalue()


def machine_rec(name, **extra):
    return dict({"kind": "machine", "name": name}, **extra)


def ws_rec(machine, name, state="running", ws="present", **extra):
    return dict({"kind": "workspace", "machine": machine, "method": "container", "name": name, "state": state, "ws": ws}, **extra)


class TestBump(unittest.TestCase):
    def test_the_worst_wins_and_garbage_reads_4(self):
        w = 0
        for c in ("2", 1, "0"):
            w = status.bump(w, c)
        self.assertEqual(w, 2)
        for c in ("", "abc", "7", "-1", "255"):
            self.assertEqual(status.bump(0, c), 4, c)


class TestWait(unittest.TestCase):
    """`--wait` polls while the answer is busy (2), and another answer ends it only once the next poll repeats it;
    `--timeout` is elapsed time, and ends the wait unanswered."""

    def wait(self, answers, timeout):
        clock, said, it = FakeClock(), [], iter(answers)

        def poll():
            clock.t += 0.5   # a walk costs time of its own
            return next(it)
        return status.wait(poll, timeout, clock, "ws", said.append, said.append), said, clock

    def test_only_a_repeated_answer_other_than_busy_ends_it(self):
        for answers, want in (([0], 0), ([4], 4), ([2, 2, 0, 0], 0), ([2, 4, 2, 0, 0], 0), ([2, 4, 4], 4)):
            rc, said, clock = self.wait(answers, 0)
            self.assertEqual(rc, want, answers)
            self.assertEqual(clock.slept, [status.WAIT_SECONDS] * (len(answers) - 1))

    def test_the_timeout_is_elapsed_time_and_says_the_work_goes_on(self):
        rc, said, clock = self.wait([2] * 100, 10)
        self.assertEqual(rc, 2)
        self.assertLess(clock.now() - FakeClock().now(), 20)
        self.assertIn("still busy after 10s", said[-1])

    def test_the_exit_is_the_waits_verdict_not_the_walk_after_it(self):
        cmd = load_cmd("status")

        class FlakyWalk:
            worst = 4

            def __init__(self, *a, **kw):
                pass

            def worst_only(self):
                return 4

            def records(self, markers=True):
                return iter(())
        with mock.patch.object(cmd, "Walk", FlakyWalk), mock.patch.object(cmd, "wait", return_value=0), \
                mock.patch.dict(os.environ, {"WK_NAME": "ws"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cmd.main(["--wait", "--records"]))
            self.assertEqual(4, cmd.main(["--records"]))


class TaskTest(WkTest):
    def setUp(self):
        super().setUp()
        self.store, self.answers, self.clock = str(self.tmp / "store"), {}, FakeClock()

    def begin(self, where, name, *plan, kind="build", abort=None, log="/nolog"):
        env = {"WK_STORE": self.store, **({"WK_ABORT_SECONDS": str(abort)} if abort else {})}
        return Records(self.store, clock=self.clock, env=env).begin(kind, where, name, "wk %s %s --kill" % (kind, name), log, list(plan))

    def reported(self, only=None):
        return status.task_records(Records(self.store, clock=self.clock, ask_place=lambda n, pid, cap: self.answers.get(n),
                                           env={"WK_STORE": self.store}), only, self.clock)


class TestTasks(TaskTest):
    def test_a_name_reports_its_tasks_and_one_that_ended_as_asked_is_not_reported(self):
        self.begin("here", "ws1", "compile")
        self.begin("here", "ws2", "jsc", kind="test")
        self.assertEqual(sorted(r["name"] for r in self.reported()[0]), ["ws1", "ws2"])
        self.assertEqual([r["name"] for r in self.reported("ws1")[0]], ["ws1"])
        for word, reported in (("0", False), ("cancelled", False), ("refused", False), ("3", True), ("stalled", True)):
            self.begin("here", "ws3", "compile").end(word)
            self.assertEqual(bool(self.reported("ws3")[0]), reported, word)

    def test_a_pid_in_a_workspace_is_asked_of_it_and_reported_on_its_machine(self):
        self.begin("place", "ws4", "compile").pid(4242, "farbox")
        for answer, state, code in ((True, "running", 2), (False, "died", 4), (None, "unanswered", 4)):
            self.answers["ws4"] = answer
            (rec,), worst = self.reported("ws4")
            self.assertEqual((rec["state"], worst, rec["machine"]), (state, code, "farbox"))

    def test_the_record_carries_its_subject_plan_steps_holds_and_kill(self):
        t = self.begin("here", "ws5", "layers", "fetch", "image", kind="yocto")
        t.step(2)
        t.set("subject", "slot base")
        t.set("holds", "ws:ws5\nbuild:here")
        (rec,), _ = self.reported("ws5")
        self.assertEqual((rec["task_kind"], rec["subject"], rec["kill"], rec["holds"]), ("yocto", "slot base", "wk yocto ws5 --kill", "ws:ws5 build:here"))
        self.assertEqual((rec["plan"], rec["steps"], rec["state"]), (["layers", "fetch", "image"], ["done", "running", "pending"], "running"))

    def test_each_verdict_is_its_exit_code(self):
        log = self.tmp / "build.log"
        for end, age, state, code in ((None, 0, "running", 2), (None, 600, "silent", 2), (None, 4000, "silent", 4), (1, 0, "failed", 1),
                                      ("stalled", 0, "stalled", 3), ("oom", 0, "oom", 3), ("gave-up", 0, "gave-up", 1)):
            with self.subTest(end=end, age=age):
                log.write_text("[9/4200] cc\n")
                os.utime(log, (self.clock.now() - age, self.clock.now() - age))
                t = self.begin("here", "w", "compile", abort=1800, log=str(log))
                if end is not None:
                    t.end(end)
                (rec,), worst = self.reported("w")
                self.assertEqual((rec["state"], worst), (state, code))
        self.begin("here", "gone", "compile").pid(4194304)
        self.assertEqual(self.reported("gone")[1], 4)


class TestTheWalk(unittest.TestCase):
    """A place whose own wk answers is asked for its records; one that cannot be asked is a line saying why."""

    def walk(self, *drivers, name=None):
        by_name = {d.name: d for d in drivers}
        reg = FakeRegistry({}, Fake(), lambda n, env: by_name[n], names=list(by_name), ws_place=lambda ws: drivers[0].name)
        w = status.Walk(REPO, name=name, env={"WK_ROW_LABEL": "here"}, reg=reg)
        return [r for r in w.records(markers=False) if r["kind"] != "exit"], w.worst

    def test_an_answering_place_is_asked_for_its_records_under_its_label(self):
        far = [machine_rec("box"), ws_rec("box", "ws", state="absent", ws="absent")]
        box = FakeDriver("box", rc=2, out="".join(json.dumps(r) + "\n" for r in far + [{"kind": "plan", "jobs": []}]))
        box.host = "box.example"
        recs, worst = self.walk(box, name="ws")
        self.assertEqual([r["kind"] for r in recs], ["machine", "machine", "workspace"])
        self.assertEqual(worst, 2)
        args, env, _ = box.asked[0]
        self.assertEqual((args, env["WK_ROW_LABEL"], env["WK_NO_DELEGATE"]), (("status", "--records", "ws"), "box", "1"))

    def test_a_place_that_cannot_be_asked_is_a_line_and_unreachable_is_4(self):
        for side, worst in (("unreachable", 4), ("stopped", 0)):
            d = FakeDriver("container", kind="container", side=side, why="timed out")
            recs, w = self.walk(d)
            self.assertEqual(([r["text"] for r in recs if r["kind"] == "raw"], w),
                             (["container: " + fleet.reason(d, side, "timed out")], worst))


class TestRenderer(unittest.TestCase):
    def test_a_partial_row_a_raw_line_and_an_empty_machine(self):
        out = render([machine_rec("m"), ws_rec("m", "bare"), machine_rec("box"), {"kind": "raw", "machine": "box", "text": "box: unreachable"},
                      machine_rec("empty")])
        self.assertRegex(out, r"(?m)^\s+bare\s+running\s+-\s+\?\s+clean")
        self.assertIn("box: unreachable", out)
        self.assertEqual(out.count("(no workspaces on it)"), 1)

    def test_a_task_is_its_state_steps_and_lines(self):
        t = {"kind": "task", "machine": "m", "task_kind": "build", "name": "ws", "state": "running", "since": "now",
             "plan": ["sync", "compile"], "steps": ["done", "running"], "holds": "ws:ws", "kill": "wk build ws --kill", "log": "/l"}
        out = render([machine_rec("m"), t])
        for want in ("[x] sync", "[>] compile", "holds: ws:ws", "kill: wk build ws --kill", "log:  /l"):
            self.assertIn(want, out)

    def test_json_is_the_merged_document_and_two_views_that_disagree_are_one_row_and_4(self):
        recs = [machine_rec("box"), ws_rec("box", "ws"), ws_rec("box", "ws", state="absent", ws="absent"), {"kind": "exit", "code": 2}]
        doc = statusview.merge(recs)
        self.assertEqual((doc["exit"], len(doc["machines"][0]["methods"][0]["workspaces"])), (4, 1))
        self.assertIn("disagree (running vs absent)", render(recs))
        self.assertEqual(statusview.merge(recs[:2] + recs[3:])["exit"], 2)

    def test_a_machine_is_drawn_when_its_last_job_flushes_and_an_unreadable_line_is_skipped(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            recs = list(statusview.records_from_lines(['{"kind":"machine","name":"m"}', "{not json"]))
        self.assertIn("unreadable record", err.getvalue())
        out = io.StringIO()
        stream = [{"kind": "plan", "jobs": [{"job": "a", "machine": "m"}, {"job": "b", "machine": "m"}]}] + recs + [{"kind": "flush", "job": "a"}]
        statusview.render_text_stream(iter(stream), out, False)
        self.assertEqual(out.getvalue().count("\nm\n"), 1)


class TestHelpLeadsWithTheSelfLine(unittest.TestCase):
    def test_help_leads_with_role_and_mode(self):
        self.assertEqual(run("help").stdout.splitlines()[0], fleet.self_line(str(REPO), clean_env(wk_root=False)))


if __name__ == "__main__":
    unittest.main()
