"""`unit rm.final_state[<place>]`: `wk rm` over each real driver on the Fake machine leaves nothing of the
workspace -- no environment, no directory, no record, no alias, no creation log, no guest file -- and its record
is the last thing to go, so an rm killed after any effect and re-run converges on that.

Run: python3 tests/run.py -k tests.test_rm_final_state
"""
import contextlib
import io
import json
import os
import shlex
import shutil
import sys
from unittest import mock

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE
from tests.test_wk_workspace import ContainerWorld, World, WorkspaceTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, places, record, workspace  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.store import Store  # noqa: E402

FAR_ROOT = "/home/u/wk"
DRIVERS = {"container": places.Container, "vm": places.Vm, "remote": places.Remote, "local": places.LocalWorkspace}


class DriverWorld(World):
    """The World with one real driver in PodmanDriver's place, its records where that driver keeps them."""

    KIND = None

    def __init__(self, tmp):
        super().__init__(tmp)
        self.env_for_driver()
        self.reg = FakeRegistry(self.env, self, lambda n, e: DRIVERS[self.KIND](n, str(REPO), e, self), names=["fakebox"])
        self.driver = self.reg.load("fakebox")
        self.records = record.of_driver(self.driver, self.clock, self)
        self.lock = Lock(self.driver.store, self, self.clock)
        self.effects = []
        self.acted = set()

    def env_for_driver(self):
        pass

    def remove(self, path):
        super().remove(path)
        if path.startswith(str(self.records.root)) and not act.dry_run():
            shutil.rmtree(path, True)

    def make(self, name="ws", marker=True, base=True):
        super().make(name, marker, base)
        self.containers.discard("wk-" + name)
        self.dirs.add(self.ws_dir(name))
        self.files[os.path.join(self.ws_dir(name), places.READY_MARKER)] = ""

    def left(self, name="ws"):
        """What of `name` is still anywhere, by where it lives; empty is gone."""
        t, out = self.driver, {}
        if t.info(name) != "absent":
            out["environment"] = t.info(name)
        if self.isdir(t.store.ws_dir(name)):
            out["directory"] = t.store.ws_dir(name)
        if self.records.list():
            out["records"] = len(self.records.list())
        if "Host wk-%s" % name in self.files.get(workspace.sshalias.alias_path(self.env), "").splitlines():
            out["alias"] = True
        if t.create_log(name) in self.files:
            out["log"] = t.create_log(name)
        return out

    def record_goes_last(self):
        """Every mutation that is not a record's lands before the first record removal."""
        root = str(self.records.root)
        work = [e for e in self.work() if e[0] in ("run", "write", "remove", "mkdir", "kill") and self.mutates(e)]
        first = next(i for i, e in enumerate(work) if e[0] == "remove" and e[1].startswith(root))
        return [e for e in work[first:] if not (e[0] == "remove" and e[1].startswith(root))]

    def mutates(self, e):
        """An ssh ControlPath directory is the connection's, not the workspace's."""
        if e[0] == "mkdir":
            return not e[1].endswith(os.path.join("wk", "ssh"))
        return e[0] != "run" or e[1] in self.acted

    def act_run(self, argv, **kw):
        self.acted.add(tuple(argv))
        return super().act_run(argv, **kw)


class RealContainerWorld(DriverWorld, ContainerWorld):
    KIND = "container"

    def make(self, name="ws", marker=True, base=True):
        World.make(self, name, marker, base)


class VmWorld(DriverWorld):
    """A macOS host with tart: the guests are `vms`, a delete takes one, and no `tart run` outlives it."""

    KIND = "vm"

    def env_for_driver(self):
        self.env.pop("WK_IN_VM")
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "tart").write_text("")
        (bindir / "tart").chmod(0o755)
        self.env["PATH"] = str(bindir)
        self.tart = os.path.realpath(str(bindir / "tart"))
        self.vms = {}
        self.react([self.tart, "list"], lambda a, f: Result(0, json.dumps(
            [{"Name": n, "State": s, "Source": "local"} for n, s in sorted(f.vms.items())])))
        self.answer([self.tart, "stop"])
        self.react([self.tart, "delete"], lambda a, f: (f.vms.pop(a[-1], None), Result(0))[1])
        self.answer(["pgrep"], rc=1)

    def make(self, name="ws", marker=True, base=True):
        super().make(name, marker, base)
        self.vms["wk-" + name] = "running"
        for f in (".run.log", ".unfiltered"):
            self.files[os.path.join(self.driver.vm_dir(), name + f)] = ""

    def left(self, name="ws"):
        out = super().left(name)
        guest = [f for f in (name + ".run.log", name + ".unfiltered") if os.path.join(self.driver.vm_dir(), f) in self.files]
        if guest:
            out["guest files"] = guest
        return out


