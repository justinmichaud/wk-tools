"""`wk build ws ...` typed on a host whose workspace `ws` builds WebKit."""
import os
import signal
import subprocess
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FlowTest, JobDriver, JobWorld, Result
from tests.support import REPO

FAR_LINE = "cd /src/WebKit && Tools/Scripts/build-webkit --jsc-only --release --makeargs=-j8"
STAGE = ["/opt/wk-tools/lib", "wk.sysimage.task", "stage", "build", "--", "env"]
TREE = '_d() { for k in $(pgrep -P "$1"); do _d "$k"; done; echo "$1"; }; _d "$1"'
DF = "Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 %d 99%% /\n"
WK = str(REPO / "wk")


class SizedDriver(JobDriver):
    """A workspace sized by the place's own reading of the machine, not the world's `size`."""

    def build_size(self, ws):
        return super(JobDriver, self).build_size(ws)


class World(JobWorld):
    """`ws` answers the dry run's far line and a checkout; `announce` is a pid the build writes down its log, which
    the start waits for the driver to read; `meanwhile(task)` is what another process does to the record once the
    build has started; `stubborn` pids ignore every signal; `sized` sizes from the place's reading of the machine."""

    def __init__(self, tmp, kind="container", sized=False):
        super().__init__(tmp, kind, b"[1/2] CXX a.o\n[2/2] LINK jsc\n")
        self.answer(["exec", "ws", "bash", "-c"])
        self.react(["exec", "ws", "env"], lambda a, f: Result(0, FAR_LINE + "\n") if "WK_DRY_RUN=1" in a else Result(1))
        self.log = os.path.join(self.ws_dir, "build.log")
        self.announce, self.meanwhile, self.stubborn = None, None, set()
        if sized:
            self.reg.make = lambda n, e: SizedDriver("box", str(REPO), dict(e, **self.conf), self, self.kind, mirror="/mirror")

    def begin(self, kind="build", name="ws", pid=4242, kill=None, **fields):
        """A job recorded as `wk build ws` (or another) leaves it, running as `pid`."""
        self.pids.add(pid)
        t = self.recs().begin(kind, "here", name, kill or "wk build %s --kill" % name, self.log, ["a"], pid=pid)
        for k, v in fields.items():
            t.set(k, v)
        return t

    def start(self, argv, out, cwd=None):
        if self.announce:
            self.out += b"wk: build pid %d\n" % self.announce
        p = super().start(argv, out, cwd)
        if self.announce:
            out.flush()
            for t in threading.enumerate():
                if t.daemon and t is not threading.current_thread():
                    t.join(5)
        if self.meanwhile:
            self.meanwhile(self.recs().list()[0])
        return p

    def kill(self, pid, sig=signal.SIGTERM):
        if pid not in self.stubborn:
            return super().kill(pid, sig)
        self.effect(("kill", pid, int(sig)))
        return True

    def state(self):
        return [(t.field("kind"), t.field("exit"), t.holds()) for t in self.recs().list()]

    def exits(self):
        return {t.field("kind"): t.field("exit") for t in self.recs().list()}

    def killed(self):
        return [e[1:] for e in self.effects if e[0] == "kill"]

    def watched(self):
        return [list(e[1]) for e in self.effects if e[0] == "watch"]

    def spawned(self):
        return [(list(e[1]), e[2]) for e in self.effects if e[0] == "spawn"]


def said(err, start="error: "):
    return next(l for l in err.splitlines() if l.startswith(start))


class BuildTest(FlowTest):
    UNSET = FlowTest.UNSET + ("WK_NAME", "WK_PRESET")

    def setUp(self):
        super().setUp()
        self.w = World(self.tmp)

    def typed(self, *argv, w=None):
        """`wk build ws <argv>`: a jsc-release build when none is given, no name inside a workspace."""
        w = w or self.w
        os.environ.pop("WK_PRESET", None)
        return self.wk(w, "build", *([] if w.in_ws else ["ws"]), *(argv or ("jsc-release",)))

    def world(self, kind="container", **kw):
        return World(self.tmp, kind, **kw)

    def on_mac(self, w):
        w.kind, w.place_os = "vm", "macos"
        return w


