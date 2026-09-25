"""`wk bench run` as a flow (lib/wk/bench/pipeline.py over lib/wk/bench/systems.py) against a Fake world:
each system through boot, deploy, run and collect, the refusals, the record a run writes, a dry run as the
recorder, and a run killed after any effect.

Rows landed here: `unit bench.pipeline_conformance[container|guest|board]`, `unit record.progress_shape[bench]`,
`unit dispatch.dry_run_is_the_recorder[bench]`, `unit killpoints[bench]`.

Run: python3 tests/run.py -k test_bench_pipeline
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from tests.test_bench_mac import Drv  # noqa: E402
from tests.test_mac_volume import FakeMac  # noqa: E402
from wk import act, decl, fleet, record, shell, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import mac, pipeline, record as brecord, systems  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Killed, Result  # noqa: E402
from wk.quiet import lib_argv  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
PLAN_JSON = json.dumps({"git_repository": {"url": "https://example.com/bench.git", "branch": "main"}})
JSC_LOG = b'wk: bench pid 77\nScore: 12\n{"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [12.0]}}}}}}\n'
RESULT = json.dumps({"Speedometer-3": {"metrics": {"Score": {"current": [30.0, 31.0]}}}})
CMD_LOADER = importlib.machinery.SourceFileLoader("cmd_bench", str(REPO / "cmd" / "bench"))
CMD = importlib.util.module_from_spec(importlib.util.spec_from_loader("cmd_bench", CMD_LOADER))
CMD_LOADER.exec_module(CMD)


class BenchTarget(targets.Target):
    """A workspace `ws` whose commands answer from the world as ("exec", ws, ...)."""

    def __init__(self, name, root, env, machine, kind):
        super().__init__(name, root, env, machine)
        self.kind = kind

    def info(self, ws):
        return "running"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def exec_argv(self, ws, argv, tty=False):
        return ["exec", ws] + list(argv), None

    def src(self, ws):
        return "/Users/admin/WebKit" if self.kind == "vm" else "/src/WebKit"

    def home(self):
        return "/Users/admin"

    def os(self):
        return "macos" if self.kind == "vm" else "linux"


class Reg(targets.Registry):
    def __init__(self, world):
        super().__init__(REPO, env=world.env, machine=world)
        self.world = world

    def load(self, name):
        return BenchTarget(name, self.root, dict(self.env), self.world, self.world.kind)

    def ws_target(self, ws):
        return self.world.kind

    def in_workspace(self):
        return False


class Proc:
    def __init__(self, rc):
        self.pid, self.rc, self.returncode = 4242, rc, None

    def poll(self):
        self.returncode = self.rc
        return self.rc

    def wait(self):
        return self.rc


class World(Fake):
    """This host benchmarking workspace `ws` on a container or a guest: the build is there, the payload
    is seeded, the machine is quiet, and the benchmark writes what run-benchmark or cli.js would."""

    def __init__(self, tmp, kind="container"):
        super().__init__("here")
        self.fake, self.kind, self.rc = self, kind, 0
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "XDG_RUNTIME_DIR": "/run/user/1", "WK_NAME": "ws",
                    "WK_IN_VM": "1", "WK_JOB_PID_TRIES": "0", "WK_POLL_SECONDS": "1"}
        self.clock = FakeClock()
        self.seed_dest = os.path.join(self.env["WK_STORE"], "cache", "bench", "jetstream3-" + SHA[:12])
        self.dirs.add(os.path.join(self.seed_dest, ".wk-seeded"))
        self.dirs.add(self.seed_dest.replace("jetstream3", "speedometer3"))
        self.dirs.add(os.path.join(self.seed_dest.replace("jetstream3", "speedometer3"), ".wk-seeded"))
        self.files[os.path.join(self.seed_dest, "cli.js")] = ""
        self.files["/run/user/1/wk/display/wayland-0"] = ""
        self.files["/proc/loadavg"] = "0.50 0.40 0.30 1/100 1\n"
        self.files["/proc/meminfo"] = "MemTotal: 33554432 kB\nMemAvailable: 30000000 kB\n"
        self.files[systems.GOVERNOR] = "performance\n"
        for prefix, out in ((["exec", "ws", "cat"], PLAN_JSON), (["exec", "ws", "test"], ""), (["git", "ls-remote"], SHA + "\trefs/heads/main\n"),
                            (["exec", "ws", "git"], SHA + "\n"), (["exec", "ws", "python3"], ""), (["exec", "ws", "mkdir"], ""),
                            (["exec", "ws", "sysctl", "-n", "hw.ncpu"], "4\n"), (["exec", "ws", "sysctl", "-n", "hw.model"], "VirtualMac2,1\n"),
                            (["exec", "ws", "sw_vers"], "15.5\n"), (["exec", "ws", "uname"], "arm64\n"),
                            (["exec", "ws", "diskutil"], ""), (["env"], "renderer=NVIDIA GeForce | gl\n"),
                            (["nproc"], "16\n"), (["sysctl", "-n", "vm.loadavg"], "{ 0.50 0.40 0.30 }\n"), (["uname", "-r"], "6.8.0\n"), (["uname", "-m"], "x86_64\n"),
                            (["lscpu"], "Model name:   Test CPU\n"), (["nvidia-smi"], "550.1\n"), (["findmnt"], "/dev/nvme0n1p2\n"),
                            (["lsblk"], json.dumps({"blockdevices": [{"name": "nvme0n1p2", "type": "part"},
                                                                     {"name": "nvme0n1", "type": "disk", "rota": False, "tran": "nvme", "model": "Fast"}]})),
                            (lib_argv(str(REPO), pipeline.QUIET, "screen_watch_start")[:3], ""),
                            (lib_argv(str(REPO), pipeline.QUIET, "screen_watch_stop")[:3], "")):
            self.answer(prefix, out=out)
        self.files["/run/wk-session-mode"] = "gpu\n"
        self.watched = []

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        """What the benchmark leaves: its log, and run-benchmark's --output-file where the system put it."""
        self.watched.append(list(argv))
        script = argv[-1]
        if "cli.js" in script:
            stdout.write(JSC_LOG)
        else:
            stdout.write(b"wk: bench pid 77\nScore: 30\n")
            out = shlex.split(script.split("exec ", 1)[1])
            dest = out[out.index("--output-file") + 1]
            if self.kind == "container":
                dest = os.path.join(self.env["WK_STORE"], "bench", dest[len("/bench/"):])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                Path(dest).write_text(RESULT)
            else:
                self._set_file(dest, RESULT)
        return Proc(self.rc)

    def bench_dir(self):
        return Path(self.env["WK_STORE"]) / "bench"

    def tasks(self):
        return brecord.tasks(str(self.bench_dir()))

    def recs(self):
        return record.Records(self.env["WK_STORE"], clock=self.clock, env=self.env, machine=self)

    def state(self):
        """What a finished run leaves: its newest task complete with one ok run, and its record ended 0."""
        tasks = self.tasks()
        if not tasks:
            return None
        st = brecord.task_state(str(self.bench_dir() / tasks[-1]), False)
        recs = [t for t in self.recs().list() if t.field("kind") == "bench"]
        return st["state"], st["ok"], recs[-1].field("exit") if recs else None