class RemoteWorld(DriverWorld):
    """A build machine over ssh whose own wk destroys a workspace: `far` is the set of workspace directories on it."""

    KIND = "remote"

    def env_for_driver(self):
        self.env.update({"WK_REMOTE_HOST": "box.example", "WK_REMOTE_ROOT": FAR_ROOT,
                         "WK_REMOTE_STORE": str(self.tmp / "rstore"), "XDG_STATE_HOME": str(self.tmp / "state")})
        self.far = set()
        self.react(["ssh"], self._ssh)
        self.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="abc1234def\n")

    def _ssh(self, argv, f):
        cmd = argv[-1]
        if "uname -s" in cmd:
            return Result(0, LINUX_PROBE)
        words = shlex.split(cmd)
        text = words[2] if words[:2] == ["sh", "-c"] else cmd
        if text.startswith("test -f $HOME/.wk-remote"):
            return Result(0)
        if "/wk doctor --probe-tools" in text:
            return Result(0, "sha=abc1234def\ndirty=no\n")
        if "/wk rm " in text:
            f.far.discard("%s/ws/%s" % (FAR_ROOT, words[-1]))
            return Result(0)
        if text.startswith("if [ ! -d"):
            return Result(0, "present\n" if shlex.split(text)[4] in f.far else "absent\n")
        path = shlex.split(text)[-1]
        if text.startswith(("test -d", "test -e")):
            return Result(0 if path in f.far else 1)
        return Result(1, "", "no far answer for: %s" % text[:60])

    def make(self, name="ws", marker=True, base=True):
        super().make(name, marker, base)
        self.far.add("%s/ws/%s" % (FAR_ROOT, name))


class LocalWorld(DriverWorld):
    """Inside workspace `ws` itself, which its own marker names."""

    KIND = "local"

    def env_for_driver(self):
        marker = self.tmp / "home" / ".wk-workspace"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("name=ws\nsrc=/src/WebKit\n")


WORLDS = {"container": RealContainerWorld, "vm": VmWorld, "remote": RemoteWorld}


class RmFinalStateTest(WorkspaceTest):
    def setUp(self):
        super().setUp()
        os.environ["WK_YES"] = "1"
        # This host is a Mac, which a guest is driven from; the store rule that says so is held by test_wk_store.
        mac = mock.patch.object(Store, "macos_host", property(lambda store: not store.env.get("WK_IN_VM")))
        mac.start()
        self.addCleanup(mac.stop)

    def world(self, cls):
        w = cls(self.tmp)
        w.make()
        w.alias()
        w.files[w.driver.create_log("ws")] = "log\n"
        w.begin().end(0)
        w.begin("build", pid=99).end(0)
        w.effects = []
        return w

    def rm(self, w):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            rc = workspace.rm_names(w.reg, w.records, ["ws"])
        return rc, err.getvalue()


class TestRmFinalState(RmFinalStateTest):
    def test_rm_final_state(self):
        """Inside a workspace (local) there is nothing to remove it from: the refusal names the host and removes nothing."""
        for kind, cls in dict(WORLDS, local=LocalWorld).items():
            with self.subTest(driver=kind):
                w = self.world(cls)
                before = w.left()
                self.assertTrue(before, "the world made nothing to remove")
                rc, err = self.rm(w)
                if kind == "local":
                    self.assertEqual((rc, w.left()), (1, before), err)
                    continue
                self.assertEqual((rc, w.left()), (0, {}), err)
                self.assertIn("Host other", w.files[workspace.sshalias.alias_path(w.env)])
                self.assertEqual(w.record_goes_last(), [])

    def test_rm_final_state_killed_after_any_effect_and_rerun(self):
        for kind, cls in WORLDS.items():
            with self.subTest(driver=kind):
                converges(self, lambda: self.world(cls), self.rm, lambda w: w.left(), max_effects=80)
