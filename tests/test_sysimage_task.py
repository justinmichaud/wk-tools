"""`wk sysimage build` and `webkit` for a buildroot image as a task (lib/wk/sysimage/task.py, the base every
workspace builder shares, and lib/wk/sysimage/buildroot.py) against a Fake world."""
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
from wk import images, job, record  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result, isolated_module  # noqa: E402
from wk.sysimage import buildroot, task  # noqa: E402

PRESET = "wpewebkit-2.46-buildroot-rpi3-32"
WS = "buildroot-" + PRESET
SHA = "a" * 40


class Box(WsDriver):
    """The container place, holding `WS` once the world has made it."""

    def podman(self):
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def info(self, ws):
        return "running" if self.machine.made else "absent"


class World(Fake):
    """This machine holding `WS` on its container place `box`: the image and the workspace exist unless `made` is
    False, and a stage writes `out` to its log and exits `rc` after `polls` polls (for ever when None).
    A subclass is one builder: its class, preset and what the shared lifecycle tests expect of it."""

    BUILDER, PRESET, KIND = buildroot.Buildroot, PRESET, "buildroot"
    OUT = b"wk-buildroot: building\nwk-buildroot: stage 'image' done\n"
    KILL = "wk sysimage build %s --stop" % PRESET
    DONE, IDLE, STOPPED = "built %s in '%s'" % (PRESET, WS), "no buildroot is running in '%s'" % WS, \
        "stopped '%s's buildroot and recorded it as cancelled" % WS
    DETACHED, DRY = "build of " + PRESET, ("  jobs         -j8 (memory-sized at 2048 MB/job)",)
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
        self.react(["env"], self._new)
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

    def _new(self, argv, f):
        self.made = True
        return Result(0)

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

    def driver(self):
        return self.BUILDER(self.reg, self.preset(), self.PRESET, self.clock)

    def recs(self):
        return job.records_of(self.reg.load("box"), self.clock, self)

    def budget_files(self):
        d = os.path.join(self.env["XDG_STATE_HOME"], "wk", "builds")
        return sorted(p for p in self.files if p.startswith(d + "/"))

    def watched(self):
        (w,) = [e for e in self.effects if e[0] == "watch"]
        return list(w[1])

    def state(self):
        return ([(t.field("kind"), t.field("exit")) for t in self.recs().list()], len(self.budget_files()))

    def running(self, stage="image", pid=77, kill=None):
        t = self.recs().begin(self.KIND, "here", self.ws, kill or self.KILL, self.log, ["a"], pid=pid)
        self.pids.add(pid)
        return t

    def plan(self):
        return ["the workspace '%s' on %s" % (WS, self.tag()), "sync wk-tools into '%s'" % WS, "build %s with -j8" % PRESET]

    def steps(self):
        return [(1, "done"), (2, "done"), (3, "running")]

    def flags(self):
        return {"/opt/wk-tools/lib/wk/sysimage/buildroot_ws.py": "image", "--name": PRESET, "--overlay-wifi": "1", "--jobs": "8"}

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

    def build(self, *rest):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.w.driver().build(list(rest))
        return rc, err.getvalue()

    def refused(self, *rest, status=1, verb="build"):
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                getattr(self.w.driver(), verb)(list(rest))
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class Lifecycle:
    """What every workspace builder's stage does as a task, run once per builder's World."""

    def test_a_stage_that_says_it_is_done_ends_ok_with_the_one_progress_record(self):
        """`record.progress_shape[sysimage]`: step n of m, since when, the log, how to stop it."""
        w = self.w
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn(w.DONE, err)
        (t,) = w.recs().list()
        self.assertEqual((t.field("kind"), t.field("name"), t.field("stage"), t.field("exit")), (w.KIND, w.ws, "image", "0"))
        self.assertEqual((t.plan(), t.steps()), (w.plan(), w.steps()))
        self.assertEqual((t.field("log"), t.field("kill"), t.field("subject")), (w.log, w.KILL, w.SUBJECT))
        self.assertTrue(t.field("started"))
        self.assertEqual(Path(w.log).read_bytes(), w.out)

    def test_the_stage_runs_under_the_wrapper_in_the_workspace(self):
        self.build()
        argv = self.w.watched()
        self.assertEqual(argv[:13], ["exec", self.w.ws] + isolated_module("/opt/wk-tools/lib", "wk.sysimage.task")
                         + ["stage", self.w.KIND, "--", "python3", "/opt/wk-tools/lib/wk/sysimage/%s_ws.py" % self.w.KIND])
        for flag, value in self.w.flags().items():
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertNotIn("--slot", argv)

    def test_the_budget_is_on_the_books_under_this_driver(self):
        self.build()
        (f,) = self.w.budget_files()
        self.assertIn("label=wk sysimage image %s\n" % self.w.ws, self.w.files[f])
        self.assertIn("\njobs=%d\nbudget_mb=%d\nholder=pid:%d\n" % (self.w.booked() + (os.getpid(),)), self.w.files[f])

    def test_done_is_the_wrapper_s_marker_not_the_exit_status(self):
        self.w.out = b"make: Nothing to be done\n"
        self.assertIn("it exited 0 and never said it was done", self.refused())
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_a_failure_ends_with_its_status_and_its_last_lines(self):
        self.w.out, self.w.rc = b"package foo failed\nwk: error: it failed.\n", 2
        err = self.refused()
        self.assertIn("the image build in '%s' failed. Last lines:" % self.w.ws, err)
        self.assertIn("    package foo failed", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "2")

    def test_a_workspace_that_is_not_there_is_made_from_the_host_image(self):
        self.w.made = False
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        (new,) = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:1] == ("env",)]
        self.assertEqual(list(new[1]), ["env", "WK_SDK_IMAGE=" + self.w.tag(), str(REPO / "wk"), "new", self.w.ws, "--on", "box"])

    def test_a_workspace_made_from_another_image_is_refused_naming_the_remake(self):
        self.w.answer(["podman", "container", "inspect"], out="localhost/wk-host:old\n")
        err = self.refused()
        self.assertIn("was made from localhost/wk-host:old", err)
        self.assertIn("wk rm %s && wk sysimage build %s" % (self.w.ws, self.w.PRESET), err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_the_host_image_is_tagged_by_its_base_and_its_containerfile_and_its_variable_names_another(self):
        b = self.w.BUILDER
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, b.BASE_IMAGE)
        self.assertRegex(tag, r"^localhost/wk-%s-host:%s-[0-9a-f]{8}$" % (self.w.KIND, re.escape(b.BASE_IMAGE.rsplit(":", 1)[-1])))
        self.w.env[b.BASE_VAR] = "docker.io/library/debian:13"
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, "docker.io/library/debian:13")
        self.assertIn(":13-", tag)

    def test_a_pid_file_a_killed_build_left_is_not_busy_and_a_live_one_is(self):
        home = os.path.join(self.w.ws_dir, "home")
        self.w.dirs.add(home)
        self.w.files[os.path.join(home, "%s-image.pid" % self.w.KIND)] = "99999\n"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.w.pids.add(99999)
        self.assertIn("%s-image (pid 99999 in the workspace)" % self.w.KIND, self.refused())

    def test_a_held_workspace_lock_is_refused_naming_the_stop(self):
        with mock.patch("wk.lock.Lock.holder_pid", return_value=55):
            self.w.pids.add(55)
            err = self.refused()
        self.assertIn("ws-%s" % self.w.ws, err)
        self.assertIn("Stop it:    " + self.w.KILL, err)

    def test_another_build_on_this_machine_is_a_barrier(self):
        from wk.resources import Budget
        Budget(self.w, self.w.env, self.w.clock).record("wk build other", 4, 8192, "pid:66")
        self.w.pids.add(66)
        with mock.patch.object(job, "holder_alive", return_value=lambda h: True):
            self.assertIn("is already building", self.refused(status=75))
        self.assertEqual(self.w.recs().list(), [])

    def test_a_target_that_is_not_a_container_is_refused(self):
        self.w.kind = "remote"
        self.assertIn("%s, and place 'box' is a remote one" % self.w.BUILDER.NEEDS, self.refused())

    def test_an_unknown_or_another_builder_s_option_is_a_usage_error(self):
        for args in self.w.UNKNOWN:
            with self.subTest(args[0]):
                self.assertIn("%s is not an option of this build" % args[0], self.refused(*args))

    def test_an_interrupt_stops_the_stage_where_it_runs_and_the_record_reads_cancelled(self):
        """`unit machine.interrupt_stops_remote_process[image build]`: the stage's announced pid is TERMed in the workspace."""
        w, real = self.w, record.Records.begin
        w.polls, w.interrupt = None, signal.SIGINT

        def begin(recs, *a, **kw):
            t = real(recs, *a, **kw)
            t.set("pid_match", w.BUILDER.PATTERN)
            t.pid(777)
            t.set("where", "place")
            return t
        w.pids.add(777)
        w.answer(["exec", w.ws, "ps", "-o", "args=", "-p", "777"], out="python3 /opt/wk-tools/lib/wk/sysimage/%s_ws.py\n" % w.KIND)
        w.react(["exec", w.ws, "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        with mock.patch.object(record.Records, "begin", begin):
            self.assertIn("interrupted -- stopping the image build in '%s'" % w.ws, self.refused(status=130))
        self.assertIn(("run", ("exec", w.ws, "kill", "-TERM", "777")), w.effects)
        self.assertEqual(w.recs().list()[0].field("exit"), "cancelled")

    def test_stop_kills_the_tree_in_the_workspace_and_reports_once_it_is_gone(self):
        w = self.w
        t = w.running(pid=1)
        t.set("pid_match", w.BUILDER.PATTERN)
        t.pid(777)
        t.set("where", "place")
        w.pids.add(777)
        w.answer(["exec", w.ws, "ps", "-o", "args=", "-p", "777"], out="python3 /opt/wk-tools/lib/wk/sysimage/%s_ws.py\n" % w.KIND)
        w.answer(["exec", w.ws, "sh", "-c"], out="778\n777\n")
        w.react(["exec", w.ws, "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("exec", w.ws, "kill", "-TERM", "778", "777")), w.effects)
        self.assertIn(w.STOPPED, err)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_one_that_outlives_a_kill_is_refused_naming_it(self):
        t = self.w.running(pid=88)
        with mock.patch.object(self.w, "kill", return_value=True):
            self.assertIn("outlived a TERM and a KILL", self.refused("--stop"))
        self.assertEqual(t.field("exit"), "cancelled")

    def test_nothing_running_says_so(self):
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0)
        self.assertIn(self.w.IDLE, err)

    def test_detached_it_returns_once_its_own_child_has_begun_its_record(self):
        w = self.w
        w.detaching = True
        rc, err = self.build("--detach")
        self.assertEqual(rc, 0, err)
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual((list(sp[1]), sp[2]), ([str(REPO / "wk"), "sysimage", "build", w.PRESET],
                                                os.path.join(w.ws_dir, "detached-image.log")))
        self.assertIn("running detached in '%s' as pid 1001 -- this end can go away" % w.ws, err)
        self.assertIn("  follow:  wk status %s --log -f" % w.ws, err)
        self.assertFalse([e for e in w.effects if e[0] == "watch"])

    def test_a_detached_child_that_ends_before_its_record_is_named(self):
        self.w.detaching = False
        self.assertIn("the detached %s of '%s' ended before it started" % (self.w.DETACHED, self.w.ws), self.refused("--detach"))

    def test_a_dry_run_reports_the_plan_and_changes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        for line in ("would build image %s (builder: %s)" % (self.w.PRESET, self.w.KIND),) + self.w.DRY:
            self.assertIn(line, err)
        self.assertEqual(self.w.recs().list(), [])
        self.assertEqual([e for e in self.w.effects if e[0] != "run"], [])

    def test_a_dry_run_arrives_only_as_the_dispatcher_s_global(self):
        self.assertIn("--dry-run is not an option of this build", self.refused("--dry-run"))
        self.assertIsNone(os.environ.get("WK_DRY_RUN"))
        self.assertEqual(self.w.recs().list(), [])

    def test_a_build_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sysimage build]`: each run its own record, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.driver().build([])
        converges(self, lambda: self.WORLD(self.tmp), run_once, self.WORLD.state)


class TestTheBuildrootLifecycle(Lifecycle, TaskTest):
    def test_a_running_stage_is_refused_by_name(self):
        t = self.w.running()
        err = self.refused()
        self.assertIn("a build is still running in '%s': buildroot (pid 77, here)" % WS, err)
        self.assertIn("Stop it:    wk sysimage build %s --stop" % PRESET, err)
        self.assertEqual(t.field("exit"), "")

    def test_a_silent_stage_is_killed_by_its_watchdog_and_ends_stalled(self):
        self.w.polls, self.w.out = None, b""
        self.w.env.update({"WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30", "WK_POLL_SECONDS": "10"})
        err = self.refused()
        self.assertIn("giving up and killing the job", err)
        self.assertIn("the image build in '%s' stalled" % WS, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_a_half_declared_kernel_pin_is_refused(self):
        p = dict(self.w.preset(), BR_KERNEL_DEB_URL="https://x/k.deb")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            buildroot.Buildroot(self.w.reg, p, PRESET, self.w.clock).build([])
        self.assertIn("a kernel by URL alone is not pinned", err.getvalue())

    def test_a_pinned_kernel_is_prepared_here_and_handed_over_through_the_download_cache(self):
        p = dict(self.w.preset(), BR_KERNEL_DEB_URL="https://x/k.deb", BR_KERNEL_DEB_SHA256="d" * 64, BR_KERNEL_RELEASE="6.1.0-rpi")
        self.w.answer(["sha256sum"], out="d" * 64 + "  x\n")
        self.w.answer(["curl"])
        self.w.answer(["mv"])
        dl = os.path.join(self.w.env["WK_STORE"], "cache", "buildroot", "dl")

        def pin(m, deb, release, out):
            m.act_run(["kernel_pin", deb, release, out])
            return os.path.join(out, "wk-kernel-%s.tar" % release)
        with contextlib.redirect_stderr(io.StringIO()) as err, mock.patch.object(buildroot, "kernel_pin", pin):
            rc = buildroot.Buildroot(self.w.reg, p, PRESET, self.w.clock).build([])
        self.assertEqual(rc, 0, err.getvalue())
        (t,) = self.w.recs().list()
        self.assertEqual(t.plan()[0], "prepare the pinned kernel 6.1.0-rpi")
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        argv = list(w[1])
        self.assertEqual(argv[argv.index("--kernel-tar") + 1], "/cache/buildroot/dl/wk-kernel-6.1.0-rpi.tar")
        self.assertLess(self.w.effects.index(("run", ("kernel_pin", os.path.join(task.cache_dir(self.w.env), "k.deb"), "6.1.0-rpi", dl))),
                        self.w.effects.index(w))


class TestTheBuilderIsTheImagePresets(TaskTest):
    """cli.py's dispatch: what cannot be built is refused by name, and each builder gets the tail."""

    def sysimage(self):
        from wk.sysimage import cli
        return cli.Sysimage(self.w.reg, self.w.clock)

    def test_a_preset_whose_conf_does_not_parse_is_refused_by_its_line_not_called_unknown(self):
        with mock.patch.object(images, "load", side_effect=images.ConfError("bad.conf:3: not a KEY=value line: x")), \
                self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.sysimage().image_preset("bad")
        self.assertIn("bad.conf:3:", err.getvalue())
        self.assertNotIn("unknown image preset", err.getvalue())

    def test_a_preset_that_needs_something_says_what(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.sysimage().build("wpewebkit-2.38-buildroot-rpi5-64", [])
        self.assertIn("cannot be built yet:\n\n    no defconfig for rpi5", err.getvalue())

    def test_a_yocto_build_is_yocto_py_s_and_a_2_52_slot_the_pgo_cycle_s(self):
        from wk import pgo
        from wk.sysimage import yocto
        with mock.patch.object(pgo.Cycle, "webkit", return_value=0) as cycle, \
                mock.patch.object(yocto.Yocto, "build", return_value=0) as yb, \
                mock.patch.object(yocto.Yocto, "webkit", return_value=0) as yw:
            self.sysimage().build("webkit-2.52-yocto-rpi5-64@moose", ["--stage", "image"])
            self.sysimage().webkit("webkit-2.52-yocto-rpi5-64", ["--slot", "base"])
            self.sysimage().webkit("wpewebkit-2.46-yocto-rpi4-64", ["--slot", "base"])
        self.assertEqual([c[0] for c in cycle.call_args_list], [(["--slot", "base"],)])
        self.assertEqual((yb.call_args[0], yw.call_args[0]), ((["--stage", "image"],), (["--slot", "base"],)))

    def test_a_fetch_or_pmos_image_takes_no_slot(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.sysimage().webkit("bridge-pinephone", ["--slot", "base"])
        self.assertIn("only a buildroot or yocto image takes WebKit slots", err.getvalue())


class TestWebkitSlot(TaskTest):
    def setUp(self):
        super().setUp()
        image = os.path.join(self.w.ws_dir, "build", "buildroot", PRESET, "output", "images", "sdcard.img")
        self.w.files[image] = ""
        self.slotdir = images.slot_dir(WS, "base", self.w.env)
        self.w.out = b"wk-buildroot-webkit: stage 'webkit-base' done\n"

    def slot(self, *more):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.w.driver().webkit(["--commit", SHA, "--slot", "base"] + list(more))
        return rc, err.getvalue()

    def test_a_slot_is_its_own_stage_and_ends_on_its_manifest(self):
        self.w.react(["exec", WS, "env"], lambda a, f: Result(0))
        real = self.w.start

        def built(*a, **kw):
            self.w.files[os.path.join(self.slotdir, "slot.json")] = "{}"
            return real(*a, **kw)
        self.w.start = built
        rc, err = self.slot()
        self.assertEqual(rc, 0, err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("stage"), t.field("exit")), ("webkit-base", "0"))
        self.assertEqual(t.field("log"), os.path.join(self.w.ws_dir, "home", "buildroot-webkit-base.log"))
        self.assertIn("slot 'base' of %s holds aaaaaaaaaaaa" % PRESET, err)

    def test_done_without_a_manifest_is_refused(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.w.driver().webkit(["--commit", SHA, "--slot", "base"])
        self.assertIn("left no %s/slot.json" % self.slotdir, err.getvalue())

    def test_no_image_is_refused_before_anything_runs(self):
        self.w.files.clear()
        err = self.refused("--commit", SHA, "--slot", "base", verb="webkit")
        self.assertIn("has no finished image", err)
        self.assertEqual(self.w.recs().list(), [])

    def test_the_arguments_are_checked_first(self):
        self.assertIn("usage: wk sysimage webkit", self.refused("--slot", "base", verb="webkit"))
        self.assertIn("40 hex digits", self.refused("--commit", "abc", "--slot", "base", verb="webkit"))
        self.assertIn("not usable", self.refused("--commit", SHA, "--slot", "../x", verb="webkit"))


class TestTheWrapperInTheWorkspace(unittest.TestCase):
    def test_it_announces_the_pid_it_execs_under_and_declares_a_wk_build(self):
        with mock.patch("os.execvpe") as ex, contextlib.redirect_stderr(io.StringIO()) as err:
            task.stage_main("buildroot", ["/x/build.sh", "--name", "p"], {"PATH": "/opt/wk-tools/container/bin:/usr/bin", "A": "1"})
        self.assertEqual(err.getvalue(), "wk: buildroot pid %d\n" % os.getpid())
        argv0, argv, env = ex.call_args[0]
        self.assertEqual((argv0, argv), ("/x/build.sh", ["/x/build.sh", "--name", "p"]))
        self.assertEqual((env["PATH"], env["WK_BUILD"], env["A"]), ("/usr/bin", "1", "1"))

    def test_the_announced_pid_is_what_the_driver_adopts(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log") as f:
            f.write("wk: buildroot pid 4321\n")
            f.flush()
            self.assertEqual(job.announced_pid(f.name, "buildroot"), 4321)

    def test_it_runs_in_the_workspace_as_a_module(self):
        cp = subprocess.run([sys.executable, "-m", "wk.sysimage.task", "stage", "buildroot", "--", "sh", "-c", "echo $WK_BUILD"],
                            env=dict(os.environ, PYTHONPATH=str(REPO / "lib")), capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout, "1\n")


class TestFetch(TaskTest):
    URL, PIN = "https://example.org/img.xz", "b" * 64

    def dest(self):
        return os.path.join(task.cache_dir(self.w.env), "img.xz")

    def fetch(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            return task.fetch_base(self.w, self.URL, self.PIN, self.w.env), err.getvalue()

    def test_a_cached_download_that_matches_its_pin_is_not_fetched_again(self):
        self.w.files[self.dest()] = "x"
        self.w.answer(["sha256sum"], out=self.PIN + "  " + self.dest() + "\n")
        self.assertEqual(self.fetch()[0], self.dest())
        self.assertFalse([e for e in self.w.effects if e[0] == "run" and e[1][0] == "curl"])

    def test_one_that_does_not_is_resumed_and_checked_before_it_is_kept(self):
        self.w.answer(["curl"])
        self.w.answer(["mv"])
        self.w.answer(["sha256sum"], out=self.PIN + "  x\n")
        self.assertEqual(self.fetch()[0], self.dest())
        part = self.dest() + ".part"
        runs = [e[1] for e in self.w.effects if e[0] == "run"]
        self.assertIn(("curl", "-fsSL", "--retry", "5", "-C", "-", "-o", part, self.URL), runs)
        self.assertEqual(runs[-1], ("mv", part, self.dest()))

    def test_a_mismatch_is_refused_naming_the_stale_pin(self):
        self.w.answer(["curl"])
        self.w.answer(["sha256sum"], out="c" * 64 + "  x\n")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            task.fetch_base(self.w, self.URL, self.PIN, self.w.env)
        self.assertIn("does not match its pin (sha256 %s, expected %s)" % ("c" * 64, self.PIN), err.getvalue())
        self.assertIn(("run", ("rm", "-f", self.dest() + ".part")), self.w.effects)

    def test_the_cache_is_the_store_s_where_this_machine_holds_it(self):
        self.assertEqual(task.cache_dir(self.w.env), os.path.join(self.w.env["WK_STORE"], "cache", "images"))

    def test_the_fetch_builder_s_dry_run_fetches_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        p = dict(images.FIELDS, IMG_PRESET="f", FET_URL=self.URL, FET_SHA256=self.PIN, FET_NOTE="an image")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            task.Fetch(self.w, p, self.w.env).build([])
        self.assertIn("would fetch image f\n  from        %s\n              not cached -- would download" % self.URL, err.getvalue())
        self.assertEqual([e for e in self.w.effects if e[0] != "run"], [])


if __name__ == "__main__":
    unittest.main()
