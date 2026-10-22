"""`wk bench run` as a flow (lib/wk/bench/pipeline.py over lib/wk/bench/systems.py) against a Fake world."""
import contextlib
import io
import json
import os
import shlex
import signal
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import fakes
from tests.fakes import FakeProc, FakeRegistry, FlowTest, WsDriver
from tests.support import REPO, as_dispatched, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import guest, job, record, project, resources, screen  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import board, mac_ab, pipeline, record as brecord, report, systems  # noqa: E402
from wk.machine import Local, Result, Ssh  # noqa: E402
from tests.fakes import Fake  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
PLAN_JSON = json.dumps({"git_repository": {"url": "https://example.com/bench.git", "branch": "main"}})
JSC_LOG = b'wk: bench pid 77\nScore: 12\n{"JetStream3.0": {"tests": {"t": {"metrics": {"Score": {"current": [12.0]}}}}}}\n'
RESULT = json.dumps({"Speedometer-3": {"metrics": {"Score": {"current": [30.0, 31.0]}}}})
CMD = load_cmd("bench")


class StubWatch:
    """screen.Watch as a run sees it: its start and stop are effects, and what `drew` names is what it caught."""

    def __init__(self, machine, root, clock, env=None):
        self.m = machine

    def start(self):
        self.m.effects.append(("watch", "start", len(getattr(self.m, "watched", []))))

    def stop(self):
        self.m.effects.append(("watch", "stop", len(getattr(self.m, "watched", []))))
        return list(getattr(self.m, "drew", []))


class CleanTest(FlowTest):
    UNSET = FlowTest.UNSET


class BenchDriver(WsDriver):
    """Workspace `ws` on a container or a guest, its scratch store this machine's own disk."""

    def __init__(self, name, root, env, machine, kind):
        mac = kind == "vm"
        super().__init__(name, root, env, machine, kind, "/Users/admin/WebKit" if mac else "/src/WebKit", "/Users/admin",
                         "macos" if mac else "linux")

    def results(self, ws):
        return Local(), os.path.join(self.store.ws_dir(ws), "bench")


class World(fakes.World):
    """This host benchmarking workspace `ws` on a container or a guest: the build is there, the payload
    is seeded, the machine is quiet, and the benchmark writes what run-benchmark or cli.js would."""

    def __init__(self, tmp, kind="container"):
        super().__init__(tmp, XDG_RUNTIME_DIR="/run/user/1", WK_NAME="ws", WK_IN_VM="1", WK_JOB_PID_TRIES="0", WK_POLL_SECONDS="1")
        self.kind, self.rc = kind, 0
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
                            (["exec", "ws", "diskutil"], ""), (["env"], "renderer=NVIDIA GeForce | gl\n"), ([str(REPO / "wk"), "doctor"], ""),
                            (["nproc"], "16\n"), (["sysctl", "-n", "vm.loadavg"], "{ 0.50 0.40 0.30 }\n"), (["uname", "-r"], "6.8.0\n"), (["uname", "-m"], "x86_64\n"),
                            (["lscpu"], "Model name:   Test CPU\n"), (["nvidia-smi"], "550.1\n"), (["findmnt"], "/dev/nvme0n1p2\n"),
                            (["lsblk"], json.dumps({"blockdevices": [{"name": "nvme0n1p2", "type": "part"},
                                                                     {"name": "nvme0n1", "type": "disk", "rota": False, "tran": "nvme", "model": "Fast"}]}))):
            self.answer(prefix, out=out)
        self.files["/run/wk-session-mode"] = "gpu\n"
        self.watched = []

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
    return CMD.run(as_dispatched("bench", argv, os.environ), registry(w), w.clock)


class BenchTest(CleanTest):
    def setUp(self):
        super().setUp()
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
        w = w or self.w
        w.env.update(extra or {})
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            invoke(w, argv or ("run", "jetstream3", "--preset", "jsc-release"))
        return err.getvalue()

    def run_dir(self, w=None):
        w = w or self.w
        (task,) = w.tasks()
        (run,) = os.listdir(w.bench_dir() / task / "runs")
        return w.bench_dir() / task / "runs" / run

    def env_json(self, w=None):
        return json.loads((self.run_dir(w) / "env.json").read_text())