def invoke(w, argv):
    """cmd/bench's run arm: its options read off the declaration, then the pipeline."""
    return CMD.run_arm(decl.Args(decl.Decl(REPO / "cmd" / "bench"), list(argv)), Reg(w), w.clock, w.popen)


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-bench-pipeline-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._env = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_BENCH_ASLR", "WK_BENCH_PATH_PAD", "WK_BENCH_ENV_PAD"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.w = World(self.tmp)

    def run_(self, w=None, *argv, extra=None):
        w = w or self.w
        w.env.update(extra or {})
        argv = argv or ("run", "jetstream3", "--config", "jsc-release", "--count", "2")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = invoke(w, argv)
        return rc, err.getvalue()

    def refused(self, *argv, w=None, extra=None):
        with self.assertRaises(Refused) as cm:
            self.run_(w, *argv, extra=extra)
        return cm.exception

    def said(self, *argv, w=None, extra=None):
        err = io.StringIO()
        w = w or self.w
        w.env.update(extra or {})
        argv = argv or ("run", "jetstream3", "--config", "jsc-release")
        with self.assertRaises(Refused), contextlib.redirect_stderr(err):
            invoke(w, argv)
        return err.getvalue()

    def run_dir(self, w=None):
        w = w or self.w
        (task,) = w.tasks()
        (run,) = os.listdir(w.bench_dir() / task / "runs")
        return w.bench_dir() / task / "runs" / run

    def env_json(self, w=None):
        return json.loads((self.run_dir(w) / "env.json").read_text())


