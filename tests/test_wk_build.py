"""`wk build` as a flow (lib/wk/build.py, lib/wk/job.py) against a Fake world:
the refusals, the record a run writes and how it ends, --kill, --detach, the
babysitter, a dry run as the recorder, and a run killed after any effect.

Owed rows landed here: `unit build.config_is_data` (its --cmakeargs and disk
halves; the configs are tests/test_buildconf.py's), `unit build.sizes_once`,
`unit build.babysit_states`, `unit record.detach_reads_its_own_build`,
`unit record.progress_shape[build]`, `unit killpoints[build]`,
`unit dispatch.dry_run_is_the_recorder[build]`.

Run: python3 tests/run.py -k test_wk_build
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import posix
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, build, job, record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

FAR_LINE = "cd /src/WebKit && Tools/Scripts/build-webkit --jsc-only --release --makeargs=-j8\n"
CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_build", str(REPO / "cmd" / "build"))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_build", CMD_LOADER))
CMD_LOADER.exec_module(CMD)
LINUX = posix.uname_result(("Linux", "h", "6", "#1", "aarch64"))


class BuildTarget(targets.Target):
    def __init__(self, name, root, env, machine, kind):
        super().__init__(name, root, env, machine)
        self.kind = kind
        self.host = "box.example" if kind == "remote" else ""

    def info(self, ws):
        return "running"

    def state(self, ws, info=None):
        return "present"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def build_size(self, ws):
        return self.machine.size

    def mirror_dir(self):
        return "/mirror"

    def os(self):
        return self.machine.target_os


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return BuildTarget("box", self.root, dict(self.env, **self.world.conf), self.world, self.world.kind)

    def ws_target(self, ws):
        return "box"

    def in_workspace(self):
        return self.world.in_ws


class Proc:
    def __init__(self, world, rc, hang, interrupt):
        self.pid, self.world, self.rc, self.hang, self.interrupt = 4242, world, rc, hang, interrupt
        self.returncode = None

    def poll(self):
        if self.interrupt is not None:
            signum, self.interrupt = self.interrupt, None
            raise job.Interrupted(signum)
        if self.hang and self.returncode is None:
            return None
        self.returncode = self.rc if self.returncode is None else self.returncode
        return self.returncode

    def wait(self):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode


class World(Fake):
    """This host building in one workspace `ws` on target `box`: the far half answers the dry-run
    line, df has room, the build writes `out` to its log and exits `rc`."""

    def __init__(self, tmp, kind="container"):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_TARGET": "box", "WK_NAME": "ws", "WK_IN_VM": "1",
                    "WK_AVAIL_MB": "65536", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2"}
        self.conf, self.kind, self.in_ws, self.target_os = {}, kind, False, "linux"
        self.size = (8, 32768, 2 if kind == "remote" else None)
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.out, self.rc, self.hang, self.interrupt = b"[1/2] CXX a.o\n[2/2] LINK jsc\n", 0, False, None
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.react(["exec", "ws", "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.answer(["exec", "ws", "grep", "-q", "WK_DRY_RUN"])
        self.answer(["exec", "ws", "bash", "-c"])
        self.react(["exec", "ws", "env"], lambda a, f: Result(0, FAR_LINE) if "WK_DRY_RUN=1" in a else Result(1))
        self.react(["bash", "-c"], self._bash)
        self.reg = Reg(self)
        self.ws_dir = os.path.join(self.env["WK_STORE"], "ws", "ws")
        os.makedirs(self.ws_dir)
        self.log = os.path.join(self.ws_dir, "build.log")

    @property
    def fake(self):
        return self

    def _bash(self, argv, f):
        if "origin_branch_fetch_step" in argv[2]:
            return Result(0, "git fetch -q origin 'topic'")
        if "t_sync_tools" in argv[2]:
            return Result(0, "tools pushed\n")
        return Result(127, "", "no bash answer")

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.effect(("watch", tuple(argv)))
        stdout.write(self.out)
        return Proc(self, self.rc, self.hang, self.interrupt)

    def recs(self):
        return build.records_of(self.reg.load("box"), self.clock, self)

    def begin(self, kind="build", name="ws", pid=4242, where="here", **kw):
        t = self.recs().begin(kind, where, name, kw.pop("kill", "wk build %s --kill" % name), kw.pop("log", self.log),
                              kw.pop("plan", ["a"]), pid=pid)
        for k, v in kw.items():
            t.set(k, v)
        return t

    def budget_files(self):
        d = os.path.join(self.env["XDG_STATE_HOME"], "wk", "builds")
        return sorted(p for p in self.files if p.startswith(d + "/"))

    def state(self):
        return ([(t.field("kind"), t.field("exit")) for t in self.recs().list()], len(self.budget_files()))


def argv_of(config="jsc-release", *more):
    return [config] + list(more)


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-build-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_CMD"):
            os.environ.pop(v, None)
        p = mock.patch.object(record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        self.w = World(self.tmp)

    def make(self, w=None, *argv):
        w = w or self.w
        opts = CMD.parse(list(argv) or argv_of())
        return build.Build(w.reg, "ws", opts, list(argv) or argv_of(), w.clock, w.popen)

    def run_(self, w=None, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.make(w, *argv).front()
        return rc, err.getvalue()

    def refused(self, w=None, *argv, status=1):
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.make(w, *argv).front()
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class TestTheRecordARunWrites(BuildTest):
    def test_a_build_that_succeeds_ends_ok_with_the_one_progress_record(self):
        """`record.progress_shape[build]`: step n of m, since when, the log, how to stop it."""
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        self.assertIn("BUILD OK  jsc-release in 'ws'", err)
        self.assertIn("running:   " + FAR_LINE.strip(), err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name"), t.field("exit")), ("build", "here", "ws", "0"))
        self.assertEqual(t.plan(), ["sync wk-tools into 'ws'", "compile jsc-release with -j8"])
        self.assertEqual(t.steps(), [(1, "done"), (2, "running")])
        self.assertEqual((t.field("log"), t.field("kill"), t.field("config")), (self.w.log, "wk build ws --kill", "jsc-release"))
        self.assertEqual(t.field("abort_after"), "1800")
        self.assertTrue(t.field("started"))
        self.assertEqual(Path(self.w.log).read_bytes(), self.w.out)

    def test_the_budget_is_on_the_books_under_this_driver(self):
        self.run_()
        (f,) = self.w.budget_files()
        text = self.w.files[f]
        self.assertIn("label=wk build ws (jsc-release)\n", text)
        self.assertIn("holder=pid:%d\n" % os.getpid(), text)
        self.assertIn("machine=here\n", text)

    def test_a_branch_is_checked_out_first_as_its_own_step(self):
        rc, err = self.run_(None, "jsc-release", "--branch", "topic")
        (t,) = self.w.recs().list()
        self.assertEqual(t.plan()[0], "check out topic")
        (co,) = [e for e in self.w.effects if e[0] == "run" and e[1][:3] == ("exec", "ws", "bash")]
        self.assertIn("git checkout -q topic", co[1][4])
        self.assertIn("git fetch -q origin 'topic'", co[1][4])

    def test_the_build_runs_the_target_half_under_the_config_environment(self):
        self.run_(None, "jsc-release", "--cmake", "-DX=1", "--env", "CC=gcc", "--", "--verbose")
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        argv = list(w[1])
        self.assertEqual(argv[:3], ["exec", "ws", "env"])
        self.assertEqual(argv[-2:], ["/opt/wk-tools/build/build-in-target.sh", "--verbose"])
        self.assertIn("CC=gcc", argv)
        self.assertTrue(any(a.startswith("WK_BUILD_CMAKE=") and a.endswith("-DX=1") for a in argv))

    def test_a_failure_names_its_first_errors_and_ends_with_its_status(self):
        self.w.out, self.w.rc = b"a.cpp:1: error: no\nninja: build stopped\n", 2
        err = self.refused(status=2)
        self.assertIn("BUILD FAILED  jsc-release in 'ws'  (exit 2", err)
        self.assertIn("  1:a.cpp:1: error: no", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "2")

    def test_a_broken_xcode_plan_names_the_directory_to_remove(self):
        self.w.target_os, self.w.out, self.w.rc = "macos", b"error: xcbuilddata/manifest.json unreadable\n", 1
        self.w.kind = "vm"
        err = self.refused(None, "mac-release")
        self.assertIn("rm -rf /src/WebKit/WebKitBuild/Release/XCBuildData", err)

    def test_a_build_killed_for_memory_ends_oom(self):
        self.w.out, self.w.rc = b"wk: memory: peak 9000MB\nwk: MEMORY LIMIT hit at 9000MB\n", 137
        err = self.refused(status=137)
        self.assertIn("BUILD KILLED FOR MEMORY", err)
        self.assertIn("peak 9000MB", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "oom")

    def test_a_silent_build_is_killed_by_its_watchdog_and_ends_stalled(self):
        self.w.hang, self.w.out = True, b""
        self.w.env.update({"WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30", "WK_POLL_SECONDS": "10"})
        err = self.refused()
        self.assertIn("no output for 30s, and nothing here is compiling or linking", err)
        self.assertIn("giving up and killing the job", err)
        self.assertIn("BUILD STALLED  jsc-release in 'ws'", err)
        self.assertIn(("kill", 4242, int(signal.SIGTERM)), self.w.effects)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_the_log_is_truncated_before_the_record_says_running(self):
        """`record.detach_reads_its_own_build`, its first half."""
        Path(self.w.log).write_text("the previous build's log\n")
        seen = []
        real = record.Records.begin

        def begin(recs, *a, **kw):
            seen.append(self.w.files.get(self.w.log))
            return real(recs, *a, **kw)
        with mock.patch.object(record.Records, "begin", begin):
            self.run_()
        self.assertEqual(seen, [""])


class TestInterrupted(BuildTest):
    def test_a_hangup_stops_the_build_and_the_record_reads_cancelled(self):
        self.w.hang, self.w.interrupt = True, signal.SIGHUP
        err = self.refused(status=129)
        self.assertIn("interrupted -- stopping the build in 'ws'", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")
        self.assertIn(("kill", 4242, int(signal.SIGTERM)), self.w.effects)

    def test_a_job_that_announced_its_pid_is_stopped_where_it_runs(self):
        self.w.hang, self.w.interrupt = True, signal.SIGINT
        real = record.Records.begin

        def begin(recs, *a, **kw):
            t = real(recs, *a, **kw)
            t.set("pid_match", build.PID_MATCH)
            t.pid(777)
            t.set("where", "target")
            return t
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "777"], out="bash /opt/wk-tools/build/build-in-target.sh\n")
        self.w.react(["exec", "ws", "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        self.w.pids.add(777)
        with mock.patch.object(record.Records, "begin", begin):
            self.refused(status=130)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")
        self.assertIn(("run", ("exec", "ws", "kill", "-TERM", "777")), self.w.effects)


class TestStoppedByItsKill(BuildTest):
    """A detached build stopped by `wk build --kill`: its driver sees its child die of the TERM (143) before the
    kill command sees the build gone, and the record still reads cancelled."""

    def test_the_driver_that_saw_143_records_cancelled(self):
        real = self.w.popen

        def killed(*a, **kw):
            (t,) = self.w.recs().list()
            t.set("stopping", "cancelled")
            return real(*a, **kw)
        self.w.popen, self.w.rc = killed, 143
        err = self.refused(status=143)
        self.assertIn("BUILD STOPPED  jsc-release in 'ws'  (by 'wk build ws --kill'", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")

    def test_the_kill_marks_the_record_before_its_term(self):
        t = self.w.begin("build", pid=12)
        self.w.pids.add(12)
        real = self.w.kill

        def driver_wins(pid, sig=signal.SIGTERM):
            t.end(143)
            return real(pid, sig)
        self.w.kill = driver_wins
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)
        self.assertEqual(t.field("exit"), "cancelled")


class TestRefusals(BuildTest):
    def test_no_config_prints_the_usage_and_the_list(self):
        err = self.refused(None, "--no-defaults", status=2)
        self.assertIn("usage: wk build <workspace> <config>", err)
        self.assertIn("  jsc-release ", err)
        self.w.in_ws = True
        self.assertIn("usage: wk build <config>", self.refused(None, "--no-defaults", status=2))

    def test_an_unknown_config_names_the_list(self):
        self.assertIn("unknown config 'nope' (wk build --list)", self.refused(None, "nope"))

    def test_cmakeargs_is_refused_naming_what_it_would_drop(self):
        """`build.config_is_data`: the config's flags are data, and --cmakeargs would replace them."""
        err = self.refused(None, "jsc-release", "--cmakeargs", "-DX=1")
        self.assertIn("--cmakeargs would replace the config's CMake flags", err)
        self.assertIn("wk build ws jsc-release --cmake -DX=1", err)
        self.assertIn("--force proceeds anyway", err)
        os.environ["WK_FORCE"] = "1"
        rc, err = self.run_(None, "jsc-release", "--cmakeargs", "-DX=1")
        self.assertEqual(rc, 0, err)
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        self.assertEqual(w[1][-1], "--cmakeargs=-DX=1")

    def test_sysroot_and_a_bad_env_are_refused(self):
        self.assertIn("--sysroot is not implemented", self.refused(None, "jsc-release", "--sysroot", "x"))
        self.assertIn("--env takes NAME=VALUE, got nope", self.refused(None, "jsc-release", "--env", "nope"))

    def test_a_held_workspace_lock_is_refused_at_once_naming_kill(self):
        lock = self.w.reg.load("box").store.lock_path("ws-ws")
        self.w.files[lock] = "pid=5555 tok=x at=now cmd=wk"
        self.w.pids.add(5555)
        err = self.refused()
        self.assertIn("'ws' is already building -- its driver holds the ws-ws lock.", err)
        self.assertIn("Stop it:    wk build ws --kill", err)
        self.assertEqual(self.w.recs().list(), [])

    def test_a_job_holding_the_checkout_is_a_barrier_naming_its_stop(self):
        self.w.begin("yocto", kill="wk sysimage build x --stop")
        self.w.pids.add(4242)
        err = self.refused()
        self.assertIn("'ws' already has a job running in it: yocto (pid 4242, here)  stop it: wk sysimage build x --stop", err)
        self.assertIn("--force proceeds anyway", err)

    def test_the_job_that_started_this_build_is_not_counted(self):
        t = self.w.begin("babysit")
        self.w.pids.add(4242)
        self.w.env["WK_TASK_PARENT"] = str(t.path)
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)

    def test_another_build_on_this_machine_is_a_retry_barrier(self):
        from wk.resources import Budget
        self.w.pids.add(99)
        Budget(self.w, self.w.env).record("wk build other (gtk-release)", 6, 9216, "pid:99")
        err = self.refused(status=act.RETRY_EXIT)
        self.assertIn("here is already building:\n      wk build other (gtk-release) (6 jobs, 9216 MB)", err)
        self.assertIn("minus 9216MB other builds", err)

    def test_a_full_disk_is_a_barrier_and_a_guest_is_asked_about_its_own(self):
        """`build.config_is_data`: a profile-guided config declares its disk need, asked of the host
        store (the image a guest grows) and of the guest."""
        full = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 10485760 99% /\n"
        self.w.answer(["df", "-Pk"], out=full)
        self.assertIn("10 GB free on %s's filesystem; this build wants about 25 GB." % self.w.env["WK_STORE"], self.refused())
        w = World(self.tmp, kind="vm")
        w.target_os = "macos"
        w.answer(["exec", "ws", "df", "-Pk"], out=full.replace("10485760", "41943040"))
        self.assertIn("40 GB free on the disk inside 'ws'; this build wants about 60 GB.", self.refused(w, "mac-release-pgo"))


