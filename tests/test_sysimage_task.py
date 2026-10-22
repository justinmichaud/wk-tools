"""`wk sysimage build` and `webkit` as a task through task.ContainerBuilder's one lifecycle: the conformance every
workspace builder (lib/wk/sysimage/buildroot.py, yocto.py) passes, then each builder's own stages, against a Fake world."""
import contextlib
import io
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeProc, FakeRegistry, WsDriver
from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import images, job, pgo, record, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from wk.machine import Result, isolated_module  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.sysimage import buildroot, task, yocto  # noqa: E402

SHA = "a" * 40
WS = "buildroot-wpewebkit-2.46-buildroot-rpi3-32"
TASK_LINE = "NOTE: Running task 7658 of 13213 (virtual:native:/w/sources/meta-clang/recipes-devtools/clang/clang_git.bb:do_compile)\n"


class Box(WsDriver):
    """The container place, holding the world's workspace once it is made."""

    def podman(self):
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def info(self, ws):
        return "running" if self.machine.made else "absent"


class World(Fake):
    """This machine holding one builder's image workspace on its container place `box`; a stage writes `out` to its log
    and exits `rc` after `polls` polls (for ever when None). A subclass is one builder and what the lifecycle expects of it."""

    BUILDER, PRESET, KIND = buildroot.Buildroot, "wpewebkit-2.46-buildroot-rpi3-32", "buildroot"
    OUT = b"wk-buildroot: building\nwk-buildroot: stage 'image' done\n"
    KILL = "wk sysimage build wpewebkit-2.46-buildroot-rpi3-32 --stop"
    DONE, STOPPED = "built wpewebkit-2.46-buildroot-rpi3-32 in", "recorded it as cancelled"
    DETACHED, DRY = "build of wpewebkit-2.46-buildroot-rpi3-32", ("  jobs         -j8 (memory-sized at 2048 MB/job)",)
    SUBJECT, UNKNOWN = "", (["--stage", "image"], ["--resume"])

    def __init__(self, tmp):
        super().__init__("here")
        self.ws = self.KIND + "-" + self.PRESET
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        store = self.tmp / "store"
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(store), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "1", "WK_AVAIL_MB": "65536",
                    "WK_CGROUP_CORES": "8", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2", "WK_ROOT": str(REPO)}
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.made, self.rc, self.polls, self.grow, self.interrupt, self.kind = True, 0, 0, None, None, "container"
        self.detaching, self.out = None, self.OUT
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.answer(["du", "-sh"], rc=1)
        self.answer(["podman", "image", "exists"])
        self.react(["podman", "container", "inspect"], lambda a, f: Result(0, self.tag() + "\n"))
        self.react(["env"], lambda a, f: (setattr(self, "made", True), Result(0))[1])
        self.react(["exec", self.ws, "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.answer(["sync-tools"])
        self.reg = FakeRegistry(self.env, self, lambda n, e: Box("box", str(REPO), self.env, self, self.kind), default=lambda: "box")
        self.ws_dir = os.path.join(str(store), "ws", self.ws)
        os.makedirs(os.path.join(self.ws_dir, "home"))
        self.log = os.path.join(self.ws_dir, "home", "%s-image.log" % self.KIND)

    @property
    def fake(self):
        return self

    def tag(self):
        return self.driver().host_image()[1]

    def preset(self):
        return images.load(self.PRESET, self.env)

    def start(self, argv, out, cwd=None):
        self.effect(("watch", tuple(argv)))
        out.write(self.out)
        return FakeProc(self.rc, self.polls, self.interrupt, self.grow)

    def spawn(self, argv, log):
        """With `detaching` set, the detached child begins its record (True) or dies before it does (False)."""
        pid = super().spawn(argv, log)
        if self.detaching:
            self.recs().begin(self.KIND, "here", self.ws, "k", self.log, ["a"], pid=pid)
        elif self.detaching is False:
            self.pids.discard(pid)
        return pid

    def driver(self, **preset):
        return self.BUILDER(self.reg, dict(self.preset(), **preset), self.PRESET, self.clock)

    def recs(self):
        return job.records_of(self.reg.load("box"), self.clock, self)

    def booking(self):
        """(jobs, MB) the record holding this machine's build budget names."""
        (t,) = [t for t in self.recs().list() if "build:here" in t.holds()]
        return int(t.field("jobs")), int(t.field("budget_mb"))

    def watched(self):
        (w,) = [e for e in self.effects if e[0] == "watch"]
        return list(w[1])

    def state(self):
        return [(t.field("kind"), t.field("exit"), t.holds()) for t in self.recs().list()]

    def running(self, stage="image", pid=77, kill=None, holds=None):
        t = self.recs().begin(self.KIND, "here", self.ws, kill or self.KILL, self.log, ["a"], pid=pid,
                              holds=["ws:" + self.ws] if holds is None else holds)
        self.pids.add(pid)
        return t

    def adopt(self, t, pid=777):
        """`t` as job.PidWatch leaves it: the stage's announced pid, killed with its tree by a TERM in the workspace."""
        t.set("pid_match", self.BUILDER.PATTERN)
        t.pid(pid)
        t.set("where", "place")
        self.pids.add(pid)
        self.answer(["exec", self.ws, "ps", "-o", "args=", "-p", str(pid)], out="python3 /opt/wk-tools/lib/wk/sysimage/%s_ws.py\n" % self.KIND)
        self.answer(["exec", self.ws, "sh", "-c"], out="%d\n%d\n" % (pid + 1, pid))
        self.react(["exec", self.ws, "kill", "-TERM"], lambda a, f: (f.pids.discard(pid), Result(0))[1])
        return t

    def plan(self):
        return ["the workspace '%s' on %s" % (self.ws, self.tag()), "sync wk-tools into '%s'" % self.ws, "build %s with -j8" % self.PRESET]

    def steps(self):
        return [(1, "done"), (2, "done"), (3, "running")]

    def tail(self):
        return ["image", self.PRESET, "--jobs", "8", "--wifi"]

    def booked(self):
        return 8, 16384


class TaskTest(unittest.TestCase):
    WORLD = World

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-sysimage-task-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_PLACE"):
            os.environ.pop(v, None)
        p = mock.patch.object(record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        self.w = self.WORLD(self.tmp)

    def fresh(self):
        self.w = self.WORLD(self.tmp)
        return self.w

    def build(self, *rest, verb="build", **preset):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = getattr(self.w.driver(**preset), verb)(list(rest))
        return rc, err.getvalue()

    def refused(self, *rest, status=1, verb="build", **preset):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused) as cm:
            getattr(self.w.driver(**preset), verb)(list(rest))
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class Lifecycle:
    """What every workspace builder's stage does as a task, run once per builder's World."""

    def test_a_stage_that_says_it_is_done_ends_ok_with_the_one_progress_record_and_its_budget(self):
        """`record.progress_shape[sysimage]`: step n of m, since when, the log, how to stop it."""
        w = self.w
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn(w.DONE, err)
        (t,) = w.recs().list()
        self.assertEqual((t.field("kind"), t.field("name"), t.field("stage"), t.field("exit")), (w.KIND, w.ws, "image", "0"))
        self.assertEqual((t.plan(), t.steps()), (w.plan(), w.steps()))
        self.assertEqual((t.field("log"), t.field("kill"), t.field("subject")), (w.log, w.KILL, w.SUBJECT))
        self.assertEqual(Path(w.log).read_bytes(), w.out)
        self.assertEqual((t.holds(), w.booking()), (["ws:" + w.ws, "build:here"], w.booked()))

    def test_the_stage_runs_its_preset_under_the_wrapper_in_the_workspace(self):
        self.build()
        argv = self.w.watched()
        head = ["exec", self.w.ws] + isolated_module("/opt/wk-tools/lib", "wk.sysimage.task") + ["stage", self.w.KIND, "--", "python3",
                                                                                                 "/opt/wk-tools/lib/wk/sysimage/%s_ws.py" % self.w.KIND]
        self.assertEqual(argv[:len(head)], head)
        tail = self.w.tail()
        self.assertEqual([a for a in argv[len(head):] if a in tail], tail)

    def test_done_is_the_wrapper_s_marker_and_a_failure_ends_with_its_status_and_last_lines(self):
        self.w.out = b"make: Nothing to be done\n"
        self.assertIn("it exited 0 and never said it was done", self.refused())
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")
        self.fresh().out, self.w.rc = b"package foo failed\nwk: error: it failed.\n", 2
        err = self.refused(status=1)
        self.assertIn("the image build in '%s' failed. Last lines:\n    package foo failed" % self.w.ws, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "2")

    def test_a_workspace_that_is_not_there_is_made_from_the_host_image_and_one_from_another_is_refused(self):
        self.w.made = False
        self.assertEqual(self.build()[0], 0)
        (new,) = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:1] == ("env",)]
        self.assertEqual(list(new[1]), ["env", "WK_SDK_IMAGE=" + self.w.tag(), str(REPO / "wk"), "new", self.w.ws, "--on", "box"])
        self.fresh().answer(["podman", "container", "inspect"], out="localhost/wk-host:old\n")
        err = self.refused()
        self.assertIn("was made from localhost/wk-host:old", err)
        self.assertIn("wk rm %s && wk sysimage build %s" % (self.w.ws, self.w.PRESET), err)

    def test_the_host_image_is_tagged_by_its_base_and_its_containerfile_and_its_variable_names_another(self):
        b = self.w.BUILDER
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, b.BASE_IMAGE)
        self.assertRegex(tag, r"^localhost/wk-%s-host:%s-[0-9a-f]{8}$" % (self.w.KIND, re.escape(b.BASE_IMAGE.rsplit(":", 1)[-1])))
        self.w.env[b.BASE_VAR] = "docker.io/library/debian:13"
        self.assertEqual(self.w.driver().host_image()[0], "docker.io/library/debian:13")

    def test_what_holds_the_workspace_is_refused_naming_the_stop(self):
        self.w.running(kill="wk sysimage build x --stop")
        err = self.refused()
        self.assertIn(self.w.ws, err)
        self.assertIn("wk sysimage build x --stop", err)

    def test_another_build_on_this_machine_is_a_barrier_and_too_little_disk_another(self):
        self.w.recs().begin("build", "here", "other", "k", "/l", ["x"], pid=66, holds=["build:here"], fields={"jobs": 4})
        self.w.pids.add(66)
        self.assertIn("is already building", self.refused(status=75))
        self.assertEqual(len(self.w.recs().list()), 1)
        self.fresh().answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 1048576 1% /\n")
        self.assertIn("1 GB free", self.refused())
        self.assertEqual(self.w.recs().list(), [])

    def test_a_place_that_is_not_a_container_or_another_builder_s_option_is_refused(self):
        for args in self.w.UNKNOWN + (["--dry-run"],):
            with self.subTest(args[0]):
                self.assertIn("%s is not an option of this build" % args[0], self.refused(*args))
        self.w.kind = "remote"
        self.assertIn("%s, and place 'box' is a remote one" % self.w.BUILDER.NEEDS, self.refused())

    def test_an_interrupt_stops_the_stage_where_it_runs_and_the_record_reads_cancelled(self):
        """`unit machine.interrupt_stops_remote_process[image build]`: the stage's announced pid is TERMed in the workspace."""
        w, real = self.w, record.Records.begin
        w.polls, w.interrupt = None, signal.SIGINT
        with mock.patch.object(record.Records, "begin", lambda recs, *a, **kw: w.adopt(real(recs, *a, **kw))):
            self.assertIn("interrupted -- stopping the image build in '%s'" % w.ws, self.refused(status=130))
        self.assertIn(("run", ("exec", w.ws, "kill", "-TERM", "778", "777")), w.effects)
        self.assertEqual(w.recs().list()[0].field("exit"), "cancelled")

    def test_stop_kills_the_tree_in_the_workspace_and_one_that_outlives_a_kill_is_named(self):
        w = self.w
        t = w.adopt(w.running(pid=1))
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("exec", w.ws, "kill", "-TERM", "778", "777")), w.effects)
        self.assertIn(w.STOPPED, err)
        self.assertEqual(t.field("exit"), "cancelled")
        t = w.running(pid=88)
        with mock.patch.object(w, "kill", return_value=True):
            self.assertIn("outlived a TERM and a KILL", self.refused("--stop"))

    def test_detached_it_returns_once_its_own_child_has_begun_its_record(self):
        w = self.w
        w.detaching = True
        rc, err = self.build("--detach")
        self.assertEqual(rc, 0, err)
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual((list(sp[1]), sp[2]), ([str(REPO / "wk"), "sysimage", "build", w.PRESET], os.path.join(w.ws_dir, "detached-image.log")))
        self.assertIn("running detached in '%s' as pid 1001 -- this end can go away" % w.ws, err)
        self.assertFalse([e for e in w.effects if e[0] == "watch"])
        self.fresh().detaching = False
        self.assertIn("the detached %s of '%s' ended before it started" % (w.DETACHED, w.ws), self.refused("--detach"))

    def test_a_dry_run_reports_the_plan_and_changes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        for line in ("would build image %s (builder: %s)" % (self.w.PRESET, self.w.KIND), "dry run -- nothing was built.") + self.w.DRY:
            self.assertIn(line, err)
        self.assertEqual((self.w.recs().list(), [e for e in self.w.effects if e[0] != "run"]), ([], []))

    def test_a_build_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sysimage build]`: each run its own record, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.driver().build([])
        converges(self, lambda: self.WORLD(self.tmp), run_once, self.WORLD.state)


class TestTheBuildrootLifecycle(Lifecycle, TaskTest):
    def test_a_silent_stage_is_killed_by_its_watchdog_and_ends_stalled(self):
        self.w.polls, self.w.out = None, b""
        self.w.env.update({"WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30", "WK_POLL_SECONDS": "10"})
        err = self.refused()
        self.assertIn("giving up and killing the job", err)
        self.assertIn("the image build in '%s' stalled" % self.w.ws, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_a_pinned_kernel_is_prepared_here_and_handed_over_through_the_download_cache(self):
        self.assertIn("a kernel by URL alone is not pinned", self.refused(BR_KERNEL_DEB_URL="https://x/k.deb"))
        self.w.answer(["sha256sum"], out="d" * 64 + "  x\n")
        self.w.answer(["curl"])
        self.w.answer(["mv"])
        dl = os.path.join(self.w.env["WK_STORE"], "cache", "buildroot", "dl")

        def pin(m, deb, release, out):
            m.act_run(["kernel_pin", deb, release, out])
            return os.path.join(out, "wk-kernel-%s.tar" % release)
        with mock.patch.object(buildroot, "kernel_pin", pin):
            rc, err = self.build(BR_KERNEL_DEB_URL="https://x/k.deb", BR_KERNEL_DEB_SHA256="d" * 64, BR_KERNEL_RELEASE="6.1.0-rpi")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.w.recs().list()[-1].plan()[0], "prepare the pinned kernel 6.1.0-rpi")
        argv = self.w.watched()
        self.assertEqual(argv[argv.index("--kernel-tar") + 1:argv.index("--kernel-tar") + 4],
                         ["/cache/buildroot/dl/wk-kernel-6.1.0-rpi.tar", "--kernel-dts", "bcm2710-rpi-3-b"])
        self.assertIn(("run", ("kernel_pin", os.path.join(task.cache_dir(self.w.env), "k.deb"), "6.1.0-rpi", dl)), self.w.effects)

    def test_a_slot_is_its_own_stage_checked_first_and_ends_on_its_manifest(self):
        w = self.w
        self.assertIn("usage: wk sysimage webkit", self.refused("--slot", "base", verb="webkit"))
        self.assertIn("40 hex digits", self.refused("--commit", "abc", "--slot", "base", verb="webkit"))
        self.assertIn("has no finished image", self.refused("--commit", SHA, "--slot", "base", verb="webkit"))
        self.assertEqual(w.recs().list(), [])
        w.files[os.path.join(w.ws_dir, "build", "buildroot", w.PRESET, "output", "images", "sdcard.img")] = ""
        w.out = b"wk-buildroot: stage 'webkit-base' done\n"
        slotdir = images.slot_dir(w.ws, "base", w.env)
        self.assertIn("left no %s/slot.json" % slotdir, self.refused("--commit", SHA, "--slot", "base", verb="webkit"))
        real = w.start
        w.start = lambda *a, **kw: (w.files.__setitem__(os.path.join(slotdir, "slot.json"), "{}"), real(*a, **kw))[1]
        rc, err = self.build("--commit", SHA, "--slot", "base", verb="webkit")
        self.assertEqual(rc, 0, err)
        t = w.recs().list()[-1]
        self.assertEqual((t.field("stage"), t.field("exit"), t.field("log")),
                         ("webkit-base", "0", os.path.join(w.ws_dir, "home", "buildroot-webkit-base.log")))
        self.assertIn("slot 'base' of %s holds aaaaaaaaaaaa" % w.PRESET, err)


class YoctoWorld(World):
    """The workspace on the preset's branch, its targets.conf holding the preset's section."""

    BUILDER, PRESET, KIND = yocto.Yocto, "wpewebkit-2.46-yocto-rpi4-64", "yocto"
    OUT = b"wk-yocto: syncing\nwk-yocto: stage 'image' done\n"
    KILL = "wk sysimage build wpewebkit-2.46-yocto-rpi4-64 --stage image --stop"
    DONE, STOPPED = "built wpewebkit-2.46-yocto-rpi4-64-", "restarting resumes"
    DETACHED = "'image' stage of wpewebkit-2.46-yocto-rpi4-64"
    DRY = ("  cross-target rpi4-64bits-mesa  (verified on wpe-2.46)", "  wifi         wk-wifi-join; the card carries the credential")
    SUBJECT, UNKNOWN = "image stage of yocto-wpewebkit-2.46-yocto-rpi4-64", (["--resume"], ["--keep-work"])

    def __init__(self, tmp):
        super().__init__(tmp)
        self.env.update({"WK_POLL_SECONDS": "10", "WK_STALL_SECONDS": "30", "WK_HEARTBEAT_SECONDS": "60"})
        self.answer(["nproc"], out="12\n")
        self.answer(["sysctl", "-n", "hw.ncpu"], out="12\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="%d\n" % (32 << 30))
        self.files["/proc/meminfo"] = "MemTotal: %d kB\nMemAvailable: %d kB\n" % (32 << 20, 30 << 20)
        self.head, self.checkout_rc = "wpe-2.46\r\n", 0
        self.sections = Result(0, "[rpi3-32bits-mesa]\n[rpi4-64bits-mesa]\n")
        self.react(["exec", self.ws, "bash", "-c"], lambda a, f: Result(0, self.head) if "rev-parse --abbrev-ref" in a[4]
                   else self.sections if a[4].startswith("cat ") else Result(self.checkout_rc))
        self.lock = os.path.join(yocto.host_workdir(self.ws_dir, "rpi4-64bits-mesa"), "build", "bitbake.lock")

    def running(self, stage="image", pid=77, kill=None, holds=None):
        t = self.recs().begin("yocto", "here", self.ws, kill or "wk sysimage build %s --stage %s --stop" % (self.PRESET, stage), self.log,
                              list(yocto.STAGES), pid=pid, holds=["ws:" + self.ws] if holds is None else holds)
        t.step_state(yocto.stage_index(stage), "running")
        self.pids.add(pid)
        return t

    def plan(self):
        return list(yocto.STAGES)

    def steps(self):
        return [(1, "pending"), (2, "pending"), (3, "running"), (4, "pending"), (5, "pending"), (6, "pending")]

    def tail(self):
        return [self.PRESET, "--stage", "image", "--jobs", str(resources.Resources(self, self.env).envelope_cores()),
                "--cross-preset", "wpe-cross"]

    def booked(self):
        argv = self.watched()
        return resources.Resources(self, self.env).envelope_cores(), int(argv[argv.index("--mem-budget") + 1])


class TestTheYoctoLifecycle(Lifecycle, TaskTest):
    WORLD = YoctoWorld

    def test_stages_budgets_and_subjects(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            yocto.stage_index("all")
        self.assertTrue(all("      %s " % s in err.getvalue() for s in yocto.STAGES))
        self.assertEqual([yocto.stage_budget(s, 79, 113000, 8) for s in ("image", "webkit", "pgo-mix")],
                         [(79, 113000), (8, 8 * yocto.WEBKIT_MB_PER_JOB), (1, yocto.WEBKIT_MB_PER_JOB)])
        self.assertEqual([yocto.disk_need(s, {"WK_BUILD_DISK_GB": "30"}) for s in ("image", "webkit", "pgo-mix")], [60, 30, 2])
        ws = "yocto-p"
        self.assertNotEqual(yocto.build_subject(ws, "webkit", "b-instr", SHA, pgo.COLLECT), yocto.build_subject(ws, "webkit", "b", SHA, pgo.USE))
        self.assertEqual([yocto.build_subject(ws, s, "s", SHA, "") for s in ("image", "pgo-mix")],
                         ["image stage of yocto-p", "mixing slot s's collection in yocto-p"])

    def test_the_record_holds_no_deadline_and_a_slot_books_its_own_jobs(self):
        self.w.env["WK_ABORT_SECONDS"] = "60"
        self.w.out = b"wk-yocto: stage 'webkit' done\n"
        rc, err = self.build("--commit", SHA, "--slot", "base", "--preset", pgo.COLLECT, verb="webkit")
        self.assertEqual(rc, 0, err)
        argv, (t,) = self.w.watched(), self.w.recs().list()
        self.assertEqual([argv[argv.index(k) + 1] for k in ("--stage", "--commit", "--slot", "--cross-preset")], ["webkit", SHA, "base", pgo.COLLECT])
        jobs = int(argv[argv.index("--webkit-jobs") + 1])
        self.assertEqual(self.w.booking(), (jobs, jobs * yocto.WEBKIT_MB_PER_JOB))
        self.assertEqual(t.field("abort_after"), "")
        self.assertIn("slot base in %s at aaaaaaaaaaaa" % self.w.ws, t.field("subject"))

    def test_a_slot_s_arguments_belong_to_the_webkit_stage(self):
        for args, said in ((("--commit", SHA, "--slot", "s"), "belong to the webkit stage"), (("--stage", "webkit", "--slot", "s"), "needs both --commit"),
                           (("--stage", "pgo-mix",), "--slot <name>"), (("--stage", "pgo-mix", "--slot", "s", "--commit", SHA), "--commit builds"),
                           (("--preset", pgo.COLLECT), "builds no WebKit"), (("--stage", "webkit", "--preset", pgo.USE), "needs --pgo-profile")):
            self.assertIn(said, self.refused(*args))

    def test_the_workspace_is_put_on_the_preset_s_branch_from_the_mirror_and_its_target_checked(self):
        self.build()
        self.assertFalse([e for e in self.w.effects if e[0] == "run" and "git checkout -q" in " ".join(e[1])])
        self.w.head = "main\n"
        self.assertEqual(self.build()[0], 0)
        (co,) = [e for e in self.w.effects if e[0] == "run" and "git checkout -q" in " ".join(e[1])]
        self.assertIn("git fetch -q wpe wpe-2.46:wpe-2.46", co[1][-1])
        self.w.checkout_rc = 1
        self.assertIn("could not check out 'wpe-2.46' from 'wpe'", self.refused())
        self.w.head, self.w.sections = "wpe-2.46\n", Result(0, "[rpi3-32bits-mesa]\n")
        err = self.refused()
        self.assertIn("wpe-2.46 has no [rpi4-64bits-mesa] section", err)
        self.assertIn("      rpi3-32bits-mesa\n", err)
        self.w.sections = Result(1)
        self.assertIn("could not read Tools/yocto/targets.conf", self.refused())
        self.assertEqual(self.build(YOC_PORT_TARGET_FROM="rpi3-32bits-mesa", YOC_MACHINE="raspberrypi4-64")[0], 0)

    def test_a_running_stage_is_refused_by_name(self):
        t = self.w.running("toolchain")
        err = self.refused("--stage", "fetch")
        self.assertIn("a 'toolchain' build is already running in '%s'" % self.w.ws, err)
        self.assertIn("Stop it:    wk sysimage build %s --stage toolchain --stop" % self.w.PRESET, err)
        self.assertEqual(t.field("exit"), "")

    def test_a_silent_stage_is_reported_not_killed_and_one_wedged_on_a_task_is_given_up_on(self):
        self.w.out, self.w.polls, self.w.rc = b"", 600, 1
        self.w.env["WK_ABORT_SECONDS"] = "60"
        err = self.refused()
        self.assertIn("not stopping it: silence is not a failure here", err)
        self.assertFalse([e for e in self.w.effects if e[0] == "kill"])
        self.w.out, self.w.polls, self.w.rc = b"", None, 0
        self.w.grow = lambda: Path(self.w.log).open("a").write(TASK_LINE)
        self.w.answer(["sh", "-c", job.TREE, "wk", "4242"], out="4242\n")
        with mock.patch.object(yocto, "WEDGE_BEATS", 3):
            err = self.refused()
        self.assertIn("wedged: the log has named clang_git.bb:do_compile for 180s", err)
        self.assertIn(("kill", 4242, 15), self.w.effects)
        self.assertEqual(self.w.recs().list()[-1].field("exit"), "stalled")

    def test_the_task_a_log_names_is_the_last_one(self):
        log = self.tmp / "b.log"
        log.write_text("NOTE: recipe zlib-1.3-r0: task do_fetch: Started\n" + TASK_LINE + "WARNING: x\n")
        self.assertEqual(yocto.task_named(str(log)), "clang_git.bb:do_compile")
        log.write_text(TASK_LINE + "NOTE: recipe zlib-1.3-r0: task do_fetch: Started\n")
        self.assertEqual(yocto.task_named(str(log)), "zlib-1.3-r0:do_fetch")
        log.write_text("wk-yocto: syncing\n")
        self.assertEqual(yocto.task_named(str(log)), "")

    def cooker(self, args="python3 /w/sources/poky/bitbake/bin/bitbake-server decafbad\n"):
        self.w.files[self.w.lock] = "555\n"
        self.w.answer(["exec", self.w.ws, "ps", "-o", "args=", "-p", "555"], out=args)
        self.w.answer(["exec", self.w.ws, "sh", "-c", job.TREE, "wk", "555"], out="556\n555\n")

    def test_stop_touches_only_its_own_stage_and_a_cooker_no_record_holds(self):
        w = self.w
        t = w.adopt(w.running("toolchain", pid=1))
        rc, err = self.build("--stop")
        self.assertIn("no 'image' build is running in '%s'; its 'toolchain' stage is." % w.ws, err)
        self.assertEqual(t.field("exit"), "")
        self.cooker("/usr/sbin/sshd -D\n")
        t.end(0)
        self.assertEqual(self.build("--stop")[0], 0)
        self.assertFalse([e for e in w.effects if e[1][2:4] in (("kill", "-TERM"), ("kill", "-KILL"))])
        self.cooker()
        w.react(["exec", w.ws, "kill", "-TERM"], lambda a, f: (f.answer(["exec", w.ws, "ps", "-o", "args=", "-p", "555"]), Result(0))[1])
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("exec", w.ws, "kill", "-TERM", "556", "555")), w.effects)
        self.assertIn("the cooker is gone", err)

    def test_a_cooker_that_outlives_a_term_and_a_kill_is_named_and_no_workspace_is_refused(self):
        self.cooker()
        self.w.answer(["exec", self.w.ws, "kill"])
        rc, err = self.build("--stop")
        self.assertEqual(rc, 1)
        self.assertIn(("run", ("exec", self.w.ws, "kill", "-KILL", "556", "555")), self.w.effects)
        self.assertIn("wk stop %s" % self.w.ws, err)
        self.w.made = False
        self.assertIn("no workspace '%s', so nothing is building" % self.w.ws, self.refused("--stop"))

    def test_a_dry_run_of_the_mix_names_the_collection_on_both_sides(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build("--stage", "pgo-mix", "--slot", "pr")
        self.assertEqual(rc, 0, err)
        self.assertIn("  collection   %s" % images.pgo_dir(self.w.ws, "pr", self.w.env), err)
        self.assertIn("  into         " + pgo.profile_path("pr"), err)
        self.assertIn("dry run -- nothing was mixed.", err)


class TestTheWrapperInTheWorkspace(unittest.TestCase):
    def test_it_announces_the_pid_it_execs_under_and_declares_a_wk_build(self):
        with mock.patch("os.execvpe") as ex, contextlib.redirect_stderr(io.StringIO()) as err:
            task.stage_main("buildroot", ["/x/build.sh", "--name", "p"], {"PATH": "/opt/wk-tools/container/bin:/usr/bin", "A": "1"})
        self.assertEqual(err.getvalue(), "wk: buildroot pid %d\n" % os.getpid())
        argv0, argv, env = ex.call_args[0]
        self.assertEqual((argv0, argv, env["PATH"], env["WK_BUILD"], env["A"]), ("/x/build.sh", ["/x/build.sh", "--name", "p"], "/usr/bin", "1", "1"))
        with tempfile.NamedTemporaryFile("w", suffix=".log") as f:
            f.write("wk: buildroot pid 4321\n")
            f.flush()
            self.assertEqual(job.announced_pid(f.name, "buildroot"), 4321)

    def test_it_runs_in_the_workspace_as_a_module(self):
        cp = subprocess.run([sys.executable, "-m", "wk.sysimage.task", "stage", "buildroot", "--", "sh", "-c", "echo $WK_BUILD"],
                            env=dict(os.environ, PYTHONPATH=str(REPO / "lib")), capture_output=True, text=True, timeout=30)
        self.assertEqual((cp.returncode, cp.stdout), (0, "1\n"), cp.stderr)


class TestFetchPinned(TaskTest):
    URL, PIN = "https://example.org/img.xz", "b" * 64

    def fetch(self):
        dest = os.path.join(task.cache_dir(self.w.env), "img.xz")
        with contextlib.redirect_stderr(io.StringIO()):
            return dest, task.fetch_pinned(self.w, self.URL, dest, self.PIN)

    def test_a_kept_download_matching_its_pin_is_not_fetched_again(self):
        self.w.files[os.path.join(task.cache_dir(self.w.env), "img.xz")] = "x"
        self.w.answer(["sha256sum"], out=self.PIN + "  x\n")
        self.assertEqual(self.fetch()[1], "")
        self.assertFalse([e for e in self.w.effects if e[0] == "run" and e[1][0] == "curl"])

    def test_one_that_does_not_is_resumed_and_checked_before_it_is_kept_or_refused(self):
        self.w.answer(["curl"])
        self.w.answer(["mv"])
        self.w.answer(["sha256sum"], out=self.PIN + "  x\n")
        dest, why = self.fetch()
        self.assertEqual(why, "")
        runs = [e[1] for e in self.w.effects if e[0] == "run"]
        self.assertIn(("curl", "-fsSL", "--retry", "5", "-C", "-", "-o", dest + ".part", self.URL), runs)
        self.assertEqual(runs[-1], ("mv", dest + ".part", dest))
        self.w.answer(["sha256sum"], out="c" * 64 + "  x\n")
        self.assertIn("does not match its pin (sha256 %s, expected %s)" % ("c" * 64, self.PIN), self.fetch()[1])
        self.assertIn(("run", ("rm", "-f", dest + ".part")), self.w.effects)


if __name__ == "__main__":
    unittest.main()
