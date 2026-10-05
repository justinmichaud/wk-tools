"""`wk test` (cmd/test) against a Fake world: the JSC and layout suites, --kill, and the record a run writes."""
import contextlib
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeProc, JobWorld
from tests.killpoints import converges
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import job, record  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402

CMD = load_cmd("test")

class World(JobWorld):
    """`sh -c` finds every layout path present unless a test says otherwise."""

    def __init__(self, tmp, kind="container"):
        super().__init__(tmp, kind, b"Ran 3 tests\nAll 3 tests passed.\n")
        self.answer(["exec", "ws", "sh", "-c"], out="")

    def state(self):
        return [(t.field("kind"), t.field("exit")) for t in self.recs().list()]


class TestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_NAME", "WK_PRESET"):
            os.environ.pop(v, None)
        p = mock.patch.object(record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        self.w = World(self.tmp)

    def run_(self, w=None, *argv):
        w = w or self.w
        os.environ["WK_NAME"] = "ws"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = CMD.main(as_dispatched("test", argv, os.environ), w.reg, w.clock)
        return rc, err.getvalue()

    def refused(self, w=None, *argv, status=None):
        w = w or self.w
        os.environ["WK_NAME"] = "ws"
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                CMD.main(as_dispatched("test", argv, os.environ), w.reg, w.clock)
        if status is not None:
            self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class TestDryRun(TestTest):
    def test_the_jsc_suite_dry_run_line(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("jsc-release", err)
        self.assertIn("run-javascriptcore-tests", err)
        self.assertEqual(self.w.effects, [e for e in self.w.effects if e[0] != "watch"])

    def test_the_layout_suite_dry_run_names_software_rendering(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_(None, "--layout", "--preset", "gtk-release")
        self.assertEqual(rc, 0, err)
        self.assertIn("run-webkit-tests", err)

    def test_layout_on_a_jsc_only_config_is_refused(self):
        err = self.refused(None, "--layout", status=1)
        self.assertIn("wk test ws --layout --preset gtk-release-asan", err)


class TestKill(TestTest):
    def test_kill_with_nothing_running_says_so_and_exits_0(self):
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)

    def test_kill_stops_a_recorded_run_and_records_it_cancelled(self):
        t = self.w.recs().begin("test", "here", "ws", "wk test ws --kill", str(self.tmp / "t.log"), ["jsc/jsc-release in ws"], pid=4242)
        self.w.pids.add(4242)
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)
        self.assertEqual(t.field("exit"), "cancelled")
        self.assertFalse(self.w.alive(4242))

    def test_a_run_that_outlives_term_and_kill_is_reported_not_claimed_stopped(self):
        t = self.w.recs().begin("test", "here", "ws", "wk test ws --kill", str(self.tmp / "t.log"), ["jsc/jsc-release in ws"], pid=4242)
        self.w.pids.add(4242)

        def stubborn_kill(pid, sig=15):
            self.w.effect(("kill", pid, int(sig)))
            return True   # the signal was sent; the process (this test says) ignored it
        self.w.kill = stubborn_kill
        self.refused(None, "--kill")
        self.assertEqual(t.field("exit"), "cancelled")


class TestSizing(TestTest):
    def test_jobs_are_sized_at_the_configs_memory_per_job(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.w.place_os, self.w.env["WK_AVAIL_MB"] = "macos", "6144"
        rc, err = self.run_(None, "--preset", "mac-release")
        self.assertEqual(rc, 0, err)
        self.assertIn("-j2", err)

    def test_the_disk_a_run_wants_is_the_configs(self):
        self.w.place_os = "macos"
        self.w.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 31457280 9% /\n")
        err = self.refused(None, "--preset", "mac-release-pgo")
        self.assertIn("60 GB", err)


class TestTheRecordARunWrites(TestTest):
    def test_a_run_that_succeeds_ends_ok_with_the_one_progress_record(self):
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("All 3 tests passed.", err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name"), t.field("exit")), ("test", "here", "ws", "0"))
        self.assertEqual(t.plan(), ["jsc/jsc-release in ws"])
        self.assertEqual(t.steps(), [(1, "running")])
        self.assertEqual(t.field("kill"), "wk test ws --kill")
        self.assertEqual(t.field("abort_after"), "1800")

    def test_a_failure_names_it_and_ends_with_its_status(self):
        self.w.out, self.w.rc = b"FAIL: fast/dom/Comment/basic.html\n", 3
        err = self.refused(status=3)
        self.assertIn("FAIL: fast/dom/Comment/basic.html", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "3")

    def test_a_run_stopped_by_its_kill_reads_cancelled_though_its_child_died_of_term(self):
        real = self.w.start

        def killed(*a, **kw):
            (t,) = self.w.recs().list()
            t.set("stopping", "cancelled")
            return real(*a, **kw)
        self.w.start, self.w.rc = killed, 143
        self.refused(status=143)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")

    def test_wk_tools_that_did_not_reach_the_workspace_is_refused_before_the_suite(self):
        self.w.answer(["sync-tools"], rc=1, err="rsync: connection refused")
        self.refused(status=1)
        self.assertEqual([e for e in self.w.effects if e[0] == "watch"], [])

    def test_a_stall_ends_stalled_and_dies(self):
        self.w.rc = 124
        self.refused(status=1)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_the_layout_suite_runs_with_software_rendering_by_default(self):
        rc, err = self.run_(None, "--layout", "--preset", "gtk-release")
        self.assertEqual(rc, 0, err)
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        line = " ".join(w[1])
        self.assertIn("LIBGL_ALWAYS_SOFTWARE=1", line)
        self.assertIn("run-webkit-tests", line)

    def test_a_missing_layout_path_is_refused_before_the_suite_runs(self):
        self.w.answer(["exec", "ws", "sh", "-c"], out="fast/gone.html\n")
        err = self.refused(None, "--layout", "--preset", "gtk-release", "fast/gone.html", status=1)
        self.assertIn("fast/gone.html", err)
        self.assertEqual(self.w.recs().list(), [])


class TestInterrupted(TestTest):
    def test_an_interrupt_stops_the_run_where_it_runs_and_the_record_reads_cancelled(self):
        class Interrupting(FakeProc):
            def poll(self):
                raise job.Interrupted(signal.SIGINT)
        real = record.Records.begin

        def begin(recs, *a, **kw):
            t = real(recs, *a, **kw)
            t.set("pid_match", CMD.PID_MATCH)
            t.pid(777)
            t.set("where", "place")
            return t
        self.w.pids.add(777)
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "777"], out="perl Tools/Scripts/run-javascriptcore-tests\n")
        self.w.react(["exec", "ws", "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        self.w.start = lambda argv, out, cwd=None: Interrupting(self.w, 0)
        with mock.patch.object(record.Records, "begin", begin):
            self.refused(status=130)
        self.assertIn(("run", ("exec", "ws", "kill", "-TERM", "777")), self.w.effects)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")


class TestLayoutPathCheck(unittest.TestCase):
    """missing_layout_paths, its shell run for real against a scratch checkout."""

    def missing(self, paths, present=(), rc=None):
        with tempfile.TemporaryDirectory() as src:
            for rel in present:
                Path(src, "LayoutTests", rel).parent.mkdir(parents=True, exist_ok=True)
                Path(src, "LayoutTests", rel).write_text("")

            def runner(argv):
                if rc is not None:
                    return Result(rc, "", "unreachable")
                cp = subprocess.run(argv, capture_output=True, text=True)
                return Result(cp.returncode, cp.stdout, cp.stderr)
            return CMD.missing_layout_paths(runner, src, paths)

    def test_every_missing_path_is_named_and_a_query_is_not_part_of_one(self):
        self.assertEqual(self.missing(["fast/gone-a.html", "fast/a.html?variant=1", "fast/gone-b.html"],
                                      present=["fast/a.html"]), ["fast/gone-a.html", "fast/gone-b.html"])

    def test_no_paths_or_a_runner_that_cannot_answer_names_none(self):
        self.assertEqual(self.missing([]), [])
        self.assertEqual(self.missing(["fast/gone.html"], rc=1), [])


class TestKillPoints(TestTest):
    def test_a_run_killed_after_any_effect_and_rerun_converges(self):
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                os.environ["WK_NAME"] = "ws"
                CMD.main([], w.reg, w.clock)
        converges(self, lambda: World(self.tmp), run_once, World.state)


if __name__ == "__main__":
    unittest.main()