class TestSizedOnce(BuildTest):
    """`build.sizes_once`: the whole machine is read once and the desktop reserved once, on the host and in a guest."""

    MEMINFO = "MemTotal: 67108864 kB\nMemAvailable: 62914560 kB\n"

    def sized(self, w, host=True):
        del w.env["WK_AVAIL_MB"]
        w.files["/proc/meminfo"] = self.MEMINFO
        w.answer(["nproc"], out="16\n")
        if not host:
            w.files[os.path.join(w.env["WK_STORE"], ".headless")] = ""
        with mock.patch("os.uname", return_value=LINUX):
            t = w.reg.load("box")
            w.size = targets.Target.build_size(t, "ws")
            rc, err = self.run_(w)
        self.assertEqual(rc, 0, err)
        return w.size, [l for l in err.splitlines() if l.startswith("resources:")][0]

    def test_a_container_on_a_desktop_host_reserves_the_desktop_once(self):
        size, line = self.sized(self.w)
        self.assertEqual(size[:2], (15, 65536 - 12288))
        self.assertIn("resources: 15 jobs (cores=15 avail=53248MB @ 1536MB/job)", line)

    def test_inside_a_guest_the_headless_reserve_is_the_only_one(self):
        size, line = self.sized(self.w, host=False)
        self.assertEqual(size[:2], (16, 65536 - 2048))
        self.assertIn("cores=16 avail=61440MB", line)

    def test_a_remote_target_is_sized_from_its_own_numbers_and_politely(self):
        w = World(self.tmp, kind="remote")
        rc, err = self.run_(w)
        self.assertIn("resources: 4 jobs (cores=8 avail=65536MB @ 1536MB/job, polite, load=2)", err)
        self.assertIn("with -j4 (nice 19)", err)

    def test_a_low_count_warns_with_its_reason(self):
        self.w.env["WK_AVAIL_MB"] = "3072"
        rc, err = self.run_()
        self.assertIn("parallelism: 2 jobs is under half of 8 cores -- the memory\n  envelope only fits 2", err)


