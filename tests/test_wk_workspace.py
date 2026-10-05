"""lib/wk/workspace.py: `wk new` and `wk rm` as flows over a fake place on a fake machine."""
import contextlib
import io
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE

sys.path.insert(0, str(REPO / "lib"))
from wk import act, places, pr, record, secrets, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402

WK = str(REPO / "wk")
ZED = str(REPO / "cmd" / "zed")
CHECKOUT = "branch=main\nupstream=origin/main\nbehind=0\nhead=abc1234\n"


def step(argv):
    """A run named by what it does: the SDK scripts by their name, a command in the workspace "exec", anything else
    by its program."""
    for a in argv:
        name = os.path.basename(a).split(".")[0]
        if name in ("wkdev-create", "sdk-refresh"):
            return name
        if name == "wkdev-enter":
            return "exec"
    return argv[0]


DRIVERS = {"container": places.Container, "vm": places.Vm, "remote": places.Remote, "local": places.LocalWorkspace}
FAR_ROOT = "/home/u/wk"


class World(Fake):
    """This host with a place per `kinds` entry, each the real driver: podman answers from `containers` (wkdev-create
    writes the ready marker), tart from `vms`, a build box's ssh from `far` (its workspace directories) or fails
    with `unreachable`; inside workspace `ws` (local) its marker names it. The store holds a mirror and one
    snapshot, `main-1`."""

    def __init__(self, tmp, kinds=None):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "WK_IN_VM": "1", "WK_PLACE": "fakebox"}
        self.clock = FakeClock()
        self.containers, self.vms, self.far, self.unreachable = set(), {}, set(), None
        self.probe = "yes"
        self.checkout = Result(0, CHECKOUT)
        self.bash = {}
        self.acted = set()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.answer(["hostname"], out="here\n")
        self.answer([WK, "sync"], out="fetched\n")
        self.answer([ZED])
        self.react(["podman", "container", "exists"], lambda a, f: Result(0 if a[-1] in f.containers else 1))
        self.react(["podman", "inspect"], lambda a, f: Result(0, "running\n") if a[2] in f.containers else Result(125, "", "no such container"))
        self.react(["podman", "rm", "-f"], self._podman_rm)
        self.react(["podman", "unshare", "rm", "-rf"], self._rm_rf)
        self.react(["env"], self._sdk)
        self.answer(["bash", os.path.join(str(REPO), "container", "sdk-refresh.sh")])
        self.answer(["install", "-m"])
        self.answer(["nproc"], out="8\n")
        self.files["/proc/meminfo"] = "MemTotal:       33554432 kB\n"
        self.react(["find"], self._find)
        self.react(["bash", "-c"], self._bash)
        self.bash["gpu_flags"] = Result(0, "")
        self.react(["git", "-C"], self._git)
        kinds = kinds or {"fakebox": "container"}
        for kind in set(kinds.values()):
            getattr(self, "_" + kind)()

        self.reg = FakeRegistry(self.env, self, lambda name, env: DRIVERS[kinds[name]](name, str(REPO), env, self), names=list(kinds))
        self.driver = self.reg.load("fakebox")
        self.records = record.of_driver(self.driver, self.clock, self)
        self.lock = Lock(self.driver.store, self, self.clock)
        keyring = Store(self.env).keyring_dir()
        for f in secrets.PUBLISHED:
            self.files[os.path.join(keyring, f)] = "published by the host\n"
        self.publish("main-1")
        self.dirs.add(self.driver.store.mirror_dir())
        self.effects = []

    def _container(self):
        pass

    def _vm(self):
        self.env.pop("WK_IN_VM")
        self.env["WK_VM_STORE"] = str(self.tmp / "vmstore")
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "tart").write_text("")
        (bindir / "tart").chmod(0o755)
        self.env["PATH"] = str(bindir)
        self.tart = t = os.path.realpath(str(bindir / "tart"))
        self.react([t, "list"], lambda a, f: Result(0, json.dumps([{"Name": n, "State": s, "Source": "local"} for n, s in sorted(f.vms.items())])))
        self.react([t, "clone"], lambda a, f: (f.vms.__setitem__(a[3], "stopped"), Result(0))[1])
        self.react([t, "delete"], lambda a, f: (f.vms.pop(a[-1], None), Result(0))[1])
        self.answer([t, "set"])
        self.answer([t, "stop"])
        self.answer(["pgrep"], rc=1)
        self.answer(["sysctl", "-n", "hw.ncpu"], out="10\n")
        self.answer(["sysctl", "-n", "hw.memsize"], out="34359738368\n")
        self.answer(["podman", "machine", "inspect"], rc=125)

    def _remote(self):
        self.env.update({"WK_REMOTE_HOST": "box.example", "WK_REMOTE_ROOT": FAR_ROOT,
                         "WK_REMOTE_STORE": str(self.tmp / "rstore"), "XDG_STATE_HOME": str(self.tmp / "state")})
        self.react(["ssh"], self._ssh)
        self.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="abc1234def\n")

    def _local(self):
        marker = self.tmp / "home" / ".wk-workspace"
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("name=ws\nsrc=/src/WebKit\n")

    def _ssh(self, argv, f):
        if f.unreachable:
            return Result(255, "", f.unreachable + "\n")
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

    def publish(self, bid, on_branch=True):
        store = self.driver.store
        d = os.path.join(store.snapshots_dir(), bid)
        self.dirs.update({store.snapshots_dir(), d, store.snapshot_tree(bid), os.path.join(store.snapshot_tree(bid), ".git")})
        self.files[os.path.join(d, "branch")] = "origin/main\n"
        self.files[store.snapshot_sha_file(bid)] = "a" * 40 + "\n"
        if not on_branch:
            self.detached = getattr(self, "detached", set()) | {bid}

    def _git(self, argv, f):
        bid = os.path.basename(os.path.dirname(argv[2]))
        if argv[3:] == ["rev-parse", "HEAD"]:
            return Result(0, "a" * 40 + "\n")
        if argv[3:5] == ["symbolic-ref", "--quiet"]:
            return Result(1) if bid in getattr(f, "detached", ()) else Result(0, "refs/heads/main\n")
        if argv[3:5] == ["rev-parse", "--abbrev-ref"]:
            return Result(0, "origin/main\n")
        return Result(127, "", "no git answer")

    @property
    def fake(self):
        return self

    def rel(self, value):
        """A path or effect with this world's scratch directory taken out, so two worlds compare."""
        if isinstance(value, tuple):
            return tuple(self.rel(v) for v in value)
        return value.replace(str(self.tmp), "") if isinstance(value, str) else value

    def remove(self, path):
        super().remove(path)
        if path.startswith(str(self.records.root)) and not act.dry_run():
            shutil.rmtree(path, True)

    def act_run(self, argv, **kw):
        self.acted.add(tuple(argv))
        return super().act_run(argv, **kw)

    def _podman_rm(self, argv, f):
        f.containers.discard(argv[-1])
        return Result(0)

    def _rm_rf(self, argv, f):
        p = argv[-1]
        f.files = {k: v for k, v in f.files.items() if k != p and not k.startswith(p + "/")}
        f.dirs = {d for d in f.dirs if d != p and not d.startswith(p + "/")}
        return Result(0)

    def wkdev_create(self, argv, f):
        f.containers.add(argv[argv.index("--name") + 1])
        home = argv[argv.index("--home") + 1]
        f.dirs.add(home)
        f.files[os.path.join(home, places.READY_MARKER)] = ""
        return Result(0)

    def _sdk(self, argv, f):
        if step(argv) == "wkdev-create":
            return self.wkdev_create(argv, f)
        if argv[-1].endswith("print-sdk-version"):
            return Result(1, "", "no checkout")
        return self._exec(["exec", argv[argv.index("--name") + 1][3:]] + list(argv[argv.index("--") + 2:]), f)

    def _find(self, argv, f):
        p = argv[1]
        return Result(0, "".join(k + "\n" for k in f.files if k.startswith(p + "/")))

    def _bash(self, argv, f):
        for key, r in f.bash.items():
            if key in argv[2]:
                return r(argv) if callable(r) else r
        return Result(127, "", "no bash answer for: %s" % argv[2][-60:])

    def _exec(self, argv, f):
        if argv[2:4] == ["kill", "-0"]:
            return Result(0 if int(argv[4]) in f.pids else 1)
        if "echo yes || echo no" in argv[-1]:
            return Result(0, f.probe + "\n") if f.probe is not None else Result(1, "", "no such container")
        return f.checkout

    def ws_dir(self, name="ws"):
        return self.driver.store.ws_dir(name)

    def make(self, name="ws", marker=True, base=True):
        d, kind = self.ws_dir(name), self.driver.kind
        for sub in ("changes", "overlay-work", "home", "build"):
            self.dirs.add(os.path.join(d, sub))
        self.dirs.add(d)
        self.files[os.path.join(d, "arch")] = "native\n"
        if base:
            self.files[os.path.join(d, "base-id")] = "main-1\n"
        if marker:
            self.files[os.path.join(d, "home" if kind == "container" else "", places.READY_MARKER)] = ""
        if kind == "container":
            self.containers.add("wk-" + name)
        elif kind == "vm":
            self.vms["wk-" + name] = "running"
            for f in (".run.log", ".unfiltered"):
                self.files[os.path.join(self.driver.vm_dir(), name + f)] = ""
        elif kind == "remote":
            self.far.add("%s/ws/%s" % (FAR_ROOT, name))

    def alias(self, name="ws"):
        conf = workspace.sshalias.alias_path(self.env)
        self.files[conf] = "Host other\n    HostName o\n" + workspace.sshalias.BLOCK % (name, "1.2.3.4", "u", "")
        return conf

    def begin(self, kind="new", name="ws", pid=4242, **kw):
        args = dict(kind=kind, where="here", name=name, kill="wk %s %s --kill" % (kind, name), log="/nolog", plan=["a"], pid=pid)
        args.update(kw)
        return self.records.begin(**args)

    def lock_files(self):
        return sorted(p for p in self.files if p.startswith(self.env["WK_LOCK_DIR"]))

    def work(self):
        """The effects that are the command's work: a lock's are process coordination, not state."""
        return [e for e in self.effects if not (isinstance(e[1], str) and e[1].startswith(self.env["WK_LOCK_DIR"]))]

    def state(self):
        """What a flow leaves, less lock files and the snapshot."""
        recs = [(t.field("kind"), t.field("exit")) for t in self.records.list()]
        if self.driver.kind == "vm":
            vmstore = self.env["WK_VM_STORE"]
            return (sorted(self.vms.items()), sorted(self.rel(p) for p in self.files if p.startswith(vmstore)),
                    sorted(self.rel(d) for d in self.dirs if d.startswith(vmstore + "/ws")), recs)
        store = str(self.tmp / "store")
        return (sorted(self.containers), sorted(self.rel(p) for p in self.files if p.startswith(store) and not p.startswith(store + "/base/")),
                sorted(self.rel(d) for d in self.dirs if d.startswith(store + "/ws")), recs,
                self.files.get(workspace.sshalias.alias_path(self.env), ""))

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
        if t.kind == "vm":
            guest = [f for f in (name + ".run.log", name + ".unfiltered") if os.path.join(t.vm_dir(), f) in self.files]
            if guest:
                out["guest files"] = guest
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


class WorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-workspace-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_CMD", "WK_DEBUG"):
            os.environ.pop(v, None)
        for p in (mock.patch.object(record, "host_name", return_value="here"), mock.patch.object(sys, "stdin", io.StringIO(""))):
            p.start()
            self.addCleanup(p.stop)
        self.w = self.make_world()

    def make_world(self, **kw):
        return World(self.tmp, **kw)

    def stderr(self, fn):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            result = fn()
        return result, err.getvalue()

    def refused(self, fn, status=1):
        with self.assertRaises(Refused) as cm:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                fn()
        self.assertEqual(cm.exception.status, status, err.getvalue())
        return err.getvalue()

    def dry_run(self):
        os.environ["WK_DRY_RUN"] = "1"

    def front(self, w=None, name="ws", **opts):
        w = w or self.w
        return workspace.new_front(w.reg, w.records, name, opts)

    def detached(self, w=None, name="ws", base="", arch="native", driver=None):
        w = w or self.w
        return workspace.new_detached_run(driver or w.driver, w.records, w.lock, w.clock, name, base, arch)

    def runs(self, w=None, head=None):
        w = w or self.w
        return [e[1] for e in w.effects if e[0] == "run" and (head is None or step(e[1]) == head)]

    def lock_takes(self, w=None, heads=()):
        """Each lock taken, as "lock <resource>", in order with the runs whose head is in `heads`."""
        out = []
        for e in (w or self.w).effects:
            if e[0] == "symlink" and not e[1].endswith(".breaking") and ".new." not in e[1]:
                out.append("lock " + os.path.basename(e[1]).split("@")[0])
            elif e[0] == "run" and step(e[1]) in heads:
                out.append(step(e[1]))
        return out

    def attempt(self, w):
        """A creation as far as this world answers it: the vm's and a build box's own create are past what it emulates."""
        with contextlib.suppress(Refused):
            self.stderr(lambda: self.detached(w))

    def bash_runs(self, w=None, fn=""):
        return [a for a in self.runs(w, "bash") if fn in a[2]]


