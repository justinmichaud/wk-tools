"""lib/wk/gc.py: `wk gc` over a fake machine. `unit gc.reclaims_or_names[<kind>]`: for every kind of rubble a plain run
either takes it or names the flag or command that does, and that flag takes it. `unit killpoints[gc]`: a run killed after
any effect and re-run converges. The dry run records the wet run's effects; one question covers the whole run, the VM's
half included; a refused flag changes nothing; a failing removal ends none of the rest.

Run: python3 tests/run.py --unit -k test_owed_gc
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, gc, record, rubble, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Local, Machine, Result  # noqa: E402
from wk.store import Bases, Store  # noqa: E402

WK = str(REPO / "wk")
MUTATIONS = ("act", "write", "mkdir", "remove")


class Host(Fake):
    """A fake machine whose act_run is an effect whether or not the run is dry, and whose `ls -1A`/`ls -1t` answer from its dirs."""

    def __init__(self, name):
        super().__init__(name)
        self.answer(["du", "-sk"], out="4\tx\n")
        self.react(["ls", "-1A"], lambda a, f: Result(0, "".join(n + "\n" for n in f.listdir(a[-1]))) if a[-1] in f.dirs
                   else Result(2, "", "no such directory"))
        self.order = {}
        self.react(["ls", "-1t"], lambda a, f: Result(0, "".join(n + "\n" for n in f.order.get(a[-1], []) if f.exists(os.path.join(a[-1], n)))))
        self.react(["ls", "-1At"], lambda a, f: Result(0, "".join(n + "\n" for n in f.order.get(a[-1], []) if f.exists(os.path.join(a[-1], n)))))

    def mkdirs(self, path):
        while path not in ("", "/"):
            self.dirs.add(path)
            path = os.path.dirname(path)

    def act_run(self, argv, **kw):
        self.effect(("act", tuple(argv)))
        return Machine.act_run(self, argv, **kw)

    def acts(self):
        return [e[1] for e in self.effects if e[0] == "act"]


class World(Host):
    """One store (records on disk, everything else in memory), the container target on this fake, and every far machine a fake."""

    def __init__(self, tmp):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.store_dir = str(self.tmp / "store")
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": self.store_dir, "WK_IN_VM": "1", "WK_ROOT": str(REPO),
                    "WK_LOCK_DIR": str(self.tmp / "locks"), "XDG_STATE_HOME": str(self.tmp / "state"), "WK_PMOS_ROOT": "/p"}
        self.clock = FakeClock()
        self.store = Store(self.env)
        self.dirs.update({self.env["WK_LOCK_DIR"], os.path.join(self.store_dir, "ws")})
        self.containers, self.images, self.volumes = set(), [], []
        self.react(["podman", "ps"], lambda a, f: Result(0, "".join("wk-%s\tUp\n" % n for n in sorted(f.containers))))
        self.react(["podman", "inspect"], lambda a, f: Result(0, "running\n") if a[2][3:] in f.containers else Result(125))
        self.react(["podman", "images"], lambda a, f: Result(0, json.dumps(f.images)))
        self.react(["podman", "image", "prune"], lambda a, f: self._keep_images(lambda i: not i.get("Dangling")))
        self.react(["podman", "rmi"], lambda a, f: self._keep_images(lambda i: i["Id"] != a[-1]))
        self.react(["podman", "volume", "ls"], lambda a, f: Result(0, "".join(v + "\n" for v in f.volumes)))
        self.react(["podman", "volume", "prune"], lambda a, f: self._no_volumes())
        self.react(["env"], self._env)
        self.container = targets.Container("container", str(REPO), self.env, self)
        self.vm = None
        self.remotes, self.boards, self.offline, self.pmos_hosts = [], [], {}, []
        self.board, self.pmos = Host("rpi5-bench"), Host("pmhost")
        self.install = types.SimpleNamespace(staging_root=lambda: None, here=self)
        self.pmos.outs, self.pmos.rcs = [], {}
        self.pmos.react(["sh", "-c"], self._pmos_sh)
        self.board.react(["ls", "-1A"], lambda a, f: Result(0, "".join(n + "\n" for n in f.listdir(a[-1]))) if a[-1] in f.dirs
                         else Result(0))
        self.effects = []

    @property
    def fake(self):
        return self

    def _keep_images(self, keep):
        self.images = [i for i in self.images if keep(i)]
        return Result(0)

    def _no_volumes(self):
        self.volumes = []
        return Result(0)

    def _env(self, argv, f):
        """`env WK_TARGET=<t> wk rm <n> --yes`: the workspace goes; `env CCACHE_DIR=... ccache ...`: nothing moves."""
        if argv[2:4] == [WK, "rm"]:
            n = argv[4]
            f.containers.discard(n)
            f._drop(os.path.join(self.store_dir, "ws", n))
        return Result(0)

    def _pmos_sh(self, argv, f):
        t = argv[2]
        if t.startswith("du -sk /p/work /p/out"):
            return Result(0, "500\t/p/work\n300\t/p/out\n")
        if t.startswith("du -sk /p/work"):
            return Result(0, "500\n")
        if t.startswith("ls -1t /p/out"):
            return Result(0, "".join(n + "\n" for n in f.outs))
        if t.startswith("cat /p/out/"):
            return Result(0, f.rcs.get(t.split("/")[3], ""))
        if t.startswith("rm -rf /p/out/"):
            f.outs.remove(t.split("/")[-1])
        return Result(0, "1M\n" if t.startswith("du -sh") else "")

    def remove(self, path):
        super().remove(path)
        if path.startswith(str(self.tmp)) and not act.dry_run():
            record._rmtree(path)

    def gc(self):
        return FakeGc(self)

    def publish(self, bid, complete=True):
        d = os.path.join(self.store.base_dir(), bid)
        self.dirs.update({self.store.base_dir(), d, os.path.join(d, "WebKit")})
        if complete:
            self.files[os.path.join(d, "sha")] = "a" * 40 + "\n"

    def workspace(self, name, container=True, ready=False, base="b1"):
        d = os.path.join(self.store_dir, "ws", name)
        self.dirs.update({d, os.path.join(d, "home")})
        if base:
            self.files[os.path.join(d, "base-id")] = base + "\n"
        if ready:
            self.files[os.path.join(d, "home", targets.READY_MARKER)] = ""
        if container:
            self.containers.add(name)

    def creation_record(self, name, pid=999999):
        d = Path(self.store_dir) / "task" / ("new-%s-20260101T000000Z" % name)
        d.mkdir(parents=True)
        for k, v in (("plan", "create"), ("kind", "new"), ("name", name), ("pid", str(pid)), ("where", "here")):
            (d / k).write_text(v + "\n")
        return d

    def state(self):
        locks, tmp = self.env["WK_LOCK_DIR"], str(self.tmp)
        return (sorted(p.replace(tmp, "") for p in self.files if not p.startswith(locks)),
                sorted(d.replace(tmp, "") for d in self.dirs if not d.startswith(locks)), sorted(self.containers), json.dumps(self.images))


class FakeGc(gc.Gc):
    def __init__(self, w):
        super().__init__(REPO, types.SimpleNamespace(machine=w, env=w.env, fleet=None), w.clock)
        self.w = w
        self.mac_host, self.host_half, self.store_half = True, True, True

    def container(self):
        return self.w.container

    def vm(self):
        return self.w.vm

    def remotes(self):
        return self.w.remotes

    def boards(self):
        return self.w.boards

    def board_machine(self, dest):
        return self.w.board

    def reach(self):
        return types.SimpleNamespace(offline=lambda n: self.w.offline.get(n, ""))

    def pmos_hosts(self):
        return self.w.pmos_hosts

    def pmos_machine(self, host):
        return self.w.pmos

    def install(self):
        return self.w.install


class FakeVm(targets.Vm):
    """The vm target over the fake: tart answers from `guests`, and its store is a directory of the world's."""

    def __init__(self, w, guests):
        env = dict(w.env, TART_HOME="/tart", WK_VM_STORE=str(w.tmp / "vmstore"))
        super().__init__("vm", str(REPO), env, w)
        self.guests = guests
        w.answer(["/bin/tart", "prune"])
        w.react(["/bin/tart", "list"], lambda a, f: Result(0, json.dumps([{"Name": g, "State": "stopped", "Source": "local"} for g in self.guests])))

    def tart(self):
        return "/bin/tart"

    @property
    def store(self):
        return Store(dict(self.env, WK_STORE=self.env["WK_VM_STORE"]))