class TestDryRun(BuildTest):
    def test_it_prints_the_plan_and_one_running_line_and_builds_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_(None, "jsc-release", "--cmake", "-DX=1", "--", "--verbose")
        self.assertEqual(rc, 0, err)
        self.assertIn("dry run -- nothing was built.", err)
        self.assertIn("  workspace: ws (box, present)", err)
        self.assertIn("  config:    jsc-release (cmake --jsc-only --no-fatal-warnings --release)", err)
        self.assertIn("  --cmake:   -DX=1 (added to the config's)", err)
        self.assertIn("  passed on: --verbose (straight to build-webkit)", err)
        self.assertEqual([l for l in err.splitlines() if "running:" in l], ["  running:   " + FAR_LINE.strip()])
        self.assertEqual(self.w.recs().list(), [])
        self.assertEqual([e for e in self.w.effects if e[0] in ("watch", "spawn")], [])

    def test_the_plan_is_the_runs_mutations(self):
        """`dispatch.dry_run_is_the_recorder[build]`."""
        class Recording(World):
            def act_run(self, argv, **kw):
                self.effects.append(("act", tuple(argv)))
                return super().act_run(argv, **kw)

        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            return [e for e in w.effects if e[0] in ("act", "write", "mkdir", "remove", "kill", "spawn")
                    if not (isinstance(e[1], str) and e[1].startswith(locks))]
        wet = Recording(self.tmp)
        self.run_(wet)
        dry = Recording(self.tmp)
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.run_(dry)
        rel = [tuple(re.sub(r"/builds/.*", "/builds/*", str(x).replace(str(w.tmp), "")) for x in e) for w in (wet, dry) for e in mutations(w)]
        n = len(mutations(wet))
        self.assertEqual(rel[:n], rel[n:])
        self.assertGreaterEqual(n, 4)
        self.assertIn("would run: exec ws env", err)
        self.assertEqual(dry.pids, set())

    def test_a_stale_far_copy_is_named_rather_than_asked(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.w.answer(["exec", "ws", "grep", "-q", "WK_DRY_RUN"], rc=1)
        rc, err = self.run_()
        self.assertIn("push this tree there first:  wk sync --tools box", err)


class TestKill(BuildTest):
    def test_nothing_running_says_so_and_is_0(self):
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0)
        self.assertIn("no babysit is running in 'ws'", err)
        self.assertIn("no build is running in 'ws' -- 'wk status ws' says what it last did", err)

    def test_the_babysitter_is_stopped_before_the_build_and_both_read_cancelled(self):
        self.w.begin("babysit", pid=11)
        self.w.begin("build", pid=12)
        self.w.pids.update({11, 12})
        rc, err = self.run_(None, "--kill")
        self.assertEqual(rc, 0, err)
        self.assertEqual([e[1] for e in self.w.effects if e[0] == "kill"], [11, 12])
        self.assertEqual({t.field("kind"): t.field("exit") for t in self.w.recs().list()}, {"babysit": "cancelled", "build": "cancelled"})
        self.assertIn("stopping the build in 'ws' (pid 12 on here)", err)
        self.assertIn("'wk build ws <config>' resumes rather than starts over.", err)

    def test_one_that_outlives_a_kill_is_refused_naming_it(self):
        class Immortal(World):
            def kill(self, pid, sig=signal.SIGTERM):
                self.effect(("kill", pid, int(sig)))
                return True
        w = Immortal(self.tmp)
        w.begin("build", pid=12)
        w.pids.add(12)
        err = self.refused(w, "--kill")
        self.assertIn("'ws's build outlived a TERM and a KILL.", err)
        self.assertIn("pid 12 did not stop on TERM after 2s -- killing it", err)

    def test_a_dry_run_stops_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.w.begin("build", pid=12)
        self.w.pids.add(12)
        rc, err = self.run_(None, "--kill")
        self.assertIn("dry run -- would TERM pid 12, KILL it after 2s, and record it cancelled", err)
        self.assertNotIn("stopped 'ws's build", err)
        self.assertEqual(self.w.pids, {12})
        self.assertEqual(self.w.recs().list()[0].field("exit"), "")