class TestConformance(BenchTest):
    def test_each_system_runs_the_one_pipeline_into_the_one_record(self):
        """`bench.pipeline_conformance[container|guest]`: boot, deploy, run, collect, and the result in the store."""
        for kind, plan, config, host in (("container", "jetstream3", "jsc-release", "container"),
                                         ("container", "speedometer3", "wpe-release", "container"),
                                         ("vm", "jetstream3", "jsc-release", "guest"),
                                         ("vm", "speedometer3", "mac-release", "guest")):
            with self.subTest(kind=kind, plan=plan):
                w = World(self.tmp, kind)
                rc, err = self.run_(w, "run", plan, "--config", config)
                self.assertEqual(rc, 0, err)
                self.assertIn("BENCH OK  %s" % plan, err)
                self.assertEqual(w.state(), ("complete", 1, "0"))
                env = self.env_json(w)
                self.assertEqual((env["bench_host"], env["config"], env["plan"], env["webkit_sha"]), (host, config, plan, SHA))
                self.assertTrue((self.run_dir(w) / "result.json").is_file(), err)

    def test_a_board_runs_the_one_pipeline_into_the_one_record(self):
        """`bench.pipeline_conformance[board]`: `--system <board>` through the same run arm, boot to collect."""
        from tests.test_bench_board import BoardWorld
        w = BoardWorld(self.tmp)
        self.assertEqual(w.invoke(), 0, w.err)
        self.assertIn("BENCH OK  jetstream3", w.err)
        self.assertEqual(w.state(), ("complete", 1, "0"))
        env = w.env_json()
        self.assertEqual((env["bench_host"], env["config"], env["plan"], env["webkit_sha"]), ("image", "buildroot-rpi5-64", "jetstream3", SHA))
        self.assertTrue((w.run_dir() / "result.json").is_file(), w.err)

    def test_a_guest_run_is_collected_from_the_guest_through_its_copy(self):
        w = World(self.tmp, "vm")
        rc, err = self.run_(w, "run", "speedometer3", "--config", "mac-release")
        self.assertEqual(rc, 0, err)
        (pull,) = [e for e in w.effects if e[0] == "copy_out"]
        self.assertTrue(pull[1].startswith("/Users/admin/wk-bench/") and pull[1].endswith("/result.json"), pull)
        self.assertEqual(Path(pull[2]), self.run_dir(w) / "result.json")
        self.assertIn("--platform osx", w.watched[0][-1])
        self.assertTrue(w.watched[0][-1].startswith("mkdir -p /Users/admin/wk-bench/"), w.watched[0][-1])

    def test_a_guest_gets_the_pinned_payload_the_store_holds(self):
        w = World(self.tmp, "vm")
        self.run_(w, "run", "jetstream3", "--config", "jsc-release")
        (push,) = [e for e in w.effects if e[0] == "copy_tree_in"]
        self.assertEqual(push[1:], (w.seed_dest, "/Users/admin/wk-bench/payload/" + os.path.basename(w.seed_dest)))
        self.assertIn("cd /Users/admin/wk-bench/payload/", w.watched[0][-1])

    def test_a_container_run_writes_through_the_stores_mounts(self):
        self.run_(None, "run", "speedometer3", "--config", "wpe-release")
        script = self.w.watched[0][-1]
        self.assertIn("--output-file /bench/", script)
        self.assertIn("--local-copy /cache/bench/speedometer3-", script)
        self.assertEqual([e for e in self.w.effects if e[0].startswith("copy")], [])

    def test_jsc_iterations_merge_into_one_result(self):
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        doc = json.loads((self.run_dir() / "result.json").read_text())
        self.assertEqual(doc["JetStream3.0"]["tests"]["t"]["metrics"]["Score"]["current"], [12.0, 12.0])
        self.assertEqual(len(self.w.watched), 2)


