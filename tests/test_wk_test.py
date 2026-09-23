"""`wk test` as a flow (cmd/test, lib/wk/job.py, lib/wk/resources.py) against a
Fake world: the JSC and layout suites, --kill, and the record a run writes.

Owed rows landed here: `unit record.progress_shape[test]`, `unit killpoints[test]`.

Run: python3 tests/run.py -k test_wk_test
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_test", str(REPO / "cmd" / "test"))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_test", CMD_LOADER))
CMD_LOADER.exec_module(CMD)

DF_ROOMY = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n"


class TestTarget(targets.Target):
    def __init__(self, name, root, env, machine, kind="container"):
        super().__init__(name, root, env, machine)
        self.kind = kind

    def info(self, ws):
        return "running"

    def state(self, ws, info=None):
        return "present"

    def os(self):
        return self.machine.target_os

    def src(self, ws):
        return "/src/WebKit"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def exec_tty(self, ws, argv, timeout=None):
        return self.machine.run_tty(["exec-tty", ws] + list(argv))

    def build_size(self, ws):
        return self.machine.size


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return TestTarget("box", self.root, dict(self.env, **self.world.conf), self.world, self.world.kind)

    def ws_target(self, ws):
        return "box"

    def in_workspace(self):
        return False


class Proc:
    def __init__(self, world, rc):
        self.pid, self.world, self.rc = 4242, world, rc
        self.returncode = None

    def poll(self):
        self.returncode = self.rc if self.returncode is None else self.returncode
        return self.returncode

    def wait(self):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode


class World(Fake):
    """This host testing workspace `ws` on target `box`: `sh -c` finds every layout
    path present, the run writes `out` to its log and exits `rc`."""

    def __init__(self, tmp, kind="container"):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_TARGET": "box", "WK_NAME": "ws", "WK_IN_VM": "1",
                    "WK_AVAIL_MB": "65536", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2"}
        self.conf, self.kind, self.target_os = {}, kind, "linux"
        self.size = (8, 32768, 2 if kind == "remote" else None)
        self.clock = FakeClock()
        self.out, self.rc = b"Ran 3 tests\nAll 3 tests passed.\n", 0
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out=DF_ROOMY)
        self.answer(["exec", "ws", "sh", "-c"], out="")   # every layout path is present unless a test says otherwise
        self.react(["bash", "-c"], self._bash)
        self.react(["exec", "ws", "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.reg = Reg(self)
        self.ws_dir = os.path.join(self.env["WK_STORE"], "ws", "ws")
        os.makedirs(self.ws_dir)

    @property
    def fake(self):
        return self

    def _bash(self, argv, f):
        if "t_sync_tools" in argv[2]:
            return Result(0, "tools pushed\n")
        return Result(127, "", "no bash answer")

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.effect(("watch", tuple(argv)))
        stdout.write(self.out)
        return Proc(self, self.rc)

    def recs(self):
        return CMD.records_of(self.reg.load("box"), self.clock, self)

    def state(self):
        return [(t.field("kind"), t.field("exit")) for t in self.recs().list()]


class TestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-test-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_NAME", "WK_CONFIG"):
            os.environ.pop(v, None)
        p = mock.patch.object(record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        self.w = World(self.tmp)

    def run_(self, w=None, *argv):
        w = w or self.w
        os.environ["WK_NAME"] = "ws"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = CMD.main(list(argv), w.reg, w.clock, w.popen)
        return rc, err.getvalue()

    def refused(self, w=None, *argv, status=None):
        w = w or self.w
        os.environ["WK_NAME"] = "ws"
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                CMD.main(list(argv), w.reg, w.clock, w.popen)
        if status is not None:
            self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class TestDryRun(TestTest):
    def test_the_jsc_suite_dry_run_line(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("dry run -- nothing was run.", err)
        self.assertIn("workspace: ws (box, present)", err)
        self.assertIn("suite:     jsc (jsc-release)", err)
        self.assertIn("run-javascriptcore-tests", err)
        self.assertEqual(self.w.effects, [e for e in self.w.effects if e[0] != "watch"])

    def test_the_layout_suite_dry_run_names_software_rendering(self):
        os.environ["WK_DRY_RUN"] = "1"
        os.environ["WK_CONFIG"] = "gtk-release"
        rc, err = self.run_(None, "--layout")
        self.assertEqual(rc, 0, err)
        self.assertIn("suite:     layout (gtk-release)", err)
        self.assertIn("run-webkit-tests", err)

    def test_layout_on_a_jsc_only_config_is_refused(self):
        err = self.refused(None, "--layout", status=1)
        self.assertIn("builds JavaScriptCore alone", err)
        self.assertIn("wk test ws --layout --config gtk-release-asan", err)


class TestKill(TestTest):
    def test_kill_with_nothing_running_says_so_and_exits_0(self):
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)
        self.assertIn("no test is running in 'ws'", err)

    def test_kill_stops_a_recorded_run_and_records_it_cancelled(self):
        t = self.w.recs().begin("test", "here", "ws", "wk test ws --kill", str(self.tmp / "t.log"), ["jsc/jsc-release in ws"], pid=4242)
        self.w.pids.add(4242)
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)
        self.assertIn("stopping the test in 'ws'", err)
        self.assertEqual(t.field("exit"), "cancelled")
        self.assertFalse(self.w.alive(4242))

    def test_a_run_that_outlives_term_and_kill_is_reported_not_claimed_stopped(self):
        t = self.w.recs().begin("test", "here", "ws", "wk test ws --kill", str(self.tmp / "t.log"), ["jsc/jsc-release in ws"], pid=4242)
        self.w.pids.add(4242)

        def stubborn_kill(pid, sig=15):
            self.w.effect(("kill", pid, int(sig)))
            return True   # the signal was sent; the process (this test says) ignored it
        self.w.kill = stubborn_kill
        err = self.refused(None, "--kill")
        self.assertIn("outlived a TERM and a KILL", err)
        self.assertEqual(t.field("exit"), "cancelled")


class TestSizing(TestTest):
    def test_jobs_are_sized_at_the_configs_memory_per_job(self):
        """An Xcode config's peak is twice a CMake one's, so 6 GB free is 2 jobs for it, not 4."""
        os.environ["WK_DRY_RUN"] = "1"
        self.w.target_os, self.w.env["WK_AVAIL_MB"] = "macos", "6144"
        rc, err = self.run_(None, "--config", "mac-release")
        self.assertEqual(rc, 0, err)
        self.assertIn("JSC tests (mac-release, -j2)", err)

    def test_the_disk_a_run_wants_is_the_configs(self):
        self.w.target_os = "macos"
        self.w.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 31457280 9% /\n")
        err = self.refused(None, "--config", "mac-release-pgo")
        self.assertIn("this test run wants about 60 GB", err)


class TestTheRecordARunWrites(TestTest):
    def test_a_run_that_succeeds_ends_ok_with_the_one_progress_record(self):
        """`record.progress_shape[test]`: step 1 of 1, since when, the log, how to stop it."""
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("TESTS OK  jsc/jsc-release in 'ws'", err)
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
        self.assertIn("TESTS FAILED  jsc/jsc-release in 'ws'  (exit 3", err)
        self.assertIn("FAIL: fast/dom/Comment/basic.html", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "3")

    def test_a_run_stopped_by_its_kill_reads_cancelled_though_its_child_died_of_term(self):
        """`wk test --kill` marks the record before its TERM, and the driver seeing 143 ends it cancelled."""
        real = self.w.popen

        def killed(*a, **kw):
            (t,) = self.w.recs().list()
            t.set("stopping", "cancelled")
            return real(*a, **kw)
        self.w.popen, self.w.rc = killed, 143
        err = self.refused(status=143)
        self.assertIn("TESTS STOPPED  jsc/jsc-release in 'ws'  (by 'wk test ws --kill'", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")

    def test_wk_tools_that_did_not_reach_the_workspace_is_refused_before_the_suite(self):
        self.w.react(["bash", "-c"], lambda a, f: Result(1, "", "rsync: connection refused") if "t_sync_tools" in a[2] else Result(127))
        err = self.refused(status=1)
        self.assertIn("pushing wk-tools into 'ws' failed -- the reason is above", err)
        self.assertEqual([e for e in self.w.effects if e[0] == "watch"], [])

    def test_a_stall_ends_stalled_and_dies(self):
        self.w.rc = 124
        err = self.refused(status=1)
        self.assertIn("TESTS STALLED  jsc/jsc-release in 'ws'", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_the_layout_suite_runs_with_software_rendering_by_default(self):
        os.environ["WK_CONFIG"] = "gtk-release"
        rc, err = self.run_(None, "--layout")
        self.assertEqual(rc, 0, err)
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        line = " ".join(w[1])
        self.assertIn("LIBGL_ALWAYS_SOFTWARE=1", line)
        self.assertIn("run-webkit-tests", line)

    def test_a_missing_layout_path_is_refused_before_the_suite_runs(self):
        os.environ["WK_CONFIG"] = "gtk-release"
        self.w.answer(["exec", "ws", "sh", "-c"], out="fast/gone.html\n")
        err = self.refused(None, "--layout", "fast/gone.html", status=1)
        self.assertIn("no such test in 'ws'", err)
        self.assertIn("fast/gone.html", err)
        self.assertEqual(self.w.recs().list(), [])


class TestKillPoints(TestTest):
    def test_a_run_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[test]`: each run its own process, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                os.environ["WK_NAME"] = "ws"
                CMD.main([], w.reg, w.clock, w.popen)
        converges(self, lambda: World(self.tmp), run_once, World.state)


if __name__ == "__main__":
    unittest.main()