class Detaching(World):
    """The spawned child begins its own record under its own pid, as `wk build` in there does."""

    def __init__(self, tmp, starts=True):
        super().__init__(tmp)
        self.starts = starts

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        if self.starts:
            self.begin("build", pid=pid)
        else:
            self.pids.discard(pid)
        return pid


class TestDetach(BuildTest):
    def test_it_returns_on_its_own_builds_record_and_not_the_last_report(self):
        """`record.detach_reads_its_own_build`, its second half."""
        w = Detaching(self.tmp)
        w.begin("build", pid=4000).end(1)
        rc, err = self.run_(w, "jsc-release", "--detach")
        self.assertEqual(rc, 0, err)
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual(list(sp[1]), [str(REPO / "wk"), "build", "ws", "jsc-release"])
        self.assertEqual(sp[2], os.path.join(w.ws_dir, "detached.log"))
        self.assertLess(w.effects.index(("write", sp[2])), w.effects.index(sp))
        self.assertIn("building jsc-release in 'ws', detached as pid 1001 -- this end can go away", err)
        self.assertIn("  stop it: wk build ws --kill", err)

    def test_a_child_that_ends_before_its_record_is_named(self):
        w = Detaching(self.tmp, starts=False)
        w.begin("build", pid=4000).end(0)
        err = self.refused(w, "jsc-release", "--detach")
        self.assertIn("the detached build of 'ws' ended before it started", err)

    def test_inside_a_workspace_the_name_is_not_passed_on(self):
        w = Detaching(self.tmp)
        w.in_ws = True
        self.run_(w, "jsc-release", "--detach", "--", "-v")
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual(list(sp[1])[1:], ["build", "jsc-release", "--", "-v"])