class TestNewFrontRefusals(WorkspaceTest):
    def test_an_invalid_name_is_refused_before_anything_else(self):
        for bad in ("-x", "", "a b", "a/b"):
            err = self.refused(lambda: self.front(name=bad))
            self.assertIn("invalid name '%s': use [a-zA-Z0-9._-], not starting with '-'" % bad, err)
        self.assertEqual(self.w.effects, [])

    def test_pr_needs_a_spec_and_a_valid_one(self):
        self.assertIn("--pr needs a spec: <user>:<branch>, <n>, or wpe:<n>", self.refused(lambda: self.front(pr="")))
        self.assertIn("'nope' is not a PR spec", self.refused(lambda: self.front(pr="nope")))
        self.assertEqual(self.runs(head=WK), [])

    def test_pr_and_no_wait_exclude_each_other(self):
        err = self.refused(lambda: self.front(pr="u:b", no_wait=True))
        self.assertIn("--pr needs the workspace to be ready, and --no-wait returns before it is.", err)
        self.assertIn("wk pr ws u:b", err)

    def test_an_unknown_arch_or_target_is_refused_with_the_bridge_or_registry_words(self):
        self.assertIn("unknown architecture 'sparc'", self.refused(lambda: self.front(arch="sparc")))
        self.assertIn("unknown place 'nosuch'.", self.refused(lambda: self.front(place="nosuch")))

    def test_a_non_native_arch_is_container_only(self):
        w = self.make_world(kinds={"fakebox": "vm"})
        err = self.refused(lambda: self.front(w, arch="armhf"))
        self.assertIn("--arch armhf is a container-only capability (this is place 'fakebox')", err)
        w = self.make_world()
        rc, _ = self.stderr(lambda: self.front(w, arch="armhf", no_wait=True))
        self.assertEqual(rc, 0)

    def test_a_local_target_refuses_on_the_terminal_naming_the_host_command(self):
        w = self.make_world(kinds={"fakebox": "local"})
        err = self.refused(lambda: self.front(w))
        self.assertIn("a workspace cannot create a workspace -- run 'wk new ws' on the host", err)
        self.assertEqual([e for e in w.effects if e[0] != "run"], [])

    def test_a_present_or_broken_workspace_is_refused_at_once(self):
        self.w.make()
        err = self.refused(lambda: self.front())
        self.assertIn("workspace 'ws' already exists (place 'fakebox').\n    'wk rm ws' first, or pick another name.", err)
        self.w.containers.clear()
        err = self.refused(lambda: self.front())
        self.assertIn("'ws' is a record without an environment -- something outside wk removed\n    the fakebox side of it. 'wk status ws' says so; 'wk rm ws' clears it.", err)
        self.assertEqual([e for e in self.w.effects if e[0] == "spawn"], [])

    def test_a_creation_already_running_is_refused_with_its_pid_and_stage(self):
        self.w.make(marker=False, base=False)
        t = self.w.begin(plan=list(workspace.PLAN), log="/tmp/new-ws.log")
        t.step_named("create")
        self.w.pids.add(4242)
        err = self.refused(lambda: self.front())
        self.assertIn("'ws' is already being created (pid 4242, at stage\n    'create'). Follow it:      tail -f /tmp/new-ws.log", err)
        self.assertIn("wk enter ws --zed   /   wk build ws <preset>", err)

    def test_a_creation_nobody_is_running_falls_through_to_the_driver(self):
        self.w.make(marker=False, base=False)
        self.w.begin(plan=list(workspace.PLAN))
        _, err = self.stderr(lambda: self.front(no_wait=True))
        self.assertIn("detached as pid", err)


class TestNewFrontDetach(WorkspaceTest):
    def spawned(self, w=None):
        return [e for e in (w or self.w).effects if e[0] == "spawn"]

    def test_the_driver_is_spawned_with_the_canonical_flags_into_a_truncated_log(self):
        rc, err = self.stderr(lambda: self.front(no_wait=True, base="main-2", arch="arm", zed=True))
        self.assertEqual(rc, 0)
        log = self.w.driver.create_log("ws")
        self.assertEqual(self.spawned(), [("spawn", (WK, "new", "ws", "--on", "fakebox", "--arch", "armhf", "--base", "main-2", "--_detached"), log)])
        self.assertIn(("write", log), self.w.effects)
        self.assertIn(("mkdir", os.path.dirname(log)), self.w.effects)

    def test_the_store_is_initialised_before_the_detach(self):
        self.stderr(lambda: self.front(no_wait=True))
        kinds = [e[0] for e in self.w.effects if e[0] in ("mkdir", "spawn")]
        self.assertEqual(kinds[0], "mkdir")
        self.assertIn(("mkdir", os.path.join(self.w.env["WK_STORE"], "ws")), self.w.effects)

    def ended(self, status):
        t = self.w.begin(plan=list(workspace.PLAN), log=self.w.driver.create_log("ws"))
        t.end(status)
        return t

    def test_the_wait_reads_the_drivers_verdict(self):
        self.ended(0)
        rc, err = self.stderr(lambda: self.front())
        self.assertEqual(rc, 0)
        self.assertIn("workspace 'ws' ready", err)
        self.assertNotIn("ready (", err)

    def test_each_failed_verdict_has_its_own_words(self):
        for status, words in (("refused", "'ws' was not created, and nothing was left half-made: the reason is\n    above, in full in"),
                              (1, "creating 'ws' failed (failed) -- the reason is above, in full in"),
                              ("cancelled", "creating 'ws' failed (cancelled)")):
            w = self.make_world()
            t = w.begin(plan=list(workspace.PLAN), log=w.driver.create_log("ws"))
            t.end(status)
            self.assertIn(words, self.refused(lambda: self.front(w)))

    def test_a_driver_that_never_writes_a_record_is_reported_crashed(self):
        class DiesAtOnce(World):
            def spawn(self, argv, log):
                pid = super().spawn(argv, log)
                self.pids.discard(pid)
                return pid
        w = DiesAtOnce(self.tmp)
        err = self.refused(lambda: self.front(w))
        self.assertIn("the process creating 'ws' is gone without having said how it ended.", err)
        self.assertIn("wk new ws --on fakebox", err)

    def test_the_wait_gives_up_after_wk_new_timeout_and_undoes_nothing(self):
        self.w.env["WK_NEW_TIMEOUT"] = "3"
        err = self.refused(lambda: self.front())
        self.assertIn("'ws' is still being created after 3s.", err)
        self.assertIn("Nothing here was undone.", err)
        self.assertEqual(self.w.clock.slept, [1, 1, 1])
        self.assertIn(1001, self.w.pids)

    def test_the_log_is_streamed_while_waiting(self):
        class Says(World):
            def spawn(self, argv, log):
                pid = super().spawn(argv, log)
                self.files[log] = "driver says hello\n"
                return pid
        w = Says(self.tmp)
        t = w.begin(plan=list(workspace.PLAN), log=w.driver.create_log("ws"))
        t.end(0)
        _, err = self.stderr(lambda: self.front(w))
        self.assertIn("driver says hello", err)


class Creates(World):
    """A host whose spawned detached run makes the workspace at once and records it done."""

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        self.make(argv[2])
        self.begin(name=argv[2], plan=list(workspace.PLAN), log=log, pid=pid).end(0)
        return pid


