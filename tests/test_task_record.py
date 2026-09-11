"""One record shape for every long-running command (lib/task.sh).

Every command that outlives its terminal writes one record through this
library, and `wk status` renders every kind through one renderer: the steps
still to come are listed and the command that stops the job is named, so a
person watching one has no file to know about and nothing to guess at.

A task is a directory under $WK_STORE/task: plan, step, pid, machine, log,
kill, argv, started, and on end exit and finished. Each field is one file, so
every write is one tmp+rename and a reader never sees half a record. Liveness
is the process table at read time -- a pid that no longer answers with no exit
recorded reads `died` -- and the renderer (lib/status-view.py render_task) puts
the plan under the task as [x] done, [>] running, [ ] pending, with the kill
command and the log beneath it.

Run: python3 -m unittest tests.test_task_record -v
"""
import json
import os
import pathlib
import subprocess
import time
import tempfile
import unittest

from tests.support import REPO, WkTest, bash

STATUS_VIEW = REPO / "lib" / "status-view.py"

PLANS = {
    "build":  ["configure", "compile", "link"],
    "test":   ["jsc/release in ws1"],
    "yocto":  ["layers", "fetch", "image", "toolchain", "webkit", "pgo-mix"],
    "pgo":    ["instrumented build", "collect on rpi5", "measured build"],
    "rc":     ["claude remote-control in ws1"],
    "new":    ["checking", "wipe", "base", "create", "init", "fetch", "register"],
    "agent-forward": ["start forward", "verify"],
}
KILLS = {
    "build": "wk build ws1 --kill",
    "test":  "kill 1234 on tolken, or ^C where it runs",
    "yocto": "wk sysimage build wpe --stage image --stop",
    "pgo":   "kill 1234 on tolken",
    "rc":    "wk ai claude ws1 --rc --stop",
    "new":   "wk new ws1 --kill",
    "agent-forward": "wk push off",
}
HERE = ("test", "pgo", "new", "agent-forward")   # the pid is this machine's


def render(records, mode="text"):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, dir="/tmp") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
        path = fh.name
    try:
        return subprocess.run(["python3", str(STATUS_VIEW), mode, path],
                              capture_output=True, text=True)
    finally:
        os.unlink(path)


def task_rec(kind, state, step, plan=None, **extra):
    rec = {"kind": "task", "machine": "tolken", "task": "%s-ws1-2026" % kind,
           "task_kind": kind, "name": "ws1", "state": state, "step": str(step),
           "since": "2026-09-10T04:00:00Z", "kill": KILLS[kind],
           "log": "/store/ws/ws1/%s.log" % kind,
           "plan": plan if plan is not None else PLANS[kind]}
    rec.update(extra)
    return rec