class TestBabysitFront(BuildTest):
    def test_it_refuses_where_it_cannot_run(self):
        self.w.in_ws = True
        self.assertIn("--babysit runs on the host", self.refused(None, "jsc-release", "--babysit"))
        self.assertIn("refusing to babysit on a remote target", self.refused(World(self.tmp, "remote"), "jsc-release", "--babysit"))
        self.assertIn("already inside a workspace", self.refused(World(self.tmp, "local"), "jsc-release", "--babysit"))

    def test_one_at_a_time_by_its_record(self):
        self.w.begin("babysit", pid=21)
        self.w.pids.add(21)
        self.assertIn("a babysitter is already running for 'ws' (pid 21)", self.refused(None, "jsc-release", "--babysit"))

    def test_it_detaches_the_loop_with_the_build_flags(self):
        self.w.env["WK_BABYSIT_MODEL"] = "sonnet"
        rc, err = self.run_(None, "jsc-release", "--babysit", "--branch", "topic", "--", "-v")
        (sp,) = [e for e in self.w.effects if e[0] == "spawn"]
        self.assertEqual(list(sp[1])[1:], ["build", "ws", "jsc-release", "--branch", "topic", "--_babysit", "--", "-v"])
        self.assertIn("(pid 1001, model sonnet, branch topic)", err)
        self.assertEqual(self.w.recs().list(), [])