class GcTest(WkTest):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"WK_YES": "1", "WK_DESTRUCTIVE": "1"})
        env.start()
        self.addCleanup(env.stop)
        for k in ("WK_DRY_RUN", "WK_CONFIRMED"):
            os.environ.pop(k, None)
        self.w = World(self.tmp)

    def run_gc(self, *flags, w=None):
        w = w or self.w
        os.environ.pop("WK_CONFIRMED", None)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = w.gc().run(flags)
        return rc, err.getvalue()

    def rows(self, w=None):
        with contextlib.redirect_stderr(io.StringIO()):
            return (w or self.w).gc().rows()


def _snapshot(w):
    w.publish("b1")
    w.publish("b2")
    return lambda: os.path.join(w.store.base_dir(), "b1") not in w.dirs


def _mirror(w):
    w.publish("b1")
    w.mkdirs(w.store.mirror())
    return lambda: w.store.mirror() not in w.dirs


def _seed(w):
    d = os.path.join(w.store.artifact_dir(), "bench", ".tmp-speedometer3-aaaaaaaaaaaa")
    w.mkdirs(d)
    return lambda: d not in w.dirs


def _bridge_image(w):
    d = os.path.join(w.store.artifact_dir(), "bridge")
    w.mkdirs(d)
    path = os.path.join(d, "moose-bmc.img")
    w.files[path] = "x" * 64
    return lambda: path not in w.files