class TestTheRecordARunWrites(BuildTest):
    def test_a_build_that_succeeds_ends_ok_with_the_one_progress_record(self):
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err, "==> BUILD")), (0, "==> BUILD OK  jsc-release in 'ws'  (0m0s)"))
        self.assertIn("running:   " + FAR_LINE, err.splitlines())
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("exit"), t.field("log"), t.field("kill"), t.field("preset"),
                          t.field("abort_after"), t.field("pid")),
                         ("build", "here", "0", self.w.log, "wk build ws --kill", "jsc-release", "1800", str(os.getpid())))
        self.assertEqual((t.plan(), t.steps()), (["sync wk-tools into 'ws'", "compile jsc-release with -j8"], [(1, "done"), (2, "running")]))
        self.assertEqual((t.holds(), t.field("jobs"), t.field("budget_mb")), (["ws:ws", "build:here"], "8", "12288"))
        self.assertEqual(Path(self.w.log).read_bytes(), self.w.out)

    def test_a_branch_is_checked_out_first_as_its_own_step(self):
        self.assertEqual(self.typed("jsc-release", "--branch", "topic")[0], 0)
        self.assertEqual(self.w.recs().list()[0].plan()[0], "check out topic")
        (co,) = [e[1][4] for e in self.w.effects if e[0] == "run" and e[1][:3] == ("exec", "ws", "bash")]
        self.assertIn("git checkout -q topic", co)
        self.assertIn("git fetch -q origin topic", co)

    def test_the_build_runs_the_target_half_under_the_config_environment(self):
        self.typed("jsc-release", "--cmake", "-DX=1", "--env", "CC=gcc", "--", "--verbose")
        (argv,) = self.w.watched()
        self.assertEqual((argv[:3], argv[6:12], argv[-2:]), (["exec", "ws", "python3"], STAGE,
                                                              ["/opt/wk-tools/build/build-in-workspace.sh", "--verbose"]))
        self.assertIn("CC=gcc", argv)
        self.assertTrue(any(a.startswith("WK_BUILD_CMAKE=") and a.endswith("-DX=1") for a in argv))

    def test_a_failure_names_its_first_errors_and_ends_with_its_status(self):
        self.w.out, self.w.rc = b"a.cpp:1: error: no\nninja: build stopped\n", 2
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err, "warning: ")), (2, "warning: BUILD FAILED  jsc-release in 'ws'  (exit 2 after 0m0s)"))
        self.assertIn("  1:a.cpp:1: error: no", err.splitlines())
        self.assertEqual(self.w.exits(), {"build": "2"})

    def test_a_broken_xcode_plan_names_the_directory_to_remove(self):
        self.on_mac(self.w).out, self.w.rc = b"error: xcbuilddata/manifest.json unreadable\n", 1
        rc, _, err = self.typed("mac-release")
        self.assertEqual(rc, 1, err)
        self.assertIn("    wk enter ws rm -rf /src/WebKit/WebKitBuild/Release/XCBuildData", err.splitlines())

    def test_a_build_killed_for_memory_ends_oom(self):
        self.w.out, self.w.rc = b"wk: memory: peak 9000MB\nwk: MEMORY LIMIT hit at 9000MB\n", 137
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err, "warning: ")), (137, "warning: BUILD KILLED FOR MEMORY  jsc-release in 'ws'  (after 0m0s)"))
        self.assertIn("  peak 9000MB. Fewer jobs: WK_MB_PER_JOB=2304 wk build ws jsc-release", err.splitlines())
        self.assertEqual(self.w.exits(), {"build": "oom"})

    def test_a_silent_build_with_nothing_compiling_is_killed_by_its_watchdog_and_ends_stalled(self):
        self.w.hang, self.w.out = True, b""
        self.w.env.update({"WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30", "WK_POLL_SECONDS": "10"})
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err)), (1, "error: BUILD STALLED  jsc-release in 'ws'  (killed after 1m5s with no output)"))
        self.assertIn("warning: no output for 30s, and nothing here is compiling or linking", err.splitlines())
        self.assertEqual((self.w.killed(), self.w.exits()), ([(4242, 15), (4242, 9)], {"build": "stalled"}))

    def test_a_silent_build_is_reported_on_its_heartbeat_with_its_busiest_compiler(self):
        self.w.hang = True
        self.w.env.update({"WK_HEARTBEAT_SECONDS": "20", "WK_POLL_SECONDS": "10", "WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30"})
        self.w.answer(["ps", "-A", "-o", "pcpu=,comm="], out=" 99.0 /usr/bin/ld64\n 40.0 clang\n 3.0 zsh\n")
        rc, _, err = self.typed()
        lines = err.splitlines()
        self.assertIn("  ... [2/2] (0m elapsed)", lines)
        self.assertIn("warning: no output for 30s, and this machine is running 2 compiler/linker process(es) -- "
                      "a full-LTO link is silent for minutes at a time", lines)
        self.assertEqual(lines[lines.index("  busiest:       ld64 at 99% CPU") + 1:][:1], ["  last progress: [2/2]"])
        self.assertIn("  tail: [2/2] LINK jsc", lines)

    def test_a_refused_build_leaves_the_running_ones_log_alone(self):
        self.w.files[self.w.log] = "the running build's log\n"
        self.w.begin("yocto", holds="ws:ws")
        self.assertEqual(self.typed()[0], 1)
        self.assertEqual(self.w.files[self.w.log], "the running build's log\n")


