"""`wk sysimage build` of a yocto preset (lib/wk/sysimage/yocto.py) as a task against a Fake world."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO
from tests import test_sysimage_task

sys.path.insert(0, str(REPO / "lib"))
from wk import images, job, resources  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.sysimage import yocto  # noqa: E402

PRESET = "wpewebkit-2.46-yocto-rpi4-64"
WS = "yocto-" + PRESET
CROSS_TARGET = "rpi4-64bits-mesa"
SHA = "a" * 40
TASK_LINE = "NOTE: Running task 7658 of 13213 (virtual:native:/w/sources/meta-clang/recipes-devtools/clang/clang_git.bb:do_compile)\n"


class World(test_sysimage_task.World):
    """The workspace on the preset's branch, its targets.conf holding the preset's section."""

    BUILDER, PRESET, KIND = yocto.Yocto, PRESET, "yocto"
    OUT = b"wk-yocto: target rpi4-64bits-mesa\nwk-yocto: stage 'image' done\n"
    KILL = "wk sysimage build %s --stage image --stop" % PRESET
    DONE, IDLE, STOPPED = "built %s-" % PRESET, "no 'image' build is running in '%s'" % WS, "restarting resumes"
    DETACHED = "'image' stage of " + PRESET
    DRY = ("  cross-target %s  (verified on wpe-2.46)" % CROSS_TARGET, "  wifi        wk-wifi-join in the image")
    SUBJECT, UNKNOWN = "image stage of %s" % WS, (["--resume"],)

    def __init__(self, tmp):
        super().__init__(tmp)
        self.env.update({"WK_POLL_SECONDS": "10", "WK_STALL_SECONDS": "30", "WK_HEARTBEAT_SECONDS": "60"})
        self.answer(["nproc"], out="12\n")
        self.answer(["sysctl", "-n", "hw.ncpu"], out="12\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="%d\n" % (32 << 30))
        self.files["/proc/meminfo"] = "MemTotal: %d kB\nMemAvailable: %d kB\n" % (32 << 20, 30 << 20)
        self.head, self.checkout_rc = "wpe-2.46\r\n", 0
        self.sections = Result(0, "[rpi3-32bits-mesa]\n[%s]\nimage_basename = webkit-dev-ci-tools\n" % CROSS_TARGET)
        self.react(["exec", WS, "bash", "-c"], self._bash)
        self.lock = os.path.join(yocto.host_workdir(self.ws_dir, CROSS_TARGET), "build", "bitbake.lock")

    def _bash(self, argv, f):
        line = argv[4]
        if "git rev-parse --abbrev-ref HEAD" in line:
            return Result(0, self.head)
        if line.startswith("cat "):
            return self.sections
        return Result(self.checkout_rc)

    def running(self, stage="image", pid=77, kill=None):
        t = self.recs().begin("yocto", "here", WS, kill or "wk sysimage build %s --stage %s --stop" % (PRESET, stage), self.log,
                              list(yocto.STAGES), pid=pid)
        t.step_state(yocto.stage_index(stage), "running")
        self.pids.add(pid)
        return t

    def plan(self):
        return list(yocto.STAGES)

    def steps(self):
        return [(1, "pending"), (2, "pending"), (3, "running"), (4, "pending"), (5, "pending"), (6, "pending")]

    def cores(self):
        return resources.Resources(self, self.env).envelope_cores()

    def flags(self):
        return {"--target": CROSS_TARGET, "--stage": "image", "--board": "rpi4", "--jobs": str(self.cores()), "--rm-work": "1",
                "--chromium": "0", "--cross-preset": "wpe-cross", "--sstate-ns": self.tag().rsplit("/", 1)[-1].replace(":", "-")}

    def booked(self):
        argv = self.watched()
        return self.cores(), int(argv[argv.index("--mem-budget") + 1])


class YoctoTest(test_sysimage_task.TaskTest):
    WORLD = World


class TestTheYoctoLifecycle(test_sysimage_task.Lifecycle, YoctoTest):
    pass


class TestTheStages(unittest.TestCase):
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
                return yocto.cross_preset(name, profile), ""
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

    def test_each_pgo_config_states_both_options_and_its_lto(self):
        """They share one build directory and cmake refuses the pair; LTO is thin to collect, full to measure."""
        for (name, profile), lto in ((("wpe-cross-pgo-collect", ""), "thin"), (("wpe-cross-pgo-use", "/x.profdata"), "full")):
            cmake = self.cross(name, profile)[0][2]
            self.assertIn("ENABLE_LLVM_PROFILE_GENERATION=", cmake, name)
            self.assertIn("USE_PGO_PROFILE=", cmake, name)
            self.assertIn("-DLTO_MODE=" + lto, cmake, name)

    def test_an_unknown_one_is_refused_listing_them_and_a_profile_needs_the_measured_one(self):
        self.assertIn("wpe-cross-pgo-use", self.cross("wpe-cross-pgo")[1])
        self.assertIn("only 'wpe-cross-pgo-use'", self.cross("wpe-cross", "/x.profdata")[1])


class TestTheRecordAStageWrites(YoctoTest):
    def test_the_record_holds_no_deadline_since_silence_is_not_a_failure(self):
        self.w.env["WK_ABORT_SECONDS"] = "60"
        self.build()
        self.assertEqual(self.w.recs().list()[0].field("abort_after"), "")

    def test_the_flags_override_the_preset(self):
        self.build("--keep-work", "--chromium", "--no-local-layer", "--local-layer", "--no-tailnet")
        argv = self.w.watched()
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--rm-work", "--chromium", "--local-layer", "--tailnet")],
                         ["0", "1", "1", "0"])

    def test_a_slot_is_the_webkit_stage_booked_at_its_own_jobs(self):
        self.w.out = b"wk-yocto: stage 'webkit' done\n"
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = self.w.driver().webkit(["--commit", SHA, "--slot", "base"])
        self.assertEqual(rc, 0, err.getvalue())
        argv = self.w.watched()
        self.assertEqual([argv[argv.index(f) + 1] for f in ("--stage", "--commit", "--slot", "--image-preset")],
                         ["webkit", SHA, "base", PRESET])
        (f,) = self.w.budget_files()
        jobs = int(argv[argv.index("--webkit-jobs") + 1])
        self.assertIn("jobs=%d\nbudget_mb=%d\n" % (jobs, jobs * yocto.WEBKIT_MB_PER_JOB), self.w.files[f])
        self.assertIn("slot base in %s at aaaaaaaaaaaa" % WS, self.w.recs().list()[0].field("subject"))


class TestTheWorkspace(YoctoTest):
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
        self.assertIn("wk sync", err)

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

    def test_a_preset_that_derives_its_target_is_not_asked_for_one(self):
        self.w.sections = Result(0, "")
        p = dict(self.w.preset(), YOC_PORT_TARGET_FROM="rpi3-32bits-mesa", YOC_MACHINE="raspberrypi4-64")
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = yocto.Yocto(self.w.reg, p, PRESET, self.w.clock).build([])
        self.assertEqual(rc, 0, err.getvalue())
        argv = self.w.watched()
        self.assertEqual(argv[argv.index("--port-target-from") + 1:argv.index("--port-target-from") + 4],
                         ["rpi3-32bits-mesa", "--port-machine", "raspberrypi4-64"])


class TestRefusals(YoctoTest):
    def test_a_running_stage_is_refused_by_name(self):
        t = self.w.running("toolchain")
        err = self.refused("--stage", "fetch")
        self.assertIn("a 'toolchain' build is already running in '%s'" % WS, err)
        self.assertIn("Stop it:    wk sysimage build %s --stage toolchain --stop" % PRESET, err)
        self.assertEqual(t.field("exit"), "")

    def test_a_slot_s_arguments_belong_to_the_webkit_stage(self):
        self.assertIn("belong to the webkit stage", self.refused("--commit", SHA, "--slot", "s"))
        self.assertIn("needs both --commit", self.refused("--stage", "webkit", "--slot", "s"))
        self.assertIn("40 hex digits", self.refused("--stage", "webkit", "--slot", "s", "--commit", "abc"))
        self.assertIn("--slot <name>", self.refused("--stage", "pgo-mix"))
        self.assertIn("--commit builds", self.refused("--stage", "pgo-mix", "--slot", "s", "--commit", SHA))
        self.assertIn("builds no WebKit", self.refused("--preset", "wpe-cross-pgo-collect"))

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
        t.set("where", "place")
        self.w.pids.add(777)
        self.w.answer(["exec", WS, "ps", "-o", "args=", "-p", "777"], out="python3 /opt/wk-tools/lib/wk/sysimage/yocto_ws.py\n")
        self.w.answer(["exec", WS, "sh", "-c"], out="778\n777\n")
        self.w.react(["exec", WS, "kill", "-TERM"], lambda a, f: (f.pids.discard(777), Result(0))[1])
        return t

    def cooker(self, args="python3 /w/sources/poky/bitbake/bin/bitbake-server decafbad\n"):
        self.w.files[self.w.lock] = "555\n"
        self.w.answer(["exec", WS, "ps", "-o", "args=", "-p", "555"], out=args)
        self.w.answer(["exec", WS, "sh", "-c", job.TREE, "wk", "555"], out="556\n555\n")

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


class TestDryRun(YoctoTest):
    def test_a_dry_run_of_the_mix_names_the_collection_on_both_sides(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.build("--stage", "pgo-mix", "--slot", "pr")
        self.assertEqual(rc, 0, err)
        self.assertIn("  collection  %s" % images.pgo_dir(WS, "pr", self.w.env), err)
        self.assertIn("  into        /src/WebKit/WebKitBuild/wk-pgo/pr/output/WPEWebKit.profdata", err)


if __name__ == "__main__":
    unittest.main()
