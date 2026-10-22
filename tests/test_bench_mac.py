"""The Mac's bench volume as a bench system (lib/wk/bench/mac.py) against a Fake install: `wk bench deploy <ws> <mac>`
staging a workspace's build, `wk bench run --system <mac>` as the one pipeline on the running install with every gate in
its preflight. Nothing here touches a Mac, bless, a helper or a browser."""
import contextlib
import io
import json
import os
import plistlib
import shlex
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeProc, WsDriver
from tests.support import REPO, requires_machine, run
from tests.test_bench_report import in_process
from tests import fakes
from tests.test_bench_pipeline import CleanTest, StubWatch, SystemConformance  # noqa: F401 -- tests/test_mac_pgo.py takes StubWatch from here

sys.path.insert(0, str(REPO / "lib"))
from wk import places, project, samply as wksamply, screen  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk import presets  # noqa: E402
from wk.bench import cli, mac, plans, record as brecord, report  # noqa: E402
from wk.bench.pipeline import Run  # noqa: E402
from wk.boot import open_driver  # noqa: E402
from tests.fake_boot import board, conf_for  # noqa: E402
from tests.fakes import Fake  # noqa: E402
from wk.machine import lib_argv  # noqa: E402
from wk.quiet import PRIV  # noqa: E402

BENCH = REPO / "cmd" / "bench"
SHA = "0123456789abcdef0123456789abcdef01234567"
RESULT = json.dumps({"Speedometer-3": {"metrics": {"Score": {"current": [30.0, 31.0]}}}})
MARKERS = {"mbp": "id=perf-macos-tolken-2026-09\nprofile=perf-macos-tolken\n", "benchvm": "id=perf-macos-benchvm\n"}
CONFS = {"mbp": 'kind=mac\nssh="tolken"\ndriver=mac-volume\nimage_preset=perf-macos-tolken\n'
                'volume="WK Bench"\ndisplay="builtin 1280x832"\n',
         "benchvm": "kind=guest\ndriver=mac-guest\nimage_preset=perf-macos-benchvm\n"}
STAGE_ID = "20260901T000000Z-mac-release"
PROBE = lib_argv(str(REPO), screen.WINDOWS, "wk_window_probe")
UNEXPECTED = lib_argv(str(REPO), screen.WINDOWS, "wk_window_unexpected")


def on_screen(fake, uninvited="", reading="MiniBrowser:Speedometer"):
    """The window server reads `reading` ("?" when it was not asked) and `uninvited` is what of it wk did not put there."""
    fake.answer(PROBE, out="windows=%s\n" % reading)
    fake.answer(UNEXPECTED, out=uninvited)


