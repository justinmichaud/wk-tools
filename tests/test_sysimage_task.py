"""`wk sysimage build` and `webkit` for a buildroot image as a task (lib/wk/sysimage/task.py,
lib/wk/sysimage/buildroot.py) against a Fake world: the record a stage writes and how it ends, the
refusals, --detach, --stop, the watchdog, a dry run, a stage killed after any effect, the wrapper a stage
runs under in the workspace, and the pinned fetch.

Rows landed here: `unit sysimage.task_states` (the buildroot half; a silent bitbake and a wedged one are
yocto's, 5.18), `unit record.progress_shape[sysimage]`, `unit killpoints[sysimage build]`.

Run: python3 tests/run.py -k test_sysimage_task
"""
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

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import build, images, job, record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import buildroot, task  # noqa: E402

PROFILE = "wpewebkit-2.46-buildroot-rpi3-32"
WS = "buildroot-" + PROFILE
SHA = "a" * 40


class Box(targets.Target):
    kind = "container"

    def _podman(self):
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def info(self, ws):
        return "running" if self.machine.made else "absent"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def sync_tools(self, ws):
        return self.machine.act_run(["sync-tools", ws]).ok


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return Box("box", self.root, self.env, self.world)

    def default(self):
        return "box"


class Proc:
    def __init__(self, rc, hang, interrupt):
        self.pid, self.rc, self.hang, self.interrupt, self.returncode = 4242, rc, hang, interrupt, None

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
    """This machine holding `WS` on its container target `box`: the image and the workspace exist unless
    `made` is False, the stage writes `out` to its log and exits `rc`."""

    def __init__(self, tmp):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        store = self.tmp / "store"
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(store), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "1", "WK_AVAIL_MB": "65536",
                    "WK_CGROUP_CORES": "8", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2", "WK_ROOT": str(REPO)}
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.made, self.rc, self.hang, self.interrupt = True, 0, False, None
        self.out = b"wk-buildroot: building\nwk-buildroot: stage 'image' done\n"
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.answer(["du", "-sh"], rc=1)
        self.answer(["podman", "image", "exists"])
        self.react(["podman", "container", "inspect"], lambda a, f: Result(0, self.tag() + "\n"))
        self.react(["env"], self._new)
        self.react(["exec", WS, "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.answer(["sync-tools"])
        self.reg = Reg(self)
        self.ws_dir = os.path.join(str(store), "ws", WS)
        os.makedirs(os.path.join(self.ws_dir, "home"))
        self.log = os.path.join(self.ws_dir, "home", "buildroot-image.log")

    @property
    def fake(self):
        return self

    def tag(self):
        return buildroot.Buildroot(self.reg, self.profile(), PROFILE, self.clock).host_image()[1]

    def _new(self, argv, f):
        self.made = True
        return Result(0)

    def profile(self):
        return images.load(PROFILE, self.env)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.effect(("watch", tuple(argv)))
        stdout.write(self.out)
        return Proc(self.rc, self.hang, self.interrupt)

    def driver(self):
        return buildroot.Buildroot(self.reg, self.profile(), PROFILE, self.clock, self.popen)

    def recs(self):
        return build.records_of(self.reg.load("box"), self.clock, self)

    def budget_files(self):
        d = os.path.join(self.env["XDG_STATE_HOME"], "wk", "builds")
        return sorted(p for p in self.files if p.startswith(d + "/"))

    def state(self):
        return ([(t.field("kind"), t.field("exit")) for t in self.recs().list()], len(self.budget_files()))


class TaskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-sysimage-task-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_TARGET"):
            os.environ.pop(v, None)
        p = mock.patch.object(record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        self.w = World(self.tmp)

    def build(self, w=None, *rest):
        w = w or self.w
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = w.driver().build(list(rest))
        return rc, err.getvalue()

    def refused(self, w=None, *rest, status=1, verb="build"):
        w = w or self.w
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                getattr(w.driver(), verb)(list(rest))
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class TestTheRecordAStageWrites(TaskTest):
    def test_a_stage_that_says_it_is_done_ends_ok_with_the_one_progress_record(self):
        """`record.progress_shape[sysimage]`: step n of m, since when, the log, how to stop it."""
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn("built %s in '%s'" % (PROFILE, WS), err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("name"), t.field("stage"), t.field("exit")), ("buildroot", WS, "image", "0"))
        self.assertEqual(t.plan(), ["the workspace '%s' on %s" % (WS, self.w.tag()), "sync wk-tools into '%s'" % WS,
                                    "build %s with -j8" % PROFILE])
        self.assertEqual(t.steps(), [(1, "done"), (2, "done"), (3, "running")])
        self.assertEqual((t.field("log"), t.field("kill")), (self.w.log, "wk sysimage build %s --stop" % PROFILE))
        self.assertTrue(t.field("started"))
        self.assertEqual(Path(self.w.log).read_bytes(), self.w.out)

    def test_the_stage_runs_under_the_wrapper_in_the_workspace(self):
        self.build()
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        argv = list(w[1])
        self.assertEqual(argv[:9], ["exec", WS, "env", "PYTHONPATH=/opt/wk-tools/lib", "python3", "-m", "wk.sysimage.task",
                                    "stage", "buildroot"])
        self.assertEqual(argv[9:12], ["--", "/opt/wk-tools/image/buildroot-build.sh", "--name"])
        self.assertIn("--overlay-wifi", argv)
        self.assertEqual(argv[argv.index("--jobs") + 1], "8")

    def test_the_budget_is_on_the_books_under_this_driver(self):
        self.build()
        (f,) = self.w.budget_files()
        self.assertIn("label=wk sysimage image %s\n" % WS, self.w.files[f])
        self.assertIn("jobs=8\nbudget_mb=16384\nholder=pid:%d\n" % os.getpid(), self.w.files[f])

    def test_done_is_the_wrapper_s_marker_not_the_exit_status(self):
        self.w.out = b"make: Nothing to be done\n"
        err = self.refused()
        self.assertIn("it exited 0 and never said it was done", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_a_failure_ends_with_its_status_and_its_last_lines(self):
        self.w.out, self.w.rc = b"package foo failed\nwk-buildroot: error: buildroot failed.\n", 2
        err = self.refused()
        self.assertIn("the image build in '%s' failed. Last lines:" % WS, err)
        self.assertIn("    package foo failed", err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "2")

    def test_a_workspace_that_is_not_there_is_made_from_the_host_image(self):
        self.w.made = False
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        (new,) = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:1] == ("env",)]
        self.assertEqual(list(new[1]), ["env", "WK_SDK_IMAGE=" + self.w.tag(), str(REPO / "wk"), "new", WS, "--target", "box"])

    def test_a_workspace_made_from_another_image_is_refused_naming_the_remake(self):
        self.w.answer(["podman", "container", "inspect"], out="localhost/wk-buildroot-host:22.04-old\n")
        err = self.refused()
        self.assertIn("was made from localhost/wk-buildroot-host:22.04-old", err)
        self.assertIn("wk rm %s && wk sysimage build %s" % (WS, PROFILE), err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")


class TestWhatItBuildsWith(TaskTest):
    def test_the_host_image_is_tagged_by_its_base_and_its_containerfile(self):
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, buildroot.BASE_IMAGE)
        self.assertRegex(tag, r"^localhost/wk-buildroot-host:22\.04-[0-9a-f]{8}$")

    def test_wk_buildroot_base_names_another_host(self):
        self.w.env["WK_BUILDROOT_BASE"] = "docker.io/library/ubuntu:24.04"
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, "docker.io/library/ubuntu:24.04")
        self.assertIn(":24.04-", tag)

    def test_a_pinned_kernel_is_prepared_here_and_handed_over_through_the_download_cache(self):
        p = dict(self.w.profile(), BR_KERNEL_DEB_URL="https://x/k.deb", BR_KERNEL_DEB_SHA256="d" * 64, BR_KERNEL_RELEASE="6.1.0-rpi")
        self.w.answer(["sha256sum"], out="d" * 64 + "  x\n")
        self.w.answer(["curl"])
        pin = str(REPO / "image" / "buildroot" / "kernel-pin.sh")
        self.w.answer([pin], out=os.path.join(self.w.env["WK_STORE"], "cache", "buildroot", "dl", "wk-kernel-6.1.0-rpi.tar"))
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = buildroot.Buildroot(self.w.reg, p, PROFILE, self.w.clock, self.w.popen).build([])
        self.assertEqual(rc, 0, err.getvalue())
        (t,) = self.w.recs().list()
        self.assertEqual(t.plan()[0], "prepare the pinned kernel 6.1.0-rpi")
        (w,) = [e for e in self.w.effects if e[0] == "watch"]
        argv = list(w[1])
        self.assertEqual(argv[argv.index("--kernel-tar") + 1], "/cache/buildroot/dl/wk-kernel-6.1.0-rpi.tar")
        self.assertLess(self.w.effects.index(("run", (pin, os.path.join(task.cache_dir(self.w.env), "k.deb"), "6.1.0-rpi",
                                                      os.path.join(self.w.env["WK_STORE"], "cache", "buildroot", "dl")))),
                        self.w.effects.index(w))