def _payload(w):
    seeds = os.path.join(w.store.artifact_dir(), "bench")
    for n in ("speedometer3-aaaaaaaaaaaa", "speedometer3-bbbbbbbbbbbb"):
        w.mkdirs(os.path.join(seeds, n))
    w.order[seeds] = ["speedometer3-bbbbbbbbbbbb", "speedometer3-aaaaaaaaaaaa"]
    return lambda: os.path.join(seeds, "speedometer3-aaaaaaaaaaaa") not in w.dirs


def _runner(w):
    d = os.path.join(w.store.artifact_dir(), "bench-runner")
    for n in ("new", "old"):
        w.mkdirs(os.path.join(d, n))
    w.order[d] = ["new", "old"]
    return lambda: os.path.join(d, "old") not in w.dirs and os.path.join(d, "new") in w.dirs


def _build_output(w):
    d = os.path.join(w.store_dir, "cache", "yocto")
    w.mkdirs(d)
    return lambda: d not in w.dirs


def _dangling_images(w):
    w.images = [{"Id": "d1", "Dangling": True, "Size": 4096, "Containers": 0}]
    return lambda: not w.images


def _container_image(w):
    w.images = [{"Id": "t1", "Names": ["ghcr.io/igalia/wkdev:old"], "Size": 4096, "Containers": 0}]
    return lambda: not w.images


def _dangling_volumes(w):
    w.volumes = ["v1"]
    return lambda: not w.volumes


def _ccache(w):
    w.mkdirs(os.path.join(w.store_dir, "cache", "ccache"))
    w.answer(["ccache", "--version"], out="ccache 4\n")
    return lambda: any(a[-1] == "--max-size=40G" for a in w.acts()) and any(a[-1] == "--cleanup" for a in w.acts())


def _tart_cache(w):
    w.vm = FakeVm(w, [])
    w.mkdirs("/tart/cache")
    return lambda: ("/bin/tart", "prune", "--space-budget", "20") in w.acts()


def _guest_base(w):
    w.vm = FakeVm(w, ["wk-base"])
    return lambda: False


def _pmos_builds(w):
    w.pmos_hosts = ["pmhost"]
    w.pmos.outs, w.pmos.rcs = ["pm-3", "pm-2", "pm-1"], {"pm-2": "0", "pm-1": "0"}
    return lambda: w.pmos.outs == ["pm-2"]


def _pmos_work(w):
    w.pmos_hosts = ["pmhost"]
    return lambda: any("rm -rf /p/work" in a[-1] for a in w.pmos.acts())


def _staged(w, which):
    w.install = types.SimpleNamespace(staging_root=lambda: "/vol", here=w)
    for n, done in (("20260101", True), ("20260102", True), ("20260103", False)):
        w.mkdirs("/vol/staged/" + n)
        if done:
            w.files["/vol/staged/%s/stage.json" % n] = "{}"
    return lambda: "/vol/staged/" + which not in w.dirs and "/vol/staged/20260102" in w.dirs


