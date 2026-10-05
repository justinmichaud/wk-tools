"""`wk bench run` as a flow (lib/wk/bench/pipeline.py over lib/wk/bench/systems.py) against a Fake world."""
import contextlib
import io
import json
import os
import shlex
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeProc, FakeRegistry, WsDriver
from tests.killpoints import converges
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from tests.test_bench_mac import StubWatch  # noqa: E402
from wk import decl, dispatch, job, record, project, screen  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import record as brecord, systems  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result, Ssh  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
PLAN_JSON = json.dumps({"git_repository": {"url": "https://example.com/bench.git", "branch": "main"}})
JSC_LOG = b'wk: bench pid 77\nScore: 12\n{"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [12.0]}}}}}}\n'
RESULT = json.dumps({"Speedometer-3": {"metrics": {"Score": {"current": [30.0, 31.0]}}}})
CMD = load_cmd("bench")


class BenchDriver(WsDriver):
    """Workspace `ws` on a container or a guest, its scratch store this machine's own disk."""

    def __init__(self, name, root, env, machine, kind):
        mac = kind == "vm"
        super().__init__(name, root, env, machine, kind, "/Users/admin/WebKit" if mac else "/src/WebKit", "/Users/admin",
                         "macos" if mac else "linux")

    def results(self, ws):
        return Local(), os.path.join(self.store.ws_dir(ws), "bench")


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
        os.makedirs(os.path.join(self.env["WK_STORE"], "ws", "ws"))   # the workspace's directory, where its tasks live
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
                                                                     {"name": "nvme0n1", "type": "disk", "rota": False, "tran": "nvme", "model": "Fast"}]}))):
            self.answer(prefix, out=out)
        self.files["/run/wk-session-mode"] = "gpu\n"
        self.watched = []

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def start(self, argv, out, cwd=None):
        self.watched.append(list(argv))
        script = argv[-1]
        if "cli.js" in script:
            out.write(JSC_LOG)
        else:
            out.write(b"wk: bench pid 77\nScore: 30\n")
            out = shlex.split(script.split("exec ", 1)[1])
            dest = out[out.index("--output-file") + 1]
            if self.kind == "container":
                dest = os.path.join(self.env["WK_STORE"], "ws", "ws", dest[len("/var/lib/wk/ws/ws/"):])
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                Path(dest).write_text(RESULT)
            else:
                self._set_file(dest, RESULT)
        return FakeProc(self.rc)

    def bench_dir(self):
        return Path(self.env["WK_STORE"]) / "ws" / "ws" / "bench"

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


def registry(w, driver=None):
    """The registry a run resolves its workspace through: a `place` (BenchDriver) of the world's kind."""
    return FakeRegistry(w.env, w, lambda n, e: (driver or BenchDriver)(n, str(REPO), e, w, w.kind),
                        ws_place=lambda ws: w.kind, in_workspace=lambda: False)


def invoke(w, argv):
    """cmd/bench's run arm: its options read off the declaration, then the pipeline."""
    argv = as_dispatched("bench", argv, os.environ)
    return CMD.run_arm(decl.Args(decl.Decl(REPO / "cmd" / "bench"), argv), registry(w), w.clock)


class BenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-bench-pipeline-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, True))
        self._env = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_BENCH_ASLR", "WK_BENCH_PATH_PAD", "WK_BENCH_ENV_PAD"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        watch = mock.patch.object(screen, "Watch", StubWatch)
        watch.start()
        self.addCleanup(watch.stop)
        self.w = World(self.tmp)

    def run_(self, w=None, *argv, extra=None):
        w = w or self.w
        w.env.update(extra or {})
        argv = argv or ("run", "jetstream3", "--preset", "jsc-release", "--count", "2")
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
        argv = argv or ("run", "jetstream3", "--preset", "jsc-release")
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
        for kind, plan, preset, host in (("container", "jetstream3", "jsc-release", "container"),
                                         ("container", "speedometer3", "wpe-release", "container"),
                                         ("vm", "jetstream3", "jsc-release", "guest"),
                                         ("vm", "speedometer3", "mac-release", "guest")):
            with self.subTest(kind=kind, plan=plan):
                w = World(self.tmp, kind)
                rc, err = self.run_(w, "run", plan, "--preset", preset)
                self.assertEqual(rc, 0, err)
                self.assertIn("BENCH OK  %s" % plan, err)
                self.assertEqual(w.state(), ("complete", 1, "0"))
                env = self.env_json(w)
                self.assertEqual((env["bench_host"], env["preset"], env["plan"], env["webkit_sha"]), (host, preset, plan, SHA))
                self.assertTrue((self.run_dir(w) / "result.json").is_file(), err)

    def test_a_guest_run_is_collected_from_the_guest_through_its_copy(self):
        w = World(self.tmp, "vm")
        rc, err = self.run_(w, "run", "speedometer3", "--preset", "mac-release")
        self.assertEqual(rc, 0, err)
        (pull,) = [e for e in w.effects if e[0] == "copy_out"]
        self.assertTrue(pull[1].startswith("/Users/admin/wk-bench/") and pull[1].endswith("/result.json"), pull)
        self.assertEqual(Path(pull[2]), self.run_dir(w) / "result.json")
        self.assertIn("--platform osx", w.watched[0][-1])
        self.assertTrue(w.watched[0][-1].startswith("mkdir -p /Users/admin/wk-bench/"), w.watched[0][-1])

    def test_a_guest_gets_the_pinned_payload_the_store_holds(self):
        w = World(self.tmp, "vm")
        self.run_(w, "run", "jetstream3", "--preset", "jsc-release")
        (push,) = [e for e in w.effects if e[0] == "copy_tree_in"]
        self.assertEqual(push[1:], (w.seed_dest, "/Users/admin/wk-bench/payload/" + os.path.basename(w.seed_dest)))
        self.assertIn("cd /Users/admin/wk-bench/payload/", w.watched[0][-1])

    def test_a_container_run_writes_into_its_workspaces_directory(self):
        self.run_(None, "run", "speedometer3", "--preset", "wpe-release")
        script = self.w.watched[0][-1]
        self.assertIn("--output-file /var/lib/wk/ws/ws/bench/", script)
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
        self.assertEqual(doc["commands"], ["wk bench run ws jetstream3 --preset jsc-release --count 2"])
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


class TestInterrupted(BenchTest):
    def test_an_interrupt_stops_the_benchmark_where_it_runs_and_the_record_reads_cancelled(self):
        class Interrupting(FakeProc):
            def poll(self):
                raise job.Interrupted(signal.SIGINT)
        real = record.Records.begin

        def begin(recs, *a, **kw):
            t = real(recs, *a, **kw)
            t.set("pid_match", project.get("BENCH_PID_MATCH"))
            t.pid(77)
            t.set("where", "place")
            return t
        self.w.pids.add(77)
        self.w.answer(["exec", "ws", "ps", "-o", "args=", "-p", "77"], out="jsc cli.js\n")
        self.w.react(["exec", "ws", "kill", "-TERM"], lambda a, f: (f.pids.discard(77), Result(0))[1])
        self.w.react(["exec", "ws", "kill", "-0"], lambda a, f: Result(0 if int(a[-1]) in f.pids else 1))
        self.w.start = lambda argv, out, cwd=None: Interrupting(0)
        with mock.patch.object(record.Records, "begin", begin):
            e = self.refused()
        self.assertEqual(e.status, 130)
        self.assertIn(("run", ("exec", "ws", "kill", "-TERM", "77")), self.w.effects)
        (t,) = self.w.recs().list()
        self.assertEqual(t.field("exit"), "cancelled")
        self.assertTrue(any(e[0] == "symlink" for e in self.w.effects), "the run took its locks")
        self.assertEqual([p for p in self.w.links if p in self.w.files], [], "and released them")


