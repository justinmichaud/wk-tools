"""A board as a bench system (lib/wk/bench/board.py): `wk bench deploy` landing a lane's slot on its bench system,
verified against the manifest read back off it, and `wk bench run <ws> <plan> --system <board>` measuring one slot
there -- the bench system found and prepared through the boot driver's channel, the session brought up, run-benchmark
run here with the board's page server behind `Machine.forward`, and the board's evidence taken whether or not the
leg produced a number. Against a FakeBoard (lib/wk/boot/fake.py) whose bench system also answers a run's on-board
files: no ssh, no hardware.

An A/B on one board (lib/wk/bench/board_ab.py) runs its legs as that same run: two slots on one booted system, or
two systems, one boot per arm switch -- `unit boot.two_systems_on_fake`; how a leg boots its system is
tests/test_pi_ab_systems.py's.

Rows landed here: `unit bench.failed_leg_keeps_evidence`, `unit record.progress_shape[board run]`,
`unit killpoints[bench deploy]`, `unit killpoints[bench]` for a board, `unit boot.two_systems_on_fake`;
`bench.pipeline_conformance[board]` is tests/test_bench_pipeline.py's. The live half reads a real board and changes
nothing on it.

Run: python3 tests/run.py -k tests.test_bench_board
"""
import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, requires_machine

sys.path.insert(0, str(REPO / "lib"))
from tests.test_bench_pipeline import PLAN_JSON, RESULT, SHA, Proc, Reg as PipelineReg, invoke  # noqa: E402
from wk import act, fleet, images, record  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import board, board_ab, cli, record as brecord  # noqa: E402
from wk.boot import cli as bootcli  # noqa: E402
from wk.boot.driver import Driver, Onboard, part  # noqa: E402
from wk.boot.fake import FakeBoard, Side  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402

WS = "buildroot-webkit-2.52-buildroot-rpi5-64"
FILES = {"usr/lib/libWPEWebKit-2.0.so.1.0.0": b"webkit-bytes-here"}
BOARD = "testboard"
BCONF = ('KIND=board\nNODE_SSH=testboard-rescue\nNODE_BENCH_SSH=testboard-bench\nNODE_DRIVER=rpi5-usb\nNODE_ROLE=bench-device\n'
         'NODE_ROOT=/dev/mmcblk0p2\nNODE_DEVICE=/dev/sda\nNODE_NOTE="test board"\n')
SLOT_DOC = {"slot": "a", "workspace": WS, "profile": "buildroot-rpi5-64", "browser": "cog", "commit": SHA, "build_id": "b1d" * 8,
            "build_config": "wpe-cross-release", "lib_dir": "usr/lib", "exec_dir": "usr/libexec/wpe-webkit-2.0",
            "bundle_dir": "usr/lib/wpe-webkit-2.0/injected-bundle", "lib_file": "usr/lib/libWPEWebKit-2.0.so.1.0.0",
            "files": {"usr/lib/libWPEWebKit-2.0.so.1.0.0": "ab" * 32}}
OD_AARCH64 = " 127 69 76 70 2 1 1 0 0 0 0 0 0 0 0 0 3 0 183 0\n"
EVIDENCE = {"elf": {"bits": 64}, "gl": {"driver": "/usr/lib/dri/v3d_dri.so"}, "jit": {}, "problems": []}


def write_slot(env, name, files=FILES):
    d = images.slot_dir(WS, name, env)
    root = os.path.join(d, "root")
    for rel, data in files.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    doc = {"slot": name, "commit": "0123456789abcdef0123456789abcdef01234567", "browser": "cog", "build_id": "deadbeef",
           "files": {rel: hashlib.sha256(data).hexdigest() for rel, data in files.items()}}
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "slot.json"), "w") as f:
        json.dump(doc, f)
    return d, doc


def move_tree(fake, src, dst):
    """`mv -f src dst` against a Fake's own dicts -- the shell command real hardware would run."""
    src_slash = src.rstrip("/") + "/"
    moved_files = {p: v for p, v in fake.files.items() if p == src or p.startswith(src_slash)}
    moved_dirs = {d for d in fake.dirs if d == src or d.startswith(src_slash)}
    for p in moved_files:
        del fake.files[p]
    for d in moved_dirs:
        fake.dirs.discard(d)
    for p, v in moved_files.items():
        fake.files[dst + p[len(src):]] = v
    for d in moved_dirs:
        fake.dirs.add(dst + d[len(src):])
    fake.dirs.add(dst)


def _mv(argv, fake):
    move_tree(fake, argv[-2], argv[-1])
    return Result(0)


def board_conf():
    return {"NODE_NAME": BOARD, "NODE_SSH": "testboard-rescue", "NODE_BENCH_SSH": "testboard-bench", "NODE_DRIVER": "rpi5-usb",
            "NODE_ROLE": "bench-device", "NODE_ROOT": "/dev/mmcblk0p2", "NODE_DEVICE": "/dev/sda", "NODE_NOTE": "test board"}


class BenchSide(Side):
    """One side as a real `Ssh` looks to a run: a destination and options, `via` the machine holding its forward,
    and each mutation recorded as asked, so a dry run's plan can be compared with a wet run's effects."""

    def __init__(self, fakeboard, name):
        super().__init__(fakeboard, name)
        self.opts, self.dest, self.via = ["-l", "root"], "testboard-" + name, self

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)


class BenchBoard(FakeBoard):
    """A FakeBoard whose bench system also answers what a run asks of it: its facts, its display, its clock, its session."""

    def __init__(self, conf, clock=None):
        super().__init__(conf, clock)
        self.sides = {"m_ssh": BenchSide(self, "host"), "i_ssh": BenchSide(self, "bench")}
        self.onboard.update({board.Script(REPO, n).text(): n for n in os.listdir(REPO / "bench" / "onboard")})
        self.display, self.weston, self.pinned, self.sysprof = "drm:card1-HDMI-A-1", True, True, False
        self.rescue("rescue-1")
        self.running = self.write_system(part("/dev/sda", 1), "sys-a")

    @property
    def bench(self):
        return self.sides["i_ssh"]

    def run_onboard(self, name, p, input=None):
        facts = "kernel=6.6.31\narch=aarch64\nthrottled=0\ntaskset=yes\nsystemd=yes\nparanoid=2\n"
        answers = {"facts.sh": facts + ("weston=yes\n" if self.weston else "") + ("sysprof=yes\n" if self.sysprof else ""),
                   "display.sh": self.display + "\n",
                   "pin-clock.sh": "governor=performance\nmin=2400000\nmax=%s\n" % ("2400000" if self.pinned else "2800000"),
                   "compositor.sh": "Output 'HDMI-A-1' enabled\n", "at-failure.sh": "--- ps\n  1 init\n"}
        if name in answers:
            return Result(0, answers[name])
        if name in ("browsers-dead.sh", "weston.sh", "seat.sh"):
            return Result(0)
        return super().run_onboard(name, p, input)

    def answer(self, side, argv, input=None):
        if side == "bench" and not self.on_rescue() and self.up:
            if argv[:1] == ["od"]:
                return Result(0, OD_AARCH64)
            if argv[:1] in (["tail"], ["chmod"]) or (argv[:2] == ["sh", "-c"] and argv[2].startswith(("killall", "echo 1 >"))):
                return Result(0, "a board message\n" if argv[:1] == ["tail"] else "")
        return super().answer(side, argv, input)