class TestInterrupted(BuildTest):
    def test_a_hangup_stops_the_build_and_the_drivers_own_record_reads_cancelled_at_once(self):
        self.w.hang, self.w.interrupt = True, signal.SIGHUP
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err, "warning: ")), (129, "warning: interrupted -- stopping the build in 'ws'"))
        self.assertEqual((self.w.killed(), self.w.exits()), ([(4242, 15), (4242, 9)], {"build": "cancelled"}))
        self.assertNotIn("did not stop", err)

    def test_a_job_that_announced_its_pid_is_adopted_and_stopped_where_it_runs(self):
        self.w.hang, self.w.interrupt, self.w.announce, self.w.env["WK_JOB_PID_TRIES"] = True, signal.SIGINT, 777, "3"
        self.w.pids.add(777)
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "777"], out="bash /opt/wk-tools/build/build-in-workspace.sh\n")
        self.w.react(["exec", "ws", "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        self.assertEqual(self.typed()[0], 130)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("exit"), t.field("pid"), t.field("where")), ("cancelled", "777", "place"))
        self.assertIn(("run", ("exec", "ws", "kill", "-TERM", "777")), self.w.effects)

    def test_a_pid_announced_that_is_not_the_job_is_not_adopted(self):
        self.w.announce, self.w.env["WK_JOB_PID_TRIES"] = 8123, "3"
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "8123"], out="/sbin/init\n")
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err, "warning: ")), (0, "warning: 'ws' names pid 8123 as its job, and that pid there is running "
                                                          "'/sbin/init', not *build-in-workspace.sh* *Tools/Scripts/build-*."))
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("pid"), t.field("pid_match")), (str(os.getpid()), ""))


class TestStoppedByItsKill(BuildTest):
    def test_the_driver_that_saw_143_records_cancelled(self):
        self.w.meanwhile, self.w.rc = lambda t: t.set("stopping", "cancelled"), 143
        rc, _, err = self.typed()
        self.assertEqual(rc, 143, err)
        self.assertRegex(said(err, "warning: "), r"^warning: BUILD STOPPED  jsc-release in 'ws'  \(by 'wk build ws --kill'")
        self.assertEqual(self.w.exits(), {"build": "cancelled"})

    def test_the_kill_marks_the_record_before_its_term(self):
        t = self.w.begin("build", pid=12)
        real = self.w.kill
        self.w.kill = lambda pid, sig=signal.SIGTERM: (t.end(143), real(pid, sig))[1]   # the driver sees its 143 first
        self.assertEqual(self.typed("--kill")[0], 0)
        self.assertEqual(self.w.exits(), {"build": "cancelled"})