def _board(w, slot):
    w.boards = [("rpi5", {"NODE_BENCH_SSH": "rpi5-bench"})]
    for n in ("a", "a.part", "a-instr"):
        w.board.mkdirs("/var/wk/slots/" + n)
    return lambda: "/var/wk/slots/" + slot not in w.board.dirs and "/var/wk/slots/a" in w.board.dirs


def _remote_mirror(w):
    far = Host("box")
    w.remotes = [types.SimpleNamespace(name="box", answers=lambda: (True, ""), root_there=lambda: "/r", machine=far)]
    return lambda: False


def _half_made(w):
    w.workspace("hm", base="")
    return lambda: "hm" not in w.containers


def _selftest(w):
    w.workspace("wk-test-abc123", ready=True)
    return lambda: "wk-test-abc123" not in w.containers


def _creation_record(w):
    d = w.creation_record("gone")
    return lambda: not d.exists()


# kind -> (make the rubble, returning "is it gone"; what takes it: "" a plain run, a --purge flag, or a command elsewhere)
KINDS = {
    "snapshot": (_snapshot, ""),
    "mirror": (_mirror, "--purge-mirror"),
    "seed": (_seed, ""),
    "bridge-image": (_bridge_image, ""),
    "payload": (_payload, ""),
    "runner": (_runner, ""),
    "build-output": (_build_output, "--purge-builds"),
    "dangling-images": (_dangling_images, ""),
    "container-image": (_container_image, "--purge-images"),
    "dangling-volumes": (_dangling_volumes, ""),
    "ccache": (_ccache, ""),
    "tart-cache": (_tart_cache, ""),
    "guest-base": (_guest_base, "wk sysimage build macos-guest-base --rm"),
    "pmos-builds": (_pmos_builds, ""),
    "pmos-work": (_pmos_work, "--purge-pmos"),
    "staged-unfinished": (lambda w: _staged(w, "20260103"), ""),
    "staged-finished": (lambda w: _staged(w, "20260101"), "--purge-builds"),
    "board-slot-copy": (lambda w: _board(w, "a.part"), ""),
    "board-slot-instrumented": (lambda w: _board(w, "a-instr"), "--purge-rubble"),
    "remote-mirror": (_remote_mirror, "wk machine rm box"),
    "half-made": (_half_made, "--purge-rubble"),
    "selftest-ws": (_selftest, ""),
    "creation-record": (_creation_record, ""),
}