class World(fakes.World):
    """A Mac with its two installs' confs, one stage on its volume, and run-benchmark writing its --output-file."""

    def __init__(self, tmp, machine="mbp", bench=True):
        super().__init__(tmp)
        self.rc, self.watched = 0, []
        self.home = str(self.tmp / "var-wk")
        machines = self.tmp / "machines"
        machines.mkdir()
        for name, text in CONFS.items():
            (machines / (name + ".conf")).write_text(text)
        self.env.update({"XDG_CONFIG_HOME": str(self.tmp / "config"), "WK_MACHINES_DIR": str(machines), "WK_BENCH_ROOT": self.home,
                         "WK_BENCH_PYTHON": "/py", "WK_POLL_SECONDS": "1"})
        self.reg = places.Registry(REPO, env=self.env, machine=self)
        self.machine = machine
        if bench:
            self._set_file(mac.images.MARKER, MARKERS[machine])
        self.stage = os.path.join(self.home, "staged", STAGE_ID)
        self.build = os.path.join(self.stage, "WebKitBuild", "Release")
        self._set_file(os.path.join(self.stage, "stage.json"), json.dumps({
            "workspace": "ws", "workspace_place": "vm", "preset": "mac-release", "webkit_sha": SHA,
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
        on_screen(self)

    def start(self, argv, out, cwd=None):
        self.watched.append(list(argv))
        out.write(b"wk: bench pid 77\nScore: 30\n")
        words = shlex.split(argv[-1].split("exec ", 1)[1])
        Path(words[words.index("--output-file") + 1]).write_text(RESULT)
        return FakeProc(self.rc)

    def results(self):
        d = Path(self.home, "results")
        return sorted(str(r.relative_to(d)) for r in d.glob("*/runs/*")) if d.is_dir() else []

    def env_json(self):
        (run,) = self.results()
        return json.loads(Path(self.home, "results", run, "env.json").read_text())


class MacTest(CleanTest):
    def setUp(self):
        super().setUp()
        watch = mock.patch.object(screen, "Watch", StubWatch)
        watch.start()
        self.addCleanup(watch.stop)
        self.w = World(self.tmp)

    def staged(self, *argv, w=None, dry=False, **o):
        """`wk bench run ws <plan> --system <mac> --slot <the stage>` on the install, as cmd/bench routes it."""
        w = w or self.w
        o = dict({"system": w.machine, "slot": STAGE_ID}, **o)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"} if dry else {}):
            cp = in_process(cli.run, REPO, w.reg, ["ws", "speedometer3"], o, False, w.clock)
        return cp.returncode, cp.stderr

    def refused(self, w):
        rc, err = self.staged(w=w)
        self.assertNotEqual(rc, 0, err)
        return err


class TestTheMacVolumeSystem(SystemConformance, MacTest):
    PLAN, OUTCOME = "speedometer3", 1
    KILL_RC, KILL_SAYS = 1, "is stopped where it runs: ^C there, or its autorun's watchdog"

    def bench(self):
        return self.staged()

    def outcome(self):
        return len(self.w.results())

    def kill(self):
        cp = in_process(cli.run, REPO, self.w.reg, ["ws"], {"system": self.w.machine, "slot": STAGE_ID}, True, self.w.clock)
        return cp.returncode, cp.stderr


class TestConformance(MacTest):
    def test_the_staged_run_lands_with_its_stage_as_its_subject(self):
        rc, err = self.staged()
        self.assertEqual(rc, 0, err)
        (run,) = self.w.results()
        self.assertTrue(run.endswith("-speedometer3-" + STAGE_ID), run)
        self.assertEqual(Path(self.w.home, "results", run, "result.json").read_text(), RESULT)
        doc = brecord.task_doc(str(Path(self.w.home, "results", run.split("/")[0])))
        self.assertEqual((doc["subject"], doc["devices"]), ({"kind": "staged", "spec": STAGE_ID}, [{"device": "mbp", "profile": "mac-release"}]))
        script = self.w.watched[0][-1]
        for word in ("/py %s/Tools/Scripts/run-benchmark" % self.w.stage, "--platform osx", "--build-directory " + self.w.build,
                     "--local-copy %s/payload/speedometer3" % self.w.stage):
            self.assertIn(word, script)

    def test_the_timeout_reaches_run_benchmark_and_a_workspace_other_than_the_stages_is_refused(self):
        rc, err = self.staged(timeout="600")
        self.assertEqual(rc, 0, err)
        self.assertIn("--timeout 600", self.w.watched[0][-1])
        w = World(self.tmp)
        cp = in_process(cli.run, REPO, w.reg, ["other", "speedometer3"], {"system": "mbp", "slot": STAGE_ID}, False, w.clock)
        self.assertIn("is staged from workspace 'ws', not 'other'", cp.stderr)


class TestTheRecord(MacTest):
    def test_a_staged_run_records_its_provenance(self):
        self.staged()
        env = self.w.env_json()
        self.assertEqual((env["bench_host"], env["machine"], env["measures"], env["profile"], env["preset"], env["webkit_sha"]),
                         ("image", "mbp", True, "perf-macos-tolken", "mac-release", SHA))
        self.assertEqual((env["host"]["kernel"], env["host"]["kernel_arch"], env["host"]["cores"], env["host"]["root_device"]),
                         ("25.0.0", "arm64", "12", "/dev/disk3s1 (Apple Fabric, ssd)"))
        self.assertEqual((env["stage"]["id"], env["staged_from"]), (STAGE_ID, self.w.stage))
        self.assertEqual((env["configuration"]["aslr"], env["role_marker_overridden"], env["forced"]), ("os-randomised", False, False))
        self.assertIn("wall_time_s", env)
        self.assertEqual(brecord.run_state(env, True), "ok")

    def test_the_staged_builds_pgo_reading_goes_with_the_run_and_none_is_carried_where_there_is_none(self):
        self.staged()
        self.assertFalse(Path(self.w.home, "results", self.w.results()[0], "profile-check.json").exists())
        w = World(self.tmp)
        w._set_file(os.path.join(w.build, "wk-profile-check.json"), '{"missing": []}')
        self.staged(w=w)
        self.assertEqual(Path(w.home, "results", w.results()[0], "profile-check.json").read_text(), '{"missing": []}')

    def test_a_rehearsal_is_refused_as_a_measurement(self):
        """A mac-guest (its `measures` fact is no) proves every phase, and its reading is recorded as no measurement."""
        w = World(self.tmp, "benchvm")
        rc, err = self.staged(w=w)
        self.assertEqual(rc, 0, err)
        env = w.env_json()
        self.assertEqual((env["machine"], env["measures"]), ("benchvm", False))
        self.assertIn("rehearsal", brecord.not_a_measurement(env))
        self.assertEqual(brecord.run_state(env, True), "rehearsal")

    def test_a_rehearsed_round_ends_the_task_and_is_never_usable(self):
        task = self.tmp / "task"
        brecord.task_write(str(task), {"task": "t", "requested": "now", "devices": "benchvm=mac-release", "plans": "speedometer3",
                                       "rounds": "1", "slots": "a,b"}, ["wk bench ab --devices mbp"])
        for arm in ("a", "b"):
            d = task / "runs" / arm
            d.mkdir(parents=True)
            (d / "result.json").write_text(RESULT)
            brecord.write_env(str(d / "env.json"), {"machine": "benchvm", "plan": "speedometer3", "ab.round": "1", "ab.arm": arm, "measures": False})
        st = brecord.task_state(str(task), False)
        self.assertEqual((st["state"], st["ok"], st["usable"]), ("complete", 0, 0))
        self.assertIn("2 rehearsed (no measurement)", st["summary"])

    def test_the_two_run_report_refuses_a_rehearsal(self):
        sides = []
        for name, measures in (("a", "1"), ("b", "")):
            d = self.tmp / name
            d.mkdir()
            (d / "result.json").write_text(RESULT)
            brecord.write_env(str(d / "env.json"), {"machine": "benchvm", "plan": "speedometer3", "measures": bool(measures)})
            sides.append(str(d))
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(io.StringIO()):
            report.two_runs([sides[0]], [sides[1]])
        self.assertIn("side B: %s -- benchvm is a rehearsal" % sides[1], str(cm.exception.code))

    def test_the_shared_cache_knob_is_exported_and_recorded(self):
        self.w.env["WK_BENCH_SHARED_CACHE"] = "avoid"
        self.staged()
        self.assertIn("export DYLD_SHARED_REGION=avoid", self.w.watched[0][-1])
        self.assertIs(self.w.env_json()["configuration"]["shared_cache"], False)

    def test_the_machines_declared_display_is_judged_and_recorded(self):
        self.staged()
        asked = [e[1] for e in self.w.effects if e[0] == "run" and e[1][:2] == ("/py", os.path.join(str(REPO), mac.CHECK))]
        self.assertEqual(asked, [("/py", os.path.join(str(REPO), mac.CHECK), "--displays-only", "--expect-display", "builtin 1280x832"),
                                 ("/py", os.path.join(str(REPO), mac.CHECK), "--build-directory", self.w.build, "--expect-display", "builtin 1280x832")])
        self.assertEqual(self.w.env_json()["display_declared"], "builtin 1280x832")

    def test_a_software_update_scan_across_a_leg_drops_it(self):
        scans = iter(['"LastFullSuccessfulDate" => 1\n', '"LastFullSuccessfulDate" => 2\n'])
        self.w.react(["/usr/bin/plutil"], lambda argv, f: mac.Result(0, next(scans)))
        rc, err = self.staged()
        self.assertEqual(rc, 0, err)
        self.assertIn("software-update scan ran during this leg", err)
        self.assertEqual(brecord.run_state(self.w.env_json(), True), "failed")


class TestTheLegsOwnGates(MacTest):
    def test_host_mode_refuses_and_its_dry_run_says_so(self):
        w = World(self.tmp, bench=False)
        self.assertIn("this is host mode", self.refused(w))
        rc, err = self.staged(w=World(self.tmp, bench=False), dry=True)
        self.assertEqual(rc, 1, err)
        self.assertIn("would fail -- a real run would stop here", err)
        self.assertIn("would run: bash -lc", err)

    def test_a_covered_screen_refuses_and_force_records_it(self):
        on_screen(self.w, "UserNotificationCenter:Notification;UserNotificationCenter:Banner")
        self.assertIn("1 preflight check(s) failed", self.refused(self.w))
        self.assertEqual(self.w.results(), [])
        w = World(self.tmp)
        on_screen(w, "UserNotificationCenter:Notification")
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

    def test_nothing_staged_names_the_deploy_command(self):
        w = World(self.tmp)
        w._drop(os.path.join(w.home, "staged"))
        self.assertIn("wk bench deploy <workspace> mbp --slot %s" % STAGE_ID, self.refused(w))

    def test_a_plan_not_pinned_into_the_stage_is_refused_rather_than_fetched(self):
        w = World(self.tmp)
        w._drop(os.path.join(w.stage, "payload", "speedometer3"))
        self.assertIn("nothing pinned for 'speedometer3'", self.refused(w))


class TestTheProfile(MacTest):
    CACHE = "/cache"

    def samply(self, cache=CACHE):
        return wksamply.store_dir(cache, wksamply.triple("arm64", "Darwin")) + "/samply"

    def capture(self, cached=True, web="321\n"):
        """samply is already in the cache, or it is not and no fetch can start."""
        w = World(self.tmp)
        w.answer(["test", "-x", self.samply()], rc=0 if cached else 1)
        w.answer(["pgrep", "-n", "-f", project.get("MAC_WEB_PROCESS")], rc=0 if web else 1, out=web)
        c = mac.Capture(self.CACHE, w, w.clock, "/tmp/p.json")
        with contextlib.redirect_stderr(io.StringIO()):
            c.run()
        return w, c

    def test_the_web_process_is_recorded(self):
        w, c = self.capture()
        self.assertEqual(c.said, "")
        self.assertIn(("act", ("sudo", "-n", self.samply(), "record", "--save-only", "--profile-name", "wk-warmup", "-o", "/tmp/p.json",
                               "-p", "321")), w.effects)

    def test_no_samply_or_no_web_process_takes_nothing(self):
        for cached, web in ((False, "321\n"), (True, "")):
            with self.subTest(cached=cached, web=web):
                w, c = self.capture(cached, web)
                self.assertTrue(c.said)
                self.assertFalse([e for e in w.effects if e[0] == "act"])

    def test_a_warmup_leg_profiles_its_web_process_beside_the_task(self):
        self.w.answer(["test", "-x", self.samply(self.w.reg.store.cache_dir())])
        self.w.answer(["pgrep", "-n", "-f", project.get("MAC_WEB_PROCESS")], out="321\n")
        rc, err = self.staged(warmup="1", arm="a", round="0")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.w.env_json()["profiler"], self.w.env_json()["warmup_kind"]), ("samply", "evidence"))
        self.assertEqual(brecord.task_runs(str(Path(self.w.home, "results", self.w.results()[0].split("/")[0]))), [])
        (cap,) = [e[1] for e in self.w.effects if e[0] == "act" and "record" in e[1]]
        self.assertTrue(cap[cap.index("-o") + 1].endswith("/warmup/mbp-a.profile.json"), cap)


class TestTheWatchdog(MacTest):
    def test_a_benchmarks_silence_is_waited_out_longer_than_a_builds_and_an_override_wins(self):
        w = self.w
        system = mac.Volume(REPO, w.reg, "ws", w.clock, "mbp")
        env = Run(system, plans, w.env).env
        self.assertEqual((env["WK_STALL_SECONDS"], env["WK_ABORT_SECONDS"]), ("900", "5400"))
        self.assertEqual(Run(system, plans, dict(w.env, WK_STALL_SECONDS="12")).env["WK_STALL_SECONDS"], "12")


class TestPreflightAsksEveryGate(MacTest):
    """A dry run on the install is the gate check: every gate a leg needs, asked, and nothing measured or rebooted."""

    def test_every_gate_is_asked_and_a_clean_install_passes_them(self):
        rc, err = self.staged(dry=True)
        self.assertEqual(rc, 0, err)
        self.assertIn("preflight clean", err)
        self.assertEqual(self.w.results(), [])
        self.assertFalse([e for e in self.w.effects if "reboot" in " ".join(e[1]) or "wk-boot-priv" in " ".join(e[1])])

    def test_each_gate_refuses_on_its_own_evidence(self):
        for gate, prefix, rc, out in (("quiesce readback", ["sudo", "-n", PRIV, "status"], 1, ""),
                                      ("brightness", ["python3", os.path.join(str(REPO), mac.WKMAC), "brightness"], 0, "0.85\n"),
                                      ("the display", ["/py", os.path.join(str(REPO), mac.CHECK), "--displays-only"], 1, "2 displays are online\n"),
                                      ("browser check", ["/py", os.path.join(str(REPO), mac.CHECK), "--build-directory"], 1, "throttled\n"),
                                      ("the screen is free", UNEXPECTED, 0, "UserNotificationCenter:Banner\n"),
                                      ("no other machine running", ["pgrep", "-x", mac.VM_PROCESS], 0, "501\n")):
            with self.subTest(gate=gate):
                w = World(self.tmp)
                w.answer(prefix, rc=rc, out=out)
                rc_, err = self.staged(w=w, dry=True)
                self.assertEqual(rc_, 1, err)
                self.assertTrue([l for l in err.splitlines() if l.split()[:1] in (["FAIL"], ["unk"]) and gate in l], err)


class TestTheInstallResolvesItself(MacTest):
    def install(self, env, marker=None):
        f = Fake()
        f.answer(["hostname", "-s"], out="elsewhere\n")
        if marker:
            f._set_file(mac.images.MARKER, marker)
        return mac.Install(REPO, f, dict(self.w.env, **env))

    def test_the_staging_root(self):
        self.assertEqual(self.install({"WK_BENCH_ROOT": ""}, MARKERS["mbp"]).staging_root(), "/var/wk")
        self.assertIsNone(self.install({"WK_BENCH_ROOT": ""}).staging_root(), "no Mac in machines/ is this one")

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


class Side:
    """The Mac as staging reaches it: its bench root, whether it is this machine, and the two puts."""

    def __init__(self, w, local):
        self.w, self.is_local, self.put_rc = w, local, 0
        self.d = types.SimpleNamespace(probe=lambda: "host")

    def root_dir(self):
        return self.w.home

    def local(self):
        return self.is_local

    def put(self, src, dest, *skip):
        self.w.effect(("put", src, dest) + skip)
        return self.put_rc

    def put_file(self, src, dest):
        self.w.effect(("put_file", src, dest))
        return 0


KIT = types.SimpleNamespace(default_preset=lambda reg, ws: "mac-release", resolve_preset=presets.resolve,
                            pin_payload=lambda here, lock, store, d, ws, plan: "/seed/%s-abc" % plan if plan != "local" else "")


class StageWorld(World):
    def __init__(self, tmp, local=True, dirty="no"):
        super().__init__(tmp, bench=False)
        self._drop(os.path.join(self.home, "staged"))
        self.side = Side(self, local)
        self.reg.load = lambda name: WsDriver(name, str(REPO), dict(self.env), self, "vm", "/Users/admin/WebKit", os="macos")
        self.reg.ws_place = lambda ws: "vm"
        self.answer(["exec", "ws", "test"], out="")
        self.answer(["exec", "ws", "git"], out=SHA + "\n")
        self.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="abc\n")
        self.answer(["git", "-C", str(REPO), "status"], out=" M lib/wk/x.py\n" if dirty == "yes" else "")
        self.answer(["hostname"], out="tolken\n")
        self.answer(["sh", "-c"], out="")
        self.answer(["rsync"], out="")
        self.dirs.update({"/seed/speedometer3-abc", "/Users/admin/WebKit/WebKitBuild/Release", "/Users/admin/WebKit/Tools"})

    def stage_(self, *names, slot="a"):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), mock.patch.object(mac, "side", lambda root, d: self.side):
            rc = mac.Volume(REPO, self.reg, "ws", self.clock, "mbp").deploy({"slot": slot, "preset": "mac-release", "plans": list(names)}, KIT)
        return rc, err.getvalue()

    def stages(self):
        staged = os.path.join(self.home, "staged")
        return sorted(json.loads(self.files[os.path.join(staged, d, "stage.json")])["preset"]
                      for d in (self.listdir(staged) if self.isdir(staged) else ())
                      if os.path.join(staged, d, "stage.json") in self.files)