class SystemConformance:
    """What every bench System does, run once per system. A system's class gives `bench(*argv)` (rc, stderr) for a run
    of `PLAN`, `outcome()` once it ran, and `kill()` (rc, stderr) with nothing running, which ends `KILL_RC` saying
    `KILL_SAYS`; `SCREEN` says whether a run is bracketed by the screen watch."""
    SCREEN, KILL_SAYS = True, "no bench is running"

    def test_a_run_is_the_one_pipeline_into_the_one_record(self):
        rc, err = self.bench()
        self.assertEqual(rc, 0, err)
        self.assertIn("BENCH OK  %s" % self.PLAN, err)
        self.assertEqual(self.outcome(), self.OUTCOME)

    def test_the_run_is_bracketed_by_the_screen_watch_where_there_is_a_screen(self):
        self.bench()
        self.assertEqual([e for e in self.w.effects if e[0] == "watch"], [("watch", "start", 0), ("watch", "stop", 1)] if self.SCREEN else [])

    def test_a_kill_with_nothing_running_says_so(self):
        rc, err = self.kill()
        self.assertEqual(rc, self.KILL_RC, err)
        self.assertIn(self.KILL_SAYS, err)


class WorkspaceSystem(SystemConformance):
    PLAN, OUTCOME, KILL_RC = "speedometer3", ("complete", 1, "0"), 0

    def bench(self):
        return self.run_(self.w, "run", self.PLAN, "--preset", self.PRESET)

    def outcome(self):
        return self.w.state()

    def kill(self):
        return self.run_(None, "run", "--kill")


class TestTheContainerSystem(WorkspaceSystem, BenchTest):
    PRESET = "wpe-release"


class TestTheGuestSystem(WorkspaceSystem, BenchTest):
    PRESET = "mac-release"

    def setUp(self):
        super().setUp()
        self.w = World(self.tmp, "vm")


class TestConformance(BenchTest):
    def test_each_plan_records_where_it_ran(self):
        for kind, plan, preset, host in (("container", "jetstream3", "jsc-release", "container"),
                                         ("container", "speedometer3", "wpe-release", "container"),
                                         ("vm", "jetstream3", "jsc-release", "guest"),
                                         ("vm", "speedometer3", "mac-release", "guest")):
            with self.subTest(kind=kind, plan=plan):
                w = World(self.tmp, kind)
                rc, err = self.run_(w, "run", plan, "--preset", preset)
                self.assertEqual(rc, 0, err)
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
        self.assertEqual(len(t.plan()), 2)
        self.assertTrue(t.plan()[0].startswith("run jetstream3 on 'ws' (jsc, 2 iteration(s))"), t.plan())
        self.assertEqual(t.steps(), [(1, "done"), (2, "running")])
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

    def test_a_failure_ends_the_record_with_its_status_and_a_silent_run_ends_stalled(self):
        for rc, ended in ((3, "3"), (124, "stalled")):
            with self.subTest(rc=rc):
                w = World(self.tmp)
                w.rc = rc
                self.refused(w=w)
                self.assertEqual(w.state(), ("complete", 0, ended), "a failed run is an ended one")


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
        from tests.test_dispatch import dispatched
        how, status, out = dispatched(["bench", *argv])
        self.assertEqual((how, status), ("exit", 2))
        return out

    def test_the_bare_form_names_run(self):
        err = self.dispatch_refuses("ws", "jetstream3")
        self.assertIn("unknown verb: ws", err)
        self.assertIn("wk bench run <workspace> <plan>", err)

    def test_a_jsc_config_cannot_run_a_gpu_plan(self):
        self.assertIn("gpu-class benchmark and jsc-release builds no browser", self.said("run", "speedometer3", "--preset", "jsc-release"))

    def test_an_unknown_config_is_named(self):
        self.assertIn("unknown preset: nosuch", self.dispatch_refuses("run", "ws1", "jetstream3", "--preset", "nosuch"))

    def test_a_board_or_remote_workspace_is_not_a_workspace_system(self):
        w = World(self.tmp, "remote")
        self.assertIn("a container workspace or a macOS guest", self.said("run", "jetstream3", w=w))

    def test_a_mac_is_a_system_whose_run_measures_what_it_has_staged(self):
        self.assertIn("wk bench deploy <workspace> mbp --slot a", self.said("run", "jetstream3", "--system", "mbp"))

    def test_a_failed_preflight_refuses_and_force_records_it(self):
        self.w.files[systems.GOVERNOR] = "powersave\n"
        self.assertIn("1 preflight check(s) failed", self.said("run", "jetstream3", "--preset", "jsc-release"))
        self.assertEqual(self.w.tasks(), [])
        rc, err = self.run_(None, "run", "jetstream3", "--preset", "jsc-release", extra={"WK_FORCE": "1"})
        env = self.env_json()
        self.assertTrue(env["forced"])
        self.assertIn("cpu governor: powersave -- wk quiesce on; ", env["preflight_notes"])

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