class TestReclaimsOrNames(GcTest):
    def test_every_kind_is_taken_by_a_plain_run_or_named_with_what_takes_it(self):
        """`gc.reclaims_or_names[<kind>]`."""
        for kind, (make, taker) in KINDS.items():
            with self.subTest(kind=kind):
                w = World(self.tmp)
                gone = make(w)
                rc, err = self.run_gc(w=w)
                self.assertEqual(rc, 0, err)
                if not taker:
                    self.assertTrue(gone(), err)
                    continue
                self.assertFalse(gone(), err)
                self.assertIn("'%s' takes it" % ("wk gc " + taker if taker.startswith("--") else taker), err)
                if taker.startswith("--"):
                    rc, err = self.run_gc(taker, w=w)
                    self.assertEqual(rc, 0, err)
                    self.assertTrue(gone(), err)

    def test_wk_disk_renders_the_same_rows(self):
        _half_made(self.w)
        _seed(self.w)
        self.w.store_dir and self.w._drop(os.path.join(self.w.store_dir, "ws", "hm", "base-id"))
        lines = [rubble.line(r, (), "a plain 'wk gc' takes it") for r in self.rows()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(any("'wk gc --purge-rubble' takes it" in l for l in lines), lines)
        self.assertTrue(any("a plain 'wk gc' takes it" in l for l in lines), lines)


class TestWhatAPlainRunKeeps(GcTest):
    def test_a_workspace_a_live_selftest_made_is_kept(self):
        _selftest(self.w)
        Lock(self.w.store, self.w, self.w.clock).hold("selftest")
        self.w.pids.add(os.getpid())
        rc, err = self.run_gc()
        self.assertIn("wk-test-abc123", self.w.containers, err)

    def test_a_seed_whose_lock_is_held_is_being_made_and_kept(self):
        gone = _seed(self.w)
        Lock(self.w.store, self.w, self.w.clock).hold("bench-seed-speedometer3-aaaaaaaaaaaa")
        self.w.pids.add(os.getpid())
        self.run_gc()
        self.assertFalse(gone())

    def test_a_workspace_still_being_created_is_not_rubble(self):
        _half_made(self.w)
        self.w.creation_record("hm", pid=4242)
        self.w.pids.add(4242)
        rc, err = self.run_gc("--purge-rubble")
        self.assertIn("hm", self.w.containers, err)

    def test_a_finished_workspace_and_its_pinned_snapshot_are_not_rubble(self):
        self.w.publish("b1")
        self.w.publish("b2")
        self.w.workspace("done", ready=True, base="b1")
        self.assertEqual([(r.kind, bool(r.why)) for r in self.rows()], [("mirror", True)])

    def test_an_unpinned_workspace_keeps_every_snapshot_and_says_why(self):
        _snapshot(self.w)
        self.w.workspace("nopin", ready=True, base="")
        rc, err = self.run_gc()
        self.assertIn(os.path.join(self.w.store.base_dir(), "b1"), self.w.dirs)
        self.assertIn("nopin never recorded a base", err)


class TestWhatWasNotLookedAt(GcTest):
    def test_a_failed_image_listing_is_named_not_reported_as_nothing(self):
        self.w.answer(["podman", "images"], rc=125, err="cannot connect to podman")
        rows = self.rows()
        self.assertEqual([(r.kb, bool(r.why)) for r in rows if r.kind == "container-image"], [(None, True)])


class TestOneQuestion(GcTest):
    def test_nothing_to_take_asks_nothing(self):
        os.environ.pop("WK_YES")
        self.assertEqual(self.run_gc()[0], 0)
        self.assertEqual(self.w.acts(), [])

    def test_declining_changes_nothing(self):
        os.environ.pop("WK_YES")
        _snapshot(self.w)
        before = self.w.state()
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
            self.w.gc().run(())
        self.assertEqual(self.w.state(), before)

    def test_a_flag_refused_by_a_row_refuses_the_run_before_anything_goes(self):
        _mirror(self.w)
        _seed(self.w)
        self.w.workspace("live", ready=True, base="b1")
        before = self.w.state()
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()) as err:
            self.w.gc().run(("--purge-mirror",))
        self.assertEqual(self.w.state(), before)
        self.assertIn("'wk rm' live first", err.getvalue())

    def test_a_failing_removal_ends_none_of_the_rest(self):
        _tart_cache(self.w)
        self.w.answer(["/bin/tart", "prune"], rc=1)
        gone = _seed(self.w)
        rc, err = self.run_gc()
        self.assertEqual(rc, 1)
        self.assertTrue(gone(), err)


class TestTheVmHalf(GcTest):
    """A macOS host: the VM's rows join the plan before the one question, and its wk acts with the answer."""

    def mac(self, rows, running=True):
        g = self.w.gc()
        g.store_half = False
        calls = []

        def wk(*args, env=None, quiet=False):
            calls.append((args, os.environ.get("WK_YES")))
            return (0, "".join(rubble.to_line(r) + "\n" for r in rows)) if "--rows" in args else (0, "")
        self.w.container = types.SimpleNamespace(machine_state=lambda: "running" if running else "stopped", wk=wk, list=lambda: [])
        return g, calls

    def test_the_vms_rows_are_asked_once_and_its_wk_takes_them_with_the_answer(self):
        g, calls = self.mac([rubble.row("snapshot", "snapshot b1", 4, take=lambda: None)])
        os.environ.pop("WK_YES")
        with mock.patch.object(act, "confirm", return_value=True) as ask, contextlib.redirect_stderr(io.StringIO()) as err:
            rc = g.run(())
        self.assertEqual((rc, ask.call_count), (0, 1), err.getvalue())
        self.assertIn("podman VM: snapshot b1", err.getvalue())
        self.assertEqual(calls, [(("gc", "--rows"), None), (("gc", "--yes"), None)])
        self.assertIsNone(os.environ.get("WK_YES"), "the answer reaches the VM's wk in its argv, not this process's environment")

    def test_the_hosts_mirror_is_kept_while_the_vm_has_a_workspace_on_it(self):
        self.w.mkdirs(self.w.store.mirror())
        g, calls = self.mac([rubble.row("mirror", "every base snapshot", 4, "--purge-mirror", lambda: None, "kept -- 'wk rm' a first")])
        with self.assertRaises(Refused), contextlib.redirect_stderr(io.StringIO()):
            g.run(("--purge-mirror",))
        self.assertIn(self.w.store.mirror(), self.w.dirs)
        self.assertEqual([c[0] for c in calls], [("gc", "--rows")])

    def test_a_stopped_vm_is_named_and_keeps_the_mirror(self):
        self.w.mkdirs(self.w.store.mirror())
        g, calls = self.mac([], running=False)
        with contextlib.redirect_stderr(io.StringIO()):
            rows = g.rows()
        self.assertEqual({r.kind: bool(r.why) for r in rows}, {"mirror": True, "store": True})

    def test_a_row_survives_the_trip_as_text(self):
        r = rubble.row("half-made", "workspace 'a'", None, "--purge-rubble", lambda: None)
        self.assertEqual(rubble.from_line(rubble.to_line(r), "vm"), r._replace(what="vm: workspace 'a'", take=rubble.FAR))