class TestRefusals(BuildTest):
    def test_a_workspace_that_is_not_running_is_refused_before_any_effect(self):
        self.on_mac(self.w)
        with mock.patch.object(JobDriver, "info", lambda self, ws: "stopped"):
            rc, _, err = self.typed("mac-release")
        self.assertEqual((rc, said(err)), (1, "error: 'ws' is not running (wk start ws)"))
        self.assertEqual((self.w.state(), os.path.exists(self.w.log)), ([], False))

    def test_cmakeargs_is_refused_naming_what_it_would_drop_and_force_passes_it_on(self):
        rc, _, err = self.typed("jsc-release", "--cmakeargs", "-DX=1")
        self.assertEqual((rc, said(err)[:76]), (1, "error: --cmakeargs would replace the preset's CMake flags, and 'jsc-release'"))
        self.assertIn("    --cmake adds to them:  wk build ws jsc-release --cmake -DX=1", err.splitlines())
        self.assertEqual(self.w.watched(), [])
        self.assertEqual(self.typed("jsc-release", "--cmakeargs", "-DX=1", "--force")[0], 0)
        self.assertEqual(self.w.watched()[0][-1], "--cmakeargs=-DX=1")

    def test_a_bad_env_is_refused(self):
        rc, _, err = self.typed("jsc-release", "--env", "nope")
        self.assertEqual((rc, said(err)), (1, "error: --env takes NAME=VALUE, got nope"))

    def test_a_job_holding_the_checkout_is_refused_naming_its_stop(self):
        held = self.w.begin("yocto", kill="wk sysimage build x --stop", holds="ws:ws")
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err)), (1, "error: 'ws' already has a job running in it:"))
        self.assertIn("    yocto (pid 4242 on here)  stop it: wk sysimage build x --stop", err.splitlines())
        self.assertEqual([t.id for t in self.w.recs().list()], [held.id])

    def test_the_job_that_started_this_build_is_not_counted(self):
        self.w.env["WK_TASK_PARENT"] = str(self.w.begin("babysit", holds="ws:ws").path)
        self.assertEqual(self.typed()[0], 0)

    def test_another_build_on_this_machine_is_a_retry_barrier(self):
        self.w.begin("build", name="other", pid=99, holds="build:here", jobs="6", budget_mb="9216")
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err)), (75, "error: here is already building:"))
        self.assertIn("      build other (6 jobs, 9216 MB)", err.splitlines())
        self.assertIn("minus 9216MB other builds", err)

    def test_a_full_disk_is_a_barrier_and_a_guest_is_asked_about_its_own(self):
        self.w.answer(["df", "-Pk"], out=DF % 10485760)
        rc, _, err = self.typed()
        self.assertEqual((rc, said(err)), (1, "error: 10 GB free on %s's filesystem; this build wants about 25 GB." % self.w.env["WK_STORE"]))
        w = self.on_mac(self.world())
        w.answer(["exec", "ws", "df", "-Pk"], out=DF % 41943040)
        rc, _, err = self.typed("mac-release-pgo", w=w)
        self.assertEqual((rc, said(err)), (1, "error: 40 GB free on the disk inside 'ws'; this build wants about 60 GB."))


