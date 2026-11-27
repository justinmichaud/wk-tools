import json
import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.pueue import world
from wk.tasks import status
from wk.core.seams import Task

PUEUE = "/home/u/wk/cache/pueue-4.0.5/pueue --config /home/u/wk/pueue/pueue.yml"


def row(state, **more):
    return dict({"id": 0, "label": "x:build:jsc-release", "group": "build", "state": state, "rc": None, "age": 65, "silent": None}, **more)


class TestRows(unittest.TestCase):
    def test_a_live_task_and_the_last_under_each_label_are_rows_and_older_endings_are_not(self):
        tasks = [Task(0, "x:build:a", "build", "failed", "", 2, 100.0), Task(1, "x:build:a", "build", "done", "", 0, 400.0),
                 Task(2, "x:test:a", "default", "running", "", None, 940.0), Task(3, "y:build:a", "build", "queued", "", None, 990.0)]
        self.assertEqual(status.rows(tasks, lambda: {2: 12.9}, 1000.0),
                         [{"id": 1, "label": "x:build:a", "group": "build", "state": "done", "rc": 0, "age": 600, "silent": None},
                          {"id": 2, "label": "x:test:a", "group": "default", "state": "running", "rc": None, "age": 60, "silent": 12},
                          {"id": 3, "label": "y:build:a", "group": "build", "state": "queued", "rc": None, "age": 10, "silent": None}])

    def test_log_ages_are_not_asked_for_when_nothing_runs(self):
        self.assertEqual(len(status.rows([Task(0, "x", "build", "done", "", 0, 1.0)], None, 2.0)), 1)

    def test_silence_is_running_until_1800_s_then_stalled(self):
        run = [Task(0, "x", "build", "running", "", None, 0.0)]
        self.assertEqual([status.rows(run, lambda: {0: s}, 5000.0)[0]["state"] for s in (0, 1799.9, 1800, 9000)], ["running", "running", "stalled", "stalled"])

    def test_a_workspaces_tasks_are_those_whose_labels_first_field_is_its_name(self):
        tasks = [Task(i, label, "default", "done", "") for i, label in enumerate(("x:build:a", "xy:build:a", "x", "y:test:x"))]
        self.assertEqual([t.id for t in tasks if t.ws == "x"], [0, 2])


class TestCode(unittest.TestCase):
    def test_the_exit_code_of_each_state(self):
        self.assertEqual([status.code([row(s)]) for s in ("queued", "running", "stalled", "failed", "killed", "done")], [2, 2, 3, 1, 1, 0])

    def test_idle_is_0(self):
        self.assertEqual(status.code([]), 0)

    def test_what_is_live_outranks_how_older_runs_ended(self):
        self.assertEqual((status.code([row("failed"), row("running")]), status.code([row("failed"), row("stalled"), row("running")]),
                          status.code([row("failed"), row("done")])), (2, 3, 1))


class TestLine(unittest.TestCase):
    def test_a_line_is_the_state_the_label_and_the_age(self):
        self.assertEqual(status.line(row("running")), "running  x:build:jsc-release            1m05s")
        self.assertEqual(status.line(row("queued", age=7300)), "queued   x:build:jsc-release            2h01m")

    def test_an_ended_task_says_how_long_ago_and_a_failed_one_its_exit_code(self):
        self.assertEqual(status.line(row("done", rc=0)), "done     x:build:jsc-release            1m05s ago")
        self.assertEqual(status.line(row("failed", rc=2)), "failed   x:build:jsc-release            1m05s ago, exit 2")
        self.assertEqual(status.line(row("killed")), "killed   x:build:jsc-release            1m05s ago")

    def test_silence_is_said_from_300_s(self):
        self.assertEqual([status.line(row("running", silent=s))[40:] for s in (299, 300)], ["1m05s", "1m05s, silent for 300s"])
        self.assertEqual(status.line(row("stalled", silent=1900))[40:], "1m05s, silent for 1900s")

    def test_a_log_is_normalised_to_what_a_terminal_would_show(self):
        self.assertEqual(status.normalise("cmake\r\n[1/3] a\r[2/3] b\r[3/3] c\nFAILED\n"), "cmake\n[3/3] c\nFAILED\n")


def busy():
    w = world()
    w.workspace("x")
    w.workspace("y")
    w.wk("build", "x", "jsc-release", "--detach")
    w.wk("build", "y", "gtk-debug", "--detach")
    return w