class TestOneRecordPerKind(WkTest):
    """Every kind of task writes through the one library and renders through
    the one renderer: the assertion is that nothing about the rendering
    depends on which command wrote the record."""

    def _sh(self, body, env=""):
        cp = bash('%s\n. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n%s'
                  % (env, REPO, REPO, body),
                  env={"WK_STORE": str(self.tmp / "store")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def _write(self, kind):
        """One record per kind, written by lib/task.sh itself."""
        plan = " ".join('"%s"' % s for s in PLANS[kind])
        where = "here" if kind in HERE else "target"
        return self._sh(
            'd=$(task_begin %s %s ws1 "%s" /store/ws/ws1/%s.log %s)\n'
            'task_step "$d" 2\n'
            'printf "%%s\\n" "$d"' % (kind, where, KILLS[kind], kind, plan)).strip()

    def test_every_kind_writes_the_same_field_set(self):
        for kind in PLANS:
            with self.subTest(kind=kind):
                d = self._write(kind)
                have = sorted(os.listdir(d))
                for field in ("plan", "step", "kill", "log", "machine",
                              "argv", "started", "kind", "name", "where"):
                    self.assertIn(field, have, kind)
                self.assertNotIn("exit", have, "a running task records no exit")
                self.assertEqual((self.tmp / "store" / "task" / os.path.basename(d)
                                  / "plan").read_text().splitlines(), PLANS[kind])

    def test_the_plan_is_declared_before_step_one(self):
        d = self._sh('task_begin build target ws1 "wk build ws1 --kill" /l a b c').strip()
        self.assertEqual((self.tmp / "store" / "task" / os.path.basename(d) / "step").read_text().strip(), "0")
        self.assertEqual((self.tmp / "store" / "task" / os.path.basename(d) / "plan").read_text().split(), ["a", "b", "c"])

    def test_a_plan_and_a_kill_command_are_both_required(self):
        for body, want in (
            ('task_begin build target ws1 "wk build ws1 --kill" /l', "no plan"),
            ('task_begin build target ws1 "" /l a b', "no kill command"),
            ('task_begin build sideways ws1 "k" /l a b', "here or target"),
            # An unset YOCTO_TASK/PGO_TASK arrives as this, and writing to
            # "/finished" fails under `set -e` with nothing said.
            ('task_end "" 0', "no task record"),
        ):
            cp = bash('. "%s/lib/common.sh"\n. "%s/lib/task.sh"\n%s' % (REPO, REPO, body),
                      env={"WK_STORE": str(self.tmp / "store")})
            self.assertNotEqual(cp.returncode, 0, body)
            self.assertIn(want, cp.stderr, body)

    def test_a_re_run_converges_onto_one_record(self):
        """Crash-only: task_begin after a kill leaves one complete record for
        that kind and name, never an 'already exists'."""
        out = self._sh(
            'task_begin yocto target ws1 "k" /l layers fetch >/dev/null\n'
            'd=$(task_begin yocto target ws1 "k" /l layers fetch)\n'
            'task_end "$d" 0\n'
            'd2=$(task_begin yocto target ws1 "k" /l layers fetch)\n'
            'printf "%s\\n" "$(task_list | wc -l)" "$(task_find yocto ws1)" "$d2"')
        count, found, newest = out.split("\n")[:3]
        self.assertEqual(count.strip(), "1")
        self.assertEqual(found, newest)
        self.assertFalse((self.tmp / "store" / "task" / os.path.basename(newest) / "exit").exists(),
                         "the re-run cleared the ended record's exit")

    def test_a_run_that_is_still_alive_is_not_superseded(self):
        """Superseding a live record would hide the run a guard reads it for:
        image/yocto.sh refuses a second cooker by finding the first's record."""
        out = self._sh(
            'd=$(task_begin yocto here ws1 "k" /l layers fetch)\n'
            'task_pid "$d" $$\n'
            'sleep 1\n'   # the id carries a UTC second, and both would be one record
            'd2=$(task_begin yocto here ws1 "k" /l layers fetch)\n'
            'printf "%s\\n" "$(task_list | wc -l)"\n'
            '[ -d "$d" ] && echo KEPT || echo GONE')
        count, kept = out.split("\n")[:2]
        self.assertEqual(count.strip(), "2")
        self.assertEqual(kept, "KEPT")

    def test_a_wait_ignores_a_record_older_than_the_floor_it_was_given(self):
        """The driver reaches task_begin only after a network fetch, so the
        newest record of that kind and name is a previous run's until then: a
        waiter given the stamp it took before spawning follows the fresh run,
        and one without it ends on the stale verdict."""
        stale = ('d=$(task_begin new here ws1 "k" /nonexistent-log checking create)\n'
                 'mv "$d" "$(dirname "$d")/new-ws1-20200101T000000Z-1"\n'
                 'task_end "$(dirname "$d")/new-ws1-20200101T000000Z-1" refused\n')
        self.assertEqual(
            self._sh(stale + 'printf "%s\\n" "$(task_wait new ws1 /nonexistent-log 0)"').strip(),
            "refused")
        out = self._sh(
            stale +
            'floor=$(task_stamp)\n'
            '( sleep 2\n'
            '  d2=$(task_begin new here ws1 "k" /nonexistent-log checking create)\n'
            '  task_end "$d2" 0 ) &\n'
            'printf "%s\\n" "$(task_wait new ws1 /nonexistent-log 30 "" "$floor")"\n'
            'wait')
        self.assertEqual(out.strip(), "ok")

    def test_a_record_of_a_longer_name_is_not_this_name_s_record(self):
        out = self._sh(
            'd=$(task_begin new here ws1-extra "k" /nonexistent-log checking)\n'
            'task_end "$d" refused\n'
            'printf "[%s]\\n" "$(task_find new ws1)"')
        self.assertEqual(out.strip(), "[]")

    def test_a_live_pid_reads_running_and_a_dead_one_died(self):
        out = self._sh(
            'd=$(task_begin pgo here p/slot "k" /nonexistent-log a b c)\n'
            'printf "%s " "$(task_verdict "$d")"\n'
            'task_pid "$d" 999999999\n'
            'printf "%s " "$(task_verdict "$d")"\n'
            'task_end "$d" 3\n'
            'printf "%s\\n" "$(task_verdict "$d")"')
        self.assertEqual(out.split(), ["running", "died", "failed"])

    def test_the_first_verdict_stands_and_a_re_run_still_clears_it(self):
        """`wk build --kill` writes cancelled and the driver it stopped then
        reaches its own end with the failure the kill caused: the record has
        one author of its end, and it is the one that got there first."""
        out = self._sh(
            'd=$(task_begin build target ws1 "k" /nonexistent-log a b)\n'
            'task_end "$d" cancelled\n'
            'task_end "$d" 1\n'
            'printf "%s " "$(task_verdict "$d")"\n'
            'd2=$(task_begin build target ws1 "k" /nonexistent-log a b)\n'
            'printf "%s\\n" "$(task_verdict "$d2")"')
        self.assertEqual(out.split(), ["cancelled", "starting"])

    def test_a_task_with_no_pid_yet_is_starting_not_died(self):
        out = self._sh('d=$(task_begin rc target ws1 "k" /l one)\n'
                       'printf "%s\\n" "$(task_verdict "$d")"')
        self.assertEqual(out.strip(), "starting")

    def test_silence_past_wk_stall_seconds_is_silent_not_died(self):
        """A job under a watchdog records the deadline it is watched against
        (`abort_after`), and silence is measured against that. A session or a
        tunnel declares none and produces no output, so its quiet log is not a
        verdict about it -- it reads `running`, with the log's age reported
        beside it."""
        log = self.tmp / "t.log"
        log.write_text("x\n")
        old = time.time() - 4000
        os.utime(log, (old, old))
        out = self._sh(
            'd=$(WK_ABORT_SECONDS=1800 task_begin build here ws1 "k" "%s" a b)\n'
            'printf "%%s " "$(WK_STALL_SECONDS=9000 task_verdict "$d")"\n'
            'printf "%%s " "$(task_verdict "$d")"\n'
            'd=$(task_begin rc here ws2 "k" "%s" session)\n'
            'printf "%%s\\n" "$(task_verdict "$d")"' % (log, log))
        self.assertEqual(out.split(), ["running", "silent", "running"])


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
        page = pathlib.Path(cp.stdout.strip()).read_text()
        self.assertIn("[&gt;]", page)
        self.assertIn("wk sysimage build wpe --stage image --stop", page)


if __name__ == "__main__":
    unittest.main()