class TestSized(BuildTest):
    """The whole machine is read once and the desktop reserved once, on the host and in a guest."""

    def sized(self, headless=False):
        w = self.world(sized=True)
        del w.env["WK_AVAIL_MB"]
        w.files["/proc/meminfo"] = "MemTotal: 67108864 kB\nMemAvailable: 62914560 kB\n"
        w.answer(["nproc"], out="16\n")
        if headless:
            w.files[os.path.join(w.env["WK_STORE"], ".headless")] = ""
        with mock.patch("os.uname", return_value=os.uname_result(("Linux", "h", "6", "#1", "aarch64"))):
            rc, _, err = self.typed(w=w)
        self.assertEqual(rc, 0, err)
        return said(err, "resources:")

    def test_a_container_on_a_desktop_host_reserves_the_desktop_once(self):
        self.assertEqual(self.sized(), "resources: 15 jobs (cores=15 avail=53248MB @ 1536MB/job)")

    def test_inside_a_guest_the_headless_reserve_is_the_only_one(self):
        self.assertEqual(self.sized(headless=True), "resources: 16 jobs (cores=16 avail=61440MB @ 1536MB/job)")

    def test_a_remote_target_is_sized_from_its_own_numbers_and_politely(self):
        rc, _, err = self.typed(w=self.world("remote"))
        self.assertEqual(said(err, "resources:"), "resources: 4 jobs (cores=8 avail=65536MB @ 1536MB/job, polite, load=2)")
        self.assertIn("==> building jsc-release in 'ws' with -j4 (nice 19)", err.splitlines())

    def test_a_low_count_warns_with_its_reason(self):
        self.w.env["WK_AVAIL_MB"] = "3072"
        rc, _, err = self.typed()
        self.assertIn("parallelism: 2 jobs is under half of 8 cores -- the memory\n  envelope only fits 2", err)


class TestDryRun(BuildTest):
    def test_it_prints_the_plan_and_one_running_line_and_builds_nothing(self):
        rc, _, err = self.typed("--dry-run", "jsc-release", "--cmake", "-DX=1", "--env", "CC=gcc", "--branch", "topic", "--", "--verbose")
        self.assertEqual(rc, 0, err)
        lines = err.splitlines()
        for line in ("dry run -- nothing was built.", "  branch:    topic (would be checked out first)", "  --env:     CC=gcc (overrides the preset's)",
                     "  workspace: ws (box, present)", "  preset:    jsc-release (cmake --jsc-only --no-fatal-warnings --release)",
                     "  --cmake:   -DX=1 (added to the preset's)", "  passed on: --verbose (straight to build-webkit)"):
            self.assertIn(line, lines)
        self.assertEqual([l for l in lines if "running:" in l], ["  running:   " + FAR_LINE])
        self.assertEqual((self.w.recs().list(), self.w.watched(), self.w.spawned()), ([], [], []))


