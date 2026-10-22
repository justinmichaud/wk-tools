"""`unit killpoints[<cmd>]`: every mutating command, killed after any effect and re-run, converges on its final
state, and its dry run is the wet run's plan and touches nothing. One row per command."""
import contextlib
import io
import json
import os
import re
import types
import unittest
import zipfile
from unittest import mock

from tests import fakes
from tests.killpoints import Row, TestConverges  # noqa: F401 -- the helper's own tests
from tests import test_ab_plan as ab, test_bench_board as bboard, test_bench_mac as bmac, test_bench_pipeline as bench
from tests import test_bench_results as bresults, test_bench_seed as bseed, test_board_pgo as bpgo
from tests import test_boot_cmd as boot, test_daemons as daemons, test_guest as guest, test_machine_cmd as machine
from tests import test_mac_tailnet as tailnet, test_owed_gc as gc, test_pr_workflow as prw, test_quiesce as quiesce
from tests import test_session as session, test_sysimage_task as task
from tests import test_sysimage_write as write, test_vm_base as vmbase, test_wk_key as key, test_wk_secrets as secrets
from tests import test_wk_sudo as sudo
from tests import test_rm_final_state as rmfs, test_repo_workspace as repo, test_sync as sync, test_wk_build as build
from tests import test_wk_test as wktest, test_wk_workspace as ws
from tests import test_wk_places
from tests.support import REPO, as_dispatched
from wk import dispatch, guest as wkguest, places, pr  # noqa: E402 -- tests.support put lib on the path
from wk.act import Refused  # noqa: E402
from wk.bench import record as brecord  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sudo import Sudo  # noqa: E402


def quiet(fn, *args, **kw):
    with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kw)


class GuestHost(ws.WorkspaceTest):
    """This host is a Mac driving a guest whose base is already built."""

    def setUp(self):
        super().setUp()
        from wk.sysimage import guestbase
        for p in (mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True),
                  mock.patch.object(guestbase.Base, "ensure", return_value=None),
                  mock.patch.object(guestbase.Base, "stale", return_value="")):
            p.start()
            self.addCleanup(p.stop)


def new(case, w, **kw):
    w.lock = Lock(w.driver.store, w, w.clock)   # each run is its own process: nothing the killed one held survives it
    quiet(case.detached, w, **kw)


def rm_mutations(w):
    """A build box's own wk is handed the dry run, and an ssh ControlPath directory is the connection's."""
    return [str(e).replace("WK_DRY_RUN=1 ", "") for e in w.mutations() if not (e[0] == "mkdir" and e[1].endswith("/wk/ssh"))]


def rm_row(kind):
    return Row(rmfs.RmFinalStateTest, lambda c: c.world(kind), lambda c, w: c.rm(w), lambda w: w.left(), rm_mutations, effects=80)


def sync_row(scope, **kw):
    def make(case):
        w = case.make_world()
        w.dirs.add(w.mirror)
        w.publish("20190101T000000Z", sha="b" * 40)
        w.workspaces["container"] = ["ws"]
        return w
    return Row(sync.SyncTest, make, lambda c, w: quiet(w.sync(scope, place="container", **kw).run), sync.World.state)


def wk_test(case, w):
    os.environ["WK_NAME"] = "ws"
    quiet(wktest.CMD.main, as_dispatched("test", [], os.environ), None, w.reg, w.clock)


def bench_row(kind, preset):
    def run(case, w):
        w.clock.t += 1
        quiet(bench.invoke, w, ("run", "speedometer3", "--preset", preset))
    return Row(bench.BenchTest, lambda c: bench.World(c.tmp, kind), run, bench.World.state)


def board_run(case, w):
    w.clock.t += 1
    w.invoke()


def board_mutations(w):
    return [("board",) + e for e in w.board.bench.mutations()] + [("here",) + e for e in w.mutations()]


def deploy_mutations(w):
    """The landed slot's checksum is verified on the board, a read a dry run has nothing to run against."""
    return [str(e).replace(str(w.tmp), "") for e in w.fake.mutations() if "sha256sum" not in str(e)]


def mac_deploy_row(local):
    def run(case, w):
        w.clock.t += 1
        w.stage_("speedometer3")
    return Row(bmac.MacTest, lambda c: bmac.StageWorld(c.tmp, local), run, bmac.StageWorld.stages)


def export_world(case):
    os.environ["WK_YES"] = "1"
    d = case.tmp / "store" / "ws" / "w" / "bench"
    if not d.exists():
        bresults.complete_task(d)
    w = bresults.mirror_task(fakes.World(), d / bresults.TASK, str(d / bresults.TASK))
    w.export_to = case.tmp / "home" / "Downloads" / (bresults.TASK + ".zip")
    return w


def export_state(w):
    data = w.files.get(str(w.export_to))
    return sorted(zipfile.ZipFile(io.BytesIO(data)).namelist()) if data else None


