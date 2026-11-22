import shlex
import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.pueue import added, world
from wk import webkit
from wk.tasks import guard, sizing

PUEUE = "/home/u/wk/cache/pueue-4.0.5/pueue --config /home/u/wk/pueue/pueue.yml"
SRC, OUT = "/home/u/wk/ws/x/src", "/home/u/wk/ws/x/build"


def held(on="lin", **how):
    w = world(**how)
    w.workspace("x", on=on)
    return w


def effects(run):
    return [e.replace(PUEUE, "pueue").split(" -- ")[0] for e in run.effects]


class TestBuild(unittest.TestCase):
    def test_a_build_is_sized_queued_under_the_guard_in_the_workspace_followed_and_its_success_returned(self):
        w = held()
        w.pueue["lin"].ending(0, "[1/2] a.o\n[2/2] jsc\n")
        r = w.wk("build", "x", "jsc-release")
        self.assertEqual((r.code, r.out), (0, "[1/2] a.o\n[2/2] jsc\n"))
        self.assertEqual(r.err, "==> x:build:jsc-release: 16 jobs at 1536 MB each, a 24576 MB budget\n==> x:build:jsc-release finished\n")
        self.assertEqual(effects(r), ["[lin] write %s/guard.py" % OUT, "[lin] pueue group add build --parallel 1",
                                      "[lin] pueue add --print-task-id --group build --label x:build:jsc-release --working-directory /home/u"])
        with open(guard.__file__) as f:
            self.assertEqual(w.machines["lin"].files[OUT + "/guard.py"], f.read())
        line = webkit.build_argv(webkit.resolve("jsc-release", "linux"), SRC, OUT, 16)
        self.assertEqual(added(w, "lin"), [["fake-exec", "x", SRC, "python3", "-I", OUT + "/guard.py", "24576", "1", "env", "WK_BUILD=1"] + line[1:]])
        self.assertEqual([c for c in r.calls if c.startswith("[lin] fake-exec ")], ["[lin] fake-exec x %s %s" % (SRC, shlex.join(sizing.PROBE))])

    def test_the_budget_is_never_under_8192_mb(self):
        w = held()
        w.probe["lin"] = "cores 2\navailable_kb 8388608\nload 0 0 0\n"
        r = w.wk("build", "x", "jsc-debug", "--detach")
        self.assertEqual((r.code, r.err.splitlines()[0], added(w, "lin")[0][6]), (0, "==> x:build:jsc-debug: 2 jobs at 1536 MB each, a 8192 MB budget", "8192"))

    def test_detach_returns_once_queued_and_says_how_to_learn_the_outcome(self):
        w = held()
        r = w.wk("build", "x", "gtk-release", "--detach")
        self.assertEqual((r.code, r.err.splitlines()[-1]),
                         (0, "==> task 0 is queued: `wk status x --wait` is its outcome, `wk status x --log` its output"))
        self.assertEqual([(t["label"], list(t["status"])) for t in w.pueue["lin"].tasks.values()], [("x:build:gtk-release", ["Running"])])

    def test_on_a_borrowed_box_it_is_niced_idle_takes_half_the_cores_the_machines_cmake_flags_and_no_cgroup_reading(self):
        w = held("box", confs={"desk": "kind=client", "box": "kind=borrowed\ncmake=-DUSE_LIBBACKTRACE=OFF"})
        r = w.wk("build", "x", "jsc-release", "--detach")
        argv = added(w, "box")[0]
        self.assertEqual(r.err.splitlines()[0], "==> x:build:jsc-release: 8 jobs at 1536 MB each, a 12288 MB budget, niced: box is borrowed")
        self.assertEqual((argv[6:15], argv[-2][-22:], argv[-1]), (["12288", "0", "nice", "-n", "19", "ionice", "-c", "3", "env"], "-DUSE_LIBBACKTRACE=OFF", "--makeargs=-j8"))

    def test_on_a_mac_it_builds_with_xcode_sized_from_total_memory(self):
        w = held("mac")
        r = w.wk("build", "x", "mac-release", "--detach")
        self.assertEqual((r.code, added(w, "mac")[0][6:]), (0, ["30720", "0", "env", "WK_BUILD=1", "WEBKIT_OUTPUTDIR=" + OUT + "/Release", "Tools/Scripts/build-webkit", "--release", "-jobs", "10"]))

    def test_the_servers_environment_caps_the_jobs(self):
        w = held(here="lin")
        w.wk("build", "x", "jsc-release", "--detach", env={"WK_MAX_JOBS": "3", "WK_MB_PER_JOB": "4096"})
        self.assertEqual((added(w, "lin")[0][6], added(w, "lin")[0][-1]), ("12288", "--makeargs=-j3"))

    def test_with_no_preset_it_repeats_the_workspaces_last_build(self):
        w = held()
        for preset in ("jsc-debug", "gtk-debug"):
            w.wk("build", "x", preset, "--detach")
            w.pueue["lin"].finish(max(w.pueue["lin"].tasks))
        r = w.wk("build", "x", "--detach")
        self.assertEqual((r.code, w.pueue["lin"].tasks[2]["label"]), (0, "x:build:gtk-debug"))

    def test_with_no_preset_and_no_build_remembered_it_is_refused_naming_the_presets_that_build_here(self):
        r = held("mac").wk("build", "x")
        self.assertEqual((r.code, r.err), (1, "error: x has built nothing, so there is no preset to repeat: name one of jsc-debug, jsc-release, "
                                              "mac-debug, mac-release\n"))

    def test_an_unknown_preset_and_one_this_server_cannot_build_are_refused_before_anything_is_measured(self):
        w = held()
        for preset, said in (("jsc-fast", "error: no preset named jsc-fast: the presets are"), ("mac-debug", "error: mac-debug builds on a mac server")):
            r = w.wk("build", "x", preset)
            self.assertEqual((r.code, r.err[:len(said)], r.effects, [c for c in r.calls if "fake-exec" in c]), (1, said, [], []))

    def test_a_second_build_in_the_workspace_is_refused_as_busy(self):
        w = held()
        w.wk("build", "x", "jsc-release", "--detach")
        r = w.wk("build", "x", "jsc-debug")
        self.assertEqual((r.code, r.err, r.effects), (2, "error: x is already building (x:build:jsc-release)\n"
                                                         "    `wk status x --wait` waits for it, `wk build x --kill` ends it\n    --force crosses this\n", []))
        r = w.wk("build", "x", "jsc-debug", "--detach", "--force")
        self.assertEqual((r.code, [t["label"] for t in w.pueue["lin"].tasks.values()]), (0, ["x:build:jsc-release", "x:build:jsc-debug"]))
        self.assertIn("warning: this command was forced past: x is already building", r.err)

    def test_a_busy_server_queues_the_build_and_says_so(self):
        w = held()
        w.workspace("y")
        w.wk("build", "x", "jsc-release", "--detach")
        r = w.wk("build", "y", "jsc-release", "--detach")
        self.assertEqual((r.code, r.err.splitlines()[0]), (0, "==> lin is building x:build:jsc-release: this build is queued behind it"))
        self.assertEqual([list(t["status"]) for t in w.pueue["lin"].tasks.values()], [["Running"], ["Queued"]])

    def test_a_failed_build_exits_1_naming_its_exit_code_and_the_log(self):
        w = held()
        w.pueue["lin"].ending(2, "FAILED: a.o\n")
        r = w.wk("build", "x", "jsc-release")
        self.assertEqual((r.code, r.out, r.err.splitlines()[-1]),
                         (1, "FAILED: a.o\n", "error: x:build:jsc-release failed (exit 2): `wk status x --log` has the end of its log"))

    def test_a_build_the_guard_killed_exits_3(self):
        w = held()
        w.pueue["lin"].ending(99, "wk: MEMORY LIMIT: this run is using 9000 MB, over its 8192 MB budget\n")
        r = w.wk("build", "x", "jsc-release")
        self.assertEqual((r.code, r.err.splitlines()[-1]),
                         (3, "error: x:build:jsc-release ran out of memory: its log says which limit (`wk status x --log`)"))

    def test_a_build_killed_while_followed_exits_1(self):
        w = held()
        w.machines["lin"].on(w.pueue["lin"].client.argv("follow"), lambda call: w.pueue["lin"].finish(0, result="Killed"))
        r = w.wk("build", "x", "jsc-release")
        self.assertEqual((r.code, r.err.splitlines()[-1]), (1, "error: x:build:jsc-release killed: `wk status x --log` has the end of its log"))

    def test_kill_ends_this_workspaces_build_and_no_other(self):
        w = held()
        w.workspace("y")
        w.wk("build", "y", "jsc-release", "--detach")
        w.wk("build", "x", "jsc-release", "--detach")
        r = w.wk("build", "x", "--kill")
        self.assertEqual((r.code, r.err, effects(r)), (0, "==> killed x:build:jsc-release\n", ["[lin] pueue remove 1"]))
        self.assertEqual(list(w.pueue["lin"].tasks), [0])

    def test_kill_with_nothing_building_says_so(self):
        r = held().wk("build", "x", "--kill")
        self.assertEqual((r.code, r.err, r.effects), (0, "==> x is not building\n", []))

    def test_an_unmeasurable_place_builds_nothing(self):
        w = held()
        w.probe["lin"] = "cores 16\navailable_kb \nload 0 0 0\n"
        r = w.wk("build", "x", "jsc-release")
        self.assertEqual((r.code, r.effects), (1, []))
        self.assertIn("error: cannot read available_kb on lin (the probe printed ``): jobs are sized from it, and nothing is built from a guess", r.err)

    def test_a_workspace_that_is_not_ready_or_not_here_is_refused(self):
        w = world(here="lin")
        w.workspace("half", state="broken")
        w.workspace("stopped")
        w.made.discard(("lin", "stopped"))
        for name, said in (("half", "error: workspace half on lin is not made (broken): wait for its `wk new`, or `wk new half` remakes a broken one\n"),
                           ("stopped", "error: workspace stopped on lin is broken, not ready\n    `wk enter stopped`, typed on the client, starts its container\n"
                                       "    --force crosses this\n"),
                           ("gone", "error: no workspace named gone on lin: `wk ls` lists them, `wk new gone` makes it\n")):
            r = w.wk("build", name, "jsc-release")
            self.assertEqual((r.code, r.err), (1, said))

    def test_it_converges_killed_at_any_point_and_its_dry_run_is_its_plan(self):
        converges(self, held, "build", "x", "jsc-release", "--detach")
        dry_run_is_the_plan(self, held, "build", "x", "jsc-release", "--detach")

    def test_a_dry_run_without_detach_follows_nothing(self):
        r = held().wk("build", "x", "jsc-release", "--dry-run")
        self.assertEqual((r.code, len(r.plan), [c for c in r.calls if " follow " in c]), (0, 3, []))