class TestCrashOnly(GcTest):
    def world(self):
        w = World(self.tmp)
        for make in (_snapshot, _seed, _payload, _runner, lambda w: _board(w, "a.part"), _selftest):
            make(w)
        return w

    def test_a_run_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[gc]`."""
        def run_once(w):
            os.environ.pop("WK_CONFIRMED", None)
            with contextlib.redirect_stderr(io.StringIO()):
                w.gc().run(())
        converges(self, self.world, run_once, World.state)

    def test_the_dry_run_records_the_wet_runs_effects_and_changes_nothing(self):
        """`dispatch.dry_run_is_the_recorder[gc]`."""
        def mutations(w):
            locks = w.env["WK_LOCK_DIR"]
            return [tuple(str(x).replace(str(w.tmp), "") for x in e) for e in w.effects + w.board.effects
                    if e[0] in MUTATIONS and not str(e[1]).startswith(locks)]
        wet, dry = self.world(), self.world()
        self.run_gc("--purge-builds", w=wet)
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        try:
            self.run_gc("--purge-builds", w=dry)
        finally:
            del os.environ["WK_DRY_RUN"]
        self.assertEqual(mutations(wet), mutations(dry))
        self.assertGreaterEqual(len(mutations(wet)), 5)
        self.assertEqual(dry.state(), before)


class TestWhatGcKeepsWhenNothingVerifies(WkTest):
    """`wk gc` removes every snapshot no workspace is overlaid on except the newest finished one (lib/wk/store.py's Bases).
    That one is the newest complete snapshot, not the current one: `current` also requires the branch record `verify`
    reads, so a machine whose snapshots predate that record would otherwise be left with none to make a workspace from."""

    def _bases(self, ids, complete=(), branch=()):
        store = self.tmp / "store"
        for i in ids:
            d = store / "base" / i
            (d / "WebKit").mkdir(parents=True)
            if i in complete:
                (d / "sha").write_text("deadbeef\n")
            if i in branch:
                (d / "branch").write_text("origin/main\n")
        (store / "ws").mkdir(parents=True, exist_ok=True)
        return store

    def _unreferenced(self, store):
        return Bases(Store({"WK_STORE": str(store)}), Local()).unreferenced()

    def test_the_newest_finished_snapshot_is_kept_though_none_verifies(self):
        store = self._bases(["20260101", "20260202"], complete=["20260101", "20260202"])
        self.assertEqual(["20260101"], self._unreferenced(store))

    def test_an_unfinished_newest_one_protects_nothing_and_goes(self):
        store = self._bases(["20260101", "20260202"], complete=["20260101"])
        self.assertEqual(["20260202"], self._unreferenced(store))

    def test_a_workspace_pins_its_snapshot_and_an_unknown_pin_keeps_them_all(self):
        store = self._bases(["20260101", "20260202", "20260303"], complete=["20260101", "20260202", "20260303"])
        (store / "ws" / "a").mkdir()
        (store / "ws" / "a" / "base-id").write_text("20260101\n")
        self.assertEqual(["20260202"], self._unreferenced(store))
        (store / "ws" / "b").mkdir()
        self.assertEqual([], self._unreferenced(store), "a workspace with no pin could be on any of them")


if __name__ == "__main__":
    unittest.main()