def export_run(case, w):
    case.export(case.bench(w))


def seed_state(w):
    return ({p: t for p, t in w.files.items() if p.startswith(bseed.SEEDS + "/")},
            sorted(d for d in w.dirs if d.startswith(bseed.SEEDS + "/")))


def ab_world(case):
    case.n += 1
    return ab.World(os.path.join(case.tmp, "kill", str(case.n)))


def ab_run(case, w):
    tasks = brecord.tasks(w.home())
    rc, err = case.quiet(w.ab(task=tasks[0] if tasks else None).go)
    case.assertEqual(rc, 0, err)


def pgo_run(case, w):
    rc, err = case.quiet(w.cycle().webkit, ["--commit", bpgo.SHA, "--slot", "pr"])
    case.assertEqual(rc, 0, err)


def boot_row(kind, action, start=None):
    def make(case):
        fake, d = boot.board(kind)
        if start:
            boot.run(getattr(boot.boot_over(fake, d), start))
        fake.applied = 0
        for m in fake.machines.values():
            m.effects.clear()
        return types.SimpleNamespace(fake=fake, d=d)

    def run(case, w):
        got, err = boot.run(getattr(boot.boot_over(w.fake, w.d), action))
        case.assertTrue(got == 0 or "already in bench mode" in err, err)

    def state(w):
        f = w.fake
        return (f.running, f.record, json.dumps(f.fat, sort_keys=True), f.one_shot)

    def mutations(w):
        return [(n,) + e for n, m in sorted(w.fake.machines.items()) for e in m.mutations()]
    return Row(unittest.TestCase, make, run, state, mutations,
               dry="wk boot --dry-run prints a summary of its own rather than running its steps against the recorder")


def daemons_world(case):
    box = daemons.Box()
    box.write(daemons.UNITS + "wk-old.service", "x")
    box.write(daemons.UNITS + "wk-proxy.service", "old")
    box.active |= {"wk-old.service", "wk-proxy.service"}
    box.effects.clear()
    return box


def daemons_run(case, w):
    sup = case.systemd(w)
    quiet(sup.prune, ("proxy", "push", "github-inject"))
    for word in ("proxy", "push", "github-inject"):
        quiet(sup.ensure, case.rows()[word])


def guest_world(case):
    w = guest.GuestWorld(case.tmp)
    w.state = "stopped"
    w.effects.clear()
    w.vm = places.Registry(str(REPO), env=w.env, machine=w).load("vm")
    return w


def guest_state(w):
    files = {p: ("live" if int(t) in w.pids else "dead") if p.endswith(".pid") else t for p, t in w.files.items()}
    return files, sorted(w.loaded), sorted(w.serving)


def tailnet_world(case):
    m = tailnet.built(tailnet.FakeMac())
    m.dirs.add("/vol")
    return m


def tailnet_run(case, w):
    with tailnet.clean_env(**{k: v for k, v in os.environ.items() if k == "WK_DRY_RUN"}):
        tailnet.quiet(tailnet.tailnet(w).stage, "/vol", "mbp")


def tailnet_mutations(w):
    """A dry run names the auth key rather than its path."""
    return [str(e).replace("/home/u/.config/wk/tailscale-authkey", "<the tailnet auth key>") for e in w.mutations()]


def machine_mutations(w):
    """The wk-tools bundle is pushed from a temporary file of its own name."""
    return [re.sub(r"wk-tools-push\.\w+", "wk-tools-push", str(e).replace(str(w.tmp), "")) for e in w.fake.mutations()]


def gc_mutations(w):
    return [("here",) + e for e in w.mutations()] + [("board",) + e for e in w.board.mutations()]


def gc_run(case, w):
    os.environ.pop("WK_CONFIRMED", None)
    quiet(w.gc().run, ("--purge-builds",))


def pr_mutations(w):
    return w.driver.mutations


def pr_open(case, w):
    try:
        quiet(prw.open_once, w.driver)
    except SystemExit:   # a dry open prints the pull request it would open and ends there
        pass


def handover_world(case):
    w = test_wk_places.RemoteTest()
    w.setUp()
    case.addCleanup(w.doCleanups)
    w.env["WK_PLACE"] = "box"
    w.ran = []
    return w


def handover_run(case, w):
    with mock.patch.object(dispatch, "_registry", w.reg), mock.patch.object(dispatch.os, "execvp", lambda f, a: w.ran.append(a)):
        dispatch.delegate_run(dispatch.delegate_driver("box"), "build", ["a", "jsc-release", "--detach"])


def handover_state(w):
    t = w.reg.load("box")
    return t.records().list(), [e for e in t.here.effects if e[0] != "run" and e != ("mkdir", t.ssh_dir())], [a[-1] for a in w.ran]


