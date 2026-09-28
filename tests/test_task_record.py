"""One record shape for every long-running command (lib/wk/record.py).

Every command that outlives its terminal writes one record through that
module, and `wk status` renders every kind through one renderer: the steps
still to come are listed and the command that stops the job is named, so a
person watching one has no file to know about and nothing to guess at.

A task is a directory under $WK_STORE/task: plan, steps/<n>, pid, machine, log,
kill, argv, started, and on end exit and finished. Each field is one file, so
every write is one tmp+rename and a reader never sees half a record. Liveness
is the process table at read time -- a pid that no longer answers with no exit
recorded reads `died` -- and the renderer (wk.statusview render_task) puts
the plan under the task as [x] done, [>] running, [-] skipped, [!] stopped
there and [ ] pending, with the kill command and the log beneath it. A plan is
a graph, so each step carries its own state and any number read as running.

Run: python3 -m unittest tests.test_task_record -v
"""
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import record, statusview  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402

PLANS = {
    "build":  ["configure", "compile", "link"],
    "test":   ["jsc/release in ws1"],
    "yocto":  ["layers", "fetch", "image", "toolchain", "webkit", "pgo-mix"],
    "pgo":    ["instrumented build", "collect on rpi5", "measured build"],
    "new":    ["checking", "wipe", "base", "create", "init", "fetch", "register"],
    "agent-forward": ["start forward", "verify"],
}
KILLS = {
    "build": "wk build ws1 --kill",
    "test":  "kill 1234 on tolken, or ^C where it runs",
    "yocto": "wk sysimage build wpe --stage image --stop",
    "pgo":   "kill 1234 on tolken",
    "new":   "wk new ws1 --kill",
    "agent-forward": "wk push off",
}


def render(records, mode="text"):
    """The renderer on synthetic records, in process."""
    records = list(records)
    if mode == "html":
        out = statusview.write_page(statusview.merge(records),
                                    os.path.join(tempfile.mkdtemp(prefix="wk-status-page-"), "status.html")) + "\n"
    else:
        buf = io.StringIO()
        statusview.render_text_stream(iter(records), buf, False)
        out = buf.getvalue()
    return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


def in_order(plan, step):
    """A plan whose steps run in order, stopped at `step`: the ones before it
    have ended, it is running, the rest have not started."""
    return ["done" if i < step else "running" if i == step else "pending"
            for i in range(1, len(plan) + 1)]


def task_rec(kind, state, step, plan=None, steps=None, **extra):
    plan = plan if plan is not None else PLANS[kind]
    rec = {"kind": "task", "machine": "tolken", "task": "%s-ws1-2026" % kind,
           "task_kind": kind, "name": "ws1", "state": state,
           "steps": steps if steps is not None else in_order(plan, step),
           "since": "2026-09-10T04:00:00Z", "kill": KILLS[kind],
           "log": "/store/ws/ws1/%s.log" % kind, "plan": plan}
    rec.update(extra)
    return rec


class TestWhatTheRecordHolds(unittest.TestCase):
    """The rules of lib/wk/record.py that its own tests (tests/test_wk_record.py) leave to this module."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-task-record-"))
        self.addCleanup(record._rmtree, self.tmp)
        self.clock = FakeClock()

    def records(self):
        return record.Records(self.tmp / "store", clock=self.clock, env={})

    def test_the_plan_is_declared_before_any_step_has_a_state(self):
        t = self.records().begin("build", "target", "ws1", "wk build ws1 --kill", "/l", ["a", "b", "c"])
        self.assertEqual(t.plan(), ["a", "b", "c"])
        self.assertEqual(os.listdir(t.path / "steps"), [], "a step had a state before it ran")

    def test_a_run_that_is_still_alive_is_not_superseded(self):
        """Superseding a live record would hide the run a guard reads it for:
        the yocto builder refuses a second cooker by finding the first's record."""
        recs = self.records()
        first = recs.begin("yocto", "here", "ws1", "k", "/l", ["layers", "fetch"], pid=os.getpid())
        self.clock.sleep(1)
        second = recs.begin("yocto", "here", "ws1", "k", "/l", ["layers", "fetch"], pid=os.getpid())
        self.assertTrue(first.path.is_dir())
        self.assertEqual([t.id for t in recs.list()], [first.id, second.id])

    def test_a_row_label_names_the_machine_only_inside_the_vm(self):
        """A walk labels rows with a target name, and only the VM is another machine's."""
        m = Fake()
        m.answer(["hostname", "-s"], out="Tolken\n")
        self.assertEqual(record.machine_name({"WK_ROW_LABEL": "container"}, m), "tolken")
        self.assertEqual(record.machine_name({"WK_IN_VM": "1"}, m), "tolken")


