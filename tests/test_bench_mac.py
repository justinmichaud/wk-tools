"""The Mac's benchmark install as a bench system (lib/wk/bench/mac.py) against a Fake install: `wk bench stage` delivering
a workspace's build, `wk bench staged` as the one pipeline on the running install, the gates asked before anything
reboots, and where each `wk bench` verb runs. Nothing here touches a Mac, bless, a helper or a browser.

Rows landed here: `unit bench.pipeline_conformance[mac-volume]`, `unit bench.one_record[mac-volume]`,
`unit bench.preflight_asks_every_gate`, `unit dispatch.where[bench]`, `unit killpoints[bench stage]`.

Run: python3 tests/run.py -k test_bench_mac
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import plistlib
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import decl, shell, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import cli, mac, record as brecord, report  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.quiet import PRIV, lib_argv  # noqa: E402

BENCH = REPO / "cmd" / "bench"
CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_bench_mac", str(BENCH))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_bench_mac", CMD_LOADER))
CMD_LOADER.exec_module(CMD)
SHA = "0123456789abcdef0123456789abcdef01234567"
RESULT = json.dumps({"Speedometer-3": {"metrics": {"Score": {"current": [30.0, 31.0]}}}})
MARKERS = {"mbp": "id=perf-macos-tolken-2026-09\nprofile=perf-macos-tolken\n", "benchvm": "id=perf-macos-benchvm\n"}
CONFS = {"mbp": 'KIND=mac\nNODE_SSH="tolken"\nNODE_DRIVER=mac-volume\nNODE_PROFILE=perf-macos-tolken\n'
                'NODE_VOLUME="WK Bench"\nNODE_DISPLAY="builtin 1280x832"\n',
         "benchvm": "KIND=guest\nNODE_DRIVER=mac-guest\nNODE_PROFILE=perf-macos-benchvm\n"}
STAGE_ID = "20260901T000000Z-mac-release"


def args(*argv):
    return decl.Args(decl.Decl(BENCH), list(argv))


class Proc:
    def __init__(self, rc):
        self.pid, self.rc, self.returncode = 4242, rc, None

    def poll(self):
        self.returncode = self.rc
        return self.rc

    def wait(self):
        return self.rc


class World(Fake):
    """A Mac with its two installs' confs, one stage on its volume, and run-benchmark writing its --output-file."""

    def __init__(self, tmp, machine="mbp", bench=True):
        super().__init__("here")
        self.fake, self.rc, self.watched = self, 0, []
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.home = str(self.tmp / "var-wk")
        machines = self.tmp / "machines"
        machines.mkdir()
        for name, text in CONFS.items():
            (machines / (name + ".conf")).write_text(text)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_CONFIG_HOME": str(self.tmp / "config"),
                    "WK_MACHINES_DIR": str(machines), "WK_BENCH_ROOT": self.home, "WK_BENCH_PYTHON": "/py", "WK_POLL_SECONDS": "1"}
        self.clock = FakeClock()
        self.reg = targets.Registry(REPO, env=self.env, machine=self)
        if bench:
            self._set_file(mac.MARKER, MARKERS[machine])
        self.stage = os.path.join(self.home, "staged", STAGE_ID)
        self.build = os.path.join(self.stage, "WebKitBuild", "Release")
        self._set_file(os.path.join(self.stage, "stage.json"), json.dumps({
            "workspace": "ws", "workspace_target": "vm", "config": "mac-release", "webkit_sha": SHA,
            "plans": "speedometer3", "wk_tools": "abc"}))
        self._set_file(os.path.join(self.build, "MiniBrowser.app/Contents/MacOS/MiniBrowser"), "")
        self.dirs.add(os.path.join(self.build, "JavaScriptCore.framework"))
        self._set_file(os.path.join(self.stage, "Tools/Scripts/run-benchmark"), "")
        self.dirs.add(os.path.join(self.stage, "payload", "speedometer3"))
        disk = plistlib.dumps({"DeviceNode": "/dev/disk3s1", "BusProtocol": "Apple Fabric", "SolidState": True}).decode()
        for prefix, out in ((["bash", "-c"], ""), (["uname", "-s"], "Darwin\n"), (["uname", "-m"], "arm64\n"), (["uname", "-r"], "25.0.0\n"),
                            (["test"], ""), (["/py"], "displays=count=1 builtin=1 points=[1280, 832]\n"), (["/py", "-c"], "1280x832\n"),
                            (["/py", "-c", "import objc"], ""), (["stat"], "bench\n"), (["id", "-un"], "bench\n"),
                            (["sudo", "-n"], "0\n"), (["sudo", "-n", PRIV, "status"], "wk-quiesce-priv: on\n"),
                            (["tmutil"], ""), (["sysctl", "-n", "vm.loadavg"], "{ 0.50 0.40 0.30 }\n"),
                            (["sysctl", "-n", "hw.model"], "Mac16,12\n"), (["sysctl", "-n", "hw.ncpu"], "12\n"),
                            (["sysctl", "-n", "hw.memsize"], "17179869184\n"), (["sw_vers"], "26.0\n"),
                            (["pmset", "-g", "therm"], "CPU_Speed_Limit = 100\n"), (["pmset", "-g", "batt"], "Now drawing from 'AC Power'\n"),
                            (["diskutil"], disk), (["python3"], "0.0\n"), (["python3", os.path.join(str(REPO), mac.WKMAC), "auto-brightness"], "off\n"),
                            ([os.path.join(str(REPO), "wk"), "bench", "staged"], "")):
            self.answer(prefix, out=out)
        for quiet in ("SecurityAgent", mac.VM_PROCESS):
            self.answer(["pgrep", "-x", quiet], rc=1)
        self.answer(["pgrep", "-n"], rc=1)
        self.answer(["tmutil"], err="No destinations configured\n")

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.watched.append(list(argv))
        stdout.write(b"wk: bench pid 77\nScore: 30\n")
        words = shlex.split(argv[-1].split("exec ", 1)[1])
        Path(words[words.index("--output-file") + 1]).write_text(RESULT)
        return Proc(self.rc)

    def results(self):
        d = os.path.join(self.home, "results")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def env_json(self):
        (run,) = self.results()
        return json.loads(Path(self.home, "results", run, "env.json").read_text())


class MacTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-bench-mac-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        saved = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_BENCH_ASLR", "WK_BENCH_PATH_PAD", "WK_BENCH_ENV_PAD"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        self.w = World(self.tmp)

    def staged(self, *argv, w=None, dry=False):
        w = w or self.w
        err = io.StringIO()
        if dry:
            os.environ["WK_DRY_RUN"] = "1"
        try:
            with contextlib.redirect_stderr(err):
                rc = mac.staged(REPO, w.reg, w.clock, CMD.staged_options(args("staged", *argv)), popen=w.popen)
        finally:
            os.environ.pop("WK_DRY_RUN", None)
        return rc, err.getvalue()

    def refused(self, *argv, w=None, dry=False):
        with self.assertRaises(Refused) as cm:
            self.staged(*argv, w=w, dry=dry)
        return cm.exception


class TestConformance(MacTest):
    def test_the_staged_run_is_the_one_pipeline_into_the_one_record(self):
        """`bench.pipeline_conformance[mac-volume]`: boot, deploy, run, collect -- the result in the run directory on the volume."""
        rc, err = self.staged("--plan", "speedometer3")
        self.assertEqual(rc, 0, err)
        self.assertIn("BENCH OK  speedometer3", err)
        (run,) = self.w.results()
        self.assertTrue(run.endswith("-speedometer3-" + STAGE_ID), run)
        self.assertEqual(Path(self.w.home, "results", run, "result.json").read_text(), RESULT)
        script = self.w.watched[0][-1]
        for word in ("/py %s/Tools/Scripts/run-benchmark" % self.w.stage, "--platform osx", "--build-directory " + self.w.build,
                     "--local-copy %s/payload/speedometer3" % self.w.stage):
            self.assertIn(word, script)

    def test_the_plan_defaults_to_the_stages_first_and_the_timeout_reaches_run_benchmark(self):
        rc, err = self.staged("--timeout", "600")
        self.assertEqual(rc, 0, err)
        self.assertIn("--plan speedometer3", self.w.watched[0][-1])
        self.assertIn("--timeout 600", self.w.watched[0][-1])

    def test_the_run_is_bracketed_by_the_screen_watch(self):
        self.staged()
        order = [e[1][2].split(";")[1].split()[0] for e in self.w.effects
                 if e[0] == "run" and e[1][:1] == ("bash",) and "screen_watch" in e[1][2]]
        self.assertEqual(order, ["screen_watch_start", "screen_watch_stop"])