class TestARestart(BenchTest):

    ARGV = ("run", "jetstream3", "--preset", "jsc-release")

    def first(self):
        rc, err = self.run_(None, *self.ARGV)
        self.assertEqual(rc, 0, err)
        (task,) = self.w.tasks()
        self.w.clock.t += 60
        return task

    def test_the_task_records_its_restart(self):
        task = self.first()
        doc = json.loads((self.w.bench_dir() / task / "task.json").read_text())
        self.assertEqual(doc["restart"], "wk bench run ws jetstream3 --preset jsc-release --task " + task)

    def test_a_task_that_holds_its_run_runs_nothing(self):
        task = self.first()
        watched = len(self.w.watched)
        rc, err = self.run_(None, *self.ARGV + ("--task", task))
        self.assertEqual(rc, 0, err)
        self.assertIn("already holds its run ok", err)
        self.assertEqual(len(self.w.watched), watched)

    def test_a_task_whose_run_failed_runs_it_again_into_the_task(self):
        task = self.first()
        os.remove(str(self.run_dir() / "result.json"))
        rc, err = self.run_(None, *self.ARGV + ("--task", task))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.w.tasks(), [task])
        self.assertEqual(len(os.listdir(self.w.bench_dir() / task / "runs")), 2)
        st = brecord.task_state(str(self.w.bench_dir() / task), False)
        self.assertEqual((st["ok"], st["failed"]), (1, 1))

    def test_a_task_that_is_not_this_request_is_refused(self):
        task = self.first()
        self.assertIn("no such task 'nope'", self.said(*self.ARGV + ("--task", "nope")))
        self.assertIn("task %s measures jetstream3, not speedometer3" % task,
                      self.said("run", "speedometer3", "--preset", "wpe-release", "--task", task))
        doc = json.loads((self.w.bench_dir() / task / "task.json").read_text())
        (self.w.bench_dir() / task / "task.json").write_text(json.dumps(dict(doc, slots=["a", "b"], restart="wk bench ab x --task " + task)))
        self.assertIn("is an A/B; restart it with its own command:\n    wk bench ab x --task " + task, self.said(*self.ARGV + ("--task", task)))


class TestWhereTheLegRecords(BenchTest):
    def test_a_workspace_whose_tasks_are_on_another_machine_is_refused_before_anything_is_written(self):
        with mock.patch.object(BenchDriver, "results", lambda t, ws: (Ssh("box", via=self.w), str(self.tmp / "far" / "bench"))):
            err = self.said()
        self.assertIn("run it on box", err)
        self.assertEqual(self.w.watched, [])
        self.assertFalse((self.tmp / "far").exists())


class TestRefusals(BenchTest):
    def dispatch_refuses(self, *argv):
        err = io.StringIO()
        with self.assertRaises(dispatch.Exit) as cm, contextlib.redirect_stderr(err):
            as_dispatched("bench", argv, os.environ)
        self.assertEqual(cm.exception.status, 2)
        return err.getvalue()

    def test_the_bare_form_names_run(self):
        err = self.dispatch_refuses("ws", "jetstream3")
        self.assertIn("unknown verb: ws", err)
        self.assertIn("wk bench run <workspace> <plan>", err)

    def test_a_jsc_config_cannot_run_a_gpu_plan(self):
        self.assertIn("gpu-class benchmark and jsc-release builds no browser", self.said("run", "speedometer3", "--preset", "jsc-release"))

    def test_an_unknown_config_is_named(self):
        self.assertIn("unknown preset: nosuch", self.dispatch_refuses("run", "jetstream3", "--preset", "nosuch"))

    def test_a_board_or_remote_workspace_is_not_a_workspace_system(self):
        w = World(self.tmp, "remote")
        self.assertIn("a container workspace or a macOS guest", self.said("run", "jetstream3", w=w))

    def test_a_mac_is_not_a_system_of_run(self):
        self.assertIn("wk bench staged", self.said("run", "jetstream3", "--system", "mbp"))

    def test_a_failed_preflight_refuses_and_force_records_it(self):
        self.w.files[systems.GOVERNOR] = "powersave\n"
        self.assertIn("1 preflight check(s) failed", self.said("run", "jetstream3", "--preset", "jsc-release"))
        self.assertEqual(self.w.tasks(), [])
        rc, err = self.run_(None, "run", "jetstream3", "--preset", "jsc-release", extra={"WK_FORCE": "1"})
        env = self.env_json()
        self.assertTrue(env["forced"])
        self.assertIn("cpu governor: powersave -- wk quiesce on; ", env["preflight_notes"])

    def test_a_busy_machine_is_refused_and_the_threshold_is_a_setting(self):
        self.w.files["/proc/loadavg"] = "5.00 4.00 3.00 1/100 1\n"
        self.assertIn("1-minute load average is 5.00", self.said())
        rc, err = self.run_(extra={"WK_BENCH_MAX_LOAD": "10"})
        self.assertIn("load 5.00, no wk builds", err)

    def test_the_load_is_rounded_before_it_is_compared(self):
        self.w.files["/proc/loadavg"] = "4.60 4.00 3.00 1/100 1\n"
        self.assertIn("1-minute load average is 4.60", self.said())
        w = World(self.tmp)
        w.files["/proc/loadavg"] = "4.40 4.00 3.00 1/100 1\n"
        rc, err = self.run_(w)
        self.assertIn("load 4.40, no wk builds", err)

    def test_a_build_on_the_books_holds_the_run_off(self):
        t = self.w.recs().begin("build", "here", "other", "k", "/l", ["x"], pid=99)
        self.w.pids.add(99)
        self.assertIn("no builds running", self.said("run", "jetstream3", "--preset", "jsc-release"))
        t.end(0)

    def test_the_jsc_runner_needs_a_seeded_cli_js(self):
        del self.w.files[os.path.join(self.w.seed_dest, "cli.js")]
        self.assertIn("has no cli.js", self.said())

    def test_a_cpu_class_browser_run_goes_headless_without_a_display(self):
        del self.w.files["/run/user/1/wk/display/wayland-0"]
        rc, err = self.run_(None, "run", "jetstream3", "--preset", "wpe-release")
        self.assertIn("running headless (cpu-class, no usable display)", err)
        self.assertIn("-- --headless", self.w.watched[0][-1])
        self.assertIn("export LIBGL_ALWAYS_SOFTWARE=1", self.w.watched[0][-1])

    def test_aslr_cannot_be_turned_off_in_a_guest(self):
        w = World(self.tmp, "vm")
        self.assertIn("ASLR cannot be turned off on macOS", self.said("run", "jetstream3", "--preset", "jsc-release", w=w,
                                                                     extra={"WK_BENCH_ASLR": "off"}))