class TestTheRecord(BenchTest):
    def test_a_run_writes_the_one_progress_record(self):
        """`record.progress_shape[bench]`: step n of m, the log, how to stop it, and how it ended."""
        rc, err = self.run_()
        self.assertEqual(rc, 0, err)
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name"), t.field("exit")), ("bench", "here", "ws", "0"))
        self.assertEqual(len(t.plan()), 3)
        self.assertTrue(t.plan()[1].startswith("run jetstream3 (jsc, 2 iteration(s))"), t.plan())
        self.assertEqual(t.steps(), [(1, "done"), (2, "done"), (3, "running")])
        self.assertEqual((t.field("kill"), t.field("abort_after")), ("wk bench run ws --kill", "5400"))
        self.assertEqual(Path(t.field("log")), self.run_dir() / "run-2.log")

    def test_the_task_names_its_request_and_its_run_the_axes(self):
        self.run_(extra={"WK_BENCH_ENV_PAD": "64"})
        (task,) = self.w.tasks()
        doc = json.loads((self.w.bench_dir() / task / "task.json").read_text())
        self.assertEqual((doc["subject"], doc["devices"]), ({"kind": "workspace", "spec": "ws"},
                                                           [{"device": "container", "profile": "jsc-release"}]))
        self.assertEqual(doc["commands"], ["wk bench run ws jetstream3 --config jsc-release --count 2"])
        env = self.env_json()
        self.assertEqual((env["class"], env["runner"], env["host"]["root_device"]), ("cpu", "jsc", "nvme0n1 Fast (nvme, ssd, no-trim)"))
        self.assertEqual(env["configuration"]["env_pad_bytes"], "64")
        self.assertIn("wall_time_s", env)

    def test_a_failure_ends_the_record_with_its_status_and_names_the_log(self):
        self.w.rc = 3
        e = self.refused()
        self.assertEqual(e.status, 3)
        self.assertEqual(self.w.state(), ("complete", 0, "3"), "a failed run is an ended one")

    def test_a_silent_run_ends_stalled(self):
        self.w.rc = 124
        self.refused()
        self.assertEqual(self.w.state()[2], "stalled")


class TestRefusals(BenchTest):
    def test_the_bare_form_names_run(self):
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err):
            CMD.main(["ws", "jetstream3"])
        self.assertIn("wk bench run <workspace> <plan>", err.getvalue())

    def test_a_jsc_config_cannot_run_a_gpu_plan(self):
        self.assertIn("gpu-class benchmark and jsc-release builds no browser", self.said("run", "speedometer3", "--config", "jsc-release"))

    def test_an_unknown_config_is_named(self):
        self.assertIn("unknown config 'nosuch'", self.said("run", "jetstream3", "--config", "nosuch"))

    def test_a_board_or_remote_workspace_is_not_a_workspace_system(self):
        w = World(self.tmp, "remote")
        self.assertIn("a container workspace or a macOS guest", self.said("run", "jetstream3", w=w))

    def test_a_failed_preflight_refuses_and_force_records_it(self):
        self.w.files[systems.GOVERNOR] = "powersave\n"
        self.assertIn("1 preflight check(s) failed", self.said("run", "jetstream3", "--config", "jsc-release"))
        self.assertEqual(self.w.tasks(), [])
        rc, err = self.run_(None, "run", "jetstream3", "--config", "jsc-release", extra={"WK_FORCE": "1"})
        env = self.env_json()
        self.assertTrue(env["forced"])
        self.assertIn("cpu governor: powersave -- wk quiesce on; ", env["preflight_notes"])

    def test_a_busy_machine_is_refused_and_the_threshold_is_a_setting(self):
        self.w.files["/proc/loadavg"] = "5.00 4.00 3.00 1/100 1\n"
        self.assertIn("1-minute load average is 5.00", self.said())
        rc, err = self.run_(extra={"WK_BENCH_MAX_LOAD": "10"})
        self.assertIn("load 5.00, no wk builds", err)

    def test_the_load_is_rounded_before_it_is_compared(self):
        """printf '%.0f' in the bash it replaces: 4.6 is 5, over the default of 4; 4.4 is 4, not over it."""
        self.w.files["/proc/loadavg"] = "4.60 4.00 3.00 1/100 1\n"
        self.assertIn("1-minute load average is 4.60", self.said())
        w = World(self.tmp)
        w.files["/proc/loadavg"] = "4.40 4.00 3.00 1/100 1\n"
        rc, err = self.run_(w)
        self.assertIn("load 4.40, no wk builds", err)

    def test_a_build_on_the_books_holds_the_run_off(self):
        t = self.w.recs().begin("build", "here", "other", "k", "/l", ["x"], pid=99)
        self.w.pids.add(99)
        self.assertIn("no builds running", self.said("run", "jetstream3", "--config", "jsc-release"))
        t.end(0)

    def test_the_jsc_runner_needs_a_seeded_cli_js(self):
        del self.w.files[os.path.join(self.w.seed_dest, "cli.js")]
        self.assertIn("has no cli.js", self.said())

    def test_a_cpu_class_browser_run_goes_headless_without_a_display(self):
        del self.w.files["/run/user/1/wk/display/wayland-0"]
        rc, err = self.run_(None, "run", "jetstream3", "--config", "wpe-release")
        self.assertIn("running headless (cpu-class, no usable display)", err)
        self.assertIn("-- --headless", self.w.watched[0][-1])
        self.assertIn("export LIBGL_ALWAYS_SOFTWARE=1", self.w.watched[0][-1])

    def test_aslr_cannot_be_turned_off_in_a_guest(self):
        w = World(self.tmp, "vm")
        self.assertIn("ASLR cannot be turned off on macOS", self.said("run", "jetstream3", "--config", "jsc-release", w=w,
                                                                     extra={"WK_BENCH_ASLR": "off"}))