class TestDeploy(MacTest):
    def test_a_deploy_carries_the_products_tools_and_each_plans_pinned_payload_as_one_slot(self):
        w = StageWorld(self.tmp)
        rc, err = w.stage_("speedometer3", "local", slot="base")
        self.assertEqual(rc, 0, err)
        dest = os.path.join(w.home, "staged", "base")
        doc = json.loads(w.files[os.path.join(dest, "stage.json")])
        self.assertEqual((doc["workspace"], doc["preset"], doc["webkit_sha"], doc["plans"], doc["wk_tools"]),
                         ("ws", "mac-release", SHA, "speedometer3", "abc"))
        (products,) = [e for e in w.effects if e[0] == "copy_tree_out" and e[2].endswith("/WebKitBuild/Release")]
        self.assertEqual(mac.PRODUCT_SKIP, products[3:])
        self.assertIn(("copy_tree_out", "/Users/admin/WebKit/Tools", os.path.join(dest, "Tools")), w.effects)
        self.assertIn(("act", ("rsync", "-a", "--exclude", ".git", "/seed/speedometer3-abc/", dest + "/payload/speedometer3/")), w.effects)
        self.assertIn("--system mbp --slot base", err)

    def test_a_dirty_tree_says_so(self):
        w = StageWorld(self.tmp, dirty="yes")
        w.stage_()
        self.assertEqual(json.loads(w.files[os.path.join(w.home, "staged", "a", "stage.json")])["wk_tools"], "abc+dirty")

    def test_a_delivered_stage_crosses_before_its_manifest(self):
        w = StageWorld(self.tmp, local=False)
        rc, err = w.stage_()
        self.assertEqual(rc, 0, err)
        puts = [e for e in w.effects if e[0] in ("put", "put_file")]
        self.assertEqual([p[0] for p in puts], ["put", "put_file"])
        self.assertEqual(puts[0][3:], mac.PUT_SKIP)
        self.assertTrue(puts[1][2].endswith("/stage.json"))
        self.assertFalse([p for p in w.files if "bench-stage" in p], "the local assembly goes once it is delivered")

    def test_a_failed_delivery_leaves_nothing_half_made(self):
        w = StageWorld(self.tmp, local=False)
        w.side.put_rc = 1
        with self.assertRaises(Refused):
            w.stage_()
        self.assertFalse([e for e in w.effects if e[0] == "put_file"])
        self.assertIn(("remove", os.path.join(w.reg.store.state_dir(), "bench-stage", "a")), w.effects)