class TestAMeasuredRunIsWatchedThroughout(BenchTest):

    def test_the_run_is_bracketed_by_the_watch(self):
        self.run_(None, "run", "speedometer3", "--preset", "wpe-release")
        self.assertEqual([e for e in self.w.effects if e[0] == "watch"], [("watch", "start", 0), ("watch", "stop", 1)])

    def test_a_dry_run_starts_no_watch(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.run_(None, "run", "speedometer3", "--preset", "wpe-release")
        self.assertNotIn("start", [e[1] for e in self.w.effects if e[0] == "watch"])

    def test_a_covered_run_fails_unless_it_is_forced(self):
        self.w.drew = ["2026-09-27T12:00:00Z\tSecurityAgent"]
        err = self.said("run", "speedometer3", "--preset", "wpe-release")
        self.assertIn("did not stay quiet", err)
        self.assertIn("SecurityAgent", err)
        w = World(self.tmp)
        w.drew = ["2026-09-27T12:00:00Z\trunning again: NotificationCenter"]
        rc, err = self.run_(w, "run", "speedometer3", "--preset", "wpe-release", extra={"WK_FORCE": "1"})
        self.assertEqual(rc, 0, err)
        self.assertIn("FORCED past a barrier: something drew over this run", err)


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
        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            return [e for e in w.effects if e[0] in ("act", "write", "mkdir", "remove", "copy_in", "copy_out", "copy_tree_in", "spawn", "kill")
                    and not (isinstance(e[1], str) and e[1].startswith(locks))]
        for kind, plan, preset in (("container", "speedometer3", "wpe-release"), ("vm", "speedometer3", "mac-release")):
            with self.subTest(kind=kind):
                wet, dry = World(self.tmp, kind), World(self.tmp, kind)
                self.run_(wet, "run", plan, "--preset", preset)
                os.environ["WK_DRY_RUN"] = "1"
                try:
                    rc, err = self.run_(dry, "run", plan, "--preset", preset)
                finally:
                    del os.environ["WK_DRY_RUN"]
                strip = [[tuple(str(x).replace(str(w.tmp), "") for x in e) for e in mutations(w)] for w in (wet, dry)]
                self.assertEqual(strip[0], strip[1])
                self.assertTrue(strip[0])
                self.assertIn("would run: " + " ".join(shlex.quote(a) for a in wet.watched[0]).replace(str(wet.tmp), str(dry.tmp)), err)
                self.assertEqual((dry.watched, dry.tasks(), dry.recs().list()), ([], [], []))


class TestKillPoints(BenchTest):
    def test_a_run_killed_after_any_effect_and_rerun_converges(self):
        def run_once(w):
            w.clock.t += 1
            with contextlib.redirect_stderr(io.StringIO()):
                invoke(w, ("run", "speedometer3", "--preset", "wpe-release"))
        for kind in ("container", "vm"):
            with self.subTest(kind=kind):
                converges(self, lambda: World(self.tmp, kind), run_once, World.state)


if __name__ == "__main__":
    unittest.main()