class TestBabysitStates(BuildTest):
    """`build.babysit_states`: one record, ended by name however it ends, and a killed one reads died."""

    def loop(self, builds, fixes=None, attempts="2"):
        """`builds` answers each inner `wk build` in turn: (status, the word its own record ends with)."""
        w = self.w
        w.env["WK_BABYSIT_ATTEMPTS"] = attempts
        results = list(builds)
        fixes = list(fixes or [])

        def inner(argv, f):
            rc, word = results.pop(0)
            t = f.begin("build", pid=5000 + len(results))
            t.end(word)
            return Result(rc, "built\n")
        w.react(["env"], inner)
        w.react(["env", "WK_NAME=ws"], lambda a, f: fixes.pop(0) if fixes else Result(0, "changed a.cpp\n"))
        return w

    def babysit(self, w, *more):
        return self.make(w, "jsc-release", "--_babysit", *more)

    def record(self):
        (t,) = [t for t in self.w.recs().list() if t.field("kind") == "babysit"]
        return t

    def report(self):
        return self.w.files[os.path.join(self.w.ws_dir, "babysit.report")]

    def test_a_build_that_succeeds_on_its_own_ends_0(self):
        w = self.loop([(0, "0")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.babysit(w).front(), 0)
        self.assertEqual(self.record().field("exit"), "0")
        self.assertIn("nothing to fix", self.report())
        (inner,) = [e[1] for e in w.effects if e[0] == "run_tty" and e[1][0] == "env" and e[1][1].startswith("WK_TASK_PARENT=")]
        self.assertEqual(list(inner[2:]), [str(REPO / "wk"), "build", "ws", "jsc-release"])
        self.assertEqual(inner[1], "WK_TASK_PARENT=%s" % self.record().path)

    def test_a_fix_then_a_good_build_ends_0_after_one_fix(self):
        w = self.loop([(1, "1"), (0, "0")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.babysit(w).front()
        self.assertEqual(self.record().field("exit"), "0")
        self.assertEqual(self.record().steps()[:2], [(1, "done"), (2, "running")])
        self.assertIn("=== fix attempt 1 (exit 0)", self.report())

    def test_a_stalled_build_ends_stalled(self):
        w = self.loop([(1, "stalled")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(Refused, self.babysit(w).front)
        self.assertEqual(self.record().field("exit"), "stalled")

    def test_still_failing_after_every_fix_ends_gave_up(self):
        w = self.loop([(1, "1")] * 3)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(Refused, self.babysit(w).front)
        self.assertEqual(self.record().field("exit"), "gave-up")
        self.assertIn("still failing after 2 fix attempt(s)", self.report())

    def test_claude_that_did_not_run_ends_error(self):
        w = self.loop([(1, "1")], fixes=[Result(1, "", "no claude")])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(Refused, self.babysit(w).front)
        self.assertEqual(self.record().field("exit"), "error")

    def test_a_branch_it_cannot_check_out_ends_error(self):
        w = self.loop([])
        w.react(["exec", "ws", "bash", "-c"], lambda a, f: Result(1))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertRaises(Refused, self.babysit(w, "--branch", "topic").front)
        self.assertEqual(self.record().field("exit"), "error")

    def test_a_stopped_one_reads_cancelled_and_a_killed_one_died(self):
        def stopped(argv, f):
            raise job.Interrupted(signal.SIGTERM)
        self.w.react(["env"], stopped)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(Refused) as cm:
                self.babysit(self.w).front()
        self.assertEqual(cm.exception.status, 143)
        self.assertEqual(self.record().field("exit"), "cancelled")
        dead = self.w.begin("babysit", name="other", pid=31)
        self.assertEqual(dead.verdict("pid"), "died")


class TestBusyReason(BuildTest):
    def target(self):
        return self.w.reg.load("box")

    def test_a_pid_file_in_the_home_alive_in_the_workspace_is_busy(self):
        home = os.path.join(self.w.ws_dir, "home")
        self.w.dirs.add(home)
        self.w.files[os.path.join(home, "buildroot-image.pid")] = "77\n"
        self.w.answer(["exec", "ws", "kill", "-0", "77"])
        self.assertEqual(build.busy_reason(self.target(), self.w.recs(), "ws"), "buildroot-image (pid 77 in the workspace)")
        self.w.answer(["exec", "ws", "kill", "-0", "77"], rc=1)
        self.assertIsNone(build.busy_reason(self.target(), self.w.recs(), "ws"))

    def test_a_pid_a_target_record_names_is_judged_by_its_kind_alone(self):
        """A pid file a record names is that record's, and a `test` record holds no checkout."""
        home = os.path.join(self.w.ws_dir, "home")
        self.w.files[os.path.join(home, "jsc-tests.pid")] = "88\n"
        self.w.begin("test", pid=88, where="target")
        self.w.pids.add(88)
        self.w.answer(["exec", "ws", "kill", "-0", "88"])
        self.assertIsNone(build.busy_reason(self.target(), self.w.recs(), "ws"))

    def test_another_workspaces_job_is_not_this_ones(self):
        self.w.begin("build", name="other", pid=4242)
        self.w.pids.add(4242)
        self.assertIsNone(build.busy_reason(self.target(), self.w.recs(), "ws"))


class TestJob(BuildTest):
    def test_a_pid_announced_down_the_log_is_adopted_only_when_it_is_the_job(self):
        t = self.w.begin("build")
        Path(self.w.log).write_text("noise\nwk: build pid 8123\n")
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "8123"], out="bash /opt/wk-tools/build/build-in-target.sh\n")
        watch = job.PidWatch(self.w.reg.load("box"), "ws", t, self.w.log, "build", build.PID_MATCH, 1)
        watch.run()
        self.assertEqual((t.field("pid"), t.field("where"), t.field("pid_match")), ("8123", "target", build.PID_MATCH))
        t2 = self.w.begin("build", name="x")
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "8123"], out="/sbin/init\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            job.PidWatch(self.w.reg.load("box"), "ws", t2, self.w.log, "build", build.PID_MATCH, 1).run()
        self.assertIn("/sbin/init", err.getvalue())
        self.assertEqual(t2.field("pid_match"), "")

    def test_the_drivers_own_pid_is_converged_at_once(self):
        t = self.w.begin("build", pid=os.getpid())
        self.assertTrue(job.kill(self.w.reg.load("box"), "ws", t, "cancelled", self.w, self.w.clock, self.w.env))
        self.assertEqual(t.field("exit"), "cancelled")
        self.assertFalse([e for e in self.w.effects if e[0] == "kill"])

    def test_descendants_go_before_their_parent_from_one_call(self):
        self.w.answer(["sh", "-c", job.TREE, "wk", "10"], out="12\n11\n10\n")
        self.assertEqual(job.descendants(self.w.run, 10), [12, 11, 10])
        self.assertEqual(len([e for e in self.w.effects if e[0] == "run"]), 1)

    def test_the_walk_finds_a_real_child_before_its_parent(self):
        parent = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
        self.addCleanup(parent.wait)
        self.addCleanup(lambda: job.kill_tree(Local(), parent.pid, signal.SIGKILL))
        for _ in range(50):
            tree = job.descendants(Local().run, parent.pid)
            if len(tree) == 2:
                break
            time.sleep(0.1)
        self.assertEqual((len(tree), tree[-1]), (2, parent.pid))

    def test_a_heartbeat_names_the_progress(self):
        self.w.hang = True
        self.w.env.update({"WK_HEARTBEAT_SECONDS": "20", "WK_POLL_SECONDS": "10", "WK_ABORT_SECONDS": "30", "WK_STALL_SECONDS": "100"})
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = job.watch(["x"], self.w.log, self.w, self.w.clock, self.w.env, None, self.w.popen)
        self.assertEqual(rc, 124)
        self.assertIn("  ... [2/2] (0m elapsed)", err.getvalue())

    def test_a_stall_report_names_the_busiest_compiler(self):
        self.w.answer(["ps", "-A", "-o", "pcpu=,comm="], out=" 99.0 /usr/bin/ld64\n 3.0 zsh\n")
        Path(self.w.log).write_text("[5/9] CXX\nlast line\n")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            job.stall_report(self.w, self.w.log, 400)
        self.assertIn("running 1 compiler/linker process(es)", err.getvalue())
        self.assertIn("busiest:       ld64 at 99% CPU", err.getvalue())
        self.assertIn("last progress: [5/9]", err.getvalue())
        self.assertIn("tail: last line", err.getvalue())


class TestKillPoints(BuildTest):
    def test_a_build_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[build]`: each run its own process, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                self.make(w).front()
        converges(self, lambda: World(self.tmp), run_once, World.state)


if __name__ == "__main__":
    unittest.main()