def task_row(cls):
    return Row(cls, lambda c: c.WORLD(c.tmp), lambda c, w: quiet(w.driver().build, []), lambda w: w.state(),
               dry="wk sysimage build --dry-run prints its plan (task.show) rather than running its stages against the recorder")


def write_run(case, w):
    e = w.run()
    if "WK_DRY_RUN" not in os.environ:
        case.assertIsNone(e, w.err)


def vmbase_run(case, w):
    case.w = w
    case.build()


def vmbase_state(w):
    marker = w.files.get(os.path.join(w.env["WK_VM_STORE"], "vm", "base.ready"), "")
    return w.state, w.disk, [l for l in marker.splitlines() if not l.startswith("finished=")]


def key_row(make, verb, minted=False, effects=80):
    """`minted`: the wet run publishes the public half of a key the dry one did not write, so has none to derive."""
    def mk(case):
        os.environ["WK_YES"] = "1"
        return make(case)

    def run(case, w):
        with mock.patch("os.urandom", return_value=b"\0" * 8):
            try:
                quiet(verb, case.key(w))
            except Refused:
                pass

    def mutations(w):
        return [e for e in w.mutations() if not (minted and (".pub" in str(e) or "/view" in str(e)))]
    return Row(key.KeyTest, mk, run, key.KeyWorld.state, mutations, effects=effects)


def secrets_world(case):
    w = secrets.World(case.tmp)
    w.seed()
    return w


def sudo_run(case, w):
    w.rc = Sudo(w, {}, linux=False).setup()