class TestStatus(unittest.TestCase):
    def test_on_a_server_it_lists_that_servers_tasks_and_exits_2_while_one_is_busy(self):
        w = busy()
        w.clock.sleep(125)
        r = w.wk("status")
        self.assertEqual((r.code, r.out, r.err, r.effects), (2, "running  x:build:jsc-release            2m05s\n"
                                                                "queued   y:build:gtk-debug              2m05s\n", "", []))

    def test_a_workspace_names_its_own_tasks_only(self):
        r = busy().wk("status", "y")
        self.assertEqual((r.code, r.out), (2, "queued   y:build:gtk-debug              0m00s\n"))

    def test_idle_is_0_and_says_so(self):
        r = world().wk("status")
        self.assertEqual((r.code, r.out, r.err), (0, "", "==> no tasks on lin\n"))

    def test_a_failed_build_is_1_until_the_next_run_under_its_label_ends_well(self):
        w = busy()
        w.pueue["lin"].finish(0, rc=1)
        w.wk("build", "y", "--kill")
        r = w.wk("status")
        self.assertEqual((r.code, r.out), (1, "failed   x:build:jsc-release            0m00s ago, exit 1\n"
                                              "killed   y:build:gtk-debug              0m00s ago\n"))
        w.wk("build", "x", "--detach")
        w.pueue["lin"].finish(2)
        self.assertEqual((w.wk("status", "x").code, w.wk("status", "x").out), (0, "done     x:build:jsc-release            0m00s ago\n"))

    def test_a_silent_build_is_warned_about_from_300_s_and_stalled_and_3_from_1800_s(self):
        w = busy()
        w.clock.sleep(400)
        r = w.wk("status", "x")
        self.assertEqual((r.code, r.out, r.err), (2, "running  x:build:jsc-release            6m40s, silent for 400s\n",
                                                  "warning: x:build:jsc-release has printed nothing for 400s: after 1800s it is called stalled\n"))
        w.clock.sleep(1500)
        w.pueue["lin"].grew[0] = w.clock.now() - 20
        self.assertEqual(w.wk("status", "x").out, "running  x:build:jsc-release            31m40s\n")
        w.clock.sleep(1780)
        r = w.wk("status", "x")
        self.assertEqual((r.code, r.out, r.err), (3, "stalled  x:build:jsc-release            1h01m, silent for 1800s\n", ""))

    def test_json_is_one_object_of_rows_with_their_server(self):
        r = busy().wk("status", "x", "--json")
        self.assertEqual((r.code, json.loads(r.out)), (2, {"tasks": [
            {"server": "lin", "id": 0, "label": "x:build:jsc-release", "group": "build", "state": "running", "rc": None, "age": 0, "silent": 0}]}))

    def test_wait_blocks_on_the_clock_while_busy_and_reports_what_it_became(self):
        w = busy()
        w.clock.after(95, lambda: w.pueue["lin"].finish(0, rc=2))
        r = w.wk("status", "x", "--wait")
        self.assertEqual((r.code, r.out, w.clock.now()), (1, "failed   x:build:jsc-release            0m00s ago, exit 2\n", 1100.0))

    def test_wait_stops_waiting_for_a_task_that_stalls(self):
        w = busy()
        r = w.wk("status", "x", "--wait")
        self.assertEqual((r.code, w.clock.now()), (3, 2800.0))

    def test_log_prints_the_end_of_the_latest_log_normalised_then_the_rows(self):
        w = busy()
        w.pueue["lin"].finish(0, rc=1, log="".join("line %d\n" % n for n in range(150)) + "[1/9] a\r[2/9] b\nFAILED: b.o\n")
        r = w.wk("status", "x", "--log")
        lines = r.out.splitlines()
        self.assertEqual((r.code, len(lines), lines[0], lines[-3:]),
                         (1, 101, "line 52", ["[2/9] b", "FAILED: b.o", "failed   x:build:jsc-release            0m00s ago, exit 1"]))

    def test_follow_streams_the_latest_task_until_it_ends_then_reports(self):
        w = busy()
        w.pueue["lin"].ending(0, "[9/9] jsc\n")
        r = w.wk("status", "x", "--follow")
        self.assertEqual((r.code, r.out), (0, "[9/9] jsc\ndone     x:build:jsc-release            0m00s ago\n"))
        self.assertIn("[lin] %s follow 0" % PUEUE, r.calls)

    def test_log_and_follow_need_a_workspace_and_one_that_has_run_something(self):
        w = busy()
        w.workspace("z")
        for flag in ("--log", "--follow"):
            r = w.wk("status", flag)
            self.assertEqual((r.code, r.err.splitlines()[0]), (1, "error: --log and --follow read one workspace's task: name the workspace"))
            r = w.wk("status", "z", flag)
            self.assertEqual((r.code, r.err), (1, "error: z has run no task: `wk build z <preset>` starts one\n"))

    def test_it_is_refused_on_a_workstation_naming_the_servers(self):
        r = world(here="desk").wk("status")
        self.assertEqual((r.code, r.calls), (1, []))
        self.assertIn("error: wk status runs on a Linux server or a Mac server (box, lin, mac), and this is desk, a workstation", r.err)


class TestStop(unittest.TestCase):
    def test_it_asks_once_naming_the_tasks_then_removes_the_queued_and_kills_the_running(self):
        w = busy()
        r = w.wk("stop", "--tasks", stdin="y\n", tty=True)
        self.assertEqual((r.code, r.err), (0, "kill 2 task(s) on lin (x:build:jsc-release, y:build:gtk-debug)? [y/N] "))
        self.assertEqual([e.replace(PUEUE, "pueue") for e in r.effects], ["[lin] pueue remove 1", "[lin] pueue kill 0"])
        self.assertEqual([(t["label"], t["status"]["Done"]["result"]) for t in w.pueue["lin"].tasks.values()], [("x:build:jsc-release", "Killed")])

    def test_declined_or_without_a_terminal_nothing_is_killed(self):
        w = busy()
        self.assertEqual([(r.code, r.effects) for r in (w.wk("stop", "--tasks", stdin="n\n", tty=True), w.wk("stop", "--tasks"))], [(1, []), (4, [])])

    def test_with_nothing_live_it_asks_nothing(self):
        r = world().wk("stop", "--tasks")
        self.assertEqual((r.code, r.err), (0, "==> no task is queued or running on lin\n"))

    def test_bare_it_is_refused_with_the_usage(self):
        r = world().wk("stop")
        self.assertEqual((r.code, r.err), (1, "error: say what to stop: --tasks\nusage: wk stop [--tasks]\n"))

    def test_it_converges_killed_at_any_point_and_its_dry_run_is_its_plan(self):
        converges(self, busy, "stop", "--tasks", "--yes")
        dry_run_is_the_plan(self, busy, "stop", "--tasks", "--yes")