class TestNewFrontTail(WorkspaceTest):
    def make_world(self, **kw):
        return Creates(self.tmp, **kw)

    def test_the_hints_follow_readiness_and_no_agent_is_started(self):
        rc, err = self.stderr(lambda: self.front())
        self.assertEqual(rc, 0)
        self.assertEqual(self.runs(head=WK), [])
        self.assertEqual(self.bash_runs(fn="wk_cred_check"), [])

    def test_a_pr_is_checked_out_once_ready_and_its_failure_is_the_commands(self):
        calls = []
        with mock.patch.object(pr, "checkout", lambda t, here, name, spec: calls.append((t.name, name, spec))):
            rc, err = self.stderr(lambda: self.front(pr="u:b"))
        self.assertEqual((rc, calls), (0, [("fakebox", "ws", "u:b")]))
        self.assertLess(err.index("workspace 'ws' ready"), len(err))
        w = self.make_world()
        with mock.patch.object(pr, "checkout", lambda *a: act.die("no branch 'b'", 3)):
            self.assertIn("no branch", self.refused(lambda: self.front(w, pr="u:b"), 3))

    def test_zed_opens_the_checkout_and_its_failure_only_warns(self):
        self.stderr(lambda: self.front(zed=True))
        self.assertEqual(self.runs(head=ZED), [(ZED, "ws")])
        w = self.make_world()
        w.answer([ZED], rc=1)
        rc, err = self.stderr(lambda: self.front(w, zed=True))
        self.assertEqual(rc, 0)
        self.assertIn("'ws' is there; opening it in Zed is what failed (above) -- 'wk zed ws' retries", err)


class TestNewOnAPeer(unittest.TestCase):
    def setUp(self):
        self.here = Fake("here")
        self.here.answer(["ssh"])
        self.driver = mock.Mock(is_local=False, hand_over=lambda cmd, args, tty: ["ssh", "peer", "wk", cmd, *args])
        for p in (mock.patch.dict(os.environ), mock.patch("os.isatty", lambda fd: True)):
            p.start()
            self.addCleanup(p.stop)
        for v in ("WK_DRY_RUN", "WK_DESTRUCTIVE"):
            os.environ.pop(v, None)

    def new(self, **opts):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return workspace.new_handed_over(self.driver, self.here, str(REPO), "ws", "native", opts), err.getvalue()
            except Refused as e:
                return e.status, err.getvalue()

    def test_the_hand_over_runs_on_a_terminal(self):
        self.assertEqual(self.new(pr="123")[0], 0)
        self.assertEqual(self.here.effects, [("run_tty", ("ssh", "peer", "wk", "new", "ws", "--pr", "123"), None)])

    def test_a_dry_run_prints_the_hand_over_and_runs_none(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.new()
        self.assertEqual((rc, self.here.effects), (0, []))
        self.assertIn("would run: ssh peer wk new ws", err)

    def test_a_base_is_refused(self):
        rc, err = self.new(base="20260101-000000")
        self.assertEqual((rc, self.here.effects), (1, []))
        self.assertIn("--base", err)


class TestNewKill(WorkspaceTest):
    def test_kill_takes_no_other_flag(self):
        err = self.refused(lambda: self.front(kill=True, no_wait=True))
        self.assertIn("'wk new ws --kill' stops the creation already running and takes\n    nothing with it -- no --base, --zed, --no-wait or --pr.", err)

    def test_nothing_running_is_said_and_is_not_a_failure(self):
        rc, err = self.stderr(lambda: self.front(kill=True))
        self.assertEqual(rc, 0)
        self.assertIn("no new is running in 'ws' -- 'wk status ws' says what it last did", err)
        self.w.begin().end(0)
        rc, err = self.stderr(lambda: self.front(kill=True))
        self.assertEqual((rc, "no new is running" in err), (0, True))

    def test_a_running_driver_gets_term_and_its_record_ends_cancelled(self):
        t = self.w.begin()
        self.w.pids.add(4242)
        rc, err = self.stderr(lambda: self.front(kill=True))
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in self.w.effects if e[0] == "kill"], [("kill", 4242, int(signal.SIGTERM))])
        self.assertEqual(t.verdict(), "cancelled")
        self.assertIn("wk rm ws    (then 'wk new ws' to start again)", err)

    def test_a_driver_that_outlives_term_and_kill_is_named_with_ps(self):
        class Immortal(World):
            def kill(self, pid, sig=signal.SIGTERM):
                self.effect(("kill", pid, int(sig)))
                return True
        w = Immortal(self.tmp)
        w.env["WK_KILL_WAIT"] = "2"
        t = w.begin()
        w.pids.add(4242)
        err = self.refused(lambda: self.front(w, kill=True))
        self.assertEqual([e for e in w.effects if e[0] == "kill"], [("kill", 4242, int(signal.SIGTERM)), ("kill", 4242, int(signal.SIGKILL))])
        self.assertEqual(w.clock.slept, [1] * 7)
        self.assertIn("the process creating 'ws' outlived a TERM and a KILL.", err)
        self.assertIn("ps -p 4242", err)
        self.assertEqual(t.verdict(), "cancelled")

    def test_a_dry_run_signals_nothing_and_leaves_the_record_running(self):
        t = self.w.begin()
        self.w.pids.add(4242)
        self.dry_run()
        rc, err = self.stderr(lambda: self.front(kill=True))
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in self.w.effects if e[0] == "kill"], [])
        self.assertIsNone(t.raw("exit"))
        self.assertIn("dry run -- would TERM pid 4242, KILL it after 15s, and record it cancelled", err)


