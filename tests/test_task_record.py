"""One renderer for every long-running command's task record (wk.statusview): the plan under the task as [x] done,
[>] running, [-] skipped, [!] stopped there and [ ] pending, with the kill command and the log beneath it."""
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import statusview  # noqa: E402

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
    "agent-forward": "wk key push off",
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


class TestTheRendererSaysWhatIsLeftAndWhatStopsIt(unittest.TestCase):

    def _text(self, rec):
        cp = render([{"kind": "machine", "name": "tolken", "self": True}, rec])
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_exactly_one_step_is_marked_running(self):
        out = self._text(task_rec("yocto", "running", 3))
        self.assertEqual((out.count("[>]"), out.count("[x]"), out.count("[ ]")), (1, 2, 3), out)
        for line in ("[>] image", "[x] layers", "[x] fetch", "[ ] toolchain", "[ ] pgo-mix"):
            self.assertIn(line, out)

    def test_two_steps_running_at_once_read_as_two(self):
        plan = ["build base", "build pr", "bench rpi4", "bench rpi5", "report"]
        out = self._text(task_rec("pgo", "running", 0, plan=plan,
                                  steps=["done", "running", "running", "pending", "pending"]))
        self.assertEqual(out.count("[>]"), 2, out)
        self.assertIn("[>] build pr", out)
        self.assertIn("[>] bench rpi4", out)
        self.assertIn("[x] build base", out)
        self.assertEqual(out.count("[ ]"), 2, out)

    def test_a_step_that_failed_and_one_never_reached_are_told_apart(self):
        plan = ["build base", "build pr", "bench rpi4", "report"]
        out = self._text(task_rec("pgo", "died", 0, plan=plan,
                                  steps=["done", "failed", "skipped", "skipped"]))
        self.assertIn("[x] build base", out)
        self.assertIn("[!] build pr", out)
        self.assertEqual(out.count("[-]"), 2, out)
        self.assertNotIn("[>]", out)

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
        self.assertNotIn("[>]", out)
        out = self._text(task_rec("build", "died", 2, exit="137"))
        self.assertIn("exit 137", out)

    def test_a_finished_task_marks_every_step_done(self):
        out = self._text(task_rec("build", "ok", 3, exit="0"))
        self.assertNotIn("[>]", out)
        self.assertNotIn("[!]", out)
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