ROWS = {
    "new": Row(ws.WorkspaceTest, lambda c: ws.World(c.tmp), new, ws.World.state, effects=80),
    # The guest has no mirror in reach, which its wet run finds and its plan cannot know.
    "new vm": Row(GuestHost, lambda c: ws.World(c.tmp, kinds={"fakebox": "vm"}), new, ws.World.state, effects=80, dry=False),
    "new tools": Row(ws.WorkspaceTest, lambda c: repo.ToolsWorld(c.tmp), lambda c, w: new(c, w, repo=repo.tools()),
                     repo.ToolsWorld.state, effects=80),
    "rm": rm_row("container"),
    "rm vm": rm_row("vm"),
    "rm remote": rm_row("remote"),
    "sync tools": sync_row("tools"),
    "sync place": sync_row("place", fix=True),
    "build": Row(build.BuildTest, lambda c: build.World(c.tmp), lambda c, w: quiet(c.make(w).front), build.World.state),
    "bench run": bench_row("container", "wpe-release"),
    "bench run vm": bench_row("vm", "mac-release"),
    "bench run board": Row(bboard.BoardTest, lambda c: c.world(), board_run, bboard.BoardWorld.state, board_mutations),
    "bench deploy board": Row(bboard.CleanTest, lambda c: bboard.DeployWorld(c.tmp), lambda c, w: w.deploy(), bboard.DeployWorld.state,
                              deploy_mutations),
    "bench deploy mac": mac_deploy_row(True),
    "bench deploy mac remote": mac_deploy_row(False),
    "bench export": Row(bresults.ExportTest, export_world, export_run, export_state),
    "bench seed": Row(bseed.WkTest, lambda c: bseed.fake(), lambda c, w: quiet(bseed.seeder(w).seed, "jetstream3", bseed.PLAN), seed_state),
    # A dry A/B or PGO cycle prints its step graph; each step is a wk command whose own row holds its plan.
    "bench ab": Row(ab.ABTest, ab_world, ab_run, lambda w: w.state(), effects=80, dry=False),
    "sysimage webkit pgo": Row(bpgo.PgoTest, lambda c: c.world(), pgo_run, bpgo.World.state, dry=False),
    "boot arm pi-sd": boot_row("pi-sd", "arm"),
    "boot arm pi-tryboot": boot_row("pi-tryboot", "arm"),
    "boot arm rpi5-usb": boot_row("rpi5-usb", "arm"),
    "boot back pi-sd": boot_row("pi-sd", "back", "arm"),
    "boot back pi-tryboot": boot_row("pi-tryboot", "back", "arm"),
    "boot back rpi5-usb": boot_row("rpi5-usb", "back", "arm"),
    "boot disarm pi-sd": boot_row("pi-sd", "disarm"),
    "boot disarm pi-tryboot": boot_row("pi-tryboot", "disarm"),
    "boot disarm rpi5-usb": boot_row("rpi5-usb", "disarm"),
    "daemons": Row(daemons.DaemonTest, daemons_world, daemons_run, lambda w: w.state(), effects=40),
    # A dry start boots no guest, and the rest of a start is what the booted guest answers.
    "start guest": Row(guest.GuestTest, guest_world, lambda c, w: quiet(wkguest.start, w.vm, "demo", fakes.FakeClock()),
                       guest_state, effects=80, dry=False),
    "sysimage stage tailnet": Row(unittest.TestCase, tailnet_world, tailnet_run,
                                  lambda w: {p: v for p, v in w.files.items() if p.startswith("/vol/")}, tailnet_mutations,
                                  effects=80),
    "machine setup box": Row(machine.MachineTest, lambda c: c.world(), lambda c, w: quiet(w.machines().setup, "box", "build"),
                             machine.World.state, machine_mutations),
    "machine rm box": Row(machine.MachineTest, lambda c: c.deployed(), lambda c, w: quiet(w.machines().rm, "box"),
                          machine.World.state, machine_mutations),
    "machine setup board": Row(machine.MachineTest, lambda c: c.world(conf=machine.BOARD), lambda c, w: quiet(w.machines().setup, "box"),
                               machine.World.state, machine_mutations),
    "gc": Row(gc.GcTest, lambda c: gc.rubble_world(c.tmp), gc_run, gc.World.state, gc_mutations),
    "pr checkout": Row(unittest.TestCase, lambda c: prw.checkout_world(),
                       lambda c, w: quiet(pr.checkout, w.driver, w, "ws", "alice:eng/x"), prw.checkout_state, pr_mutations),
    "pr rebase": Row(unittest.TestCase, lambda c: prw.rebase_world(), lambda c, w: quiet(prw.CMD_PR_MODULE.pr_rebase, w.driver, "ws"),
                     lambda w: dict(w.local), pr_mutations),
    "pr open": Row(unittest.TestCase, lambda c: prw.GitWorld(), pr_open, lambda w: (set(w.pushed), any(e[0] == "exec" for e in w.effects)), pr_mutations),
    "pr open from a box": Row(unittest.TestCase, lambda c: prw.box_world(), lambda c, w: prw.box_push(w), prw.box_state),
    "quiesce on": Row(quiesce.QuiesceTest, lambda c: quiesce.World(bench=True), lambda c, w: c.quiet_run(w.q().on), lambda w: w.state()),
    "quiesce off": Row(quiesce.QuiesceTest, lambda c: c.quiesced(quiesce.World, bench=True), lambda c, w: c.quiet_run(w.q().off),
                       lambda w: w.state()),
    "quiesce session on": Row(session.SessionTest, lambda c: c.world("bmc"), lambda c, w: c.go(lambda s: s.on(False), w.s()),
                              lambda w: w.state()),
    "quiesce session gdm": Row(session.SessionTest, lambda c: c.world(None), lambda c, w: c.go(lambda s: s.gdm(False), w.s()),
                               lambda w: w.state()),
    "quiesce session off": Row(session.SessionTest, lambda c: c.world("gpu"), lambda c, w: c.go(lambda s: s.off(), w.s()),
                               lambda w: w.state()),
    # The hand-over execs the box's own wk, which is handed the dry run with the rest of the command.
    "build on a box": Row(unittest.TestCase, handover_world, handover_run, handover_state, dry=False),
    "sysimage build buildroot": task_row(task.TaskTest),
    "sysimage build yocto": task_row(type("YoctoTask", (task.TaskTest,), {"WORLD": task.YoctoWorld})),
    "sysimage write": Row(write.WriteTest, lambda c: write.World(), write_run, write.World.state, effects=80,
                          dry="wk sysimage write --dry-run prints the order it would write in rather than running its steps against the recorder"),
    # A dry base build boots no guest, and the rest of the build is what the booted guest answers.
    "sysimage build vm base": Row(vmbase.BaseTest, lambda c: vmbase.BaseWorld(c.tmp), vmbase_run, vmbase_state, effects=80, dry=False),
    "key setup": key_row(lambda c: c.keyed_fleet(), lambda k: k.setup(), effects=120),
    "key deploy": key_row(lambda c: c.keyed_fleet(), lambda k: k.deploy()),
    "key ensure": key_row(lambda c: key.KeyWorld(c.tmp), lambda k: k.ensure(), minted=True),
    "key adopt": key_row(lambda c: key.KeyWorld(c.tmp), lambda k: k.adopt_verb("fork", lambda: b"KEY:taken\n"), minted=True),
    "secrets publish": Row(secrets.SecretsTest, secrets_world, lambda c, w: secrets.quiet(w.sec().publish), lambda w: w.state()),
    "key sudo setup": Row(unittest.TestCase, lambda c: sudo._setup_fake(free_before=False, property_holds=True), sudo_run,
                          lambda w: sorted(w.files.items())),
    "test": Row(wktest.TestTest, lambda c: wktest.World(c.tmp), wk_test, wktest.World.state,
                dry="wk test --dry-run stops before the wk-tools push its wet run makes, so the push is not in its plan"),
}

for _cls in (row.tests(name) for name, row in ROWS.items()):
    globals()[_cls.__name__] = _cls
del _cls
