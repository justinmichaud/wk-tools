"""`wk test ws ...` typed on a host whose workspace `ws` runs the JSC and layout suites."""
import os
import subprocess
import tempfile
import unittest

from tests.fakes import FlowTest, JobWorld, Result

JSC = "Tools/Scripts/run-javascriptcore-tests --release --jsc-only --no-build --no-fail-fast --memory-limited"


class World(JobWorld):
    """`sh -c` in `ws` runs for real against `layout` (the LayoutTests files present); `stubborn` is a run that
    ignores every signal; `meanwhile(task)` is what another process does to the run's record once it has started,
    and `interrupt` a ^C on the first look at the run."""

    def __init__(self, tmp, kind="container"):
        super().__init__(tmp, kind, b"Ran 3 tests\nAll 3 tests passed.\n")
        self.layout, self.stubborn, self.meanwhile = set(), False, None
        self.react(["exec", "ws", "sh", "-c"], self._sh)

    def _sh(self, argv, f):
        with tempfile.TemporaryDirectory() as src:
            for rel in f.layout:
                os.makedirs(os.path.dirname(os.path.join(src, "LayoutTests", rel)), exist_ok=True)
                open(os.path.join(src, "LayoutTests", rel), "w").close()
            cp = subprocess.run(["sh", "-c", argv[-1].replace("/src/WebKit", src)], capture_output=True, text=True)
        return Result(cp.returncode, cp.stdout, cp.stderr)

    def kill(self, pid, sig=15):
        if not self.stubborn:
            return super().kill(pid, sig)
        self.effect(("kill", pid, int(sig)))
        return True

    def start(self, argv, out, cwd=None):
        p = super().start(argv, out, cwd)
        if self.meanwhile:
            self.meanwhile(self.recs().list()[0])
        return p

    def state(self):
        return [(t.field("kind"), t.field("exit")) for t in self.recs().list()]

    def watched(self):
        return [" ".join(e[1]) for e in self.effects if e[0] == "watch"]


def refusal(err):
    return next(l for l in err.splitlines() if l.startswith("error: "))


class TestTest(FlowTest):
    UNSET = FlowTest.UNSET + ("WK_NAME", "WK_PRESET")

    def setUp(self):
        super().setUp()
        self.w = World(self.tmp)

    def typed(self, *argv):
        return self.wk(self.w, "test", "ws", *argv)

    def would_run(self, *argv):
        rc, _, err = self.typed("--dry-run", *argv)
        (line,) = [l for l in err.splitlines() if l.startswith("  would run: ")]
        self.assertEqual((rc, self.w.watched()), (0, []), err)
        return line[len("  would run: "):]

    def begun(self):
        """A test run recorded as running as pid 4242, as `wk test ws` leaves it."""
        self.w.pids.add(4242)
        return self.w.recs().begin("test", "here", "ws", "wk test ws --kill", str(self.tmp / "t.log"), ["jsc/jsc-release in ws"], pid=4242)


class TestDryRun(TestTest):
    def test_the_jsc_suite_is_named_with_its_runner_and_no_output_dir_but_an_asan_presets(self):
        self.assertEqual(self.would_run(), "bash -lc 'cd /src/WebKit && nice -n 10 %s'" % JSC)
        self.assertEqual(self.would_run("--preset", "jsc-release-asan"), "bash -lc 'cd /src/WebKit && "
                         "WEBKIT_OUTPUTDIR=/src/WebKit/WebKitBuild/asan nice -n 10 %s'" % JSC)

    def test_a_path_given_to_the_jsc_suite_is_the_runners_filter_without_jstests(self):
        self.assertTrue(self.would_run("JSTests/stress/arith-add.js", "stress/add.js").endswith(
            "--memory-limited --filter '\"'\"'^(stress/arith\\-add\\.js|stress/add\\.js)(\\..*)?$'\"'\"''"))

    def test_layout_on_a_jsc_only_preset_is_refused_naming_a_full_port_preset(self):
        rc, _, err = self.typed("--layout")
        self.assertEqual((rc, err.splitlines()[2]), (1, "        wk test ws --layout --preset gtk-release-asan"))
        self.assertEqual(self.w.watched(), [])