class TestTheBuilderIsTheProfiles(TaskTest):
    """cli.py's dispatch: what cannot be built is refused by name, and each builder gets the tail."""

    def sysimage(self):
        from wk.sysimage import cli
        return cli.Sysimage(self.w.reg, self.w.clock)

    def test_a_profile_that_needs_something_says_what(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.sysimage().build("wpewebkit-2.38-buildroot-rpi5-64", [])
        self.assertIn("cannot be built yet:\n\n    no defconfig for rpi5", err.getvalue())

    def test_yocto_and_pmos_go_to_their_bash_arms_with_the_spec(self):
        from wk import shell
        with mock.patch.object(shell, "sysimage_arms") as arms:
            self.sysimage().build("webkit-2.52-yocto-rpi5-64@moose", ["--stage", "image"])
            self.sysimage().webkit("webkit-2.52-yocto-rpi5-64", ["--slot", "base"])
        self.assertEqual([c[0][1:] for c in arms.call_args_list],
                         [("yocto", "webkit-2.52-yocto-rpi5-64@moose", "--stage", "image"),
                          ("yocto-webkit", "webkit-2.52-yocto-rpi5-64", "--slot", "base")])

    def test_a_fetch_or_pmos_image_takes_no_slot(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.sysimage().webkit("bridge-pinephone", ["--slot", "base"])
        self.assertIn("only a buildroot or yocto image takes WebKit slots", err.getvalue())


class TestRefusals(TaskTest):
    def test_a_running_stage_is_refused_by_name(self):
        t = self.w.recs().begin("buildroot", "here", WS, "wk sysimage build %s --stop" % PROFILE, self.w.log, ["a"], pid=77)
        self.w.pids.add(77)
        err = self.refused()
        self.assertIn("a build is still running in '%s': buildroot (pid 77, here)" % WS, err)
        self.assertIn("Stop it:    wk sysimage build %s --stop" % PROFILE, err)
        self.assertEqual(t.field("exit"), "")

    def test_a_pid_file_a_killed_build_left_is_not_busy_and_a_live_one_is(self):
        home = os.path.join(self.w.ws_dir, "home")
        self.w.dirs.add(home)
        self.w.files[os.path.join(home, "buildroot-image.pid")] = "99999\n"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.w.pids.add(99999)
        self.assertIn("buildroot-image (pid 99999 in the workspace)", self.refused())

    def test_a_held_workspace_lock_is_refused_naming_the_stop(self):
        with mock.patch("wk.lock.Lock.holder_pid", return_value=55):
            self.w.pids.add(55)
            err = self.refused()
        self.assertIn("'%s' is already building -- its driver holds the ws-%s lock." % (WS, WS), err)

    def test_another_build_on_this_machine_is_a_barrier(self):
        from wk.resources import Budget
        Budget(self.w, self.w.env, self.w.clock).record("wk build other", 4, 8192, "pid:66")
        self.w.pids.add(66)
        with mock.patch.object(build, "holder_alive", return_value=lambda h: True):
            err = self.refused(None, status=75)
        self.assertIn("is already building", err)

    def test_a_target_that_is_not_a_container_is_refused(self):
        with mock.patch.object(Box, "kind", "remote"):
            err = self.refused()
        self.assertIn("a buildroot image builds in a container workspace, and target 'box' is a remote one", err)

    def test_an_unknown_option_is_a_usage_error(self):
        self.assertIn("unknown option: --stage", self.refused(None, "--stage", "image"))

    def test_a_half_declared_kernel_pin_is_refused(self):
        p = dict(self.w.profile(), BR_KERNEL_DEB_URL="https://x/k.deb")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            buildroot.Buildroot(self.w.reg, p, PROFILE, self.w.clock).build([])
        self.assertIn("a kernel by URL alone is not pinned", err.getvalue())


class TestTheWatchdog(TaskTest):
    def test_a_silent_stage_is_killed_by_its_watchdog_and_ends_stalled(self):
        self.w.hang, self.w.out = True, b""
        self.w.env.update({"WK_ABORT_SECONDS": "60", "WK_STALL_SECONDS": "30", "WK_POLL_SECONDS": "10"})
        err = self.refused()
        self.assertIn("giving up and killing the job", err)
        self.assertIn("the image build in '%s' stalled" % WS, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_an_interrupt_stops_the_stage_and_the_record_reads_cancelled(self):
        self.w.hang, self.w.interrupt = True, signal.SIGHUP
        err = self.refused(status=129)
        self.assertIn("interrupted -- stopping the image build in '%s'" % WS, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "cancelled")


class TestStop(TaskTest):
    def test_stop_kills_the_tree_in_the_workspace_and_reports_once_it_is_gone(self):
        t = self.w.recs().begin("buildroot", "here", WS, "k", self.w.log, ["a"], pid=1)
        t.set("pid_match", buildroot.PATTERN)
        t.pid(777)
        t.set("where", "target")
        self.w.pids.add(777)
        self.w.answer(["exec", WS, "ps", "-o", "args=", "-p", "777"], out="bash /opt/wk-tools/image/buildroot-build.sh --name x\n")
        self.w.answer(["exec", WS, "sh", "-c"], out="778\n777\n")
        self.w.react(["exec", WS, "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        rc, err = self.build(None, "--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("exec", WS, "kill", "-TERM", "778", "777")), self.w.effects)
        self.assertIn("stopped '%s's buildroot and recorded it as cancelled" % WS, err)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_one_that_outlives_a_kill_is_refused_naming_it(self):
        t = self.w.recs().begin("buildroot", "here", WS, "k", self.w.log, ["a"], pid=88)
        self.w.pids.add(88)
        with mock.patch.object(self.w, "kill", return_value=True):
            err = self.refused(None, "--stop")
        self.assertIn("outlived a TERM and a KILL", err)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_nothing_running_says_so(self):
        rc, err = self.build(None, "--stop")
        self.assertEqual(rc, 0)
        self.assertIn("no buildroot is running in '%s'" % WS, err)


class Detaching(World):
    def __init__(self, tmp, starts=True):
        super().__init__(tmp)
        self.starts = starts

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        if self.starts:
            self.recs().begin("buildroot", "here", WS, "k", self.log, ["a"], pid=pid)
        else:
            self.pids.discard(pid)
        return pid


class TestDetach(TaskTest):
    def test_it_returns_once_its_own_child_has_begun_its_record(self):
        w = Detaching(self.tmp)
        rc, err = self.build(w, "--detach")
        self.assertEqual(rc, 0, err)
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual(list(sp[1]), [str(REPO / "wk"), "sysimage", "build", PROFILE])
        self.assertEqual(sp[2], os.path.join(w.ws_dir, "detached-image.log"))
        self.assertIn("running detached in '%s' as pid 1001 -- this end can go away" % WS, err)
        self.assertIn("  follow:  wk logs %s -f" % WS, err)
        self.assertFalse([e for e in w.effects if e[0] == "watch"])

    def test_a_child_that_ends_before_its_record_is_named(self):
        err = self.refused(Detaching(self.tmp, starts=False), "--detach")
        self.assertIn("the detached build of %s of '%s' ended before it started" % (PROFILE, WS), err)


class TestDryRun(TaskTest):
    def test_a_dry_run_reports_the_plan_and_changes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn("would build image %s (builder: buildroot)" % PROFILE, err)
        self.assertIn("  jobs         -j8 (memory-sized at 2048 MB/job)", err)
        self.assertEqual(self.w.recs().list(), [])
        self.assertFalse([e for e in self.w.effects if e[0] not in ("run",)])

    def test_the_tail_s_dry_run_is_the_dispatcher_s(self):
        self.build(None, "--dry-run")
        self.assertEqual(os.environ.get("WK_DRY_RUN"), "1")
        self.assertEqual(self.w.recs().list(), [])


class TestWebkitSlot(TaskTest):
    def setUp(self):
        super().setUp()
        image = os.path.join(self.w.ws_dir, "build", "buildroot", PROFILE, "output", "images", "sdcard.img")
        self.w.files[image] = ""
        self.slotdir = images.slot_dir(WS, "base", self.w.env)
        self.w.out = b"wk-buildroot-webkit: stage 'webkit-base' done\n"

    def slot(self, *more):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.w.driver().webkit(["--commit", SHA, "--slot", "base"] + list(more))
        return rc, err.getvalue()

    def test_a_slot_is_its_own_stage_and_ends_on_its_manifest(self):
        self.w.react(["exec", WS, "env"], lambda a, f: Result(0))
        real = self.w.popen

        def built(*a, **kw):
            self.w.files[os.path.join(self.slotdir, "slot.json")] = "{}"
            return real(*a, **kw)
        self.w.popen = built
        rc, err = self.slot()
        self.assertEqual(rc, 0, err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("stage"), t.field("exit")), ("webkit-base", "0"))
        self.assertEqual(t.field("log"), os.path.join(self.w.ws_dir, "home", "buildroot-webkit-base.log"))
        self.assertIn("slot 'base' of %s holds aaaaaaaaaaaa" % PROFILE, err)

    def test_done_without_a_manifest_is_refused(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.w.driver().webkit(["--commit", SHA, "--slot", "base"])
        self.assertIn("left no %s/slot.json" % self.slotdir, err.getvalue())

    def test_no_image_is_refused_before_anything_runs(self):
        self.w.files.clear()
        err = self.refused(None, "--commit", SHA, "--slot", "base", verb="webkit")
        self.assertIn("has no finished image", err)
        self.assertEqual(self.w.recs().list(), [])

    def test_the_arguments_are_checked_first(self):
        self.assertIn("usage: wk sysimage webkit", self.refused(None, "--slot", "base", verb="webkit"))
        self.assertIn("40 hex digits", self.refused(None, "--commit", "abc", "--slot", "base", verb="webkit"))
        self.assertIn("not usable", self.refused(None, "--commit", SHA, "--slot", "../x", verb="webkit"))


class TestKillPoints(TaskTest):
    def test_a_build_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sysimage build]`: each run its own record, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.driver().build([])
        converges(self, lambda: World(self.tmp), run_once, World.state)


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
        self.assertRegex(cp.stderr, r"^wk: buildroot pid \d+\n$")


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

    def test_one_that_does_not_is_resumed_and_checked(self):
        self.w.answer(["curl"])
        self.w.answer(["sha256sum"], out=self.PIN + "  x\n")
        self.assertEqual(self.fetch()[0], self.dest())
        self.assertIn(("run", ("curl", "-fsSL", "--retry", "5", "-C", "-", "-o", self.dest(), self.URL)), self.w.effects)

    def test_a_mismatch_is_refused_naming_the_stale_pin(self):
        self.w.answer(["curl"])
        self.w.answer(["sha256sum"], out="c" * 64 + "  x\n")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            task.fetch_base(self.w, self.URL, self.PIN, self.w.env)
        self.assertIn("checksum mismatch", err.getvalue())

    def test_the_cache_is_the_store_s_where_this_machine_holds_it(self):
        self.assertEqual(task.cache_dir(self.w.env), os.path.join(self.w.env["WK_STORE"], "cache", "images"))

    def test_the_fetch_builder_s_dry_run_fetches_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        p = dict(images.FIELDS, IMG_PROFILE="f", FET_URL=self.URL, FET_SHA256=self.PIN, FET_NOTE="an image")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            task.Fetch(self.w, p, self.w.env).build([])
        self.assertIn("would fetch image f\n  from        %s\n              not cached -- would download" % self.URL, err.getvalue())
        self.assertEqual([e for e in self.w.effects if e[0] != "run"], [])


if __name__ == "__main__":
    unittest.main()