class TestTheStagingSide(unittest.TestCase):
    """What staging and a plant reach a Mac's bench install through (mac.Side), over FakeMac and FakeGuest."""

    def side(self, kind, **state):
        fake, d = board(kind)
        for k, v in state.items():
            setattr(fake, k, v)
        d.probe()
        return fake, d, mac.side(REPO, d)

    def test_the_staging_root_and_the_home_answer_on_that_side(self):
        fake, d, s = self.side("mac-volume")
        self.assertEqual((s.root_dir(), s.home_dir()), ("/Volumes/WK Bench - Data/private/var/wk", "/Volumes/WK Bench - Data/Users/bench"))
        fake.data = False
        self.assertEqual(s.root_dir(), "/Volumes/WK Bench/var/wk")
        fake.attached = False
        self.assertIsNone(s.root_dir())
        fake.enter_bench()
        d.probe()
        self.assertEqual((s.root_dir(), s.home_dir()), ("/var/wk", "/Users/bench"))
        fake, d, s = self.side("mac-guest", st="running")
        self.assertEqual((s.root_dir(), s.home_dir(), s.local(), s.manager(), s.tools(s.manager())), ("/var/wk", "/Users/admin", False, d.ch.via, str(REPO)))

    def test_nothing_is_staged_onto_a_running_measurement(self):
        fake, d, s = self.side("mac-volume")
        fake.enter_bench()
        d.probe()
        for put in (s.put, s.put_file):
            with self.subTest(put=put.__name__), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
                put("/tmp", "/var/wk/x")
            self.assertIn("running measurement", err.getvalue())
        self.assertEqual(fake.effects, [])

    def test_a_tree_travels_as_a_tar_without_history_or_bytecode_and_replaces_the_one_there(self):
        dest = "/Volumes/WK Bench - Data/private/var/wk/wk-tools"
        with tempfile.TemporaryDirectory() as tmp:
            for rel in ("a.txt", ".git/HEAD", "lib/__pycache__/x.pyc", "lib/x.py"):
                Path(tmp, rel).parent.mkdir(parents=True, exist_ok=True)
                Path(tmp, rel).write_text("x")
            fake, d, s = self.side("mac-volume")
            self.assertEqual(s.put(tmp, dest, ".git", "__pycache__"), 0)
        tar = [e[1] for e in fake.effects if e[0] == "tar" and isinstance(e[1], tuple)][0]
        self.assertIn("./lib/x.py", tar)
        self.assertFalse([n for n in tar if ".git" in n or "__pycache__" in n], tar)
        ran = [e for e in fake.effects if e[0] != "push" and isinstance(e[1], str)]
        tmp_tar = ran[-1][-1]
        self.assertEqual(ran, [("rm", "-rf", dest), ("mkdir", "-p", dest), ("tar", "-xf", tmp_tar, "-C", dest), ("rm", "-f", tmp_tar)])

    def test_a_guests_root_is_owned_before_anything_lands_and_a_dry_run_owns_nothing(self):
        fake, d, s = self.side("mac-guest", st="running")
        self.assertEqual(s.put_file("/dev/null", "/var/wk/stage.json"), 0)
        ran = [e for e in fake.effects if e[0] != "push"]
        self.assertEqual(ran[:2], [("mkdir", "-p", "/var/wk"), ("chown", "-R", "admin", "/var/wk")])
        self.assertEqual(ran[2][:2], ("mv", "-f"))
        fake, d, s = self.side("mac-guest", st="running")
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(io.StringIO()):
            s.own("/var/wk")
        self.assertEqual(fake.effects, [])

    def test_the_restart_is_ready_only_when_the_helper_names_its_detach(self):
        fake, d, s = self.side("mac-volume")
        self.assertTrue(s.restart_ready())
        fake.helper_is = "old"
        self.assertFalse(s.restart_ready())
        self.assertIn("older than this tree", s.restart_detail())
        fake.helper_is = None
        self.assertIn("plain sudo there wants a password", s.restart_detail())

    def test_standing_on_the_mac_runs_it_here_and_its_tools_are_where_the_tree_is_declared(self):
        via = Fake("here")
        via.answer(["hostname", "-s"], out="Tolken\n")
        via.answer(["sh", "-c"], out="id=x\n")
        via.answer(["test", "-x", "Development/wk-tools/wk"], rc=0)
        d = open_driver(REPO, conf_for("mac-volume"), env={}, via=via)
        self.assertEqual(d.probe(), "bench x")
        s = mac.side(REPO, d)
        self.assertTrue(s.local(), "a deploy to that Mac standing on it sends nothing")
        self.assertIs(s.manager(), via)
        self.assertEqual(s.tools(via), "Development/wk-tools")


class TestBenchReachesTheRealMbp(unittest.TestCase):
    @requires_machine("tolken")
    def test_wk_boot_reaches_mbp(self):
        cp = run("boot", "mbp", "--status")
        self.assertIn(cp.returncode, (0, 2, 3), cp.stdout)


if __name__ == "__main__":
    unittest.main()