class TestNewDriver(WorkspaceTest):
    def acts(self, w=None):
        w = w or self.w
        return [e for e in w.work() if e[0] in ("write", "mkdir", "remove") or (e[0] == "run" and step(e[1]) in ("sdk-refresh", "wkdev-create", WK))]

    def test_the_steps_land_in_order_and_the_record_ends_0(self):
        rc, err = self.stderr(lambda: self.detached())
        self.assertEqual(rc, 0)
        heads = [step(e[1]) for e in self.acts() if e[0] == "run"]
        self.assertEqual(heads, ["sdk-refresh", WK, "wkdev-create", WK])
        ws = self.w.ws_dir()
        self.assertEqual(self.w.files[os.path.join(ws, "base-id")], "main-1\n")
        self.assertIn("wk-ws", self.w.containers)
        (t,) = self.w.records.list()
        self.assertEqual((t.field("kind"), t.verdict(), t.plan()), ("new", "ok", list(workspace.PLAN)))
        self.assertEqual([s for _, s in t.steps()], ["done"] * 7 + ["running"])
        self.assertEqual(t.field("kill"), "wk new ws --kill")
        self.assertEqual(self.w.lock_files(), [])

    def test_the_agents_are_installed_once_it_is_up_and_before_the_fetch(self):
        self.stderr(lambda: self.detached())
        execs = [a[-1] for a in self.runs(head="exec")]
        install = next(i for i, c in enumerate(execs) if "claude.ai/install.sh" in c)
        config = next(i for i, c in enumerate(execs) if "claude/workspace-config.py" in c)
        fetch = next(i for i, c in enumerate(execs) if "echo yes || echo no" in c)
        self.assertLess(install, config)
        self.assertLess(config, fetch)

    def test_an_agent_install_that_fails_fails_the_creation_and_a_rerun_remakes_it(self):
        def failing(argv, f):
            return Result(1, "claude=failed\n") if "claude.ai/install.sh" in argv[-1] else World._exec(f, argv, f)
        self.w._exec = failing
        err = self.refused(lambda: self.detached())
        self.assertIn("could not install the coding agents in 'ws' (claude=failed pi=?)", err)
        (t,) = self.w.records.list()
        self.assertNotEqual("0", t.field("exit"))
        self.assertEqual("creating", workspace.creation_state(self.w.driver, self.w.records, "ws"))
        del self.w._exec
        rc, err = self.stderr(lambda: self.detached())
        self.assertEqual(0, rc, err)
        self.assertIn("'ws' exists but was never finished -- destroying it and starting again", err)

    def test_the_sdk_is_refreshed_under_its_lock_before_the_store_lock_and_the_create(self):
        self.stderr(lambda: self.detached())
        self.assertEqual(self.lock_takes(heads=("sdk-refresh", "wkdev-create")),
                         ["lock sdk", "sdk-refresh", "lock ws-ws", "lock store", "wkdev-create"])
        for kind in ("vm", "remote"):
            w = self.make_world(kinds={"fakebox": kind})
            self.attempt(w)
            takes = self.lock_takes(w, heads=("sdk-refresh",))
            self.assertEqual(takes[0], "lock ws-ws", kind)
            self.assertNotIn("lock sdk", takes, kind)
            self.assertNotIn("sdk-refresh", takes, kind)

    def test_the_mirror_is_refreshed_alone_before_the_store_lock_and_only_where_the_checkout_reads_it(self):
        self.stderr(lambda: self.detached())
        self.assertEqual(self.lock_takes(heads=(WK, "wkdev-create")),
                         ["lock sdk", "lock ws-ws", WK, "lock store", "wkdev-create", WK])
        self.assertEqual(self.runs(head=WK)[0], (WK, "sync", "--mirror"))
        for kind, want in (("vm", [(WK, "sync", "--mirror")]), ("remote", [])):
            w = self.make_world(kinds={"fakebox": kind})
            self.attempt(w)
            self.assertEqual([r for r in self.runs(w, WK) if "--mirror" in r], want, kind)

    def test_a_mirror_refresh_that_failed_refuses_the_creation_naming_the_remedy(self):
        self.w.answer([WK, "sync", "--mirror"], rc=1, err="no request broker\n")
        err = self.refused(lambda: self.detached())
        self.assertIn("no request broker", err)
        self.assertIn("'ws' was not created.\n    Fix that:  wk sync --mirror   then  wk new ws", err)
        self.assertNotIn("wk-ws", self.w.containers)

    def test_a_present_broken_or_unreachable_workspace_is_refused_and_the_record_says_refused(self):
        cases = []
        w = self.make_world()
        w.make()
        cases.append((w, "workspace 'ws' already exists (place 'fakebox').\n    'wk rm ws' first, or pick another name."))
        w = self.make_world()
        w.make()
        w.containers.clear()
        cases.append((w, "'ws' is a record without an environment: creation finished, and the\n    fakebox side of it is gone -- something outside wk removed it."))
        w = self.make_world(kinds={"fakebox": "remote"})
        w.unreachable = "Connection refused"
        cases.append((w, "cannot reach the machine behind place 'fakebox', so whether 'ws' is\n    already there cannot be known"))
        for w, words in cases:
            err = self.refused(lambda: self.detached(w))
            self.assertIn(words, err)
            (t,) = w.records.list()
            self.assertEqual(t.verdict(), "refused")
            self.assertEqual(t.stage(), ["checking"])
            self.assertEqual([a for a in self.runs(w) if step(a) == "wkdev-create"], [])
            self.assertEqual(w.lock_files(), [])

    def test_a_half_made_workspace_is_wiped_and_remade(self):
        self.w.make(marker=False, base=False)
        conf = self.w.alias()
        rc, err = self.stderr(lambda: self.detached())
        self.assertEqual(rc, 0)
        self.assertIn("'ws' exists but was never finished -- destroying it and starting again", err)
        heads = [step(a) if step(a) == "wkdev-create" else a[:3] for a in self.runs()
                 if step(a) == "wkdev-create" or a[:2] in (("podman", "rm"), ("podman", "unshare"))]
        self.assertEqual(heads, [("podman", "rm", "-f"), ("podman", "unshare", "rm"), "wkdev-create"])
        self.assertNotIn("Host wk-ws", self.w.files[conf])
        self.assertIn("Host other", self.w.files[conf])
        self.assertEqual(self.w.records.list()[0].verdict(), "ok")

    def test_a_half_made_workspace_the_destroy_leaves_is_a_refusal_not_a_create_over_it(self):
        self.w.make(marker=False, base=False)
        self.w.react(["podman", "rm", "-f"], lambda a, f: Result(0))
        err = self.refused(lambda: self.detached())
        self.assertIn("could not destroy the half-made workspace 'ws'; still here: the fakebox environment\n"
                      "    'wk rm ws' retries exactly that, then 'wk new ws'", err)
        self.assertEqual([a for a in self.runs() if step(a) == "wkdev-create"], [])
        self.assertEqual(self.w.records.list()[0].verdict(), "failed")
        self.assertEqual(self.w.lock_files(), [])

    def test_a_creation_that_died_after_the_marker_is_remade_not_refused(self):
        self.w.make()
        self.w.begin(plan=list(workspace.PLAN))
        self.assertEqual(workspace.creation_state(self.w.driver, self.w.records, "ws"), "creating")
        rc, err = self.stderr(lambda: self.detached())
        self.assertEqual(rc, 0)
        self.assertIn("'ws' exists but was never finished -- destroying it and starting again", err)
        self.assertEqual([t.verdict() for t in self.w.records.list()], ["ok"])
        w = self.make_world()
        w.make()
        w.begin(plan=list(workspace.PLAN)).end(0)
        self.assertEqual(workspace.creation_state(w.driver, w.records, "ws"), "present")
        self.assertIn("already exists", self.refused(lambda: self.detached(w)))

    def test_a_creation_still_running_is_not_remade(self):
        self.w.make()
        self.w.begin(plan=list(workspace.PLAN))
        self.w.pids.add(4242)
        self.assertEqual(workspace.creation_state(self.w.driver, self.w.records, "ws"), "present")

    def test_no_mirror_refuses_naming_wk_sync_and_creates_nothing(self):
        self.w.dirs.discard(self.w.driver.store.mirror_dir())
        err = self.refused(lambda: self.detached())
        self.assertIn("no WebKit mirror at %s" % self.w.driver.store.mirror_dir(), err)
        self.assertIn("wk sync    makes it", err)
        self.assertEqual([a for a in self.runs() if step(a) == "wkdev-create"], [])
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["base"]))

    def test_no_base_snapshot_refuses_naming_wk_sync_and_creates_nothing(self):
        self.w.publish("main-1", on_branch=False)
        err = self.refused(lambda: self.detached())
        self.assertIn("no snapshot this machine can build a workspace from:  wk sync\n    publishes one.", err)
        self.assertEqual([a for a in self.runs() if step(a) == "wkdev-create"], [])
        self.assertNotIn(self.w.ws_dir(), self.w.dirs)
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["base"]))
        self.assertEqual(self.w.lock_files(), [])

    def test_a_given_base_is_verified_and_its_refusal_is_the_reason(self):
        err = self.refused(lambda: self.detached(base="main-9"))
        self.assertIn("snapshot main-9 does not exist", err)
        self.w.publish("main-9", on_branch=False)
        self.assertIn("snapshot main-9 is not on branch main tracking origin/main", self.refused(lambda: self.detached(base="main-9")))
        w = self.make_world(kinds={"fakebox": "vm"})
        self.attempt(w)
        self.assertEqual(self.runs(w, head="git"), [])

    def test_a_workspace_that_never_initialises_is_refused_after_wk_ready_timeout(self):
        def no_firstrun(argv, f):
            f.containers.add(argv[argv.index("--name") + 1])
            return Result(0)
        self.w.wkdev_create = no_firstrun
        self.w.env["WK_READY_TIMEOUT"] = "2"
        self.w.driver = self.w.reg.load("fakebox")
        err = self.refused(lambda: self.detached())
        self.assertIn("'ws' was created but never finished initialising", err)
        self.assertIn("wk new ws    (destroys it and retries)", err)
        self.assertEqual(self.w.clock.slept, [1, 1])
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["init"]))

    def test_a_create_that_fails_ends_the_record_with_its_status(self):
        self.w.wkdev_create = lambda a, f: Result(125, "", "podman: boom\n")
        err = self.refused(lambda: self.detached(), 125)
        self.assertIn("wkdev-create failed for 'ws' (exit 125)", err)
        self.assertEqual(self.w.records.list()[0].field("exit"), "125")