class TestKill(BuildTest):
    def test_nothing_running_says_so_and_is_0(self):
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, err.splitlines()), (0, ["no babysit is running in 'ws' -- 'wk status ws' says what it last did",
                                                      "no build is running in 'ws' -- 'wk status ws' says what it last did"]))

    def test_the_babysitter_is_stopped_before_the_build_and_both_read_cancelled(self):
        self.w.begin("babysit", pid=11)
        self.w.begin("build", pid=12)
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, err.splitlines()[-1]), (0, "  'wk build ws <preset>' resumes rather than starts over."))
        self.assertEqual((self.w.killed(), self.w.exits()), ([(11, 15), (12, 15)], {"babysit": "cancelled", "build": "cancelled"}))
        self.assertIn("==> stopping the build in 'ws' (pid 12 on here)", err.splitlines())

    def test_one_that_outlives_a_kill_is_refused_naming_it(self):
        self.w.begin("build", pid=12)
        self.w.stubborn.add(12)
        rc, _, err = self.typed("--kill")
        self.assertEqual((rc, said(err)), (1, "error: 'ws's build outlived a TERM and a KILL."))
        self.assertIn("warning: pid 12 did not stop on TERM after 2s -- killing it", err.splitlines())

    def test_a_dry_run_stops_nothing(self):
        self.w.begin("build", pid=12)
        rc, _, err = self.typed("--kill", "--dry-run")
        self.assertIn("dry run -- would TERM pid 12, KILL it after 2s, and record it cancelled", err.splitlines())
        self.assertEqual((rc, self.w.pids, self.w.exits()), (0, {12, os.getpid()}, {"build": ""}))

    def test_a_build_is_stopped_children_first_from_one_walk_of_its_tree(self):
        self.w.begin("build", pid=10)
        self.w.answer(["sh", "-c", TREE, "wk", "10"], out="12\n11\n10\n")
        self.assertEqual(self.typed("--kill")[0], 0)
        self.assertEqual(self.w.killed(), [(12, 15), (11, 15), (10, 15)])
        self.assertEqual(len([e for e in self.w.effects if e[:2] == ("run", ("sh", "-c", TREE, "wk", "10"))]), 1)

    def test_the_walk_finds_a_real_child_before_its_parent(self):
        parent = subprocess.Popen(["sh", "-c", "sleep 30 & wait"])
        self.addCleanup(parent.wait)
        self.addCleanup(parent.kill)
        for _ in range(50):
            child = subprocess.run(["pgrep", "-P", str(parent.pid)], capture_output=True, text=True).stdout.split()
            if child:
                break
            time.sleep(0.1)
        self.addCleanup(os.kill, int(child[0]), signal.SIGKILL)
        self.w.react(["sh", "-c"], lambda a, f: Result(0, subprocess.run(a, capture_output=True, text=True).stdout))
        self.w.begin("build", pid=parent.pid)
        self.typed("--kill")
        self.assertEqual(self.w.killed(), [(int(child[0]), 15), (parent.pid, 15)])


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
        w = Detaching(self.tmp)
        w.begin("build", pid=4000).end(1)
        rc, _, err = self.typed("jsc-release", "--detach", w=w)
        self.assertEqual((rc, err.splitlines()[0]), (0, "==> building jsc-release in 'ws', detached as pid 1001 -- this end can go away"))
        self.assertEqual(w.spawned(), [([WK, "build", "ws", "jsc-release"], os.path.join(w.ws_dir, "detached.log"))])
        self.assertLess(w.effects.index(("write", os.path.join(w.ws_dir, "detached.log"))), [e[0] for e in w.effects].index("spawn"))
        self.assertIn("  stop it: wk build ws --kill", err.splitlines())

    def test_a_child_that_ends_before_its_record_is_named_by_the_machines_answer(self):
        w = Detaching(self.tmp, starts=False)
        w.begin("build", pid=4000).end(0)
        with mock.patch("os.waitpid", side_effect=AssertionError("asked this process, not the machine")):
            rc, _, err = self.typed("jsc-release", "--detach", w=w)
        self.assertEqual((rc, said(err)), (1, "error: the detached build of 'ws' ended before it started -- what it said is"))

    def test_inside_a_workspace_the_name_is_not_passed_on(self):
        w = Detaching(self.tmp)
        w.in_ws = True
        self.typed("jsc-release", "--detach", "--", "-v", w=w)
        self.assertEqual([argv[1:] for argv, _ in w.spawned()], [["build", "jsc-release", "--", "-v"]])


class TestBabysitFront(BuildTest):
    def test_it_refuses_where_it_cannot_run(self):
        self.w.in_ws = True
        for w, why in ((self.w, "error: --babysit runs on the host, not in a workspace"),
                       (self.world("remote"), "error: refusing to babysit on a remote place: a shared machine has no sandbox for Claude"),
                       (self.world("local"), "error: already inside a workspace -- run the build and claude directly")):
            rc, _, err = self.typed("jsc-release", "--babysit", w=w)
            self.assertEqual((rc, said(err)), (1, why))

    def test_one_at_a_time_by_its_record(self):
        self.w.begin("babysit", pid=21)
        rc, _, err = self.typed("jsc-release", "--babysit")
        self.assertEqual((rc, said(err)), (1, "error: a babysitter is already running for 'ws' (pid 21)."))

    def test_it_detaches_the_loop_with_the_build_flags(self):
        self.w.env["WK_BABYSIT_MODEL"] = "sonnet"
        rc, _, err = self.typed("jsc-release", "--babysit", "--branch", "topic", "--", "-v")
        self.assertEqual((rc, err.splitlines()[0]), (0, "==> babysitter started for jsc-release in 'ws' (pid 1001, model sonnet, branch topic)"))
        self.assertEqual([argv[1:] for argv, _ in self.w.spawned()],
                         [["build", "ws", "jsc-release", "--branch", "topic", "--_babysit", "--", "-v"]])
        self.assertEqual(self.w.recs().list(), [])