class TestAMeasuredRunIsWatchedThroughout(BenchTest):
    """The preflight reads the screen once and a run is minutes long: a dialog that draws mid-run covers every leg after it."""

    def test_the_run_is_bracketed_by_the_watch(self):
        self.run_(None, "run", "speedometer3", "--config", "wpe-release")
        order = [e[1][2] for e in self.w.effects if e[0] == "run" and e[1][:1] == ("bash",) and "screen_watch" in e[1][2]]
        self.assertEqual([o.split(";")[1].split()[0] for o in order], ["screen_watch_start", "screen_watch_stop"])

    def test_a_covered_run_fails_unless_it_is_forced(self):
        self.w.answer(lib_argv(str(REPO), pipeline.QUIET, "screen_watch_stop")[:3], rc=1, out="12:00:00Z\tSecurityAgent\n")
        err = self.said("run", "speedometer3", "--config", "wpe-release")
        self.assertIn("did not stay quiet", err)
        self.assertIn("SecurityAgent", err)
        w = World(self.tmp)
        w.answer(lib_argv(str(REPO), pipeline.QUIET, "screen_watch_stop")[:3], rc=1, out="x\n")
        rc, err = self.run_(w, "run", "speedometer3", "--config", "wpe-release", extra={"WK_FORCE": "1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("keeping the number anyway", err)


class TestKnobs(BenchTest):
    def test_the_path_pad_is_made_in_the_workspace_and_run_through(self):
        self.run_(extra={"WK_BENCH_PATH_PAD": "4"})
        links = [e[1] for e in self.w.effects if e[0] == "run" and e[1][:3] == ("exec", "ws", "ln")]
        self.assertEqual(links[0][-1], "/tmp/wk-bench-pad-pppp/jsc")
        self.assertIn("/tmp/wk-bench-pad-pppp/jsc cli.js", self.w.watched[0][-1])
        self.assertEqual(self.env_json()["configuration"]["path_len"], "4")

    def test_aslr_off_is_setarch_in_a_container(self):
        self.run_(extra={"WK_BENCH_ASLR": "off"})
        self.assertIn("exec setarch $(uname -m) -R -- ", self.w.watched[0][-1])


class TestRootDevice(unittest.TestCase):
    def test_a_linux_disk_names_its_bus_rotation_and_trim(self):
        w = World(Path(tempfile.mkdtemp(prefix="wk-rootdev-")))
        w.files["/sys/block/nvme0n1/queue/discard_max_bytes"] = "2199023255040\n"
        self.assertEqual(systems.root_device(w.run, w.read, "/", False), "nvme0n1 Fast (nvme, ssd, trim)")

    def test_a_mac_volume_names_its_node_from_the_plist(self):
        import plistlib
        f = Fake()
        f.answer(["diskutil"], out=plistlib.dumps({"DeviceNode": "/dev/disk3s1", "BusProtocol": "Apple Fabric",
                                                   "SolidState": True}).decode())
        self.assertEqual(systems.root_device(f.run, f.read, "/", True), "/dev/disk3s1 (Apple Fabric, ssd)")

    def test_what_cannot_be_read_is_unknown(self):
        f = Fake()
        self.assertEqual(systems.root_device(f.run, f.read, "/", True), "unknown")
        self.assertEqual(systems.root_device(f.run, f.read, "/", False), "unknown")


class TestKill(BenchTest):
    def test_nothing_running_says_so(self):
        rc, err = self.run_(None, "run", "--kill")
        self.assertEqual(rc, 0)
        self.assertIn("no bench is running in 'ws'", err)


class TestDryRun(BenchTest):
    def test_the_plan_is_the_runs_mutations(self):
        """`dispatch.dry_run_is_the_recorder[bench]`: the dry run prints the wet run's effects and its benchmark line, and writes no record."""
        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            return [e for e in w.effects if e[0] in ("act", "write", "mkdir", "remove", "copy_in", "copy_out", "copy_tree_in", "spawn", "kill")
                    and not (isinstance(e[1], str) and e[1].startswith(locks))]
        for kind, plan, config in (("container", "speedometer3", "wpe-release"), ("vm", "speedometer3", "mac-release")):
            with self.subTest(kind=kind):
                wet, dry = World(self.tmp, kind), World(self.tmp, kind)
                self.run_(wet, "run", plan, "--config", config)
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    rc, err = self.run_(dry, "run", plan, "--config", config)
                finally:
                    del os.environ["WK_DRY_RUN"]
                strip = [[tuple(str(x).replace(str(w.tmp), "") for x in e) for e in mutations(w)] for w in (wet, dry)]
                self.assertEqual(strip[0], strip[1])
                self.assertGreaterEqual(len(strip[0]), 2)
                self.assertIn("would run: " + " ".join(shlex.quote(a) for a in wet.watched[0]).replace(str(wet.tmp), str(dry.tmp)), err)
                self.assertEqual((dry.watched, dry.tasks(), dry.recs().list()), ([], [], []))


class TestKillPoints(BenchTest):
    def test_a_run_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[bench]`: each run is its own task; whatever a killed one held goes with it."""
        def run_once(w):
            w.clock.t += 1
            with contextlib.redirect_stderr(io.StringIO()):
                invoke(w, ("run", "speedometer3", "--config", "wpe-release"))
        for kind in ("container", "vm"):
            with self.subTest(kind=kind):
                converges(self, lambda: World(self.tmp, kind), run_once, World.state)


MBP_CONF = ('KIND=mac\nNODE_SSH="tolken"\nNODE_BENCH_SSH="tolken-bench"\nNODE_DRIVER=mac-volume\n'
            'NODE_VOLUME="WK Bench"\nNODE_PROFILE=perf-macos-tolken\n')


class MacBenchTarget(BenchTarget):
    """A macOS VM workspace with a build already present -- `needs_base` is a golden-base concern
    `Stage.run()`'s `wait_ready` does not need to re-ask of a fake that is already 'running'; `Stage`
    is the only caller that pulls a whole tree out of it, so the pull is an effect here, as it is on
    5.27's own `Target` double (tests/test_bench_mac.py) -- nothing stages a real build tree."""
    needs_base = False

    def pull_dir(self, ws, src, dest, exclude=()):
        self.machine.effect(("copy_tree_out", src, dest) + tuple(exclude))


class MacReg(Reg):
    """`Stage.run()` re-resolves the workspace target through the registry, not through the `System`
    it was handed, so this is the one place a `--system mbp` run's target comes from."""

    def load(self, name):
        return MacBenchTarget(name, self.root, dict(self.env), self.world, self.world.kind)


class MacWorld(Fake):
    """The driving machine for `wk bench run <ws> <plan> --system mbp`: its own store, a macOS
    workspace target to stage from, and a fake Mac (5.12's `FakeMac`, over its own `Channel`
    protocol) it reaches to arm, run over ssh and bring back. `wk bench staged` on the install is
    not re-simulated (5.27 already tests it): the ssh invocation is answered directly, the way any
    remote command is faked here."""

    RESULT_ID = "20260924T000000Z-speedometer3-ws"

    def __init__(self, tmp):
        super().__init__("here")
        self.fake, self.kind, self.rc = self, "vm", 0
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        machines = self.tmp / "machines"
        machines.mkdir()
        (machines / "mbp.conf").write_text(MBP_CONF)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_NAME": "ws", "WK_MACHINES_DIR": str(machines),
                    "WK_MAC_BENCH_TOOLS": "/tools", "WK_POLL_SECONDS": "1", "WK_JOB_PID_TRIES": "0"}
        self.clock = FakeClock()
        self.home = "/var/wk"
        conf = dict(fleet.Fleet(str(REPO), self.env).load("mbp"), NODE_NAME="mbp")
        self.mac = FakeMac(conf, env=self.env, clock=self.clock)
        self.mac.write_system(conf["NODE_PROFILE"])   # the bench install's own /etc/wk-image id, read back by mac-probe.sh
        for prefix, out in ((["exec", "ws", "test"], ""), (["exec", "ws", "git"], SHA + "\n"),
                            (["exec", "ws", "cat"], PLAN_JSON), ([str(REPO / "cmd" / "version")], "sha=abc\ndirty=no\n"),
                            (["git", "ls-remote"], SHA + "\trefs/heads/main\n"), (["rsync"], "")):
            self.answer(prefix, out=out)
        # Pinned already, so the seed step neither clones for real nor differs between a wet and a dry run.
        self.seed_dest = os.path.join(self.env["WK_STORE"], "cache", "bench", "speedometer3-" + SHA[:12])
        self.dirs.add(self.seed_dest)
        self.dirs.add(os.path.join(self.seed_dest, ".wk-seeded"))
        self.watched = []

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def run(self, argv, input=None, timeout=None):
        if argv[:1] == ["ssh"]:
            self.record_run(argv)
            return self._ssh_answer(argv[-1])
        if argv[:1] == ["scp"]:
            self.record_run(argv)
            return Result(0)
        return super().run(argv, input=input, timeout=timeout)

    def _ssh_answer(self, cmd):
        if "test -x /tools/wk" in cmd:
            return Result(0, "/tools\n")
        if cmd == "test -d %s" % (self.home + "/results"):
            return Result(0)
        if cmd == "ls -1A %s" % (self.home + "/results"):
            return Result(0, self.RESULT_ID + "\n")
        return Result(127, "", "MacWorld: no answer for: %s" % cmd)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.watched.append(list(argv))
        if "wk bench staged" in argv[-1]:
            stdout.write(("wk: bench pid 77\nBENCH OK  speedometer3 -> %s/results/%s/result.json\n"
                          % (self.home, self.RESULT_ID)).encode())
        return Proc(self.rc)

    def system(self, target_kind="vm"):
        self.kind = target_kind
        target = MacBenchTarget("ws", str(REPO), dict(self.env), self, target_kind)
        reg = MacReg(self)
        conf = dict(fleet.Fleet(str(REPO), self.env).load("mbp"), NODE_NAME="mbp")
        return mac.MacHostSystem(str(REPO), reg, target, "ws", self.clock, "mbp", conf,
                                  channel_factory=lambda conf, env, ch, via, root: self.mac,
                                  stage_driver=lambda root, conf: Drv(self, False))

    def go(self, plan="speedometer3", config="mac-release", popen=None):
        system = self.system()
        r = mac.HostRun(str(REPO), system.reg, system, self.clock, self.env, popen or self.popen)
        return r.go(plan, {"config": config})

    def bench_dir(self):
        return Path(self.env["WK_STORE"]) / "bench"

    def tasks(self):
        return brecord.tasks(str(self.bench_dir()))

    def recs(self):
        return record.Records(self.env["WK_STORE"], clock=self.clock, env=self.env, machine=self)


class MacHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-bench-mac-host-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._env = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.w = MacWorld(self.tmp)

    def run_dir(self, w=None):
        w = w or self.w
        (task,) = w.tasks()
        (run,) = os.listdir(w.bench_dir() / task / "runs")
        return w.bench_dir() / task / "runs" / run


class TestMacHostConformance(MacHostTest):
    def test_the_pipeline_runs_boot_deploy_run_and_collect_into_the_one_record(self):
        """`bench.pipeline_conformance[mac-volume]`: staged, armed, run over ssh, collected, and left in
        bench mode -- the fake Mac's own driver (5.11/5.12) refuses to bless itself back from there,
        which the fleet install faces too (only the host install carries the boot helper)."""
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = self.w.go()
        out = err.getvalue()
        self.assertEqual(rc, 0, out)
        self.assertIn("BENCH OK", out)
        self.assertEqual(self.w.mac.running, "bench", "arm() put it in bench mode")
        self.assertIn("cannot bless itself back", out)
        (copy,) = [e for e in self.w.effects if e[0] == "run" and e[1][:1] == ("scp",)]
        self.assertTrue(copy[1][-2].endswith(self.w.RESULT_ID + "/result.json"), copy)
        env = json.loads((self.run_dir() / "env.json").read_text())
        self.assertEqual(env["remote"]["run"], self.w.RESULT_ID)


class TestMacHostRecord(MacHostTest):
    def test_a_run_writes_the_one_progress_record(self):
        """`record.progress_shape[bench]`: the same task record a container or guest run writes."""
        with contextlib.redirect_stderr(io.StringIO()):
            self.w.go()
        (t,) = self.w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name")), ("bench", "here", "ws"))
        self.assertEqual(t.steps(), [(1, "done"), (2, "done"), (3, "running")])


class TestMacHostDryRun(MacHostTest):
    def test_the_plan_is_the_runs_mutations(self):
        """`dispatch.dry_run_is_the_recorder[bench]`, `--system mbp`: the dry run's mutations are the
        wet run's (5.27's own `Stage` dry-run parity), and it never arms or reboots the real machine
        -- `Boot.arm()`'s own dry-run branch returns before either bless call reaches it."""
        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            return [e for e in w.effects if e[0] in ("act", "write", "mkdir", "remove", "bench_put", "bench_put_file")
                    and not (isinstance(e[1], str) and e[1].startswith(locks))]
        wet, dry = MacWorld(self.tmp), MacWorld(self.tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            wet.go()
        os.environ["WK_DRY_RUN"] = "1"
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = dry.go()
        finally:
            del os.environ["WK_DRY_RUN"]
        out = err.getvalue()
        strip = [[tuple(str(x).replace(str(w.tmp), "") for x in e) for e in mutations(w)] for w in (wet, dry)]
        self.assertEqual(strip[0], strip[1])
        self.assertIn("dry run -- nothing was benchmarked", out)
        self.assertEqual(dry.mac.running, "host", "a dry run must not arm or reboot the real machine")
        self.assertEqual((dry.tasks(), dry.recs().list()), ([], []))


class TestMacHostKillPoints(MacHostTest):
    def test_a_run_killed_while_staging_and_rerun_converges(self):
        """`killpoints[bench]`, `--system mbp`'s own new caller of 5.27's crash-only `Stage`: every
        effect up to the reboot converges on a rerun, from a fresh world, the same as any other kill
        point in this tree. A kill after the machine is told to reboot is not resumable this way --
        `Boot.arm()` refuses an already-armed machine by design (only `wk boot mbp --back` undoes an
        arming), so nothing past that point is exercised here; a person, not a rerun, notices it."""
        def run_once(w):
            w.clock.t += 1
            with contextlib.redirect_stderr(io.StringIO()):
                w.go()
        probe = MacWorld(self.tmp)
        run_once(probe)
        cleanup = [i for i, e in enumerate(probe.effects) if e[0] == "remove" and "/bench-stage/" in e[1] and not e[1].endswith(".stage.json")]
        converges(self, lambda: MacWorld(self.tmp), run_once, lambda w: w.mac.running, max_effects=cleanup[-1] + 1)


if __name__ == "__main__":
    unittest.main()