class TestFreshen(WorkspaceTest):
    def freshen(self, w=None):
        w = w or self.w
        return self.stderr(lambda: workspace.freshen(w.driver, "ws", w))[1]

    def test_a_mirror_in_reach_is_fetched_with_wk_sync(self):
        err = self.freshen()
        self.assertEqual(self.runs(head=WK), [(WK, "sync", "ws")])
        self.assertIn("'ws' is on main at abc1234, up to date with origin/main", err)
        probe = [a for a in self.runs(head="exec")][0]
        self.assertIn("[ -d %s ]" % self.w.driver.mirror_dir(), probe[-1])

    def test_no_mirror_names_wk_sync_and_still_reads_the_checkout(self):
        self.w.probe = "no"
        err = self.freshen()
        self.assertEqual(self.runs(head=WK), [])
        self.assertIn("no mirror in reach of 'ws', so its checkout is as old as what it was made from", err)
        self.assertIn("wk sync ws    fetches it over the network", err)
        self.assertIn("is on main", err)

    def test_a_workspace_nothing_answers_in_is_reported_not_fetched(self):
        self.w.probe = None
        err = self.freshen()
        self.assertIn("nothing to run in 'ws' yet, so its checkout was not fetched in", err)
        self.assertIn("wk sync ws    once it is up", err)
        self.assertEqual(len(self.runs(head="exec")), 1)

    def test_a_failed_fetch_warns_and_is_not_fatal(self):
        self.w.answer([WK, "sync"], rc=1, err="fetch: refused\n")
        err = self.freshen()
        self.assertIn("fetch: refused", err)
        self.assertIn("'ws' is there; fetching in it is what failed (above).\n    'wk sync ws' retries", err)

    def test_the_four_checkout_reports(self):
        for out, words in ((Result(0, "detached=abc1234\n"), "'ws' is not on a branch (detached at abc1234)"),
                           (Result(1, "", "gone"), "could not read the checkout in 'ws' to say where it is"),
                           (Result(0, "branch=eng/local\n"), "'ws' is on 'eng/local', which tracks nothing -- 'git pull' and\n    'wk pr' have no upstream to name:  wk sync ws --fix"),
                           (Result(0, "branch=main\nupstream=origin/main\nbehind=2\nmoved=refused\nhead=abc\n"),
                            "'main' in 'ws' and origin/main have diverged, so the checkout is left where\n    it is:  git rebase origin/main   in the workspace"),
                           (Result(0, "branch=main\nupstream=origin/main\nbehind=2\nmoved=2\nhead=def5678\n"),
                            "'ws' is on main at def5678, 2 commit(s) on from its snapshot (origin/main)")):
            w = self.make_world()
            w.checkout = out
            self.assertIn(words, self.freshen(w))

    def test_a_dry_run_names_the_fetch_and_asks_the_workspace_nothing(self):
        self.dry_run()
        err = self.freshen()
        self.assertIn("would run: %s sync ws" % WK, err)
        self.assertEqual(self.runs(head="exec"), [])


class TestRecordHelpers(WorkspaceTest):
    def test_live_lines_name_every_live_job_and_skip_dead_pids(self):
        self.w.begin("build", "ws1", pid=4242)
        self.w.begin("agent-forward", "ws1", pid=4243, kill="wk key push off")
        self.w.begin("test", "ws1", pid=4244)
        self.w.begin("build", "ws2", pid=4245)
        self.w.pids.update({4242, 4243, 4245})
        self.assertEqual(workspace.live_task_lines(self.w.records, "ws1"),
                         "    agent-forward (pid 4243 on here)  stop it:  wk key push off\n"
                         "    build (pid 4242 on here)  stop it:  wk build ws1 --kill")
        self.assertEqual(len(workspace.task_records_for(self.w.records, "ws1")), 3)

    def test_every_record_of_that_workspace_goes_and_no_others(self):
        self.w.begin("build", "ws1")
        self.w.begin("test", "ws1", kill="x")
        self.w.begin("build", "ws2")
        workspace.remove_task_records(self.w.records, "ws1")
        self.assertEqual([t.id.split("-")[1] for t in self.w.records.list()], ["ws2"])
        self.assertEqual(len([e for e in self.w.effects if e[0] == "remove"]), 2)