class TestTheRecord(MacTest):
    def test_a_staged_run_records_its_provenance(self):
        """`bench.one_record[mac-volume]`: kernel, arch, profile, root device, cores, the stage it ran and the machine."""
        self.staged()
        env = self.w.env_json()
        self.assertEqual((env["bench_host"], env["machine"], env["measures"], env["profile"], env["config"], env["webkit_sha"]),
                         ("image", "mbp", True, "perf-macos-tolken", "mac-release", SHA))
        self.assertEqual((env["host"]["kernel"], env["host"]["kernel_arch"], env["host"]["cores"], env["host"]["root_device"]),
                         ("25.0.0", "arm64", "12", "/dev/disk3s1 (Apple Fabric, ssd)"))
        self.assertEqual((env["stage"]["id"], env["staged_from"]), (STAGE_ID, self.w.stage))
        self.assertEqual((env["configuration"]["aslr"], env["role_marker_overridden"], env["forced"]), ("os-randomised", False, False))
        self.assertIn("wall_time_s", env)
        rundir = os.path.join(self.w.home, "results", self.w.results()[0])
        self.assertEqual(brecord.run_state(rundir, env), "ok")

    def test_a_rehearsal_is_refused_as_a_measurement(self):
        """A mac-guest (B_MEASURES=no) proves every phase, and its reading is recorded as no measurement."""
        w = World(self.tmp, "benchvm")
        rc, err = self.staged(w=w)
        self.assertEqual(rc, 0, err)
        env = w.env_json()
        self.assertEqual((env["machine"], env["measures"]), ("benchvm", False))
        self.assertIn("rehearsal", brecord.not_a_measurement(env))
        rundir = os.path.join(w.home, "results", w.results()[0])
        self.assertEqual(brecord.run_state(rundir, env), "rehearsal")

    def test_a_rehearsed_round_ends_the_task_and_is_never_usable(self):
        task = self.tmp / "task"
        brecord.task_write(str(task), ["task=t", "requested=now", "devices=benchvm=mac-release", "plans=speedometer3",
                                       "rounds=1", "slots=a,b"], ["wk bench ab --devices mbp"])
        for arm in ("a", "b"):
            d = task / "runs" / arm
            d.mkdir(parents=True)
            (d / "result.json").write_text(RESULT)
            brecord.write_env(str(d / "env.json"), ["machine=benchvm", "plan=speedometer3", "ab.round=1", "ab.arm=" + arm],
                              bool_fields=["measures="])
        st = brecord.task_state(str(task), False)
        self.assertEqual((st["state"], st["ok"], st["usable"]), ("complete", 0, 0))
        self.assertIn("2 rehearsed (no measurement)", st["summary"])

    def test_the_two_run_report_refuses_a_rehearsal(self):
        sides = []
        for name, measures in (("a", "1"), ("b", "")):
            d = self.tmp / name
            d.mkdir()
            (d / "result.json").write_text(RESULT)
            brecord.write_env(str(d / "env.json"), ["machine=benchvm", "plan=speedometer3"], bool_fields=["measures=" + measures])
            sides.append(str(d))
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            report.two_runs([sides[0]], [sides[1]])
        self.assertIn("side B: %s -- benchvm is a rehearsal" % sides[1], str(cm.exception.code))

    def test_the_shared_cache_knob_is_exported_and_recorded(self):
        self.w.env["WK_BENCH_SHARED_CACHE"] = "avoid"
        self.staged()
        self.assertIn("export DYLD_SHARED_REGION=avoid", self.w.watched[0][-1])
        self.assertIs(self.w.env_json()["configuration"]["shared_cache"], False)

    def test_the_declared_display_is_judged_and_recorded(self):
        self.staged("--expect-display", "builtin 1280x832")
        asked = [e[1] for e in self.w.effects if e[0] == "run" and e[1][:2] == ("/py", os.path.join(str(REPO), mac.CHECK))]
        self.assertEqual(asked, [("/py", os.path.join(str(REPO), mac.CHECK), "--displays-only", "--expect-display", "builtin 1280x832")])
        self.assertEqual(self.w.env_json()["display_declared"], "builtin 1280x832")