class TestTheRendererSaysWhatIsLeftAndWhatStopsIt(unittest.TestCase):
    """One renderer for every kind: the step now running, the steps done, the
    steps to come, the command a person types to stop it, and the log."""

    def _text(self, rec):
        cp = render([{"kind": "machine", "name": "tolken", "self": True}, rec])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_exactly_one_step_is_marked_running(self):
        out = self._text(task_rec("yocto", "running", 3))
        self.assertEqual(out.count("[>]"), 1, out)
        self.assertIn("[>] image", out)
        self.assertIn("[x] layers", out)
        self.assertIn("[x] fetch", out)
        self.assertIn("[ ] toolchain", out)
        self.assertIn("[ ] pgo-mix", out)
        self.assertEqual(out.count("[x]"), 2, out)
        self.assertEqual(out.count("[ ]"), 3, out)

    def test_two_steps_running_at_once_read_as_two(self):
        """A graph runs what is ready, so a plan is not a line number: with two
        arms in flight the record says two, not one further on than the other."""
        plan = ["build base", "build pr", "bench rpi4", "bench rpi5", "report"]
        out = self._text(task_rec("pgo", "running", 0, plan=plan,
                                  steps=["done", "running", "running", "pending", "pending"]))
        self.assertEqual(out.count("[>]"), 2, out)
        self.assertIn("[>] build pr", out)
        self.assertIn("[>] bench rpi4", out)
        self.assertIn("[x] build base", out)
        self.assertEqual(out.count("[ ]"), 2, out)

    def test_a_step_that_failed_and_one_never_reached_are_told_apart(self):
        """What the scheduler knows reaches the reader: the step that failed,
        and the steps it fed that were never run for it."""
        plan = ["build base", "build pr", "bench rpi4", "report"]
        out = self._text(task_rec("pgo", "died", 0, plan=plan,
                                  steps=["done", "failed", "skipped", "skipped"]))
        self.assertIn("[x] build base", out)
        self.assertIn("[!] build pr", out)
        self.assertEqual(out.count("[-]"), 2, out)
        self.assertNotIn("[>]", out, "nothing is running in a task that died")

    def test_it_names_the_kill_command_and_the_log_for_every_kind(self):
        for kind in PLANS:
            with self.subTest(kind=kind):
                out = self._text(task_rec(kind, "running", 1))
                self.assertIn("kill: " + KILLS[kind], out)
                self.assertIn("log:  /store/ws/ws1/%s.log" % kind, out)
                self.assertIn(kind, out)

    def test_a_dead_task_says_died_and_whether_it_recorded_an_exit(self):
        out = self._text(task_rec("build", "died", 2))
        self.assertIn("died", out)
        self.assertIn("no exit recorded", out)
        self.assertNotIn("[>]", out, "nothing is running in a dead task")
        out = self._text(task_rec("build", "died", 2, exit="137"))
        self.assertIn("exit 137", out)

    def test_a_finished_task_marks_every_step_done(self):
        out = self._text(task_rec("build", "ok", 3, exit="0"))
        self.assertNotIn("[>]", out)
        self.assertNotIn("[!]", out, "a task that ran to the end stopped at no step")
        self.assertEqual(out.count("[x]"), 3, out)

    def test_the_page_renders_the_same_plan(self):
        cp = render([{"kind": "machine", "name": "tolken", "self": True},
                     task_rec("yocto", "running", 3)], "html")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        page = Path(cp.stdout.strip()).read_text()
        self.assertIn("[&gt;]", page)
        self.assertIn("wk sysimage build wpe --stage image --stop", page)


if __name__ == "__main__":
    unittest.main()
