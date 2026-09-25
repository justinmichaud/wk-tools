"""`wk sysimage build` of a yocto profile (lib/wk/sysimage/yocto.py) as a task against a Fake world: the stage
index the record steps, what each stage books, the refusals, the branch and target checks, the cross configs,
the watchdog's silent and wedged verdicts, --stop and the cooker a killed driver leaves, --detach, a dry run,
a stage killed after any effect.

Rows landed here: `unit sysimage.task_states` (the yocto half).

Run: python3 tests/run.py -k test_yocto_stage
"""
import contextlib
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
from wk import build, images, job, record, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import yocto  # noqa: E402

PROFILE = "wpewebkit-2.46-yocto-rpi4-64"
WS = "yocto-" + PROFILE
CROSS_TARGET = "rpi4-64bits-mesa"
SHA = "a" * 40
TASK_LINE = "NOTE: Running task 7658 of 13213 (virtual:native:/w/sources/meta-clang/recipes-devtools/clang/clang_git.bb:do_compile)\n"


class Box(targets.Target):
    kind = "container"

    def podman(self):
        return ["podman"]

    def ctr(self, ws):
        return "wk-" + ws

    def info(self, ws):
        return "running" if self.machine.made else "absent"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return Box("box", self.root, self.env, self.world)

    def default(self):
        return "box"


class Proc:
    """Running for `polls` polls, then exiting `rc`; `grow` is called on each poll, to write to the log."""

    def __init__(self, rc, polls, grow):
        self.pid, self.rc, self.polls, self.grow, self.returncode = 4242, rc, polls, grow, None

    def poll(self):
        if self.polls is None or self.polls > 0:
            if self.polls:
                self.polls -= 1
            if self.grow:
                self.grow()
            return None
        self.returncode = self.rc if self.returncode is None else self.returncode
        return self.returncode

    def wait(self):
        self.returncode = -9 if self.returncode is None else self.returncode
        return self.returncode