class TestTheLegsOwnGates(MacTest):
    def test_host_mode_refuses_and_its_dry_run_says_so(self):
        w = World(self.tmp, bench=False)
        self.assertIn("this is host mode", str(self.said(w)))
        rc, err = self.staged(w=World(self.tmp, bench=False), dry=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("would fail -- a real run would stop here", err)
        self.assertIn("would run: bash -lc", err)

    def said(self, w):
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err):
            mac.staged(REPO, w.reg, w.clock, CMD.staged_options(args("staged")), popen=w.popen)
        return err.getvalue()

    def test_a_covered_screen_refuses_and_force_records_it(self):
        self.w.answer(lib_argv(str(REPO), mac.QUIET, "screen_blocker")[:3], out="UserNotificationCenter\n")
        self.assertIn("1 preflight check(s) failed", self.said(self.w))
        self.assertEqual(self.w.results(), [])
        w = World(self.tmp)
        w.answer(lib_argv(str(REPO), mac.QUIET, "screen_blocker")[:3], out="UserNotificationCenter\n")
        w.env["WK_FORCE"] = "1"
        rc, err = self.staged(w=w)
        self.assertEqual(rc, 0, err)
        self.assertTrue(w.env_json()["forced"])
        self.assertIn("the screen is free: on the screen, and nothing wk put there: UserNotificationCenter", w.env_json()["preflight_notes"])

    def test_the_leg_pauses_the_daemons_again_before_it_judges_them_and_its_dry_run_does_not(self):
        self.staged()
        pause = [i for i, e in enumerate(self.w.effects) if e[0] == "act" and e[1][:3] == ("sudo", "-n", "bash")]
        probe = [i for i, e in enumerate(self.w.effects) if e[0] == "run" and e[1][:2] == ("bash", "-c") and "wk_quiet_desktop_probe" in e[1][2]]
        self.assertTrue(pause and probe and pause[0] < probe[0], (pause, probe))
        dry = World(self.tmp)
        self.staged(w=dry, dry=True)
        self.assertEqual([e for e in dry.effects if e[0] == "act" and e[1][:3] == ("sudo", "-n", "bash")][:1],
                         [("act", tuple(["sudo", "-n"] + lib_argv(str(REPO), mac.DESKTOP, "wk_quiet_daemons_pause")))])

    def test_an_unknown_machine_is_a_failed_gate(self):
        w = World(self.tmp)
        w._set_file(mac.MARKER, "id=someone-else\n")
        self.assertIn("the marker names no machine", self.said(w))

    def test_nothing_staged_names_the_stage_command(self):
        w = World(self.tmp)
        w._drop(os.path.join(w.home, "staged"))
        self.assertIn("wk bench stage <workspace> --to mbp", self.said(w))

    def test_the_listing_names_each_stage_and_result(self):
        self.staged()
        rc, err = self.staged("--ls")
        self.assertEqual(rc, 0)
        self.assertIn(STAGE_ID, err)
        self.assertIn("mac-release 0123456789", err)