class TestRootDevice(unittest.TestCase):
    def test_a_linux_disk_names_its_bus_rotation_and_trim(self):
        w = World(Path(tempfile.mkdtemp(prefix="wk-rootdev-")))
        w.files["/sys/block/nvme0n1/queue/discard_max_bytes"] = "2199023255040\n"
        self.assertEqual(systems.linux_disk(w.run, w.read, "/"), "nvme0n1 Fast (nvme, ssd, trim)")

    def test_what_cannot_be_read_is_unknown(self):
        f = Fake()
        self.assertEqual(systems.macos_facts(f.run, "/")["host.root_device"], "unknown")
        self.assertEqual(systems.linux_disk(f.run, f.read, "/"), "unknown")


class TestCoresValid(unittest.TestCase):
    def test_the_documented_shapes_and_nothing_else(self):
        for spec, ok in (("0-3", True), ("2,3", True), ("0-1,4", True), ("7", True),
                         ("a", False), ("1-", False), ("-1", False), ("1,,2", False)):
            with self.subTest(spec=spec), contextlib.redirect_stderr(io.StringIO()):
                try:
                    pipeline.check_cores(spec)
                    self.assertTrue(ok)
                except Refused:
                    self.assertFalse(ok)


class TestAPinnedRun(BenchTest):
    """`unit bench.pins_cores`: a run pins the cores it records."""

    def test_a_container_run_is_exec_d_under_taskset_and_records_it(self):
        rc, err = self.run_(None, "run", "jetstream3", "--preset", "jsc-release", "--cores", "0-3")
        self.assertEqual(rc, 0, err)
        self.assertIn("exec taskset -c 0-3 ", self.w.watched[0][-1])
        self.assertEqual(self.env_json()["cores"], {"set": "0-3", "pinned": True})

    def test_an_unpinned_run_records_that_it_was_not(self):
        self.run_()
        self.assertNotIn("taskset", self.w.watched[0][-1])
        self.assertEqual(self.env_json()["cores"], {"set": "", "pinned": False})

    def test_a_guest_records_its_vcpu_count_and_refuses_a_pin(self):
        w = World(self.tmp, "vm")
        self.assertIn("no pin exists on macOS; the guest's vCPU count is not a pin",
                      self.said("run", "jetstream3", "--preset", "jsc-release", "--cores", "0-1", w=w))
        self.run_(w, "run", "jetstream3", "--preset", "jsc-release")
        self.assertEqual(self.env_json(w)["host"]["cores"], "4")

    def test_an_invalid_set_is_refused_before_anything_runs(self):
        self.assertIn("is not a valid Linux cpu list", self.said("run", "jetstream3", "--cores", "a-b"))
        self.assertEqual(self.w.effects, [])


def failing_df():
    m = Fake()
    m.answer(["df", "-Pk"], rc=1, err="df: no such file")
    return m


def disk_admit():
    b = resources.Budget(failing_df(), {})
    b.disk_admit("this build", 60, b.free_gb("/s"), "/s")


def disk_admit_on_garbage():
    resources.Budget(Fake(), {}).disk_admit("this build", 60, resources.parse_df("garbage"), "the guest")


def host_disk():
    guest.host_disk(types.SimpleNamespace(machine=failing_df(), env={}))


def load_row():
    system = types.SimpleNamespace(here=Fake(), reg=types.SimpleNamespace(env={}), host_os="linux")
    rows = systems.System.idle_rows(system, types.SimpleNamespace(list=lambda: []))
    if brecord.failed(rows):
        raise Refused(1)


