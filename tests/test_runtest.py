import unittest

from tests.killpoints import converges, dry_run_is_the_plan
from tests.pueue import added, world
from wk.core.machine import Result

SRC, OUT = "/home/u/wk/ws/x/src", "/home/u/wk/ws/x/build"
EXEC = "[lin] fake-exec x %s " % SRC


def built(preset="jsc-release", on="lin"):
    w = world(here=on)
    w.workspace("x", on=on)
    w.wk("build", "x", preset, "--detach")
    w.pueue[on].finish(0)
    return w


class TestRun(unittest.TestCase):
    def test_it_runs_the_last_built_jsc_in_the_workspace_and_returns_its_status(self):
        w = built()
        w.machines["lin"].on(["fake-exec", "x", SRC, "env"], Result(3))
        r = w.wk("run", "x", "--", "-e", "print(1)")
        self.assertEqual((r.code, r.effects), (3, [EXEC + "env LD_LIBRARY_PATH=%s/JSCOnly/Release/lib %s/JSCOnly/Release/bin/jsc --validateOptions=1 -e 'print(1)'"
                                                   % (OUT, OUT)]))

    def test_preset_chooses_another_build_and_a_full_port_runs_its_browser(self):
        r = built().wk("run", "x", "--preset", "gtk-debug")
        self.assertEqual((r.code, r.effects), (0, [EXEC + "env WEBKIT_OUTPUTDIR=%s/GTK/Debug Tools/Scripts/run-minibrowser --gtk --debug" % OUT]))

    def test_with_nothing_built_and_no_preset_it_is_refused(self):
        w = world()
        w.workspace("x")
        r = w.wk("run", "x")
        self.assertEqual((r.code, r.err[:67], r.effects), (1, "error: x has built nothing, so there is no preset to repeat: name o", []))

    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        r = built().wk("run", "x", "--dry-run")
        self.assertEqual((r.code, r.effects, r.plan), (0, [], [EXEC + "env LD_LIBRARY_PATH=%s/JSCOnly/Release/lib %s/JSCOnly/Release/bin/jsc --validateOptions=1"
                                                               % (OUT, OUT)]))


class TestTest(unittest.TestCase):
    def test_jsc_tests_run_as_a_task_in_the_default_group_sized_and_followed(self):
        w = built()
        w.pueue["lin"].ending(0, "Ran 100 tests\n")
        r = w.wk("test", "x", "--", "stress/a.js")
        self.assertEqual((r.code, r.out, r.err), (0, "Ran 100 tests\n", "==> x:test:jsc-release: 16 jobs at 1536 MB each\n"
                                                                        "==> x:test:jsc-release finished\n"))
        self.assertEqual(added(w, "lin")[1], ["fake-exec", "x", SRC, "env", "WEBKIT_OUTPUTDIR=" + OUT + "/JSCOnly/Release",
                                              "Tools/Scripts/run-javascriptcore-tests", "--jsc-only", "--release", "--no-build", "--no-fail-fast",
                                              "--memory-limited", "--child-processes=16", "--filter", r"^(stress/a\.js)(\..*)?$"])
        self.assertEqual([(t["group"], t["label"]) for t in w.pueue["lin"].tasks.values()], [("build", "x:build:jsc-release"), ("default", "x:test:jsc-release")])

    def test_failing_tests_exit_1_and_detach_returns_at_once(self):
        w = built()
        w.pueue["lin"].ending(4)
        self.assertEqual(w.wk("test", "x").code, 1)
        r = w.wk("test", "x", "--detach")
        self.assertEqual((r.code, r.err.splitlines()[-1]), (0, "==> task 2 is queued: `wk status x --wait` is its outcome, `wk status x --log` its output"))

    def test_layout_tests_are_checked_to_exist_first_then_run_for_the_preset_named(self):
        w = built()
        r = w.wk("test", "x", "--preset", "gtk-release", "--detach", "--", "fast/dom/a.html?x=1", "--verbose")
        check = [c for c in r.calls if c.startswith(EXEC + "sh -c 'for t in")]
        self.assertEqual((r.code, len(check), check[0].endswith(" %s 'fast/dom/a.html?x=1'" % SRC)), (0, 1, True))
        self.assertEqual(added(w, "lin")[1][-7:], ["--gtk", "--release", "--no-build", "--no-retry-failures", "--child-processes=13", "fast/dom/a.html?x=1", "--verbose"])

    def test_a_layout_test_that_does_not_exist_is_refused_because_the_runner_would_pass(self):
        w = built("gtk-release")
        w.missing["lin"] = "fast/gone.html\nnope\n"
        r = w.wk("test", "x", "--", "fast/gone.html", "nope", "fast/a.html")
        self.assertEqual((r.code, r.err, r.effects), (1, "error: no such layout test in x: fast/gone.html, nope\n"
                                                         "    a test is named relative to LayoutTests/ or to the checkout\n", []))

    def test_an_existence_check_that_fails_is_not_read_as_all_present(self):
        w = built("gtk-release")
        w.missing["lin"] = Result(125, "", "no such container")
        r = w.wk("test", "x", "--", "fast/a.html")
        self.assertEqual((r.code, r.err.splitlines()[0]), (1, "error: cannot check that the layout tests named exist in x: no such container"))
        self.assertEqual((w.wk("test", "x", "--detach", "--force", "--", "fast/a.html").code, len(added(w, "lin"))), (0, 2))

    def test_layout_tests_with_no_test_named_check_nothing(self):
        r = built("gtk-release").wk("test", "x", "--detach")
        self.assertEqual((r.code, [c for c in r.calls if "for t in" in c]), (0, []))

    def test_it_converges_killed_at_any_point_and_its_dry_run_is_its_plan(self):
        converges(self, built, "test", "x", "--detach")
        dry_run_is_the_plan(self, built, "test", "x", "--detach")