class TestKill(TestTest):
    def test_kill_with_nothing_running_says_so_and_exits_0(self):
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, err.splitlines()[0]), (0, "no test is running in 'ws' -- 'wk status ws' says what it last did"))
        self.assertEqual([e for e in self.w.effects if e[0] == "kill"], [])

    def test_kill_stops_a_recorded_run_and_records_it_cancelled(self):
        self.begun()
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, err.splitlines()[-1]), (0, "==> stopped 'ws's test and recorded it as cancelled"))
        self.assertEqual(([e for e in self.w.effects if e[0] == "kill"], self.w.state()), ([("kill", 4242, 15)], [("test", "cancelled")]))

    def test_a_run_that_outlives_term_and_kill_is_reported_not_claimed_stopped(self):
        self.begun()
        self.w.stubborn, self.w.env["WK_KILL_WAIT"] = True, "0"   # the dispatcher's clock is the real one: KILL's own 5 s grace stays
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, err.splitlines()[-2]), (1, "error: the test run in 'ws' outlived a TERM and a KILL."))
        self.assertEqual(([e[2] for e in self.w.effects if e[0] == "kill"], self.w.state()), ([15, 9], [("test", "cancelled")]))


class TestSizing(TestTest):
    def test_jobs_are_sized_at_the_presets_memory_per_job(self):
        self.w.place_os, self.w.env["WK_AVAIL_MB"] = "macos", "6144"
        rc, _, err = self.typed("--dry-run", "--preset", "mac-release")
        self.assertEqual(rc, 0, err)
        self.assertIn("==> JSC tests (mac-release, -j2)", err.splitlines())

    def test_the_disk_a_run_wants_is_the_presets(self):
        self.w.place_os = "macos"
        self.w.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 31457280 9% /\n")
        rc, _, err = self.typed("--preset", "mac-release-pgo")
        self.assertEqual(rc, 1, err)
        self.assertRegex(refusal(err), r"^error: 30 GB free on \S+'s filesystem; this test run wants about 60 GB\.$")
        self.assertEqual((self.w.watched(), self.w.state()), ([], []))