def local_holders():
    """The claim asked of this store alone: the fleet's other stores are real machines."""
    return mock.patch.object(board, "fleet_holders", lambda root, env, records, res: list(records.holders(res)))


class DeployReg:
    def __init__(self, env):
        self.env, self.machine, self.store = env, Local(), Store(env)

    def ws_target(self, ws):
        return "container"

    def load(self, name):
        return mock.Mock(**{"info.return_value": "running"})

    def in_workspace(self):
        return False


class DeployWorld:
    def __init__(self, tmp):
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"WK_STORE": str(self.tmp / "store"), "WK_MACHINES_DIR": str(self.tmp / "machines")}
        os.makedirs(self.env["WK_MACHINES_DIR"])
        (Path(self.env["WK_MACHINES_DIR"]) / (BOARD + ".conf")).write_text(BCONF)
        self.fake = Fake("board")
        self.fake.react(["mv", "-f"], _mv)
        self.clock = FakeClock()
        self.board = BenchBoard(board_conf(), self.clock)
        self.slotdir, self.doc = write_slot(self.env, "a")

    def bench(self):
        return cli.Bench(str(REPO), DeployReg(self.env), self.clock)

    def deploy(self, name="a", ok=True):
        self.fake.answer(["sh"], rc=0 if ok else 1, out="" if ok else "usr/lib/libWPEWebKit-2.0.so.1.0.0: FAILED\n")
        with local_holders(), contextlib.redirect_stderr(io.StringIO()):
            return self.bench().deploy(WS, BOARD, name, machine=self.fake, driver=Driver(REPO, board_conf(), self.board))

    def state(self):
        return dict(self.fake.files), set(self.fake.dirs)


class TestDeploy(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_deploy_copies_the_tree(self):
        w = DeployWorld(self.tmp)
        w.deploy()
        dest = board.slot_path("a")
        self.assertEqual(w.fake.files[os.path.join(dest, "root", "usr/lib/libWPEWebKit-2.0.so.1.0.0")],
                         FILES["usr/lib/libWPEWebKit-2.0.so.1.0.0"])
        self.assertEqual(json.loads(w.fake.files[os.path.join(dest, "slot.json")]), w.doc)
        self.assertNotIn(dest + ".part", w.fake.dirs)

    def test_the_manifest_is_read_back_and_compared(self):
        """The sums fed to the board's sha256sum come from the slot.json read back off the board's own
        copy, not the local one -- a corrupted manifest transfer would be caught too."""
        w = DeployWorld(self.tmp)
        w.deploy()
        ran = [e for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c")]
        self.assertEqual(len(ran), 1)

    def test_a_mismatch_refuses(self):
        w = DeployWorld(self.tmp)
        with self.assertRaises(Refused):
            w.deploy(ok=False)
        dest, part_ = board.slot_path("a"), board.slot_path("a") + ".part"
        self.assertNotIn(dest, w.fake.dirs)
        self.assertIn(os.path.join(part_, "slot.json"), w.fake.files)

    def test_an_unbuilt_slot_refuses_before_anything_is_copied(self):
        w = DeployWorld(self.tmp)
        with self.assertRaises(Refused):
            w.deploy(name="never-built")
        self.assertEqual(w.fake.effects, [])

    def test_an_unknown_board_refuses(self):
        w = DeployWorld(self.tmp)
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
            w.bench().deploy(WS, "not-a-real-board", "a", machine=w.fake)

    def test_a_board_armed_for_a_boot_it_has_not_taken_is_not_deployed_to(self):
        """The barrier asks the boot driver's own record: an arming not yet spent means the filesystem answering
        ssh is not the one about to run."""
        w = DeployWorld(self.tmp)
        w.board.running = w.board.conf["NODE_ROOT"]
        w.board.rescue("")
        w.board.record = "image=sys-b\narmed_boot_id=boot-%d\n" % w.board.boots
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err), local_holders():
            w.bench().deploy(WS, BOARD, "a", machine=w.fake, driver=Driver(REPO, board_conf(), w.board))
        self.assertIn("armed for system 'sys-b'", err.getvalue())
        self.assertEqual([e for e in w.fake.effects if e[0] != "run"], [])

    def test_a_board_that_declares_no_driver_is_refused(self):
        w = DeployWorld(self.tmp)
        (Path(w.env["WK_MACHINES_DIR"]) / "bare.conf").write_text("KIND=board\nNODE_SSH=bare\n")
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()), local_holders():
            w.bench().deploy(WS, "bare", "a", machine=w.fake)


class TestADeployClaimsTheBoard(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def records(self, w):
        return record.Records(env=w.env)

    def test_the_claim_is_held_while_it_lands_and_ended_after(self):
        w = DeployWorld(self.tmp)
        w.deploy()
        (t,) = self.records(w).list()
        self.assertEqual((t.raw("holds"), t.field("exit")), ("device:" + BOARD, "0"))

    def test_a_board_another_task_holds_is_not_deployed_to(self):
        w = DeployWorld(self.tmp)
        self.records(w).begin("bench", "here", "other", "kill 1", "", ["x"], holds="device:" + BOARD, pid=os.getpid())
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err), local_holders():
            w.bench().deploy(WS, BOARD, "a", machine=w.fake, driver=Driver(REPO, board_conf(), w.board))
        self.assertIn("another live task holds it", err.getvalue())
        self.assertEqual(w.fake.effects, [])


class TestDeployDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)

    def test_the_plan_is_the_runs_mutations(self):
        """`dispatch.dry_run_is_the_recorder[bench]`: a dry deploy records the same effects a wet one
        does -- each already skips its own mutation under --dry-run -- and lands no bytes."""
        wet, dry = DeployWorld(self.tmp), DeployWorld(self.tmp)
        wet.deploy()
        os.environ["WK_DRY_RUN"] = "1"
        try:
            dry.deploy()
        finally:
            del os.environ["WK_DRY_RUN"]

        def strip(w):
            return [tuple(str(x).replace(str(w.tmp), "") for x in e) for e in w.fake.effects
                    if e[0] in ("mkdir", "remove", "copy_in", "copy_tree_in")]
        self.assertEqual(strip(wet), strip(dry))
        self.assertEqual(dry.fake.files, {})
        self.assertNotIn(board.slot_path("a"), dry.fake.dirs)