class TestTheProfile(MacTest):
    """--profile: samply waits for the web process and records it for the length of the run."""

    def capture(self, samply="/samply\n", web="321\n"):
        w = World(self.tmp)
        w.answer(shell.argv(str(REPO), '. "%s/lib/profiler.sh"; samply_fetch' % REPO, "arm64", "Darwin")[:3], out=samply)
        w.answer(["pgrep", "-n", "-f", mac.WEB_PROCESS], rc=0 if web else 1, out=web)
        rundir = self.tmp / ("run-%d" % len(os.listdir(self.tmp)))
        rundir.mkdir()
        c = mac.Capture(str(REPO), w, w.clock, "/tmp/p.json", str(rundir))
        with contextlib.redirect_stderr(io.StringIO()):
            c.run()
        return w, c

    def test_the_web_process_is_recorded(self):
        w, c = self.capture()
        self.assertTrue(c.taken)
        self.assertIn(("act", ("sudo", "-n", "/samply", "record", "--save-only", "--profile-name", "wk-warmup", "-o", "/tmp/p.json",
                               "-p", "321")), w.effects)

    def test_no_samply_or_no_web_process_takes_nothing(self):
        for samply, web in (("", "321\n"), ("/samply\n", "")):
            with self.subTest(samply=samply, web=web):
                w, c = self.capture(samply, web)
                self.assertFalse(c.taken)
                self.assertFalse([e for e in w.effects if e[0] == "act"])

    def test_a_staged_run_records_where_its_profile_is(self):
        self.w.answer(shell.argv(str(REPO), '. "%s/lib/profiler.sh"; samply_fetch' % REPO, "arm64", "Darwin")[:3], out="/samply\n")
        self.w.answer(["pgrep", "-n", "-f", mac.WEB_PROCESS], out="321\n")
        rc, err = self.staged("--profile", "/tmp/p.json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.w.env_json()["profile"], "/tmp/p.json")


class TestTheWatchdog(MacTest):
    def test_a_benchmarks_silence_is_waited_out_longer_than_a_builds_and_an_override_wins(self):
        w = self.w
        system = mac.MacVolumeSystem(REPO, w.reg, w.clock, mac.Install(REPO, w, w.env), w.home, w.stage, {})
        env = mac.StagedRun(REPO, w.reg, system, w.clock, w.env, None).env
        self.assertEqual((env["WK_STALL_SECONDS"], env["WK_ABORT_SECONDS"]), ("900", "5400"))
        env = mac.StagedRun(REPO, w.reg, system, w.clock, dict(w.env, WK_STALL_SECONDS="12"), None).env
        self.assertEqual(env["WK_STALL_SECONDS"], "12")


class TestPreflightAsksEveryGate(MacTest):
    """`bench.preflight_asks_every_gate`: each gate asked over the running install, in order, reading only."""

    def ask(self, w=None):
        w = w or self.w
        with contextlib.redirect_stderr(io.StringIO()):
            return mac.Gates(REPO, w, w.clock, w.env, "speedometer3", "builtin 1280x832", w.build, "/py").ask()

    def test_every_gate_is_asked_and_nothing_is_written(self):
        rows = self.ask()
        self.assertEqual([r[0] for r in rows], list(mac.GATES))
        self.assertEqual([r for r in rows if not r[1]], [])
        self.assertEqual([e for e in self.w.effects if e[0] != "run"], [])
        self.assertFalse([e for e in self.w.effects if "reboot" in " ".join(e[1]) or "wk-boot-priv" in " ".join(e[1])])
        self.assertIn(("run", (os.path.join(str(REPO), "wk"), "bench", "staged", "--dry-run", "--plan", "speedometer3",
                               "--expect-display", "builtin 1280x832")), self.w.effects)

    def test_each_gate_refuses_on_its_own_evidence(self):
        for gate, prefix, rc, out in (("quiesce readback", ["sudo", "-n", PRIV, "status"], 1, ""),
                                      ("brightness", ["python3"], 0, "0.85\n"),
                                      ("display mode", ["/py"], 1, "2 displays are online\n"),
                                      ("staged dry run", [os.path.join(str(REPO), "wk"), "bench", "staged"], 1, ""),
                                      ("window in front", lib_argv(str(REPO), mac.QUIET, "screen_blocker")[:3], 0, "?\n"),
                                      ("no other machine running", ["pgrep", "-x", mac.VM_PROCESS], 0, "501\n")):
            with self.subTest(gate=gate):
                w = World(self.tmp)
                w.answer(prefix, rc=rc, out=out)
                failed = [r[0] for r in self.ask(w) if not r[1]]
                self.assertIn(gate, failed)

    def test_the_command_asks_them_and_exits_on_the_verdict(self):
        rc, err = self.staged("--gates")
        self.assertEqual(rc, 0, err)
        self.assertIn("every gate passes", err)
        self.assertEqual(self.w.results(), [])
        w = World(self.tmp)
        w.answer(["pgrep", "-x", mac.VM_PROCESS], out="501\n")
        rc, err = self.staged("--gates", w=w)
        self.assertEqual(rc, 1)
        self.assertIn("1 gate(s) refuse a run here", err)

    def test_the_declared_display_is_the_machines_own(self):
        self.staged("--gates")
        self.assertIn(("run", ("/py", os.path.join(str(REPO), mac.CHECK), "--displays-only", "--expect-display", "builtin 1280x832")),
                      self.w.effects)


class TestTheInstallResolvesItself(MacTest):
    def install(self, env, marker=None):
        f = Fake()
        if marker:
            f._set_file(mac.MARKER, marker)
        return mac.Install(REPO, f, dict(self.w.env, **env))

    def test_the_staging_root(self):
        self.assertEqual(self.install({"WK_BENCH_ROOT": "/x"}).staging_root(), "/x")
        self.assertEqual(self.install({"WK_BENCH_ROOT": ""}, MARKERS["mbp"]).staging_root(), "/var/wk")
        self.assertIsNone(self.install({"WK_BENCH_ROOT": ""}).staging_root(), "host mode names no machine by itself")

    def test_the_marker_names_the_machine(self):
        self.assertEqual(self.install({}, MARKERS["mbp"]).machine()[0], "mbp")
        self.assertEqual(self.install({}, MARKERS["benchvm"]).machine()[0], "benchvm")
        self.assertEqual(self.install({}, "id=x\n").machine(), ("", None))

    def test_the_python_with_pyobjc(self):
        f = Fake()
        f.answer(["/mine"], rc=0)
        self.assertEqual(mac.staged_python(f, {"WK_BENCH_PYTHON": "/mine"}), "/mine")
        f = Fake()
        f.answer([mac.PYTHONS[1]], rc=0)
        self.assertEqual(mac.staged_python(f, {"WK_BENCH_PYTHON": "/broken"}), mac.PYTHONS[1])

    def test_no_python_with_pyobjc_refuses(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(Refused):
            mac.staged_python(Fake(), {})


class Target(targets.Target):
    kind = "vm"

    def wait_ready(self, ws, clock, timeout=None):
        return None

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def pull_dir(self, ws, src, dest, exclude=()):
        self.machine.effect(("copy_tree_out", src, dest) + tuple(exclude))

    def src(self, ws):
        return "/Users/admin/WebKit"

    def os(self):
        return "macos"


class Drv:
    def __init__(self, w, local):
        self.w, self.local, self.put_rc = w, local, 0

    def bench_root(self):
        return self.w.home

    def bench_local(self):
        return self.local

    def bench_put(self, src, dest, *skip):
        self.w.effect(("bench_put", src, dest) + skip)
        return self.put_rc

    def bench_put_file(self, src, dest):
        self.w.effect(("bench_put_file", src, dest))
        return 0

    def c(self, key):
        return {"NODE_VOLUME": "WK Bench"}.get(key, "")

    def facts(self):
        return {"BOOT_ARMING": "command"}


class StageWorld(World):
    def __init__(self, tmp, local=True, dirty="no"):
        super().__init__(tmp)
        self._drop(os.path.join(self.home, "staged"))
        self.drv = Drv(self, local)
        self.reg.load = lambda name: Target(name, str(REPO), dict(self.env), self)
        self.reg.ws_target = lambda ws: "vm"
        self.answer(["exec", "ws", "test"], out="")
        self.answer(["exec", "ws", "git"], out=SHA + "\n")
        self.answer([os.path.join(str(REPO), "cmd", "version")], out="sha=abc\ndirty=%s\n" % dirty)
        self.answer(["hostname"], out="tolken\n")
        self.answer(["sh", "-c"], out="")
        self.answer(["rsync"], out="")
        self.dirs.add("/seed/speedometer3-abc")

    def stage_(self, *argv):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            a = args("stage", "ws", "--to", "mbp", "--config", "mac-release", *argv)
            rc = mac.stage(REPO, self.reg, self.clock, a.positionals[1:], a.value("--to"), a.value("--config"),
                           mac.plans(a.order, a.values("--plan"), a.values("--payload")), driver=lambda root, conf: self.drv)
        return rc, err.getvalue()

    def stages(self):
        staged = os.path.join(self.home, "staged")
        return sorted(json.loads(self.files[os.path.join(staged, d, "stage.json")])["config"]
                      for d in (self.listdir(staged) if self.isdir(staged) else ())
                      if os.path.join(staged, d, "stage.json") in self.files)


class TestStage(MacTest):
    def test_a_stage_carries_the_products_tools_and_each_pinned_payload(self):
        w = StageWorld(self.tmp)
        rc, err = w.stage_("--plan", "speedometer3", "--payload", "/seed/speedometer3-abc", "--plan", "jetstream3")
        self.assertEqual(rc, 0, err)
        dest = os.path.join(w.home, "staged", "%s-mac-release" % w.clock.stamp())
        doc = json.loads(w.files[os.path.join(dest, "stage.json")])
        self.assertEqual((doc["workspace"], doc["config"], doc["webkit_sha"], doc["plans"], doc["payloads_pinned"], doc["wk_tools"]),
                         ("ws", "mac-release", SHA, "speedometer3,jetstream3", "speedometer3", "abc"))
        (products,) = [e for e in w.effects if e[0] == "copy_tree_out" and e[2].endswith("/WebKitBuild/Release")]
        self.assertEqual(mac.PRODUCT_SKIP, products[3:])
        self.assertNotIn("dSYM", " ".join(products), "the machine that profiles never had the build tree")
        self.assertIn(("copy_tree_out", "/Users/admin/WebKit/Tools", os.path.join(dest, "Tools")), w.effects)
        self.assertIn(("act", ("rsync", "-a", "--exclude", ".git", "/seed/speedometer3-abc/", dest + "/payload/speedometer3/")), w.effects)
        self.assertIn("wk bench staged --plan speedometer3", err)

    def test_a_dirty_tree_says_so(self):
        w = StageWorld(self.tmp, dirty="yes")
        w.stage_()
        self.assertEqual(w.stages(), ["mac-release"])
        (d,) = w.listdir(os.path.join(w.home, "staged"))
        self.assertEqual(json.loads(w.files[os.path.join(w.home, "staged", d, "stage.json")])["wk_tools"], "abc+dirty")

    def test_a_payload_that_is_not_there_is_refused_before_anything_is_copied(self):
        w = StageWorld(self.tmp)
        with self.assertRaises(Refused):
            w.stage_("--plan", "speedometer3", "--payload", "/nowhere")
        self.assertEqual([e for e in w.effects if e[0] != "run"], [])

    def test_a_payload_names_the_plan_it_follows(self):
        w = StageWorld(self.tmp)
        with self.assertRaises(Refused):
            w.stage_("--payload", "/seed/speedometer3-abc")

    def test_a_delivered_stage_crosses_before_its_manifest(self):
        w = StageWorld(self.tmp, local=False)
        rc, err = w.stage_()
        self.assertEqual(rc, 0, err)
        puts = [e for e in w.effects if e[0] in ("bench_put", "bench_put_file")]
        self.assertEqual([p[0] for p in puts], ["bench_put", "bench_put_file"])
        self.assertEqual(puts[0][3:], mac.PUT_SKIP)
        self.assertTrue(puts[1][2].endswith("/stage.json"))
        self.assertFalse([p for p in w.files if "bench-stage" in p], "the local assembly goes once it is delivered")

    def test_a_failed_delivery_leaves_nothing_half_made(self):
        w = StageWorld(self.tmp, local=False)
        w.drv.put_rc = 1
        with self.assertRaises(Refused):
            w.stage_()
        self.assertFalse([e for e in w.effects if e[0] == "bench_put_file"])
        self.assertIn(("remove", os.path.join(w.reg.store.state_dir(), "bench-stage", "%s-mac-release" % w.clock.stamp())), w.effects)

    def test_the_dry_run_is_the_stages_mutations(self):
        def mutations(w):
            return [e for e in w.effects if e[0] not in ("run",)]
        for local in (True, False):
            with self.subTest(local=local):
                wet, dry = StageWorld(self.tmp, local), StageWorld(self.tmp, local)
                wet.stage_("--plan", "speedometer3", "--payload", "/seed/speedometer3-abc")
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    dry.stage_("--plan", "speedometer3", "--payload", "/seed/speedometer3-abc")
                finally:
                    del os.environ["WK_DRY_RUN"]
                strip = [[tuple(str(x).replace(str(w.tmp), "") for x in e) for e in mutations(w)] for w in (wet, dry)]
                self.assertEqual(strip[0], strip[1])
                self.assertEqual(dry.stages(), [])

    def test_a_stage_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[bench stage]`: a rerun is a new stage, and what a killed one left has no manifest."""
        def run_once(w):
            w.clock.t += 1
            with contextlib.redirect_stderr(io.StringIO()):
                w.stage_("--plan", "speedometer3", "--payload", "/seed/speedometer3-abc")
        for local in (True, False):
            with self.subTest(local=local):
                converges(self, lambda: StageWorld(self.tmp, local), run_once, StageWorld.stages)


class TestWhere(unittest.TestCase):
    """`dispatch.where[bench]`: a workspace run goes where the workspace is; the Mac's stage and its staged run are this
    host's own, so `bench` on the Mac's volume runs on the Mac; each verb is handed only the flags that apply to it."""

    def test_each_verb_runs_where_its_declaration_says(self):
        d = decl.Decl(BENCH)
        for argv, want in ((["run", "ws", "p"], "workspace"), (["stage", "ws"], "host"), (["staged"], "host"),
                           (["seed", "ws", "p"], "dynamic"), (["ls"], "dynamic"), (["report", "t"], "host"), (["mac"], "host")):
            with self.subTest(argv=argv):
                self.assertEqual(d.where_for(argv), want)

    def test_the_dynamic_verbs_answer_for_themselves(self):
        reg = targets.Registry(REPO, env={}, machine=Fake())
        self.assertEqual(cli.where(reg, ["ls"]), "local")
        self.assertEqual(cli.where(reg, ["ls", "--continued"]), "store")

    def test_each_verb_takes_only_its_own_flags(self):
        d = decl.Decl(BENCH)
        for verb, has, lacks in (("stage", "--to=", "--gates"), ("staged", "--gates", "--cores="), ("run", "--cores=", "--to=")):
            with self.subTest(verb=verb):
                opts = d.opts_for([verb]).split(",")
                self.assertIn(has, opts)
                self.assertNotIn(lacks, opts)


if __name__ == "__main__":
    unittest.main()