class TestTheRecordARunWrites(TestTest):
    def test_a_run_that_succeeds_ends_ok_with_the_one_progress_record(self):
        rc, _, err = self.typed()
        self.assertEqual((rc, err.splitlines()[-3]), (0, "==> TESTS OK  jsc/jsc-release in 'ws'  (0m0s)"))
        self.assertEqual(self.w.watched(), ["exec ws bash -lc echo \"wk: test pid $$\" >&2\ncd /src/WebKit && nice -n 10 " + JSC])
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name"), t.field("exit"), t.field("kill"), t.field("abort_after")),
                         ("test", "here", "ws", "0", "wk test ws --kill", "1800"))
        self.assertEqual((t.plan(), t.steps()), (["jsc/jsc-release in ws"], [(1, "running")]))

    def test_a_failure_names_it_and_ends_with_its_status(self):
        self.w.out, self.w.rc = b"FAIL: fast/dom/Comment/basic.html\n", 3
        rc, _, err = self.typed()
        self.assertEqual((rc, self.w.state()), (3, [("test", "3")]))
        self.assertEqual(err.splitlines()[-3:-1], ["failures:", "  FAIL: fast/dom/Comment/basic.html"])

    def test_a_run_stopped_by_its_kill_reads_cancelled_though_its_child_died_of_term(self):
        self.w.meanwhile, self.w.rc = lambda t: t.set("stopping", "cancelled"), 143
        rc, _, err = self.typed()
        self.assertEqual((rc, self.w.state()), (143, [("test", "cancelled")]))
        self.assertRegex(err.splitlines()[-1], r"^warning: TESTS STOPPED  jsc/jsc-release in 'ws'  \(by 'wk test ws --kill'")

    def test_wk_tools_that_did_not_reach_the_workspace_is_refused_before_the_suite(self):
        self.w.answer(["sync-tools"], rc=1, err="rsync: connection refused")
        rc, _, err = self.typed()
        self.assertEqual((rc, err.splitlines()[-1]), (1, "error: pushing wk-tools into 'ws' failed -- the reason is above"))
        self.assertEqual((self.w.watched(), self.w.state()), ([], []))

    def test_a_stall_ends_stalled_and_dies(self):
        self.w.rc = 124
        rc, _, err = self.typed()
        self.assertEqual((rc, self.w.state()), (1, [("test", "stalled")]))
        self.assertIn("error: TESTS STALLED  jsc/jsc-release in 'ws' (killed after 0m0s with no output)", err.splitlines())

    def test_the_layout_suite_runs_with_software_rendering_by_default(self):
        rc, _, err = self.typed("--layout", "--preset", "gtk-release")
        self.assertEqual(rc, 0, err)
        (line,) = self.w.watched()
        self.assertRegex(line, r"\ncd /src/WebKit && LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe "
                               r"WEBKIT_DISABLE_DMABUF_RENDERER=1 nice -n 10 Tools/Scripts/run-webkit-tests ")

    def test_every_missing_layout_path_is_refused_before_the_suite_runs_and_a_query_is_not_part_of_one(self):
        self.w.layout = {"fast/a.html"}
        rc, _, err = self.typed("--layout", "--preset", "gtk-release", "fast/gone-a.html", "fast/a.html?variant=1", "fast/gone-b.html")
        lines = err.splitlines()
        i = lines.index(refusal(err))
        self.assertEqual((rc, lines[i:i + 3]), (1, ["error: no such test in 'ws':", "      fast/gone-a.html", "      fast/gone-b.html"]))
        self.assertEqual((self.w.watched(), self.w.state()), ([], []))

    def test_present_layout_paths_or_a_workspace_that_cannot_answer_run_the_suite(self):
        self.w.layout = {"fast/a.html"}
        self.assertEqual(self.typed("--layout", "--preset", "gtk-release", "fast/a.html")[0], 0)
        self.w.answer(["exec", "ws", "sh", "-c"], rc=1, err="sh: cannot run")
        self.assertEqual(self.typed("--layout", "--preset", "gtk-release", "fast/gone.html")[0], 0)
        self.assertEqual(len(self.w.watched()), 2)

    def test_no_layout_path_asks_the_workspace_nothing(self):
        self.assertEqual(self.typed("--layout", "--preset", "gtk-release")[0], 0)
        self.assertEqual([e for e in self.w.effects if e[1][:4] == ("exec", "ws", "sh", "-c")], [])


class TestInterrupted(TestTest):
    def test_an_interrupt_stops_the_run_where_it_runs_and_the_record_reads_cancelled(self):
        def adopted(t):   # what the run's pid watch records once the run announces pid 777 in the workspace
            t.set("pid_match", "*Tools/Scripts/run-*")
            t.pid(777)
            t.set("where", "place")
        self.w.meanwhile, self.w.interrupt = adopted, 2
        self.w.pids.add(777)
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "777"], out="perl Tools/Scripts/run-javascriptcore-tests\n")
        self.w.react(["exec", "ws", "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        rc, _, err = self.typed()
        self.assertEqual((rc, self.w.state()), (130, [("test", "cancelled")]))
        self.assertIn(("run", ("exec", "ws", "kill", "-TERM", "777")), self.w.effects)
        self.assertIn("warning: interrupted -- stopping the test run in 'ws'", err.splitlines())


if __name__ == "__main__":
    unittest.main()