class TestRmPlan(WorkspaceTest):
    def plan(self, w=None, name="ws"):
        w = w or self.w
        return workspace.rm_plan(w.reg, w.records, name)

    def test_a_directory_here_is_a_workspace_whatever_the_machine_says(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.dirs.add(w.ws_dir())
        w.unreachable = "down"
        t, what = self.plan(w)
        self.assertEqual((t.name, what), ("fakebox", "workspace"))

    def test_an_environment_with_no_directory_is_a_workspace(self):
        self.w.containers.add("wk-ws")
        self.assertEqual(self.plan()[1], "workspace")

    def test_only_a_creation_record_is_a_record_on_the_target_that_holds_it(self):
        self.w.begin()
        self.assertEqual(self.plan()[1], "record")
        w = self.make_world(kinds={"fakebox": "container", "box2": "remote"})
        record.of_driver(w.reg.load("box2"), w.clock, w).begin("new", "here", "ws", "wk new ws --kill", "/nolog", ["a"], pid=1)
        t, what = self.plan(w)
        self.assertEqual((t.name, what), ("box2", "record"))

    def test_nothing_anywhere_is_1_and_a_machine_that_did_not_answer_is_2_with_ssh_words(self):
        self.refused(lambda: self.plan(), 1)
        w = self.make_world(kinds={"fakebox": "remote"})
        w.unreachable = "Host key verification failed."
        err = self.refused(lambda: self.plan(w), 2)
        self.assertIn("'ws' has no record here, and fakebox did not answer: Host key verification failed.", err)
        self.assertIn("re-run once fakebox answers.", err)

    def test_a_name_on_two_places_is_refused_by_the_registry(self):
        def two(ws):
            raise LookupError("workspace '%s' exists on places: a b -- this cannot be\n    resolved; remove one, or set WK_PLACE" % ws)
        self.w.reg = FakeRegistry(self.w.env, self.w, self.w.reg.make, names=["fakebox"], ws_place=two)
        self.assertIn("exists on places: a b", self.refused(lambda: self.plan()))


class TestRmOne(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.w.make()
        self.conf = self.w.alias()
        self.clog = self.w.driver.create_log("ws")
        self.w.files[self.clog] = "log\n"
        self.w.begin().end(0)
        self.w.begin("build", pid=99).end(0)

    def rm(self, w=None, what="workspace", name="ws"):
        w = w or self.w
        return workspace.rm_one(w.driver, w.records, w.lock, name, what)

    def gone(self):
        self.assertEqual(self.w.containers, set())
        self.assertNotIn(self.w.ws_dir(), self.w.dirs)
        self.assertNotIn(self.clog, self.w.files)
        self.assertNotIn("Host wk-ws", self.w.files[self.conf])
        self.assertIn("Host other", self.w.files[self.conf])
        self.assertEqual(self.w.records.list(), [])

    def test_a_workspace_is_destroyed_with_its_log_alias_and_records(self):
        rc, err = self.stderr(self.rm)
        self.assertEqual(rc, 0)
        self.gone()
        self.assertEqual([a[:3] for a in self.runs(head="podman") if a[1] in ("rm", "unshare")], [("podman", "rm", "-f"), ("podman", "unshare", "rm")])

    def test_the_record_goes_last(self):
        self.stderr(self.rm)
        tail = [e for e in self.w.effects if e[0] in ("write", "remove")][-4:]
        self.assertEqual([e[0] for e in tail], ["write", "remove", "remove", "remove"])
        self.assertEqual(tail[0][1], self.conf)
        self.assertEqual(tail[1][1], self.clog)
        self.assertTrue(tail[2][1].startswith(str(self.w.tmp / "store" / "task")))

    def test_a_record_with_nothing_left_is_forgotten(self):
        self.w.containers.clear()
        self.w._rm_rf(["rm", self.w.ws_dir()], self.w)
        rc, err = self.stderr(lambda: self.rm(what="record"))
        self.assertEqual(rc, 0)
        self.assertIn("'ws' had nothing left but its record; forgotten", err)
        self.gone()
        self.assertEqual(self.runs(head="podman"), [])

    def test_a_running_job_refuses_before_any_lock_or_effect(self):
        self.w.begin("build", pid=4242)
        self.w.pids.add(4242)
        err = self.refused(self.rm)
        self.assertIn("'ws' has work running in it, and destroying it under a running job\n    leaves that job compiling into a directory that is gone:\n"
                      "    build (pid 4242 on here)  stop it:  wk build ws --kill", err)
        self.assertIn("wk-ws", self.w.containers)
        self.assertEqual(self.lock_takes(), [])

    def test_a_local_target_refuses_naming_the_host(self):
        w = self.make_world(kinds={"fakebox": "local"})
        self.assertIn("a workspace cannot destroy itself -- run 'wk rm ws' on the host", self.refused(lambda: self.rm(w)))

    def test_modified_files_in_the_overlay_are_counted_in_a_warning(self):
        for f in ("a.cpp", "b/c with a space.h"):
            self.w.files[os.path.join(self.w.ws_dir(), "changes", f)] = "x"
        _, err = self.stderr(self.rm)
        self.assertIn("ws has 2 modified file(s) in its overlay", err)

    def test_a_remote_checkout_is_destroyed_without_asking_it_for_a_process(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.make()
        _, err = self.stderr(lambda: self.rm(w))
        self.assertEqual([], [e[1] for e in w.effects if e[0] == "run" and e[1][:3] == ("exec", "ws", "kill")])
        self.assertNotIn("remote-control", err)

    def test_what_a_destroy_leaves_is_named_and_the_records_stay(self):
        self.w.react(["podman", "rm", "-f"], lambda a, f: Result(0))
        rc, err = self.stderr(self.rm)
        self.assertEqual(rc, 1)
        self.assertIn("'ws' was not fully destroyed; still here: the fakebox environment", err)
        self.assertIn("re-run 'wk rm ws' -- what is left is exactly what it will find and retry", err)
        self.assertEqual(len(self.w.records.list()), 2)
        self.assertIn("Host wk-ws", self.w.files[self.conf])



class TestRmNames(WorkspaceTest):
    def names(self, *names, w=None):
        w = w or self.w
        return workspace.rm_names(w.reg, w.records, list(names))

    def test_the_question_names_each_one_with_its_target_once_and_no_terminal_declines(self):
        self.w.make("a")
        self.w.make("b")
        err = self.refused(lambda: self.names("a", "b"))
        self.assertEqual(err.count("destroy them?"), 1)
        self.assertIn("these 2 workspace(s), and every change in them, go:\n    a@fakebox\n    b@fakebox\ndestroy them?", err)
        self.assertIn("aborted", err)
        self.assertEqual(self.w.containers, {"wk-a", "wk-b"})

    def test_a_name_that_is_not_there_is_said_and_the_rest_of_the_batch_still_goes(self):
        os.environ["WK_YES"] = "1"
        self.w.make("keep-not")
        rc, err = self.stderr(lambda: self.names("keep-not", "nope"))
        self.assertEqual(rc, 1)
        self.assertIn("no such workspace: nope", err)
        self.assertIn("workspace 'keep-not' destroyed", err)
        self.assertEqual(self.w.containers, set())
        self.assertIn("invalid name '-x'", self.refused(lambda: self.names("-x")))

    def test_nothing_found_asks_nobody(self):
        rc, err = self.stderr(lambda: self.names("nope"))
        self.assertEqual(rc, 1)
        self.assertNotIn("destroy them?", err)

    def test_an_unanswering_machine_is_1_without_the_no_such_line(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.unreachable = "Host key verification failed."
        rc, err = self.stderr(lambda: self.names("ws", w=w))
        self.assertEqual(rc, 1)
        self.assertIn("fakebox did not answer: Host key verification failed.", err)
        self.assertNotIn("no such workspace", err)

    def test_one_refusal_does_not_end_the_batch_and_the_worst_status_is_returned(self):
        os.environ["WK_YES"] = "1"
        self.w.make("a")
        self.w.make("b")
        self.w.begin("build", "a", pid=4242)
        self.w.pids.add(4242)
        rc, err = self.stderr(lambda: self.names("a", "b"))
        self.assertEqual(rc, 1)
        self.assertIn("'a' has work running in it", err)
        self.assertIn("workspace 'b' destroyed", err)
        self.assertEqual(self.w.containers, {"wk-a"})
        self.assertEqual(self.w.lock_files(), [])


class TestRmAll(WorkspaceTest):
    def listing(self, *rows):
        doc = {"workspaces": [{"place": t, "name": n} for t, n in rows]}
        self.w.answer([WK, "ls", "--json"], out=json.dumps(doc))

    def test_the_rows_are_wk_ls_s_with_this_machines_label_stripped(self):
        self.listing(("fakebox", "alpha"), ("here:box2", "beta"), ("box3:extra", "gamma"), ("", "nameless"))
        self.assertEqual(workspace.all_workspaces(self.w.reg, self.w.records), [("fakebox", "alpha"), ("box2", "beta"), ("box3", "gamma")])
        self.w.answer([WK, "ls", "--json"], rc=1)
        self.assertIn("'wk ls --json' did not answer with the workspaces to destroy", self.refused(lambda: workspace.rm_all(self.w.reg, self.w.records)))

    def test_a_dry_run_names_each_one_and_the_removal_it_would_run(self):
        self.dry_run()
        self.listing(("fakebox", "alpha"), ("fakebox", "beta"))
        rc, err = self.stderr(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual(rc, 0)
        self.assertIn("would ask:", err)
        self.assertIn("    alpha@fakebox\n    beta@fakebox\ndestroy them?", err)
        self.assertIn("would run: env WK_PLACE=fakebox %s rm alpha --yes" % WK, err)
        self.assertIn("rm beta --yes", err)

    def test_with_nothing_to_destroy_it_says_so_and_asks_nobody(self):
        self.listing()
        rc, err = self.stderr(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual(rc, 0)
        self.assertIn("no workspaces -- nothing to destroy", err)
        self.assertNotIn("destroy them?", err)

    def test_each_removal_goes_through_its_places_own_wk_rm_and_the_worst_status_wins(self):
        os.environ["WK_YES"] = "1"
        self.listing(("fakebox", "alpha"), ("box2", "beta"))
        self.w.answer(["env", "WK_PLACE=fakebox"])
        self.w.answer(["env", "WK_PLACE=box2"], rc=1)
        rc, _ = self.stderr(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual(rc, 1)
        calls = [list(e[1]) for e in self.w.effects if e[0] == "run" and e[1][0] == "env"]
        self.assertEqual(calls, [["env", "WK_PLACE=fakebox", WK, "rm", "alpha", "--yes"], ["env", "WK_PLACE=box2", WK, "rm", "beta", "--yes"]])

    def test_declining_destroys_nothing(self):
        self.listing(("fakebox", "alpha"))
        self.refused(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual([], [e for e in self.w.effects if e[0] == "run" and e[1][0] == "env"])


class Recording(World):
    """Records every act_run as ("act", argv), dry or wet."""

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def mutations(self):
        return [self.rel(e) for e in self.work() if e[0] in ("act", "write", "mkdir", "remove", "kill", "spawn")]


class TestKillPoints(WorkspaceTest):
    def test_new_over_each_real_driver_killed_after_any_effect_and_rerun_converges(self):
        from wk.sysimage import guestbase
        from wk.store import Store

        def run_once(w):
            w.lock = Lock(w.driver.store, w, w.clock)   # each run is its own process: nothing the killed one held survives it
            with contextlib.redirect_stderr(io.StringIO()):
                self.detached(w)
        for kind in ("container", "vm"):
            with self.subTest(driver=kind), contextlib.ExitStack() as guest_host:
                if kind == "vm":
                    guest_host.enter_context(mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True))
                    guest_host.enter_context(mock.patch.object(guestbase.Base, "ensure", return_value=None))
                    guest_host.enter_context(mock.patch.object(guestbase.Base, "stale", return_value=""))
                converges(self, lambda: World(self.tmp, kinds={"fakebox": kind}), run_once, World.state, max_effects=80)
                w = World(self.tmp, kinds={"fakebox": kind})
                run_once(w)
                self.assertEqual(w.driver.state("ws"), "present")

    def rm_world(self, cls=World):
        w = cls(self.tmp)
        w.make()
        w.alias()
        w.files[w.driver.create_log("ws")] = "log\n"
        w.files[os.path.join(w.ws_dir(), "changes", "a.cpp")] = "x"
        w.begin().end(0)
        w.begin("build", pid=99).end(0)
        return w

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet = Recording(self.tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            self.detached(wet)
        dry = Recording(self.tmp)
        before = dry.state()
        self.dry_run()
        with contextlib.redirect_stderr(io.StringIO()):
            self.detached(dry)
        self.assertEqual(dry.mutations(), wet.mutations())
        self.assertGreaterEqual(len(dry.mutations()), 9)
        self.assertEqual(dry.state(), before)
        self.assertEqual(dry.pids, {os.getpid()})
        self.assertEqual(dry.clock.slept, [])

    def test_a_dry_run_of_rm_is_the_wet_runs_plan_and_touches_nothing(self):
        os.environ["WK_YES"] = "1"
        wet = self.rm_world(Recording)
        with contextlib.redirect_stderr(io.StringIO()):
            workspace.rm_names(wet.reg, wet.records, ["ws"])
        dry = self.rm_world(Recording)
        before = dry.state()
        self.dry_run()
        with contextlib.redirect_stderr(io.StringIO()):
            workspace.rm_names(dry.reg, dry.records, ["ws"])
        self.assertEqual(dry.mutations(), wet.mutations())
        self.assertIn(("act", ("podman", "rm", "-f", "wk-ws")), dry.mutations())
        self.assertEqual(dry.state(), before)

    def test_a_dry_run_of_the_front_detaches_nothing(self):
        self.dry_run()
        rc, err = self.stderr(lambda: self.front())
        self.assertEqual(rc, 0)
        self.assertEqual([e for e in self.w.effects if e[0] == "spawn"], [])
        self.assertRegex(err, r"would run: .*/wkdev-create .*--name wk-ws")
        self.assertNotIn("detached as pid", err)
        self.assertEqual(self.w.records.list(), [])


if __name__ == "__main__":
    unittest.main()