class TestAnUnmeasuredPreflightIsUnknownNotFailure(unittest.TestCase):
    CASES = {"disk_admit on a df that fails": disk_admit,
             "disk_admit on a df nobody can parse": disk_admit_on_garbage,
             "host_disk on a df that fails": host_disk,
             "the bench machine-idle row on an unreadable load average": load_row}

    def test_each_goes_ahead(self):
        for name, check in self.CASES.items():
            with self.subTest(name), contextlib.redirect_stderr(io.StringIO()):
                check()

    def test_a_measured_shortfall_still_refuses(self):
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
            resources.Budget(Fake(), {}).disk_admit("this build", 60, 1, "/s")

    def test_an_unreadable_word_size_does_not_refuse_the_profiler_stage(self):
        run = types.SimpleNamespace(board="b", doc={}, bench=lambda: Fake(), facts_={})
        leg = types.SimpleNamespace(o={}, slot="s")
        with contextlib.redirect_stderr(io.StringIO()):
            board.Board.profiler_stage(run, leg)

    def test_an_unread_clock_is_not_a_failed_clock_check(self):
        system = types.SimpleNamespace(clk={}, display="hdmi", throttled=lambda: "", doc={})
        rows, _ = board.Board.checks(system, types.SimpleNamespace(slot="a"))
        self.assertEqual([], brecord.failed(rows))
        self.assertEqual(["clock pinned"], [what for _, what, _ in brecord.unmeasured(rows)])

    def test_a_measured_unpinned_clock_still_fails(self):
        system = types.SimpleNamespace(clk={"min": "600000", "max": "1400000"}, display="hdmi", throttled=lambda: "", doc={})
        rows, _ = board.Board.checks(system, types.SimpleNamespace(slot="a"))
        self.assertEqual(["clock pinned"], [what for _, what, _ in brecord.failed(rows)])

    def test_an_absent_staging_root_or_bench_home_refuses_the_preflight(self):
        for root, failed in (("", "staging root"), ("/r", "bench home")):
            mac = types.SimpleNamespace(measures=True, root_dir=lambda root=root: root, home_dir=lambda: "", test=lambda *a: True,
                                        read=lambda path: "provisioning complete")
            run = types.SimpleNamespace(d=types.SimpleNamespace(mode="host", c=lambda k: ""), name="m", mac=mac)
            run.gates = lambda run=run: mac_ab.Plant.gates(run)
            with self.subTest(failed), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(1, mac_ab.Plant.preflight(run))
                self.assertEqual([what for _, what, _, _ in brecord.failed(run.gates())], [failed])

    def test_a_reboot_stops_on_a_display_or_firmware_default_it_could_not_read(self):
        for guest, display, firmware in ((True, (brecord.UNKNOWN, "no JSON"), None),
                                         (False, (True, ""), brecord.UNKNOWN),
                                         (False, (True, ""), False)):
            run = types.SimpleNamespace(name="m", display=lambda display=display: display,
                                        firmware=lambda firmware=firmware, guest=guest: (True, "a guest") if guest else (firmware, "x"),
                                        d=types.SimpleNamespace(boot_id=lambda: self.fail("rebooted")))
            with self.subTest(guest=guest, display=display, firmware=firmware), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(Refused):
                    mac_ab.Plant.restart(run)

    def test_gates_and_preflight_count_the_rows_not_measured(self):
        self.assertEqual("", brecord.not_measured(0))
        self.assertIn("2", brecord.not_measured(2))

    def test_a_reading_that_could_not_be_taken_is_named_in_the_result_and_its_report(self):
        rows = [(True, "a", "fine"), (brecord.UNKNOWN, "b", "not read"), (False, "c", "measured bad")]
        notes = brecord.preflight_notes(rows, ["n"])
        self.assertIn("c: measured bad", notes)
        self.assertIn(brecord.UNMEASURED + "b: not read", notes)
        self.assertNotIn(brecord.UNMEASURED + "c", notes)
        run = {"env": {"preflight_notes": notes}, "state": "ok", "dir": "/nowhere"}
        verdicts = {(v, c) for v, c, _ in report.checks("/nowhere", {}, [run], "workspace")}
        self.assertIn(("unknown", "preflight"), verdicts)

    def test_an_unknown_row_renders_apart_from_ok_and_fail(self):
        for ok, word in ((True, "ok"), (False, "FAIL"), (brecord.UNKNOWN, "unk")):
            with self.subTest(word), contextlib.redirect_stderr(io.StringIO()) as e:
                pipeline.check(ok, "x", "d")
            self.assertTrue(e.getvalue().lstrip().startswith(word))


if __name__ == "__main__":
    unittest.main()