class TestDeployKillPoints(unittest.TestCase):
    def test_a_deploy_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[bench deploy]`: a kill at any point leaves either the old slot or nothing --
        never a half-written one -- and a rerun reaches the same final tree."""
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        converges(self, lambda: DeployWorld(tmp), lambda w: w.deploy(), DeployWorld.state)


class TestInAWorkspaceItIsABrokerRequest(unittest.TestCase):
    """A sandboxed workspace reaches a board only through the broker, which runs the same command on the workstation."""

    def world(self, socket_up=True):
        tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-"))
        self.addCleanup(shutil.rmtree, str(tmp), ignore_errors=True)
        here = Fake("here")
        here.answer(["test", "-S"], rc=0 if socket_up else 1)
        here.files[str(REPO / "container" / "broker" / "wk-broker-client.py")] = ""
        here.answer(["env"], rc=0)
        reg = mock.Mock()
        reg.env, reg.machine = {"WK_BROKER_SOCKET": "/run/wk/broker.sock", "WK_NAME": WS}, here
        reg.in_workspace.return_value = True
        return here, reg

    def test_a_deploy_is_a_stage_request(self):
        here, reg = self.world()
        rc = self.deploy(reg)
        self.assertEqual(rc, 0)
        (call,) = [e for e in here.effects if e[0] == "run_tty"]
        self.assertEqual(call[1][-4:], ("stage", "machine=" + BOARD, "workspace=" + WS, "slot=b"))

    def deploy(self, reg):
        return cli.Bench(str(REPO), reg, FakeClock()).deploy(WS, BOARD, "b")

    def test_a_board_run_is_a_run_request(self):
        from wk.bench import pipeline
        here, reg = self.world()
        rc = pipeline.run(str(REPO), reg, ["jetstream3"], {"system": BOARD, "count": "2"}, False, FakeClock())
        self.assertEqual(rc, 0)
        (call,) = [e for e in here.effects if e[0] == "run_tty"]
        self.assertEqual(call[1][-5:], ("run", "machine=" + BOARD, "workspace=" + WS, "plan=jetstream3", "count=2"))

    def test_no_broker_names_the_stage_that_opens_it(self):
        here, reg = self.world(socket_up=False)
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err):
            self.deploy(reg)
        self.assertIn("./setup --stage broker", err.getvalue())
        self.assertIn("wk bench deploy %s %s --slot b" % (WS, BOARD), err.getvalue())


class TestTheBrokerRunsTheSameCommandOnTheWorkstation(unittest.TestCase):
    def broker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("wk_broker", REPO / "container" / "broker" / "wk-broker.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.fleet = lambda: {BOARD: {"name": BOARD, "role": "bench-device", "os": "any", "profile": "", "note": ""}}
        return mod

    def test_stage_is_a_deploy_and_run_a_board_run_of_the_named_workspace(self):
        b = self.broker()
        _, argv, _ = b.build_stage({"machine": BOARD, "workspace": WS, "slot": "b"})
        self.assertEqual(argv[1:], ["bench", "deploy", WS, BOARD, "--slot", "b"])
        _, argv, _ = b.build_run({"machine": BOARD, "workspace": WS, "plan": "jetstream3", "count": "2"})
        self.assertEqual(argv[1:], ["bench", "run", WS, "jetstream3", "--system", BOARD, "--count", "2"])

    def test_a_run_names_its_workspace(self):
        b = self.broker()
        with self.assertRaises(b.Refused):
            b.build_run({"machine": BOARD, "plan": "jetstream3"})


class BoardWorld(Fake):
    """This host running `wk bench run ws <plan> --system testboard`: its store, the mirror the runner tree is
    exported from (already exported), the payload pinned, and a BenchBoard up in bench mode with slot `a` on it.
    `popen` is run-benchmark: it writes the result and the driver's running-binary check, and notes whether
    the board's forward was held while it ran."""

    def __init__(self, tmp, rc=0):
        super().__init__("here")
        self.kind, self.rc, self.driver_cls = "container", rc, Driver
        self.fails = lambda exports: False
        self.evidence = lambda exports: EVIDENCE
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        machines = self.tmp / "machines"
        machines.mkdir()
        (machines / (BOARD + ".conf")).write_text(BCONF)
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_NAME": "ws", "WK_MACHINES_DIR": str(machines),
                    "WK_IN_VM": "1", "WK_JOB_PID_TRIES": "0", "WK_POLL_SECONDS": "1"}
        self.clock = FakeClock()
        self.board = BenchBoard(board_conf(), self.clock)
        self.fake = self.board.bench
        self.board.bench.files[board.slot_path("a") + "/slot.json"] = json.dumps(SLOT_DOC)
        self.store = Store(self.env)
        self.tree = os.path.join(self.store.artifact_dir(), "bench-runner", SHA[:12])
        self.dirs.add(self.store.mirror())
        self.files[os.path.join(self.tree, "Tools", "Scripts", "run-benchmark")] = ""
        self.files[os.path.join(self.tree, "Tools", "Scripts", "webkitpy/benchmark_runner/data/plans/jetstream3.plan")] = PLAN_JSON
        self.seed_dest = os.path.join(self.env["WK_STORE"], "cache", "bench", "jetstream3-" + SHA[:12])
        self.dirs.update({self.seed_dest, os.path.join(self.seed_dest, ".wk-seeded")})
        for prefix, out in ((["git", "-C"], SHA + "\n"), (["git", "ls-remote"], SHA + "\trefs/heads/main\n"),
                            (["python3", "-c", board.FREE_PORT], "45678\n")):
            self.answer(prefix, out=out)
        self.react(["mv", "-f"], _mv)
        self.watched, self.forwarded = [], []

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        return super().act_run(argv, **kw)

    def popen(self, argv, stdin=None, stdout=None, stderr=None, cwd=None):
        self.watched.append((list(argv), cwd))
        self.forwarded.append(set(self.board.bench.pids))
        script = argv[-1]
        exports = dict(l[len("export "):].split("=", 1) for l in script.splitlines() if l.startswith("export "))
        self.exports = {k: shlex.split(v)[0] if v else "" for k, v in exports.items()}
        run = shlex.split(script.splitlines()[-1].split(" && exec ", 1)[1])
        rc = 3 if self.fails(self.exports) else self.rc
        stdout.write(b"Score: 30\n")
        if rc == 0:
            Path(run[run.index("--output-file") + 1]).write_text(RESULT)
        with open(self.exports["WK_BOARD_EVIDENCE"], "a") as f:
            f.write(json.dumps({"ok": rc == 0}) + "\n")
        if self.exports.get("WK_BOARD_WARMUP"):
            Path(self.exports["WK_BOARD_WARMUP"]).write_text(json.dumps(self.evidence(self.exports)))
        self.board.bench.files[board.BROWSER_LOG] = "browser said this\n"
        self.clock.t += 60   # a run takes time, so two legs of one slot are two run directories
        return Proc(rc)

    def patches(self):
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(bootcli, "driver_for", lambda root, conf: self.driver_cls(REPO, conf, self.board)))
        stack.enter_context(local_holders())
        return stack

    def invoke(self, *argv):
        err = io.StringIO()
        with self.patches(), contextlib.redirect_stderr(err):
            try:
                rc = invoke(self, ("run",) + (argv or ("jetstream3",)) + ("--system", BOARD))
            except Refused as e:
                rc = e.status
        self.err = err.getvalue()
        return rc

    def leg(self, plan="jetstream3", **o):
        """One leg as an A/B runs it, with the leg's own options and no workspace."""
        reg = PipelineReg(self)
        err = io.StringIO()
        with self.patches(), contextlib.redirect_stderr(err):
            system = board.for_board(str(REPO), reg, "", self.clock, BOARD)
            r = board.BoardRun(str(REPO), reg, system, self.clock, self.env, self.popen)
            os.makedirs(r.bench_dir, exist_ok=True)
            try:
                rc = r.go(plan, o)
            except Refused as e:
                rc = e.status
        self.err = err.getvalue()
        return rc

    def bench_dir(self):
        return Path(self.store.bench_dir())

    def tasks(self):
        return brecord.tasks(str(self.bench_dir()))

    def recs(self):
        return record.Records(self.store.record_dir(), clock=self.clock, env=self.env, machine=self)

    def run_dir(self, task=None):
        task = task or self.tasks()[-1]
        runs = sorted(os.listdir(self.bench_dir() / task / "runs"))
        return self.bench_dir() / task / "runs" / runs[-1]

    def runs(self):
        """Every run of the last task, in the order they ran, as its env.json."""
        d = self.bench_dir() / self.tasks()[-1] / "runs"
        return [json.loads((d / r / "env.json").read_text()) for r in sorted(os.listdir(d))]

    def ran(self, name):
        """How many times the bench system ran on-board file `name` as a mutation."""
        text = (board.Script if (REPO / "bench" / "onboard" / name).is_file() else Onboard)(REPO, name).text()
        return sum(1 for e in self.board.bench.effects if e[0] == "act" and e[1][-1].endswith(text))

    def env_json(self):
        return json.loads((self.run_dir() / "env.json").read_text())

    def state(self):
        tasks = self.tasks()
        if not tasks:
            return None
        st = brecord.task_state(str(self.bench_dir() / tasks[-1]), False)
        recs = [t for t in self.recs().list() if t.field("kind") == "bench"]
        return st["state"], st["ok"], recs[-1].field("exit") if recs else None


class BoardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-board-run-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), ignore_errors=True)
        self._env = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_DEVICE_HELD", "WK_BENCH_ASLR", "WK_BENCH_ENV_PAD"):
            os.environ.pop(v, None)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        self.addCleanup(act._forced.clear)

    def world(self, **kw):
        return BoardWorld(self.tmp, **kw)


class TestARun(BoardTest):
    def test_the_board_is_prepared_and_the_run_is_run_here_with_its_forward_held(self):
        w = self.world()
        self.assertEqual(w.invoke(), 0, w.err)
        self.assertIn("BENCH OK  jetstream3", w.err)
        ((argv, cwd),) = w.watched
        self.assertEqual((argv[:2], cwd), (["bash", "-c"], w.tree))
        self.assertTrue(w.forwarded[0], "no forward was held while run-benchmark ran")
        self.assertEqual(w.board.bench.pids, set(), "the forward outlived the run")
        self.assertIn(("forward", 45678), w.board.bench.effects)
        self.assertEqual(w.exports["WK_BOARD_URL"], "127.0.0.1:45678")
        self.assertEqual(w.exports["WK_BOARD_SSH"], "ssh -l root testboard-bench")
        self.assertTrue(w.board.kept, "the bench system was not claimed against its self-return watchdog")
        self.assertEqual(json.loads(w.exports["WK_BOARD_EXPECT"])["lib"], "/var/wk/slots/a/root/usr/lib/libWPEWebKit-2.0.so.1.0.0")

    def test_the_run_is_recorded_as_the_slot_it_measured(self):
        w = self.world()
        w.invoke("jetstream3", "--count", "2")
        env = w.env_json()
        self.assertEqual((env["bench_host"], env["machine"], env["system"], env["build_slot"], env["webkit_sha"], env["workspace"]),
                         ("image", BOARD, "sys-a", "a", SHA, WS))
        self.assertEqual((env["session_mode"], env["gpu_renderer"], env["host"]["cpu_khz"], env["host"]["dvfs_pinned"]),
                         ("drm", "gl", "2400000", True))
        self.assertTrue(env["verified"])
        doc = json.loads((w.bench_dir() / w.tasks()[-1] / "task.json").read_text())
        self.assertEqual((doc["subject"], doc["devices"]), ({"kind": "slots", "spec": "a"}, [{"device": BOARD, "profile": "buildroot-rpi5-64"}]))
        self.assertEqual(doc["commands"], ["wk bench run ws jetstream3 --system testboard --slot a --count 2"])
        self.assertEqual((w.run_dir() / "browser.log").read_text(), "browser said this\n")

    def test_a_run_writes_the_one_progress_record(self):
        """`record.progress_shape[board run]`: step n of m, the log, how to stop it, how it ended -- and the board claimed."""
        w = self.world()
        w.invoke()
        (t,) = w.recs().list()
        self.assertEqual((t.field("kind"), t.field("where"), t.field("name"), t.field("exit")), ("bench", "here", "ws", "0"))
        self.assertEqual(len(t.plan()), 3)
        self.assertTrue(t.plan()[0].startswith("bring up the session on testboard for slot 'a'"), t.plan())
        self.assertEqual(t.steps(), [(1, "done"), (2, "done"), (3, "running")])
        self.assertEqual((t.field("kill"), t.raw("holds")), ("wk bench run ws --kill --system testboard", "device:testboard"))
        self.assertEqual(Path(t.field("log")), w.run_dir() / "run.log")

    def test_every_effect_on_the_board_goes_through_its_bench_system(self):
        """The rescue/host install a bench-device answers on is probed, never acted on: a board benched on its rescue is the regression."""
        w = self.world()
        w.invoke()
        self.assertEqual([e for e in w.board.sides["m_ssh"].effects if e[0] != "run"], [])
        self.assertTrue([e for e in w.board.bench.effects if e[0] == "act"])

    def test_a_failed_leg_keeps_its_evidence(self):
        """`bench.failed_leg_keeps_evidence`: the board's account of the failure, its browser's log and its messages
        are kept, the browser is stopped, and the record ends with the run's status."""
        w = self.world(rc=3)
        self.assertEqual(w.invoke(), 3, w.err)
        d = w.run_dir()
        self.assertIn("--- ps", (d / "diagnose" / "board-at-failure.txt").read_text())
        self.assertEqual((d / "browser.log").read_text(), "browser said this\n")
        self.assertEqual((d / "board.log").read_text(), "a board message\n")
        self.assertFalse(w.env_json()["verified"])
        self.assertTrue([e for e in w.board.bench.effects if e[0] == "act" and e[1][2].startswith("killall cog")])
        self.assertEqual(w.state(), ("complete", 0, "3"))


class TestRefusals(BoardTest):
    def refused(self, w, *argv):
        self.assertNotEqual(w.invoke(*argv), 0, w.err)
        return w.err

    def test_a_board_in_host_mode_is_not_measured(self):
        w = self.world()
        w.board.running = w.board.conf["NODE_ROOT"]
        w.board.rescue("")
        self.assertIn("not running a wk bench system", self.refused(w))
        self.assertEqual(w.watched, [])

    def test_a_board_on_its_rescue_system_is_a_barrier(self):
        w = self.world()
        w.board.running = w.board.conf["NODE_ROOT"]
        self.assertIn("running its rescue system", self.refused(w))

    def test_a_board_armed_for_a_boot_it_has_not_taken_is_not_measured(self):
        w = self.world()
        w.board.running = w.board.conf["NODE_ROOT"]
        w.board.rescue("")
        w.board.record = "image=sys-b\narmed_boot_id=boot-%d\n" % w.board.boots
        self.assertIn("armed for system 'sys-b'", self.refused(w))

    def test_no_slot_names_the_deploy(self):
        w = self.world()
        self.assertIn("wk bench deploy <lane> testboard --slot b", self.refused(w, "jetstream3", "--slot", "b"))

    def test_an_instrumented_slot_is_never_measured(self):
        w = self.world()
        w.board.bench.files[board.slot_path("a") + "/slot.json"] = json.dumps(dict(SLOT_DOC, build_config=board.INSTRUMENTED))
        self.assertIn("is an instrumented build", self.refused(w))

    def test_a_leg_on_the_wrong_system_is_refused_before_anything_runs(self):
        w = self.world()
        self.assertNotEqual(w.leg(expect="sys-b", slot="a"), 0)
        self.assertIn("not the 'sys-b' this leg is for", w.err)
        self.assertEqual(w.watched, [])
        self.assertEqual(w.leg(expect="sys-a", slot="a"), 0, w.err)

    def collecting(self):
        w = self.world()
        w.board.bench.files[board.slot_path("a-instr") + "/slot.json"] = json.dumps(dict(SLOT_DOC, build_config=board.INSTRUMENTED))
        return w

    def test_a_collection_needs_an_instrumented_slot(self):
        self.assertIn("would write no profile", self.refused(self.collecting(), "jetstream3", "--slot", "a", "--collect"))

    def test_a_collection_lands_in_its_lane_and_in_no_task(self):
        w = self.collecting()
        self.assertEqual(w.invoke("jetstream3", "--slot", "a-instr", "--collect"), 0, w.err)
        self.assertEqual(w.tasks(), [])
        self.assertTrue(Path(images.pgo_dir("ws", "a", w.env), "jetstream3", "result.json").is_file())
        run = w.watched[0][0][-1]
        self.assertIn("--generate-pgo-profiles", run)
        self.assertIn("--count 1 --timeout 7200", run)
        self.assertEqual(w.exports["WK_BOARD_PGO"], "/var/wk/pgo")
        self.assertIn("LLVM_PROFILE_FILE=/var/wk/pgo/WPEWebKit_%p.profraw", w.exports["WK_BOARD_LAUNCH"])

    def test_a_collection_takes_one_iteration(self):
        self.assertIn("runs one iteration", self.refused(self.collecting(), "jetstream3", "--slot", "a-instr", "--collect", "--count", "3"))

    def test_the_slot_decides_the_build_so_config_is_refused(self):
        self.assertIn("--config: a board runs the slot it holds", self.refused(self.world(), "jetstream3", "--config", "wpe-release"))

    def test_no_display_and_an_unpinned_clock_fail_preflight_and_force_records_them(self):
        w = self.world()
        w.board.display, w.board.pinned = "", False
        self.assertIn("2 preflight check(s) failed", self.refused(w))
        w = self.world()
        w.board.display, w.board.pinned, w.board.weston = "", False, True
        w.env["WK_FORCE"] = "1"
        os.environ["WK_FORCE"] = "1"
        self.assertEqual(w.invoke(), 0, w.err)
        env = w.env_json()
        self.assertTrue(env["forced"] and env["software"])
        self.assertEqual((env["session_mode"], env["gpu_renderer"]), ("headless-rdp", "pixman"))
        self.assertIn("clock pinned: min 2400000, max 2800000", env["preflight_notes"])

    def test_cores_without_taskset_on_the_image_is_refused(self):
        w = self.world()
        w.board.run_onboard = lambda name, p, input=None: (Result(0, "arch=aarch64\n") if name == "facts.sh"
                                                            else BenchBoard.run_onboard(w.board, name, p, input))
        self.assertIn("has no taskset", self.refused(w, "jetstream3", "--cores", "0-3"))

    def test_another_live_task_holding_the_board_is_a_barrier(self):
        w = self.world()
        w.pids.add(99)
        w.recs().begin("bench", "here", "other", "kill 99", "/l", ["x"], holds="device:" + BOARD, pid=99)
        self.assertIn("another live task holds it", self.refused(w))


class TestTheLaunch(unittest.TestCase):
    """What the board's shell runs to start the browser: the slot's own libraries, its own cache, the pin, and
    the JIT tier report only when asked for, on a warmup leg."""

    def system(self, session="drm"):
        s = board.BoardSystem(str(REPO), mock.Mock(), None, "", FakeClock(), BOARD, None, Fake())
        s.doc, s.session = dict(SLOT_DOC), session
        return s

    def leg(self, **o):
        leg = mock.Mock()
        leg.slot, leg.cores, leg.o = "pr", o.pop("cores", ""), o
        return leg

    def test_every_launch_names_its_own_cache_directory(self):
        self.assertIn("XDG_CACHE_HOME=/tmp/wk-webkit-cache", self.system().launch(self.leg()))

    def test_cores_become_a_taskset_prefix_on_the_board(self):
        out = self.system().launch(self.leg(cores="0-3"))
        self.assertIn("exec taskset -c 0-3 env LD_LIBRARY_PATH=/var/wk/slots/pr/root/usr/lib", out)
        self.assertTrue(out.endswith("/usr/bin/cog"), out)
        self.assertNotIn("taskset", self.system().launch(self.leg()))

    def test_the_jit_tier_report_is_asked_for_on_a_warmup_leg_only(self):
        self.assertNotIn("JSC_reportDFGCompileTimes=1", self.system().launch(self.leg(warmup="1")))
        self.assertNotIn("JSC_reportDFGCompileTimes=1", self.system().launch(self.leg(jit_tiers="1")))
        self.assertIn("JSC_reportDFGCompileTimes=1", self.system().launch(self.leg(warmup="1", jit_tiers="1")))

    def test_a_launch_is_posix_sh(self):
        """The driver runs the launch text through the board's /bin/sh (busybox ash); dash's parser is the judge."""
        import subprocess
        s = self.system()
        s.doc["browser"] = "minibrowser"
        sh = shutil.which("dash") or "sh"
        for session in ("drm", "rdk"):
            s.session = session
            cp = subprocess.run([sh, "-n"], input=s.launch(self.leg()), capture_output=True, text=True, timeout=10)
            self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_a_collection_writes_its_profile_where_the_image_expects(self):
        out = self.system().launch(self.leg(pgo_dir="/p", pgo_file="/var/wk/pgo/lib_%p.profraw"))
        self.assertIn("LLVM_PROFILE_FILE=/var/wk/pgo/lib_%p.profraw", out)

    def test_the_evidence_and_the_profile_of_one_leg_are_two_files(self):
        s = self.system()
        leg = self.leg(arm="a")
        leg.out = "/b/t/runs/r"
        self.assertEqual(s.warm_file(leg, "evidence.json"), "/b/t/warmup/testboard-a.evidence.json")
        self.assertNotEqual(s.warm_file(leg, "evidence.json"), s.warm_file(leg, "profile.json"))

    def test_the_word_size_is_the_measured_librarys_not_the_kernels(self):
        self.assertEqual(board.elf_arch(OD_AARCH64), "aarch64")
        self.assertEqual(board.elf_arch(" 127 69 76 70 1 1 1 0 0 0 0 0 0 0 0 0 3 0 40 0"), "armv7l")
        self.assertEqual(board.elf_arch(""), "")


class TestTheBoardsShell(unittest.TestCase):
    def test_every_file_parses_as_posix_sh(self):
        import subprocess
        for f in sorted((REPO / "bench" / "onboard").iterdir()):
            with self.subTest(file=f.name):
                cp = subprocess.run(["sh", "-n", str(f)], capture_output=True, text=True, timeout=10)
                self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_seat_is_found_by_its_pid_file_not_by_a_pattern_its_own_shell_carries(self):
        """`sh -c <file>` carries the whole file in its own command line, so a pgrep for a word in it matches that shell."""
        self.assertNotIn("pgrep", (REPO / "bench" / "onboard" / "seat.sh").read_text())


class TestALegForAnAB(BoardTest):
    """An A/B's leg lands in the A/B's task, with its round and arm."""

    def task(self, w):
        taskdir = w.bench_dir() / "t1"
        brecord.task_write(str(taskdir), ["task=t1", "requested=x", "devices=" + BOARD, "plans=jetstream3", "rounds=2", "slots=a,b"], ["wk bench run --ab"])
        return taskdir

    def test_a_leg_lands_in_the_task_it_names_with_its_round_and_arm(self):
        w = self.world()
        self.task(w)
        self.assertEqual(w.leg(slot="a", task="t1", round="1", arm="b", slot_a="a", slot_b="b"), 0, w.err)
        env = w.env_json()
        self.assertEqual((env["task"], env["ab"]), ("t1", {"round": "1", "arm": "b", "slot_a": "a", "slot_b": "b"}))
        self.assertEqual(w.tasks(), ["t1"])
        (t,) = w.recs().list()
        self.assertEqual((t.field("name"), t.field("kill")[:5]), (BOARD, "kill "))

    def test_an_unknown_task_is_refused_before_the_board_is_touched(self):
        w = self.world()
        self.assertNotEqual(w.leg(slot="a", task="nope"), 0)
        self.assertIn("no such task 'nope'", w.err)
        self.assertEqual(w.board.bench.effects, [])

    def test_a_settle_leg_is_recorded_as_one(self):
        w = self.world()
        self.task(w)
        w.leg(slot="a", task="t1", settle="1")
        self.assertTrue(w.run_dir().name.endswith("-settle"))
        env = w.env_json()
        self.assertEqual((env["warmup"], env["warmup_kind"]), (True, "settle"))

    def test_a_warmup_leg_stages_the_profiler_and_keeps_both_artifacts(self):
        from wk.quiet import lib_argv
        w = self.world()
        taskdir = self.task(w)
        samply = w.tmp / "samply"
        samply.write_text("binary")
        w.answer(lib_argv(str(REPO), "lib/profiler.sh", "profiler_resolve")[:3], out="samply upstream publishes samply for aarch64\n")
        w.answer(lib_argv(str(REPO), "lib/profiler.sh", "samply_fetch")[:3], out=str(samply) + "\n")
        capture = "%s/%s-a.profile.json" % (board.PROF_REMOTE, BOARD)
        w.board.bench.files[capture] = "{\"meta\": {}}"
        self.assertEqual(w.leg(slot="a", task="t1", round="0", arm="a", warmup="1"), 0, w.err)
        self.assertIn(("copy_in", str(samply), board.PROF_REMOTE + "/samply"), w.board.bench.effects)
        self.assertTrue([e for e in w.board.bench.effects if e[0] == "act" and "perf_event_paranoid" in e[1][-1]])
        self.assertTrue((taskdir / "warmup" / "testboard-a.evidence.json").is_file())
        self.assertTrue((taskdir / "warmup" / "testboard-a.profile.json").is_file())
        self.assertEqual(w.exports["WK_BOARD_PROFILE"], "samply:" + capture)
        env = w.env_json()
        self.assertEqual((env["warmup_kind"], env["profiler"], env["host"]["perf_event_paranoid"]), ("evidence", "samply", "2"))

    def test_no_warmup_profile_stages_nothing(self):
        w = self.world()
        self.task(w)
        self.assertEqual(w.leg(slot="a", task="t1", arm="a", warmup="1", no_warmup_profile="1"), 0, w.err)
        self.assertFalse([e for e in w.effects if e[0] == "run" and "profiler.sh" in " ".join(e[1])])
        self.assertEqual(w.exports["WK_BOARD_PROFILE"], "")
        self.assertEqual(w.env_json()["profiler"], "none")


class TestTheRunnerTree(BoardTest):
    def test_an_export_lands_whole_or_not_at_all(self):
        w = self.world()
        del w.files[os.path.join(w.tree, "Tools", "Scripts", "run-benchmark")]
        w.answer(["sh", "-c"], out="")
        with contextlib.redirect_stderr(io.StringIO()):
            tree, sha = board.runner_tree(PipelineReg(w), w, str(REPO))
        self.assertEqual((tree, sha), (w.tree, SHA))
        order = [e[1][:2] if e[0] == "act" else e[:2] for e in w.effects if e[0] in ("act", "remove", "mkdir")]
        self.assertEqual(order, [("remove", w.tree + ".tmp"), ("mkdir", w.tree + ".tmp"), ("sh", "-c"), ("remove", w.tree), ("mv", "-f")])
        self.assertIn(("copy_in", str(REPO / board.DRIVER), os.path.join(w.tree, board.DRIVERS, "wk_board_driver.py")), w.effects)

    def test_no_mirror_names_wk_sync(self):
        w = self.world()
        w.dirs.discard(w.store.mirror())
        err = io.StringIO()
        with self.assertRaises(Refused), contextlib.redirect_stderr(err):
            board.runner_tree(PipelineReg(w), w, str(REPO))
        self.assertIn("'wk sync' makes one", err.getvalue())


class TestKill(BoardTest):
    def test_nothing_running_says_so(self):
        w = self.world()
        self.assertEqual(w.invoke("--kill"), 2, w.err)
        self.assertIn("no bench is running for 'ws'", w.err)

    def test_a_live_run_is_stopped_and_recorded_cancelled(self):
        w = self.world()
        w.pids.add(99)
        w.recs().begin("bench", "here", "ws", "k", "/l", ["x"], pid=99)
        self.assertEqual(w.invoke("--kill"), 0, w.err)
        self.assertEqual(w.recs().list()[-1].field("exit"), "cancelled")


class TestDryRun(BoardTest):
    def test_the_plan_is_the_runs_mutations(self):
        """`dispatch.dry_run_is_the_recorder[bench]` for a board: what a dry run says it would do on the board and
        here is what a wet run does, and it writes no record."""
        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            both = [("board",) + e for e in w.board.bench.effects] + [("here",) + e for e in w.effects]
            return [tuple(str(x).replace(str(w.tmp), "") for x in e) for e in both
                    if e[1] in ("act", "write", "mkdir", "remove", "copy_in", "copy_out", "forward")
                    and not (isinstance(e[2], str) and e[2].startswith(locks))]
        wet, dry = self.world(), self.world()
        wet.invoke()
        os.environ["WK_DRY_RUN"] = "1"
        try:
            self.assertEqual(dry.invoke(), 0, dry.err)
        finally:
            del os.environ["WK_DRY_RUN"]
        self.assertEqual(mutations(wet), mutations(dry))
        self.assertIn("would run: bash -c", dry.err)
        self.assertEqual((dry.watched, dry.tasks(), dry.recs().list()), ([], [], []))


class TestKillPoints(BoardTest):
    def test_a_run_killed_after_any_effect_on_the_board_and_rerun_converges(self):
        """`killpoints[bench]` for a board: each run is its own task, and nothing a killed one left is trusted."""
        def run_once(w):
            w.clock.t += 1
            w.invoke()
        converges(self, self.world, run_once, BoardWorld.state)


class ABTest(BoardTest):
    def world(self, **kw):
        """A world whose warmup legs can profile: samply resolves and fetches here, and each arm's capture is on the board."""
        from wk.quiet import lib_argv
        w = super().world(**kw)
        samply = w.tmp / "samply"
        samply.write_text("binary")
        w.answer(lib_argv(str(REPO), "lib/profiler.sh", "profiler_resolve")[:3], out="samply upstream publishes samply for aarch64\n")
        w.answer(lib_argv(str(REPO), "lib/profiler.sh", "samply_fetch")[:3], out=str(samply) + "\n")
        for arm in "ab":
            w.board.bench.files["%s/%s-%s.profile.json" % (board.PROF_REMOTE, BOARD, arm)] = "{}"
        return w

    def ab_world(self, **kw):
        w = self.world(**kw)
        w.board.bench.files[board.slot_path("b") + "/slot.json"] = json.dumps(dict(SLOT_DOC, slot="b", commit="f" * 40))
        return w

    def order(self, w):
        return [(e["ab"]["round"], e["ab"]["arm"], e["build_slot"], e.get("warmup_kind", "")) for e in w.runs()]


class TestASlotAB(ABTest):
    """`--ab A,B`: two slots on the booted system, the system held fixed."""

    def test_a_discarded_warmup_then_rounds_that_alternate_the_lead(self):
        w = self.ab_world()
        self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--rounds", "2"), 0, w.err)
        self.assertEqual(self.order(w), [("0", "a", "a", "evidence"), ("0", "b", "b", "evidence"),
                                         ("1", "a", "a", ""), ("1", "b", "b", ""), ("2", "b", "b", ""), ("2", "a", "a", "")])
        self.assertEqual({(e["ab"]["slot_a"], e["ab"]["slot_b"], e["task"]) for e in w.runs()}, {("a", "b", w.tasks()[-1])})

    def test_the_board_is_prepared_once_and_rechecked_every_leg(self):
        """The clock pin, the claim and the session are taken once on a boot; every leg re-reads the probe and its slot."""
        w = self.ab_world()
        self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--rounds", "2"), 0, w.err)
        self.assertEqual((w.ran("pin-clock.sh"), w.ran("keep.sh"), w.ran("weston.sh"), w.ran("browsers-dead.sh")), (1, 1, 1, 1))
        probes = [e for e in w.board.bench.effects if e[0] == "run" and e[1][-1] == Onboard(REPO, "probe.sh").text()]
        self.assertGreaterEqual(len(probes), 6)
        self.assertEqual(len({e["runner_sha"] for e in w.runs()}), 1)
        self.assertEqual(len([e for e in w.effects if e[0] == "run" and e[1][:2] == ("git", "-C")]), 1, "the runner was resolved per leg")

    def test_the_task_records_the_board_with_its_profile(self):
        w = self.ab_world()
        w.invoke("jetstream3", "--ab", "a,b", "--rounds", "2", "--count", "2")
        doc = json.loads((w.bench_dir() / w.tasks()[-1] / "task.json").read_text())
        self.assertEqual((doc["subject"], doc["devices"], doc["slots"], doc["rounds"], doc["count"]),
                         ({"kind": "slots", "spec": "a,b"}, [{"device": BOARD, "profile": "buildroot-rpi5-64"}], ["a", "b"], 2, "2"))
        self.assertEqual(doc["commands"], ["wk bench run ws jetstream3 --system testboard --ab a,b --rounds 2 --count 2"])
        self.assertTrue(w.tasks()[-1].endswith("-testboard-a-vs-b"))

    def test_the_board_is_held_for_the_whole_ab_and_each_leg_is_its_own_record(self):
        w = self.ab_world()
        self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--rounds", "1"), 0, w.err)
        recs = w.recs().list()
        held = [t for t in recs if t.raw("holds") == "device:" + BOARD]
        self.assertEqual(len(held), 1)
        self.assertEqual((held[0].field("name"), held[0].field("exit"), held[0].field("kill")),
                         ("ws", "0", "wk bench run ws --kill --system testboard"))
        legs = [t for t in recs if t.field("name") == "ws-leg"]
        self.assertTrue(legs)
        self.assertEqual({(t.raw("holds"), t.field("kill")) for t in legs}, {(None, "wk bench run ws --kill --system testboard")})

    def test_a_board_another_task_holds_is_not_benched(self):
        w = self.ab_world()
        w.pids.add(99)
        w.recs().begin("bench", "here", "other", "kill 99", "/l", ["x"], holds="device:" + BOARD, pid=99)
        self.assertNotEqual(w.invoke("jetstream3", "--ab", "a,b"), 0)
        self.assertIn("another live task holds it", w.err)
        self.assertEqual(w.watched, [])

    def test_a_warmup_that_finds_different_renderers_refuses_before_any_round(self):
        w = self.ab_world()
        w.evidence = lambda ex: dict(EVIDENCE, gl={"driver": "/usr/lib/dri/%s.so" % ("swrast" if "/slots/b/" in ex["WK_BOARD_EXPECT"] else "v3d")})
        self.assertNotEqual(w.invoke("jetstream3", "--ab", "a,b"), 0)
        self.assertIn("the warmup round says these two arms are not what the A/B claims", w.err)
        self.assertEqual(len(w.runs()), 2)

    def test_a_lost_leg_drops_its_round_and_three_stop_the_ab(self):
        w = self.ab_world()
        w.fails = lambda ex: "/slots/b/" in ex["WK_BOARD_EXPECT"] and ex["WK_BOARD_WARMUP"] == ""
        self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--rounds", "5"), 1, w.err)
        self.assertIn("stopping early", w.err)
        self.assertIn("no round produced both halves", w.err)
        self.assertEqual(len(w.runs()), 2 + 3 * 2)

    def test_an_unknown_task_is_refused_before_the_board_is_touched(self):
        w = self.ab_world()
        self.assertNotEqual(w.invoke("jetstream3", "--ab", "a,b", "--task", "nosuch"), 0)
        self.assertIn("no such task 'nosuch'", w.err)
        self.assertEqual(w.board.bench.effects, [])

    def test_a_missing_slot_is_refused_before_the_task_is_made(self):
        w = self.world()
        self.assertNotEqual(w.invoke("jetstream3", "--ab", "a,b"), 0)
        self.assertIn("has no slot 'b'", w.err)
        self.assertEqual(w.tasks(), [])

    def test_the_shape_of_an_ab_is_refused_before_anything_runs(self):
        for argv, why in ((("--ab", "a,a"), "two different arms"), (("--ab", "a"), "two names separated by a comma"),
                          (("--ab", "a,b", "--ab-systems", "x,y"), "pick one"), (("--ab", "a,b", "--rounds", "0"), "at least 1"),
                          (("--ab", "a,b", "--slot", "a"), "names its two slots in --ab"), (("--rounds", "2",), "belongs to an A/B"),
                          (("--no-warmup-profile",), "belongs to an A/B"), (("--ab", "a,b", "--cores", "x"), "not a valid Linux cpu list"),
                          (("--ab", "a,b", "--collect"), "neither arm of a comparison")):
            with self.subTest(argv=argv):
                w = self.ab_world()
                self.assertNotEqual(w.invoke("jetstream3", *argv), 0)
                self.assertIn(why, w.err)
                self.assertEqual((w.board.bench.effects, w.tasks()), ([], []))

    def test_a_32_bit_system_drops_what_it_cannot_run_from_both_arms(self):
        w = self.ab_world()
        w.board.roots[part("/dev/sda", 2)]["id"] = "webkit-2.52-yocto-rpi3-32-ebb646f3bf67"
        w.files[os.path.join(w.tree, "Tools", "Scripts", "webkitpy/benchmark_runner/data/plans/jetstream3.plan")] = json.dumps(
            dict(json.loads(PLAN_JSON), subtests={"": [n for n, _ in board_ab.exclusions(str(REPO), "jetstream3", 32)] + ["b", "c"]}))
        self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--rounds", "1"), 0, w.err)
        self.assertEqual({e["subtests_excluded"].split(",")[0] for e in w.runs()}, {"argon2-wasm"})
        self.assertTrue(all("--subtests b c" in argv[-1] for argv, _ in w.watched))

    def test_a_dry_run_runs_the_warmup_through_the_recorder_and_records_nothing(self):
        w = self.ab_world()
        os.environ["WK_DRY_RUN"] = "1"
        try:
            self.assertEqual(w.invoke("jetstream3", "--ab", "a,b", "--no-warmup-profile"), 0, w.err)
        finally:
            del os.environ["WK_DRY_RUN"]
        self.assertEqual((w.watched, w.tasks(), w.recs().list()), ([], [], []))
        self.assertIn("would run: bash -c", w.err)


class TestTwoSystemsOnFake(ABTest):
    """`unit boot.two_systems_on_fake`: `--ab-systems` on a board holding two systems -- each leg's system armed and booted,
    read back from its own marker, measured, and the board disarmed and handed back to its rescue at the end."""

    def systems_world(self):
        w = self.world()
        w.driver_cls = Rpi5Usb
        w.board.write_system(part("/dev/sda", 3), "sys-b")
        return w

    def test_every_leg_runs_on_its_arms_system_after_a_settle_run_on_that_boot(self):
        w = self.systems_world()
        self.assertEqual(w.invoke("jetstream3", "--ab-systems", "sys-a,sys-b", "--rounds", "2"), 0, w.err)
        want = {"a": "sys-a", "b": "sys-b"}
        self.assertTrue(all(e["system"] == want[e["ab"]["arm"]] for e in w.runs()), [(e["ab"], e["system"]) for e in w.runs()])
        self.assertEqual(self.order(w), [("0", "a", "a", "evidence"), ("0", "b", "a", "evidence"),
                                         ("1", "a", "a", "settle"), ("1", "a", "a", ""), ("1", "b", "a", "settle"), ("1", "b", "a", ""),
                                         ("2", "b", "a", "settle"), ("2", "b", "a", ""), ("2", "a", "a", "settle"), ("2", "a", "a", "")])

    def test_a_system_is_prepared_once_per_boot(self):
        """Five boots are benched on (the warmup's two, round 1's two, round 2's switch back to A); round 2 leads with the
        system round 1 ended on, so it boots nothing and prepares nothing."""
        w = self.systems_world()
        w.invoke("jetstream3", "--ab-systems", "sys-a,sys-b", "--rounds", "2")
        self.assertEqual((w.ran("pin-clock.sh"), w.ran("weston.sh")), (5, 5))

    def test_the_board_ends_disarmed_on_its_rescue(self):
        w = self.systems_world()
        self.assertEqual(w.invoke("jetstream3", "--ab-systems", "sys-a,sys-b", "--rounds", "1"), 0, w.err)
        self.assertTrue(w.board.on_rescue())
        self.assertIsNone(w.board.record)
        self.assertIsNone(w.board.one_shot)

    def test_the_task_is_the_two_systems_on_one_slot(self):
        w = self.systems_world()
        w.invoke("jetstream3", "--ab-systems", "sys-a,sys-b", "--rounds", "1")
        doc = json.loads((w.bench_dir() / w.tasks()[-1] / "task.json").read_text())
        self.assertEqual((doc["subject"], doc["devices"], doc["slots"]),
                         ({"kind": "systems", "spec": "sys-a,sys-b"}, [{"device": BOARD, "profile": ""}], ["a"]))
        self.assertEqual(doc["commands"], ["wk bench run ws jetstream3 --system testboard --ab-systems sys-a,sys-b --slot a --rounds 1"])


def live_board(name):
    """A real board's bench system, read through the same channel and on-board files a run uses; nothing on it changes."""
    conf = bootcli.load_conf(str(REPO), name, os.environ)
    d = bootcli.driver_for(str(REPO), conf)
    from wk import targets
    system = board.BoardSystem(str(REPO), targets.Registry(REPO, machine=Local()), None, "", FakeClock(), name, d)
    return d, system


class TestARealBoardAnswersWhatALegRecords(unittest.TestCase):
    """`live bench.evidence[<b>]`, the read-only half: the bench system answers the probe, its facts, its display
    and its slots' manifests through the channel a run takes. A leg itself pins the clock, kills browsers and
    starts a compositor, so `live bench.leg_completes[<b>]` is a person's run (docs/PLAN.md 5.23, owed)."""

    def evidence(self, name):
        d, system = live_board(name)
        mode = d.probe()
        if not mode.startswith("bench"):
            self.skipTest("%s is in %s, not bench mode" % (name, mode))
        facts = board.kv_all(system.sh(system.ob("facts.sh")).out)
        self.assertTrue(facts.get("kernel") and facts.get("arch"), facts)
        for slot in system.bench().listdir(board.SLOTS_DIR):
            doc = system.manifest(slot)
            self.assertIn(doc["lib_file"], doc["files"])

    @requires_machine("root@rpi3-bench")
    def test_rpi3(self):
        self.evidence("rpi3")

    @requires_machine("root@rpi4-bench")
    def test_rpi4(self):
        self.evidence("rpi4")

    @requires_machine("root@rpi5-bench")
    def test_rpi5(self):
        self.evidence("rpi5")


if __name__ == "__main__":
    unittest.main()