class World(Fake):
    """This machine holding `WS` on its container target `box`, the workspace on the profile's branch and its
    targets.conf holding the profile's section; a stage writes `out` to its log and exits `rc` after `polls`."""

    def __init__(self, tmp):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        store = self.tmp / "store"
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(store), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "1", "WK_AVAIL_MB": "65536",
                    "WK_CGROUP_CORES": "8", "WK_JOB_PID_TRIES": "0", "WK_KILL_WAIT": "2", "WK_ROOT": str(REPO),
                    "WK_POLL_SECONDS": "10", "WK_STALL_SECONDS": "30", "WK_HEARTBEAT_SECONDS": "60"}
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.made, self.rc, self.polls, self.grow = True, 0, 0, None
        self.out = b"wk-yocto: target rpi4-64bits-mesa\nwk-yocto: stage 'image' done\n"
        self.answer(["hostname"], out="here\n")
        self.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 209715200 1% /\n")
        self.answer(["du", "-sh"], rc=1)
        self.answer(["podman", "image", "exists"])
        self.answer(["nproc"], out="12\n")
        self.answer(["sysctl", "-n", "hw.ncpu"], out="12\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="%d\n" % (32 << 30))
        self.files["/proc/meminfo"] = "MemTotal: %d kB\nMemAvailable: %d kB\n" % (32 << 20, 30 << 20)
        self.react(["podman", "container", "inspect"], lambda a, f: Result(0, self.tag() + "\n"))
        self.react(["env"], self._new)
        self.head, self.checkout_rc = "wpe-2.46\r\n", 0
        self.sections = Result(0, "[rpi3-32bits-mesa]\n[%s]\nimage_basename = webkit-dev-ci-tools\n" % CROSS_TARGET)
        self.react(["exec", WS, "bash", "-c"], self._bash)
        self.react(["exec", WS, "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.reg = Reg(self)
        self.ws_dir = os.path.join(str(store), "ws", WS)
        os.makedirs(os.path.join(self.ws_dir, "home"))
        self.log = os.path.join(self.ws_dir, "home", "yocto-image.log")
        self.lock = os.path.join(yocto.host_workdir(self.ws_dir, CROSS_TARGET), "build", "bitbake.lock")

    @property
    def fake(self):
        return self

    def tag(self):
        return self.driver().host_image()[1]

    def _bash(self, argv, f):
        line = argv[4]
        if "git rev-parse --abbrev-ref HEAD" in line:
            return Result(0, self.head)
        if line.startswith("cat "):
            return self.sections
        return Result(self.checkout_rc)

    def _new(self, argv, f):
        self.made = True
        return Result(0)

    def profile(self):
        return images.load(PROFILE, self.env)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.effect(("watch", tuple(argv)))
        stdout.write(self.out)
        return Proc(self.rc, self.polls, self.grow)

    def driver(self):
        return yocto.Yocto(self.reg, self.profile(), PROFILE, self.clock, self.popen)

    def recs(self):
        return build.records_of(self.reg.load("box"), self.clock, self)

    def budget_files(self):
        d = os.path.join(self.env["XDG_STATE_HOME"], "wk", "builds")
        return sorted(p for p in self.files if p.startswith(d + "/"))

    def watched(self):
        (w,) = [e for e in self.effects if e[0] == "watch"]
        return list(w[1])

    def state(self):
        return ([(t.field("kind"), t.field("exit")) for t in self.recs().list()], len(self.budget_files()))

    def running(self, stage, pid=77):
        t = self.recs().begin("yocto", "here", WS, "wk sysimage build %s --stage %s --stop" % (PROFILE, stage), self.log,
                              list(yocto.STAGES), pid=pid)
        t.step_state(yocto.stage_index(stage), "running")
        self.pids.add(pid)
        return t


class YoctoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-yocto-stage-"))
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

    def cores(self):
        return yocto.Resources(self.w, self.w.env).envelope_cores()

    def build(self, *rest, w=None):
        w = w or self.w
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = w.driver().build(list(rest))
        return rc, err.getvalue()

    def refused(self, *rest, w=None, status=1):
        w = w or self.w
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                w.driver().build(list(rest))
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()


class TestTheStages(unittest.TestCase):
    def test_a_stage_s_index_is_its_place_in_the_order(self):
        self.assertEqual([yocto.stage_index(s) for s in yocto.STAGES], [1, 2, 3, 4, 5, 6])
        self.assertEqual(yocto.stage_index("image"), 3)

    def test_an_unknown_stage_is_refused_naming_every_stage(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            yocto.stage_index("all")
        for s in yocto.STAGES:
            self.assertIn("      %s " % s, err.getvalue())

    def test_a_bitbake_stage_books_the_machine_a_slot_its_jobs_and_the_mix_one(self):
        for s in ("layers", "fetch", "image", "toolchain"):
            self.assertEqual(yocto.stage_budget(s, 79, 113000, 8), (79, 113000), s)
        self.assertEqual(yocto.stage_budget("webkit", 79, 113000, 8), (8, 8 * yocto.WEBKIT_MB_PER_JOB))
        self.assertEqual(yocto.stage_budget("pgo-mix", 79, 113000, 8), (1, yocto.WEBKIT_MB_PER_JOB))

    def test_a_stage_asks_for_the_disk_it_needs(self):
        env = {"WK_BUILD_DISK_GB": "30"}
        self.assertEqual(yocto.disk_need("image", True, False, env), 180)
        self.assertEqual(yocto.disk_need("image", False, True, env), 60)
        self.assertEqual(yocto.disk_need("webkit", False, True, env), 30)
        self.assertEqual(yocto.disk_need("pgo-mix", False, True, env), 2)


class TestTheCrossConfigs(unittest.TestCase):
    def cross(self, name, profile=""):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return yocto.cross_config(name, profile), ""
            except Refused:
                return None, err.getvalue()

    def test_the_plain_one_adds_nothing(self):
        self.assertEqual(self.cross("wpe-cross")[0], ("", "", "", ""))

    def test_the_collection_config_instruments_with_clang_into_the_board_s_directory(self):
        (cc, cxx, cmake, pgo), _ = self.cross("wpe-cross-pgo-collect")
        self.assertEqual((cc, cxx, pgo), ("clang", "clang++", "collect"))
        self.assertIn("-DENABLE_LLVM_PROFILE_GENERATION=ON", cmake)
        self.assertIn("-DPGO_PROFILE_DIR=/var/wk/pgo", cmake)

    def test_the_measured_config_needs_a_profile_and_builds_against_it(self):
        self.assertIn("PGO_PROFILE_PATH", self.cross("wpe-cross-pgo-use")[1])
        (cc, _, cmake, pgo), _ = self.cross("wpe-cross-pgo-use", "/src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata")
        self.assertEqual((cc, pgo), ("clang", "use"))
        self.assertIn("-DUSE_PGO_PROFILE=ON -DPGO_PROFILE_PATH=/src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata", cmake)

    def test_each_pgo_config_states_both_options(self):
        """They share one build directory and cmake refuses the pair (WEBKIT_OPTION_CONFLICT)."""
        for name, profile in (("wpe-cross-pgo-collect", ""), ("wpe-cross-pgo-use", "/x.profdata")):
            cmake = self.cross(name, profile)[0][2]
            self.assertIn("ENABLE_LLVM_PROFILE_GENERATION=", cmake, name)
            self.assertIn("USE_PGO_PROFILE=", cmake, name)

    def test_an_unknown_one_is_refused_listing_them_and_a_profile_needs_the_measured_one(self):
        self.assertIn("wpe-cross-pgo-use", self.cross("wpe-cross-pgo")[1])
        self.assertIn("That is 'wpe-cross-pgo-use'", self.cross("wpe-cross", "/x.profdata")[1])


class TestTheRecordAStageWrites(YoctoTest):
    def test_a_stage_that_says_it_is_done_ends_ok_stepping_only_its_own_index(self):
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn("built %s-" % PROFILE, err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("name"), t.field("stage"), t.field("exit")), ("yocto", WS, "image", "0"))
        self.assertEqual(t.plan(), list(yocto.STAGES))
        self.assertEqual(t.steps(), [(1, "pending"), (2, "pending"), (3, "running"), (4, "pending"), (5, "pending"), (6, "pending")])
        self.assertEqual((t.field("log"), t.field("kill")), (self.w.log, "wk sysimage build %s --stage image --stop" % PROFILE))
        self.assertEqual(t.field("subject"), "image stage of %s" % WS)

    def test_the_record_holds_no_deadline_since_silence_is_not_a_failure(self):
        self.w.env["WK_ABORT_SECONDS"] = "60"
        self.build()
        self.assertEqual(self.w.recs().list()[0].field("abort_after"), "")

    def test_the_stage_runs_in_the_workspace_under_the_wrapper(self):
        self.build()
        argv = self.w.watched()
        self.assertEqual(argv[:12], ["exec", WS, "env", "PYTHONPATH=/opt/wk-tools/lib", "python3", "-m", "wk.sysimage.task",
                                     "stage", "yocto", "--", "python3", "/opt/wk-tools/lib/wk/sysimage/yocto_target.py"])
        for flag, value in (("--target", CROSS_TARGET), ("--stage", "image"), ("--board", "rpi4"), ("--jobs", str(self.cores())),
                            ("--rm-work", "1"), ("--chromium", "0"), ("--cross-config", "wpe-cross"),
                            ("--sstate-ns", self.w.tag().rsplit("/", 1)[-1].replace(":", "-"))):
            self.assertEqual(argv[argv.index(flag) + 1], value, flag)
        self.assertNotIn("--slot", argv)

    def test_the_booked_budget_is_what_the_stage_enforces(self):
        self.build()
        argv = self.w.watched()
        (f,) = self.w.budget_files()
        self.assertIn("label=wk sysimage image %s\n" % WS, self.w.files[f])
        self.assertIn("\njobs=%d\n" % self.cores(), self.w.files[f])
        self.assertIn("budget_mb=%s\n" % argv[argv.index("--mem-budget") + 1], self.w.files[f])

    def test_the_flags_override_the_profile(self):
        self.build("--keep-work", "--chromium", "--no-local-layer", "--local-layer", "--no-tailnet")
        argv = self.w.watched()
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--rm-work", "--chromium", "--local-layer", "--tailnet")],
                         ["0", "1", "1", "0"])

    def test_done_is_the_wrapper_s_marker_not_the_exit_status(self):
        self.w.out = b"wk-yocto: layers already synced\n"
        self.assertIn("it exited 0 and never said it was done", self.refused())
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_a_slot_is_the_webkit_stage_booked_at_its_own_jobs(self):
        self.w.out = b"wk-yocto: stage 'webkit' done\n"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.w.driver().webkit(["--commit", SHA, "--slot", "base"])
        self.assertEqual(rc, 0, err.getvalue())
        argv = self.w.watched()
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--stage", "--commit", "--slot", "--profile")],
                         ["webkit", SHA, "base", PROFILE])
        (f,) = self.w.budget_files()
        jobs = int(argv[argv.index("--webkit-jobs") + 1])
        self.assertIn("jobs=%d\nbudget_mb=%d\n" % (jobs, jobs * yocto.WEBKIT_MB_PER_JOB), self.w.files[f])
        self.assertIn("slot base in %s at aaaaaaaaaaaa" % WS, self.w.recs().list()[0].field("subject"))


class TestTheWorkspace(YoctoTest):
    def test_a_workspace_that_is_not_there_is_made_from_the_host_image(self):
        self.w.made = False
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        (new,) = [e for e in self.w.effects if e[0] == "run_tty" and e[1][:1] == ("env",)]
        self.assertEqual(list(new[1]), ["env", "WK_SDK_IMAGE=" + self.w.tag(), str(REPO / "wk"), "new", WS, "--target", "box"])

    def test_the_host_image_is_tagged_by_its_base_and_its_containerfile(self):
        self.assertRegex(self.w.tag(), r"^localhost/wk-yocto-host:24\.04-[0-9a-f]{8}$")

    def test_wk_yocto_base_names_another_host(self):
        self.w.env["WK_YOCTO_BASE"] = "docker.io/library/ubuntu:26.04"
        base, tag = self.w.driver().host_image()
        self.assertEqual(base, "docker.io/library/ubuntu:26.04")
        self.assertIn(":26.04-", tag)

    def test_a_workspace_made_from_another_image_is_refused_naming_the_remake(self):
        self.w.answer(["podman", "container", "inspect"], out="localhost/wk-yocto-host:24.04-old\n")
        err = self.refused()
        self.assertIn("wk rm %s && wk sysimage build %s" % (WS, PROFILE), err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_a_workspace_on_another_branch_is_checked_out_from_the_mirror(self):
        self.w.head = "main\n"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        (co,) = [e for e in self.w.effects if e[0] == "run" and "git checkout -q" in " ".join(e[1])]
        self.assertIn("git fetch -q wpe wpe-2.46:wpe-2.46", co[1][-1])

    def test_a_branch_the_mirror_lacks_names_the_sync_that_fills_it(self):
        self.w.head, self.w.checkout_rc = "main\n", 1
        err = self.refused()
        self.assertIn("could not check out 'wpe-2.46' from 'wpe'", err)
        self.assertIn("        wk sync\n", err)

    def test_a_workspace_on_the_branch_is_left_alone(self):
        self.build()
        self.assertFalse([e for e in self.w.effects if e[0] == "run" and "git checkout -q" in " ".join(e[1])])

    def test_a_branch_with_no_section_for_the_target_is_refused_listing_what_it_has(self):
        self.w.sections = Result(0, "[rpi3-32bits-mesa]\n")
        err = self.refused()
        self.assertIn("wpe-2.46 has no [%s] section" % CROSS_TARGET, err)
        self.assertIn("      rpi3-32bits-mesa\n", err)
        self.assertFalse([e for e in self.w.effects if e[0] == "watch"])

    def test_an_unreadable_targets_conf_is_refused_rather_than_read_as_present(self):
        self.w.sections = Result(1)
        self.assertIn("could not read Tools/yocto/targets.conf", self.refused())

    def test_a_profile_that_derives_its_target_is_not_asked_for_one(self):
        self.w.sections = Result(0, "")
        p = dict(self.w.profile(), YOC_PORT_TARGET_FROM="rpi3-32bits-mesa", YOC_MACHINE="raspberrypi4-64")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = yocto.Yocto(self.w.reg, p, PROFILE, self.w.clock, self.w.popen).build([])
        self.assertEqual(rc, 0, err.getvalue())
        argv = self.w.watched()
        self.assertEqual(argv[argv.index("--port-target-from") + 1:argv.index("--port-target-from") + 4],
                         ["rpi3-32bits-mesa", "--port-machine", "raspberrypi4-64"])


class TestRefusals(YoctoTest):
    def test_a_running_stage_is_refused_by_name(self):
        t = self.w.running("toolchain")
        err = self.refused("--stage", "fetch")
        self.assertIn("a 'toolchain' build is already running in '%s'" % WS, err)
        self.assertIn("Stop it:    wk sysimage build %s --stage toolchain --stop" % PROFILE, err)
        self.assertEqual(t.field("exit"), "")

    def test_a_target_that_is_not_a_container_is_refused(self):
        with mock.patch.object(Box, "kind", "remote"):
            self.assertIn("needs a container workspace, and target 'box' is a remote one", self.refused())

    def test_a_slot_s_arguments_belong_to_the_webkit_stage(self):
        self.assertIn("belong to the webkit stage", self.refused("--commit", SHA, "--slot", "s"))
        self.assertIn("needs both --commit", self.refused("--stage", "webkit", "--slot", "s"))
        self.assertIn("40 hex digits", self.refused("--stage", "webkit", "--slot", "s", "--commit", "abc"))
        self.assertIn("--slot <name>", self.refused("--stage", "pgo-mix"))
        self.assertIn("--commit builds", self.refused("--stage", "pgo-mix", "--slot", "s", "--commit", SHA))
        self.assertIn("builds no WebKit", self.refused("--config", "wpe-cross-pgo-collect"))

    def test_an_unknown_option_is_a_usage_error(self):
        self.assertIn("unknown option: --bogus", self.refused("--bogus"))

    def test_too_little_disk_is_a_barrier(self):
        self.w.answer(["df", "-Pk"], out="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/x 1 1 1048576 1% /\n")
        self.assertIn("1 GB free", self.refused())
        self.assertEqual(self.w.recs().list(), [])


class TestTheWatchdog(YoctoTest):
    def test_a_silent_stage_is_reported_and_not_killed(self):
        self.w.out, self.w.polls, self.w.rc = b"", 600, 1
        self.w.env["WK_ABORT_SECONDS"] = "60"
        err = self.refused()
        self.assertIn("no output for 30s", err)
        self.assertIn("not stopping it: silence is not a failure here", err)
        self.assertFalse([e for e in self.w.effects if e[0] == "kill"])
        self.assertEqual(self.w.recs().list()[0].field("exit"), "1")

    def test_one_naming_the_same_task_beat_after_beat_is_wedged_and_given_up_on(self):
        def grow():
            with open(self.w.log, "a") as f:
                f.write(TASK_LINE)
        self.w.out, self.w.polls, self.w.grow = b"", None, grow
        self.w.answer(["sh", "-c", job.TREE, "wk", "4242"], out="4242\n")
        with mock.patch.object(yocto, "WEDGE_BEATS", 3):
            err = self.refused()
        self.assertIn("wedged: the log has named clang_git.bb:do_compile for 180s", err)
        self.assertIn(("kill", 4242, 15), self.w.effects)
        self.assertIn("the image build in '%s' stalled" % WS, err)
        self.assertEqual(self.w.recs().list()[0].field("exit"), "stalled")

    def test_a_task_that_changes_is_never_wedged(self):
        n = [0]

        def grow():
            n[0] += 1
            with open(self.w.log, "a") as f:
                f.write(TASK_LINE.replace("7658", str(n[0])).replace("clang_git", "r%d" % (n[0] // 6)))
        self.w.polls, self.w.grow = 600, grow
        with mock.patch.object(yocto, "WEDGE_BEATS", 3):
            rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertNotIn("wedged", err)

    def test_the_task_a_log_names_is_the_last_one(self):
        log = self.tmp / "b.log"
        log.write_text("NOTE: recipe zlib-1.3-r0: task do_fetch: Started\n" + TASK_LINE + "WARNING: x\n")
        self.assertEqual(yocto.task_named(str(log)), "clang_git.bb:do_compile")
        log.write_text(TASK_LINE + "NOTE: recipe zlib-1.3-r0: task do_fetch: Started\n")
        self.assertEqual(yocto.task_named(str(log)), "zlib-1.3-r0:do_fetch")
        log.write_text("wk-yocto: syncing\n")
        self.assertEqual(yocto.task_named(str(log)), "")


class TestStop(YoctoTest):
    def adopted(self, stage):
        t = self.w.running(stage, pid=1)
        t.set("pid_match", yocto.PATTERN)
        t.pid(777)
        t.set("where", "target")
        self.w.pids.add(777)
        self.w.answer(["exec", WS, "ps", "-o", "args=", "-p", "777"], out="python3 /opt/wk-tools/lib/wk/sysimage/yocto_target.py\n")
        self.w.answer(["exec", WS, "sh", "-c"], out="778\n777\n")
        self.w.react(["exec", WS, "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        return t

    def cooker(self, args="python3 /w/sources/poky/bitbake/bin/bitbake-server decafbad\n"):
        self.w.files[self.w.lock] = "555\n"
        self.w.answer(["exec", WS, "ps", "-o", "args=", "-p", "555"], out=args)
        self.w.answer(["exec", WS, "sh", "-c", job.TREE, "wk", "555"], out="556\n555\n")

    def test_stop_kills_the_stage_s_tree_and_then_says_it_resumes(self):
        t = self.adopted("image")
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn(("run", ("exec", WS, "kill", "-TERM", "778", "777")), self.w.effects)
        self.assertIn("restarting resumes", err)
        self.assertEqual(t.field("exit"), "cancelled")

    def test_stop_of_a_stage_that_is_not_the_running_one_touches_nothing(self):
        t = self.adopted("toolchain")
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn("no 'image' build is running in '%s'; its 'toolchain' stage is." % WS, err)
        self.assertFalse([e for e in self.w.effects if e[1][2:4] in (("kill", "-TERM"), ("kill", "-KILL"))])
        self.assertEqual(t.field("exit"), "")

    def test_a_cooker_a_killed_driver_left_is_stopped_when_no_record_holds_it(self):
        self.cooker()
        self.w.react(["exec", WS, "kill", "-TERM"], lambda a, f: (f.answer(["exec", WS, "ps", "-o", "args=", "-p", "555"]), Result(0))[1])
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertIn("no 'image' build is running in '%s'" % WS, err)
        self.assertIn(("run", ("exec", WS, "kill", "-TERM", "556", "555")), self.w.effects)
        self.assertIn("the cooker is gone", err)

    def test_a_lock_naming_a_pid_that_is_not_bitbake_is_left_alone(self):
        self.cooker("/usr/sbin/sshd -D\n")
        rc, err = self.build("--stop")
        self.assertEqual(rc, 0, err)
        self.assertFalse([e for e in self.w.effects if e[1][2:4] in (("kill", "-TERM"), ("kill", "-KILL"))])

    def test_a_cooker_that_outlives_a_term_and_a_kill_is_named(self):
        self.cooker()
        self.w.answer(["exec", WS, "kill"])
        rc, err = self.build("--stop")
        self.assertEqual(rc, 1)
        self.assertIn(("run", ("exec", WS, "kill", "-KILL", "556", "555")), self.w.effects)
        self.assertIn("wk stop %s" % WS, err)

    def test_stop_in_no_workspace_is_refused(self):
        self.w.made = False
        self.assertIn("no workspace '%s', so nothing is building" % WS, self.refused("--stop"))


class Detaching(World):
    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        self.recs().begin("yocto", "here", WS, "k", self.log, list(yocto.STAGES), pid=pid)
        return pid


class TestDetachAndDryRun(YoctoTest):
    def test_detach_returns_once_its_own_child_has_begun_its_record(self):
        w = Detaching(self.tmp)
        rc, err = self.build("--stage", "fetch", "--detach", w=w)
        self.assertEqual(rc, 0, err)
        (sp,) = [e for e in w.effects if e[0] == "spawn"]
        self.assertEqual(list(sp[1]), [str(REPO / "wk"), "sysimage", "build", PROFILE, "--stage", "fetch"])
        self.assertEqual(sp[2], os.path.join(w.ws_dir, "detached-fetch.log"))
        self.assertFalse([e for e in w.effects if e[0] == "watch"])

    def test_a_dry_run_reports_the_plan_and_changes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build()
        self.assertEqual(rc, 0, err)
        self.assertIn("would build image %s (builder: yocto)" % PROFILE, err)
        self.assertIn("  cross-target %s  (verified on wpe-2.46)" % CROSS_TARGET, err)
        self.assertIn("  wifi        wk-wifi-join in the image", err)
        self.assertEqual(self.w.recs().list(), [])
        self.assertEqual([e for e in self.w.effects if e[0] != "run"], [])

    def test_a_dry_run_of_the_mix_names_the_collection_on_both_sides(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build("--stage", "pgo-mix", "--slot", "pr")
        self.assertEqual(rc, 0, err)
        self.assertIn("  collection  %s" % images.pgo_dir(WS, "pr", self.w.env), err)
        self.assertIn("  into        /src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata", err)


class TestKillPoints(YoctoTest):
    def test_a_stage_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sysimage build]` for yocto: each run its own record, whatever the killed one held gone with it."""
        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.driver().build([])
        converges(self, lambda: World(self.tmp), run_once, World.state)


if __name__ == "__main__":
    unittest.main()