class TestBabysitStates(BuildTest):
    def loop(self, builds, fixes=None, attempts="2"):
        """The babysitter's own run, its inner builds ending `builds` (rc, record exit) and its fixes `fixes`."""
        w = self.w = self.world()
        w.env["WK_BABYSIT_ATTEMPTS"] = attempts
        builds, fixes = list(builds), list(fixes or [])

        def inner(argv, f):
            rc, word = builds.pop(0)
            f.begin("build", pid=5000 + len(builds)).end(word)
            return Result(rc, "built\n")
        w.react(["env"], inner)
        w.react([WK, "ai", "claude", "ws"], lambda a, f: fixes.pop(0) if fixes else Result(0, "changed a.cpp\n"))
        return w

    def babysit(self, *more):
        rc, _, err = self.typed("jsc-release", "--_babysit", *more)
        (t,) = [t for t in self.w.recs().list() if t.field("kind") == "babysit"]
        return rc, t, self.w.files[os.path.join(self.w.ws_dir, "babysit.report")]

    def test_a_build_that_succeeds_on_its_own_ends_0(self):
        self.loop([(0, "0")])
        rc, t, report = self.babysit()
        self.assertEqual((rc, t.field("exit")), (0, "0"))
        self.assertIn("nothing to fix", report)
        (inner,) = [list(e[1]) for e in self.w.effects if e[0] == "run_tty" and e[1][0] == "env"]
        self.assertEqual(inner, ["env", "WK_TASK_PARENT=%s" % t.path, WK, "build", "ws", "jsc-release"])

    def test_a_fix_then_a_good_build_ends_0_after_one_fix(self):
        self.loop([(1, "1"), (0, "0")])
        rc, t, report = self.babysit()
        self.assertEqual((rc, t.field("exit"), t.steps()[:2]), (0, "0", [(1, "done"), (2, "running")]))
        self.assertIn("=== fix attempt 1 (exit 0)", report)

    def test_each_way_it_gives_up_is_its_own_verdict(self):
        """A stalled build, every fix used, a claude that did not run, a branch it cannot check out."""
        def no_checkout(w):
            w.react(["exec", "ws", "bash", "-c"], lambda a, f: Result(1))
            return ("--branch", "topic")
        for word, builds, fixes, setup, says in (
                ("stalled", [(1, "stalled")], None, None, "the build stalled"),
                ("gave-up", [(1, "1")] * 3, None, None, "still failing after 2 fix attempt(s)"),
                ("error", [(1, "1")], [Result(1, "", "no claude")], None, "claude did not run (exit 1)"),
                ("error", [], None, no_checkout, "could not check out branch 'topic'")):
            with self.subTest(word=word, says=says):
                w = self.loop(builds, fixes)
                rc, t, report = self.babysit(*(setup(w) if setup else ()))
                self.assertEqual((rc, t.field("exit")), (1, word))
                self.assertIn(says, report)

    def test_a_stopped_one_reads_cancelled_and_a_killed_one_died(self):
        self.w.react(["env"], lambda a, f: (os.kill(os.getpid(), signal.SIGTERM), Result(0))[1])
        rc, t, report = self.babysit()
        self.assertEqual((rc, t.field("exit")), (143, "cancelled"))
        self.assertIn("a person stopped the babysitter (wk build ws --kill)", report)
        w = self.world()
        w.begin("babysit", pid=31)
        w.pids.discard(31)
        os.environ.pop("WK_PRESET", None)
        rc, out, _ = self.wk(w, "status", "ws")
        self.assertIn("      ! babysit 'ws' died without recording an exit -- its log is %s" % w.log, out.splitlines())


if __name__ == "__main__":
    unittest.main()
