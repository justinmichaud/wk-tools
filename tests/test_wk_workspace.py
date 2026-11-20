"""lib/wk/workspace.py: `wk new` and `wk rm` as flows over a fake place on a fake machine."""
import io
import json
import os
import shlex
import shutil
import signal
import sys
import time
import unittest
from unittest import mock

from tests import fakes
from tests.fakes import FakeRegistry
from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE

sys.path.insert(0, str(REPO / "lib"))
from wk import act, places, pr, record, repos, secrets, workspace  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Result  # noqa: E402
from wk.store import Store  # noqa: E402

WK = str(REPO / "wk")
CHECKOUT = "branch=main\nhead=abc1234\ntracks=origin/main\nbehind=0\n"


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


DRIVERS = {"container": places.Container, "vm": places.Vm, "remote": places.Remote, "local": places.LocalWorkspace, "peer": places.Peer}
FAR_ROOT = "/home/u/wk"


class World(fakes.World):
    """This host with a place per `kinds` entry, each the real driver: podman answers from `containers` (wkdev-create
    writes the ready marker), tart from `vms`, a build box's ssh from `far` (its workspace directories) or fails
    with `unreachable`; inside workspace `ws` (local) its marker names it. The store holds a mirror and one
    snapshot, `main-1`."""

    def __init__(self, tmp, kinds=None):
        super().__init__(tmp, WK_IN_VM="1", WK_PLACE="fakebox")
        self.containers, self.vms, self.far, self.unreachable = set(), {}, set(), None
        self.probe = "yes"
        self.checkout, self.merged = Result(0, CHECKOUT), Result(0)
        self.bash = {}
        self.acted = set()
        self.answer([WK, "sync"], out="fetched\n")
        self.react(["podman", "container", "exists"], lambda a, f: Result(0 if a[-1] in f.containers else 1))
        self.react(["podman", "inspect"], lambda a, f: Result(0, "running\n") if a[2] in f.containers else Result(125, "", "no such container"))
        self.react(["podman", "rm", "-f"], self._podman_rm)
        self.react(["podman", "unshare", "rm", "-rf"], self._rm_rf)
        self.react(["env"], self._sdk)
        self.answer(["bash", os.path.join(str(REPO), "container", "sdk-refresh.sh")])
        self.answer(["bash", "-lc"])   # the agents cache, filled from the store's side
        for argv in ([WK, "zed"], ["install", "-m"], ["podman", "image", "exists"]):
            self.answer(argv)
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
            return Result(0, "2.55-v1-abc\n")
        return self._in_ws(["exec", argv[argv.index("--name") + 1][3:]] + list(argv[argv.index("--") + 2:]), f)

    def _find(self, argv, f):
        p = argv[1]
        return Result(0, "".join(k + "\n" for k in f.files if k.startswith(p + "/")))

    def _bash(self, argv, f):
        for key, r in f.bash.items():
            if key in argv[2]:
                return r(argv) if callable(r) else r
        return Result(127, "", "no bash answer for: %s" % argv[2][-60:])

    def _in_ws(self, argv, f):
        if argv[2:4] == ["kill", "-0"]:
            return Result(0 if int(argv[4]) in f.pids else 1)
        if "echo yes || echo no" in argv[-1]:
            return Result(0, f.probe + "\n") if f.probe is not None else Result(1, "", "no such container")
        return f.merged if "merge" in argv else f.checkout

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

    def records_on(self, place):
        return record.of_driver(self.reg.load(place), self.clock, self)

    def begin(self, kind="new", name="ws", pid=4242, **kw):
        args = dict(kind=kind, where="here", name=name, kill="wk %s %s --kill" % (kind, name), log="/nolog", plan=["a"], pid=pid)
        args.update(kw)
        return self.records.begin(**args)

    def lock_files(self):
        return sorted(p for p in self.files if p.startswith(self.env["WK_LOCK_DIR"]))

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


class WorkspaceTest(fakes.FlowTest):
    world_class = World

    def setUp(self):
        super().setUp()
        p = mock.patch.object(sys, "stdin", io.StringIO(""))
        p.start()
        self.addCleanup(p.stop)
        self.w = self.make_world()

    def make_world(self, **kw):
        if "vm" in (kw.get("kinds") or {}).values():
            mac = mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True)
            mac.start()
            self.addCleanup(mac.stop)
        return self.world_class(self.tmp, **kw)

    def front(self, w=None, name="ws", **opts):
        w = w or self.w
        return workspace.new_front(w.reg, w.records, name, opts, pr)

    def detached(self, w=None, name="ws", base="", arch="native", driver=None, repo=None):
        w = w or self.w
        return workspace.new_detached_run(driver or w.driver, w.records, w.lock, w.clock, name, base, arch, repo or repos.default())

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


NEW_PLAN = ["checking", "wipe", "base", "create", "init", "agents", "fetch", "register"]


class Flow(WorkspaceTest):
    """`wk <argv>` typed into a world: (exit status, stderr), with that world's effects only the command's."""

    def make_world(self, **kw):
        return self.ahead(super().make_world(**kw))

    @staticmethod
    def ahead(w):
        """A record the world writes is after the front's floor, so a wait ends on its first look.
        TODO: cmd/new and cmd/rm build Records on the real clock, not the dispatcher's; then this goes, and the waits'
        sleeps (WK_NEW_TIMEOUT, WK_KILL_WAIT, WK_READY_WAIT) are the world's to count."""
        w.clock.t = time.time() + 60
        return w

    def typed(self, *argv, w=None):
        w = w or self.w
        w.effects = []
        for v in self.UNSET:   # the dispatcher exports --yes and --dry-run into this process
            os.environ.pop(v, None)
        rc, _, err = self.wk(w, *argv)
        return rc, err

    def new(self, *argv, w=None):
        return self.typed("new", "ws", *argv, w=w)

    def detach(self, *argv, w=None):
        """The detached half, as the front spawns it."""
        return self.new("--_detached", *argv, w=w)

    def spawned(self, w=None):
        return [e for e in (w or self.w).effects if e[0] == "spawn"]

    def created(self, w=None):
        return [a for a in self.runs(w) if step(a) == "wkdev-create"]


def said(err):
    """The line a person acts on: a refusal's first line, else the last thing said."""
    lines = err.splitlines()
    return next((x for x in lines if x.startswith("error: ")), lines[-1] if lines else "")


class TestNewRefusals(Flow):
    def test_an_invalid_name_is_refused_before_anything_else(self):
        for bad in ("", "a b", "a/b"):
            rc, err = self.typed("new", bad)
            self.assertEqual((rc, said(err), self.w.effects), (1, "error: invalid name '%s': use [a-zA-Z0-9._-], not starting with '-'" % bad, []))

    def test_pr_needs_a_spec_and_a_valid_one_and_no_no_wait(self):
        for argv, line in ((["--pr", ""], "error: --pr needs a spec: <user>:<branch>, <n>, or wpe:<n>"),
                           (["--pr", "nope"], "error: 'nope' is not a PR spec: <user>:<branch>, a pull request number, or wpe:<number>"),
                           (["--pr", "u:b", "--no-wait"], "error: --pr needs the workspace to be ready, and --no-wait returns before it is.")):
            rc, err = self.new(*argv)
            self.assertEqual((rc, said(err), self.spawned()), (1, line, []), argv)

    def test_an_unknown_arch_or_place_is_refused(self):
        for argv, line in ((["--arch", "sparc"], "error: unknown architecture 'sparc'"), (["--on", "nosuch"], "error: unknown place 'nosuch'.")):
            rc, err = self.new(*argv)
            self.assertEqual((rc, said(err).split(" (")[0].split(":  ")[0]), (1, line), argv)

    def test_a_non_native_arch_is_container_only(self):
        self.assertEqual(self.new("--arch", "armhf", "--no-wait")[0], 0)
        rc, err = self.new("--arch", "armhf", w=self.make_world(kinds={"fakebox": "vm"}))
        self.assertEqual((rc, said(err)), (1, "error: --arch armhf is a container-only capability (this is place 'fakebox')."))

    def test_inside_a_workspace_it_refuses_naming_the_host_command(self):
        w = self.make_world(kinds={"fakebox": "local"})
        rc, err = self.typed("new", w=w)
        self.assertEqual((rc, said(err), w.effects),
                         (1, "error: a workspace cannot create a workspace -- run 'wk new ws' on the host", []))

    def test_a_present_or_broken_workspace_is_refused_at_once(self):
        self.w.make()
        rc, err = self.new()
        self.assertEqual((rc, said(err)), (1, "error: workspace 'ws' already exists (place 'fakebox')."))
        self.w.containers.clear()
        rc, err = self.new()
        self.assertEqual((rc, said(err), self.spawned()), (1, "error: 'ws' is a record without an environment: creation finished, and the", []))

    def test_a_missing_mirror_refuses_naming_wk_sync_before_anything_runs(self):
        """`unit new.refuses_without_mirror`: refused before the detach and the `wk sync --mirror` that would clone it."""
        for kind in ("container", "vm"):
            w = self.make_world(kinds={"fakebox": kind})
            w.dirs.discard(w.driver.store.mirror_dir())
            rc, err = self.new(w=w)
            self.assertEqual((rc, said(err), self.runs(w), self.spawned(w)), (1, "error: no WebKit mirror at %s:" % w.driver.store.mirror_dir(), [], []), kind)

    def test_a_name_alive_on_another_machine_is_refused_naming_it(self):
        """`unit status.duplicate_name`: refused before any lock, record or detach; a machine that does not answer warns."""
        w = self.make_world(kinds={"fakebox": "container", "box2": "remote"})
        w.far.add("%s/ws/ws" % FAR_ROOT)
        rc, err = self.new(w=w)
        self.assertEqual((rc, said(err), [e for e in w.effects if e[0] in ("spawn", "write", "symlink")]),
                         (1, "error: workspace 'ws' already exists (place 'box2').", []))
        w.unreachable = "down"
        rc, err = self.new("--no-wait", w=w)
        self.assertEqual(rc, 0)
        self.assertEqual(said(err.splitlines()[0]), "warning: could not ask box2 over ssh: down -- what is there is not in this answer")

    def test_a_creation_already_running_is_refused_with_its_pid_and_its_log(self):
        self.w.make(marker=False, base=False)
        self.w.begin(plan=NEW_PLAN, log="/tmp/new-ws.log").step_named("create")
        self.w.pids.add(4242)
        rc, err = self.new()
        self.assertEqual((rc, said(err), self.spawned()), (1, "error: 'ws' is already being created (pid 4242, at stage", []))
        self.assertIn("    'create'). Follow it:      tail -f /tmp/new-ws.log", err.splitlines())

    def test_a_creation_nobody_is_running_is_started_again(self):
        self.w.make(marker=False, base=False)
        self.w.begin(plan=NEW_PLAN)
        rc, err = self.new("--no-wait")
        self.assertEqual((rc, len(self.spawned())), (0, 1))


class TestNewDetaches(Flow):
    def test_the_detached_half_is_spawned_with_the_canonical_flags_into_a_fresh_log_after_the_store(self):
        rc, err = self.new("--no-wait", "--base", "main-2", "--arch", "arm", "--zed")
        log = self.w.driver.create_log("ws")
        self.assertEqual((rc, self.spawned()), (0, [("spawn", (WK, "new", "ws", "--on", "fakebox", "--arch", "armhf", "--repo", "webkit",
                                                                "--base", "main-2", "--_detached"), log)]))
        made = [e for e in self.w.effects if e[0] in ("mkdir", "write", "spawn")]
        self.assertEqual((made[0], made[-3:]), (("mkdir", self.w.env["WK_STORE"]), [("mkdir", os.path.dirname(log)), ("write", log), self.spawned()[0]]))
        self.assertEqual(said(err), "  open it:  wk enter ws --zed   (waits for it to be ready)")

    def ended(self, status, w=None):
        w = w or self.w
        w.begin(plan=NEW_PLAN, log=w.driver.create_log("ws")).end(status)

    def test_the_wait_ends_on_the_detached_halfs_verdict(self):
        self.ended(0)
        rc, err = self.new()
        self.assertEqual((rc, err.splitlines()[0]), (0, "==> workspace 'ws' ready"))
        for status, line in (("refused", "error: 'ws' was not created, and nothing was left half-made: the reason is"),
                             (1, "error: creating 'ws' failed (failed) -- the reason is above, in full in 'wk status ws --log --all'."),
                             ("cancelled", "error: creating 'ws' failed (cancelled) -- the reason is above, in full in 'wk status ws --log --all'.")):
            w = self.make_world()
            self.ended(status, w)
            rc, err = self.new(w=w)
            self.assertEqual((rc, said(err)), (1, line), status)

    def test_a_detached_half_that_never_writes_a_record_is_reported_crashed(self):
        class DiesAtOnce(World):
            def spawn(self, argv, log):
                pid = super().spawn(argv, log)
                self.pids.discard(pid)
                return pid
        rc, err = self.new(w=self.ahead(DiesAtOnce(self.tmp)))
        self.assertEqual((rc, said(err)), (1, "error: the process creating 'ws' is gone without having said how it ended."))

    def test_the_wait_gives_up_after_wk_new_timeout_and_undoes_nothing(self):
        self.w.env["WK_NEW_TIMEOUT"] = "1"
        rc, err = self.new()
        self.assertEqual((rc, said(err)), (1, "error: 'ws' is still being created after 1s. Nothing here was undone."))
        self.assertEqual([e for e in self.w.effects if e[0] == "kill"], [])
        self.assertIn(1001, self.w.pids)

    def test_the_log_is_streamed_while_waiting(self):
        class Says(World):
            def spawn(self, argv, log):
                pid = super().spawn(argv, log)
                self.files[log] = "driver says hello\n"
                return pid
        w = self.ahead(Says(self.tmp))
        self.ended(0, w)
        self.assertEqual(self.new(w=w)[1].splitlines()[0], "driver says hello")


class Creates(World):
    """A host whose spawned detached run makes the workspace at once and records it done."""

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        self.make(argv[2])
        self.begin(name=argv[2], plan=NEW_PLAN, log=log, pid=pid).end(0)
        return pid


class TestNewOnceReady(Flow):
    world_class = Creates

    def test_the_hints_follow_readiness_and_nothing_more_runs(self):
        rc, err = self.new()
        self.assertEqual((rc, said(err), self.runs(head=WK) + self.runs(head="bash") + self.runs(head="exec")),
                         (0, "  wk zed ws            the checkout, in Zed", []))

    def test_a_pr_is_checked_out_once_ready_and_its_refusal_is_the_commands(self):
        rc, err = self.new("--pr", "u:b")
        self.assertEqual((rc, said(err)), (1, "error: no branch 'b' in WebKit/WPEWebKit under 'u'."))
        w = self.make_world()
        w.answer(["git", "ls-remote", "https://github.com/u/WebKit"], out="b" * 40 + "\trefs/heads/b\n")
        rc, err = self.new("--pr", "u:b", w=w)
        fetch = [a for a in self.runs(w) if "fetch" in a]
        self.assertEqual((rc, said(err), len(fetch)), (1, "error: cannot tell whether 'b' in 'ws' has work the PR head does not.", 1))

    def test_zed_opens_the_checkout_and_its_failure_only_warns(self):
        rc, err = self.new("--zed")
        self.assertEqual((rc, [r for r in self.runs() if r[:2] == (WK, "zed")]), (0, [(WK, "zed", "ws")]))
        w = self.make_world()
        w.answer([WK, "zed"], rc=1)
        rc, err = self.new("--zed", w=w)
        self.assertEqual((rc, said(err)), (0, "warning: 'ws' is there; opening it in Zed is what failed (above) -- 'wk zed ws' retries"))


class Peers(World):
    """A peer workstation at `box.example` beside the container place: its own wk answers `ls --json` with nothing and
    makes what its `wk new` is asked."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, kinds={"fakebox": "container", "peer": "peer"})

    def _peer(self):
        self._remote()

    def _ssh(self, argv, f):
        text = argv[-1]
        if "test -x " in text or "/wk new " in text:
            return Result(0)
        if "/wk ls --json" in text:
            return Result(0, '{"workspaces": []}')
        return super()._ssh(argv, f)


class TestNewOnAPeer(Flow):
    world_class = Peers

    def handed(self):
        return [(e[0], e[1][-1].split(" && ")[-1]) for e in self.w.effects if e[0] in ("act", "run_tty")]

    def test_the_peers_own_wk_new_is_run_on_a_terminal_with_what_was_asked(self):
        rc, err = self.new("--on", "peer", "--pr", "123")
        line = "WK_ROW_LABEL=peer %s/tools/wk new ws --pr 123'" % FAR_ROOT
        self.assertEqual((rc, self.handed()), (0, [("act", line), ("run_tty", line)]))

    def test_a_dry_run_prints_the_hand_over_and_runs_none(self):
        rc, err = self.new("--on", "peer", "--dry-run")
        self.assertEqual((rc, [e for e in self.handed() if e[0] == "run_tty"]), (0, []))
        self.assertRegex(err, r"^would run: ssh .* box.example .*WK_DRY_RUN=1 WK_ROW_LABEL=peer \S+/wk new ws")

    def test_a_base_is_refused(self):
        rc, err = self.new("--on", "peer", "--base", "20260101-000000")
        self.assertEqual((rc, said(err), self.handed()), (1, "error: --base names a snapshot in this machine's store, and peer makes 'ws' from its own:", []))


class TestNewKill(Flow):
    def test_kill_takes_no_other_flag(self):
        rc, err = self.new("--kill", "--no-wait")
        self.assertEqual((rc, said(err)), (1, "error: 'wk new ws --kill' stops the creation already running and takes"))

    def test_nothing_running_is_said_and_is_not_a_failure(self):
        self.assertEqual(self.new("--kill"), (0, "no new is running in 'ws' -- 'wk status ws' says what it last did\n"))
        self.w.begin().end(0)
        self.assertEqual(self.new("--kill")[0], 0)

    def test_a_running_creation_gets_term_and_its_record_ends_cancelled(self):
        self.w.begin()
        self.w.pids.add(4242)
        rc, err = self.new("--kill")
        self.assertEqual((rc, [e for e in self.w.effects if e[0] == "kill"], said(err)),
                         (0, [("kill", 4242, int(signal.SIGTERM))], "  keeping:  wk rm ws    (then 'wk new ws' to start again)"))
        self.assertEqual(self.w.state()[3], [("new", "cancelled")])

    def test_one_that_outlives_term_and_kill_is_named_with_ps(self):
        class Immortal(World):
            def kill(self, pid, sig=signal.SIGTERM):
                self.effect(("kill", pid, int(sig)))
                return True
        w = self.ahead(Immortal(self.tmp))
        w.env["WK_KILL_WAIT"] = "0"
        w.begin()
        w.pids.add(4242)
        rc, err = self.new("--kill", w=w)
        self.assertEqual((rc, said(err)), (1, "error: the process creating 'ws' outlived a TERM and a KILL. It holds the workspace lock:"))
        self.assertEqual([e for e in w.effects if e[0] == "kill"], [("kill", 4242, int(signal.SIGTERM)), ("kill", 4242, int(signal.SIGKILL))])
        self.assertIn("        ps -p 4242", err.splitlines())
        self.assertEqual(w.state()[3], [("new", "cancelled")])

    def test_a_dry_run_signals_nothing_and_leaves_the_record_running(self):
        self.w.begin()
        self.w.pids.add(4242)
        rc, err = self.new("--kill", "--dry-run")
        self.assertEqual((rc, [e for e in self.w.effects if e[0] == "kill"], self.w.state()[3]), (0, [], [("new", "")]))
        self.assertIn("dry run -- would TERM pid 4242, KILL it after 15s, and record it cancelled", err)


if __name__ == "__main__":
    unittest.main()


class TestNewDetachedHalf(Flow):
    def test_the_steps_land_in_order_and_the_record_ends_ok_with_no_lock_held(self):
        rc, err = self.detach()
        heads = [step(e[1]) for e in self.w.work() if e[0] == "act" and step(e[1]) in ("sdk-refresh", "wkdev-create", WK)]
        self.assertEqual((rc, said(err), heads), (0, "==> workspace 'ws' created", ["sdk-refresh", WK, "wkdev-create", WK]))
        self.assertEqual((self.w.files[os.path.join(self.w.ws_dir(), "base-id")], self.w.containers), ("main-1\n", {"wk-ws"}))
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), [s for _, s in t.steps()], t.field("kill")), ("ok", ["done"] * 7 + ["running"], "wk new ws --kill"))
        self.assertEqual(self.w.lock_files(), [])

    def test_the_agents_are_installed_once_it_is_up_and_before_the_fetch(self):
        self.detach()
        execs = [a[-1] for a in self.runs(head="exec")]
        order = [next(i for i, c in enumerate(execs) if what in c) for what in ("claude.ai/install.sh", "claude/workspace-config.py", "echo yes || echo no")]
        self.assertEqual(order, sorted(order))

    def test_an_agent_install_that_fails_fails_the_creation_and_a_rerun_remakes_it(self):
        self.w._in_ws = lambda argv, f: Result(1, "claude=failed\n") if "claude.ai/install.sh" in argv[-1] else World._in_ws(f, argv, f)
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.w.state()[3]), (1, "error: could not install the coding agents in 'ws' (claude=failed pi=?).", [("new", "1")]))
        del self.w._in_ws
        rc, err = self.detach()
        self.assertEqual((rc, "warning: 'ws' exists but was never finished -- destroying it and starting again" in err.splitlines()), (0, True))

    def test_the_sdk_is_refreshed_under_its_lock_alone_and_only_for_a_container(self):
        self.detach()
        self.assertEqual(self.lock_takes(heads=("sdk-refresh", "wkdev-create")), ["lock sdk", "sdk-refresh", "lock holds", "lock store", "wkdev-create"])
        for kind in ("vm", "remote"):
            w = self.make_world(kinds={"fakebox": kind})
            self.detach(w=w)
            self.assertEqual(self.lock_takes(w, heads=("sdk-refresh",))[0], "lock holds", kind)

    def test_the_mirror_is_refreshed_before_the_store_lock_and_only_where_the_checkout_reads_it(self):
        self.detach()
        self.assertEqual(self.lock_takes(heads=(WK, "wkdev-create")), ["lock sdk", "lock holds", WK, "lock store", "wkdev-create", WK])
        self.assertEqual(self.runs(head=WK)[0], (WK, "sync", "--mirror"))
        for kind, want in (("vm", [(WK, "sync", "--mirror")]), ("remote", [])):
            w = self.make_world(kinds={"fakebox": kind})
            self.detach(w=w)
            self.assertEqual([r for r in self.runs(w, WK) if "--mirror" in r], want, kind)

    def test_a_mirror_refresh_that_failed_refuses_the_creation_naming_the_remedy(self):
        self.w.answer([WK, "sync", "--mirror"], rc=1, err="no request broker\n")
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.w.containers), (1, "error: the mirror refresh did not finish (above), so 'ws' was not created.", set()))

    def test_a_present_broken_or_unreachable_workspace_is_refused_and_the_record_says_refused(self):
        present, broken, far = self.make_world(), self.make_world(), self.make_world(kinds={"fakebox": "remote"})
        present.make()
        broken.make()
        broken.containers.clear()
        far.unreachable = "Connection refused"
        for w, line in ((present, "error: workspace 'ws' already exists (place 'fakebox')."),
                        (broken, "error: 'ws' is a record without an environment: creation finished, and the"),
                        (far, "error: cannot reach the machine behind place 'fakebox', so whether 'ws' is")):
            rc, err = self.detach(w=w)
            (t,) = w.records.list()
            self.assertEqual((rc, said(err), t.verdict(), t.stage(), self.created(w), w.lock_files()), (1, line, "refused", ["checking"], [], []))

    def test_a_half_made_workspace_is_wiped_and_remade(self):
        self.w.make(marker=False, base=False)
        conf = self.w.alias()
        rc, err = self.detach()
        heads = [step(a) if step(a) == "wkdev-create" else a[:3] for a in self.runs()
                 if step(a) == "wkdev-create" or a[:2] in (("podman", "rm"), ("podman", "unshare"))]
        self.assertEqual((rc, "warning: 'ws' exists but was never finished -- destroying it and starting again" in err.splitlines()), (0, True))
        self.assertEqual(heads, [("podman", "rm", "-f"), ("podman", "unshare", "rm"), "wkdev-create"])
        self.assertEqual((self.w.files[conf], self.w.state()[3]), ("Host other\n    HostName o\n", [("new", "0")]))

    def test_a_half_made_workspace_the_destroy_leaves_is_a_refusal_not_a_create_over_it(self):
        self.w.make(marker=False, base=False)
        self.w.react(["podman", "rm", "-f"], lambda a, f: Result(0))
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.created()), (1, "error: could not destroy the half-made workspace 'ws'; still here: the fakebox environment", []))
        self.assertEqual((self.w.records.list()[0].verdict(), self.w.lock_files()), ("failed", []))

    def test_a_creation_that_died_after_the_marker_is_remade_and_a_finished_one_is_refused(self):
        self.w.make()
        self.w.begin(plan=NEW_PLAN)
        rc, err = self.detach()
        self.assertEqual((rc, "warning: 'ws' exists but was never finished -- destroying it and starting again" in err.splitlines(), self.w.state()[3]), (0, True, [("new", "0")]))
        w = self.make_world()
        w.make()
        w.begin(plan=NEW_PLAN).end(0)
        self.assertEqual(said(self.detach(w=w)[1]), "error: workspace 'ws' already exists (place 'fakebox').")

    def test_a_creation_still_running_is_not_remade(self):
        self.w.make()
        self.w.begin(plan=NEW_PLAN)
        self.w.pids.add(4242)
        rc, err = self.new()
        self.assertEqual((rc, said(err), self.spawned()), (1, "error: workspace 'ws' already exists (place 'fakebox').", []))

    def test_no_base_snapshot_refuses_naming_wk_sync_and_creates_nothing(self):
        self.w.publish("main-1", on_branch=False)
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.created(), self.w.ws_dir() in self.w.dirs), (1, "error: no snapshot this machine can build a workspace from:  wk sync", [], False))
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage(), self.w.lock_files()), ("failed", ["base"], []))

    def test_a_given_base_is_verified_and_its_refusal_is_the_reason_and_a_guest_reads_no_git(self):
        self.assertEqual(said(self.detach("--base", "main-9")[1]), "error: snapshot main-9 does not exist")
        self.w.publish("main-9", on_branch=False)
        self.assertEqual(said(self.detach("--base", "main-9")[1]), "error: snapshot main-9 is not on branch main tracking origin/main (HEAD is")
        w = self.make_world(kinds={"fakebox": "vm"})
        self.detach(w=w)
        self.assertEqual(self.runs(w, head="git"), [])

    def test_a_workspace_that_never_initialises_is_refused_after_wk_ready_wait(self):
        def no_firstrun(argv, f):
            f.containers.add(argv[argv.index("--name") + 1])
            return Result(0)
        self.w.wkdev_create = no_firstrun
        self.w.env["WK_READY_WAIT"] = "1"
        rc, err = self.detach()
        self.assertEqual((rc, said(err)), (1, "error: 'ws' was created but never finished initialising (above):"))
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["init"]))

    def test_a_create_that_fails_ends_the_record_with_its_status(self):
        self.w.wkdev_create = lambda a, f: Result(125, "", "podman: boom\n")
        rc, err = self.detach()
        self.assertEqual((rc, said(err), self.w.state()[3]), (125, "error: wkdev-create failed for 'ws' (exit 125); what it said is above", [("new", "125")]))


class TestNewFetches(Flow):
    """The detached half's last step: fetch from the mirror in reach and say where the checkout is."""

    def fetched(self, w=None):
        rc, err = self.detach(w=w)
        self.assertEqual(rc, 0, err)
        return [r for r in self.runs(w, WK) if r != (WK, "sync", "--mirror")], self.runs(w, "exec")[-1][-1], err.splitlines()

    def test_a_mirror_in_reach_is_fetched_with_wk_sync(self):
        syncs, last, lines = self.fetched()
        self.assertEqual((syncs, lines[-2]), ([(WK, "sync", "ws")], "==> 'ws' is on main at abc1234, up to date with origin/main"))
        probe = [a[-1] for a in self.runs(head="exec") if "echo yes || echo no" in a[-1]]
        self.assertEqual(probe, ["[ -n {0} ] && [ -d {0} ] && echo yes || echo no".format(self.w.driver.mirror_dir())])

    def test_no_mirror_names_wk_sync_and_still_reads_the_checkout(self):
        self.w.probe = "no"
        syncs, last, lines = self.fetched()
        self.assertEqual((syncs, lines[-2]), ([], "==> 'ws' is on main at abc1234, up to date with origin/main"))
        self.assertIn("  wk sync ws    fetches it over the network", lines)

    def test_a_workspace_nothing_answers_in_is_reported_not_fetched(self):
        self.w.probe = None
        syncs, last, lines = self.fetched()
        self.assertEqual((syncs, "echo yes || echo no" in last, lines[-2]), ([], True, "==> nothing runs in 'ws' yet, so its checkout is fetched in when it starts"))

    def test_a_failed_fetch_warns_and_is_not_fatal(self):
        self.w.answer([WK, "sync", "ws"], rc=1, err="fetch: refused\n")
        syncs, last, lines = self.fetched()
        self.assertEqual((syncs, "warning: 'ws' is there; fetching in it is what failed (above)." in lines), ([(WK, "sync", "ws")], True))

    def test_each_checkout_is_reported_by_where_it_is(self):
        behind = Result(0, "branch=main\ntracks=origin/main\nbehind=2\nhead=def5678\n")
        for out, merged, line in ((Result(0, "branch=\nhead=abc1234\n"), None, "warning: 'ws' is not on a branch (detached at abc1234), so 'git status',"),
                                  (Result(1, "", "gone"), None, "warning: could not read the checkout in 'ws' to say where it is"),
                                  (Result(0, "branch=eng/local\ntracks=\n"), None, "warning: 'ws' is on 'eng/local', which tracks nothing -- 'git pull' and"),
                                  (behind, Result(1), "warning: 'main' in 'ws' and origin/main have diverged, so the checkout is left where"),
                                  (behind, Result(0), "==> 'ws' is on main at def5678, 2 commit(s) on from its snapshot (origin/main)")):
            w = self.make_world()
            w.checkout, w.merged = out, merged
            w._in_ws = lambda a, f: Result(0) if "claude" in a[-1] else World._in_ws(f, a, f)   # the agents install whatever git says
            self.assertIn(line, self.fetched(w)[2], line)

    def test_a_dry_run_names_the_fetch_and_asks_the_workspace_nothing(self):
        rc, err = self.new("--dry-run")
        self.assertEqual((rc, self.runs(head="exec"), self.spawned()), (0, [], []))
        self.assertIn("would run: %s sync ws" % WK, err.splitlines())


class TestRmFinds(Flow):
    """What `wk rm <name>` takes `name` to be, before it destroys anything."""

    def test_a_directory_here_is_a_workspace_whatever_the_machine_says(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.dirs.add(w.ws_dir())
        w.unreachable = "down"
        self.assertEqual(said(self.typed("rm", "ws", "--yes", w=w)[1]), "error: cannot reach 'box.example' over ssh: down")
        w.dirs.discard(w.ws_dir())
        rc, err = self.typed("rm", "ws", "--yes", w=w)
        self.assertEqual((rc, said(err)), (1, "error: 'ws' has no record here, and fakebox did not answer: down"))

    def test_an_environment_with_no_directory_is_a_workspace(self):
        self.w.containers.add("wk-ws")
        rc, err = self.typed("rm", "ws", "--yes")
        self.assertEqual((rc, said(err), self.w.containers), (0, "==> workspace 'ws' destroyed", set()))

    def test_only_a_creation_record_is_a_record_and_it_is_the_place_that_holds_it(self):
        self.w.begin()
        self.assertEqual(self.typed("rm", "ws", "--yes"), (0, "==> 'ws' had nothing left but its record; forgotten\n"))
        w = self.make_world(kinds={"fakebox": "container", "box2": "remote"})
        w.records_on("box2").begin("new", "here", "ws", "wk new ws --kill", "/nolog", ["a"], pid=1)
        self.assertIn("    ws@box2", self.typed("rm", "ws", w=w)[1].splitlines())

    def test_nothing_anywhere_is_no_such_workspace_and_nobody_is_asked(self):
        rc, err = self.typed("rm", "nope")
        self.assertEqual((rc, err), (1, "error: no such workspace: nope -- 'wk ls' lists them\n"))
        self.assertEqual(said(self.typed("rm", "a b")[1]), "error: invalid name 'a b': use [a-zA-Z0-9._-], not starting with '-'")

    def test_a_name_on_two_places_is_refused_by_the_registry(self):
        def two(ws):
            raise LookupError("workspace '%s' exists on places: a b -- this cannot be\n    resolved; remove one, or set WK_PLACE" % ws)
        self.w.reg = FakeRegistry(self.w.env, self.w, self.w.reg.make, names=["fakebox"], ws_place=two)
        rc, err = self.typed("rm", "ws", "--yes")
        self.assertEqual((rc, said(err), self.w.mutations()), (1, "error: workspace 'ws' exists on places: a b -- this cannot be", []))


class TestRmOne(Flow):
    def setUp(self):
        super().setUp()
        self.w.make()
        self.conf = self.w.alias()
        self.clog = self.w.driver.create_log("ws")
        self.w.files[self.clog] = "log\n"
        self.w.begin().end(0)
        self.w.begin("build", pid=99).end(0)
        self.w.begin("build", "other", pid=98).end(0)

    def rm(self, *argv):
        return self.typed("rm", "ws", "--yes", *argv)

    def gone(self):
        self.assertEqual((self.w.containers, self.w.ws_dir() in self.w.dirs, self.clog in self.w.files), (set(), False, False))
        self.assertEqual((self.w.files[self.conf], [t.field("name") for t in self.w.records.list()]), ("Host other\n    HostName o\n", ["other"]))

    def test_everything_of_it_goes_and_nothing_of_another(self):
        rc, err = self.rm()
        self.assertEqual((rc, said(err)), (0, "==> workspace 'ws' destroyed"))
        self.gone()

    def test_a_record_with_nothing_left_is_forgotten(self):
        self.w.containers.clear()
        self.w._rm_rf(["rm", self.w.ws_dir()], self.w)
        rc, err = self.rm()
        self.assertEqual((rc, said(err), [e for e in self.w.effects if e[0] == "act"]), (0, "==> 'ws' had nothing left but its record; forgotten", []))
        self.gone()

    def test_a_running_job_refuses_naming_each_live_one_before_any_effect(self):
        self.w.begin("build", pid=4242)
        self.w.begin("push-forward", pid=4243, kill="wk stop ws")
        self.w.begin("test", pid=4244)
        self.w.pids.update({4242, 4243})
        rc, err = self.rm()
        self.assertEqual((rc, said(err), err.splitlines()[-2:]), (1, "error: 'ws' has work running in it, and destroying it under a running job",
                                                                  ["    build (pid 4242 on here)  stop it:  wk build ws --kill",
                                                                   "    push-forward (pid 4243 on here)  stop it:  wk stop ws"]))
        self.assertEqual((self.w.containers, self.lock_takes()), ({"wk-ws"}, ["lock holds"]))

    def test_inside_a_workspace_it_refuses_naming_the_host(self):
        rc, err = self.typed("rm", "--yes", w=self.make_world(kinds={"fakebox": "local"}))
        self.assertEqual((rc, said(err)), (1, "error: a workspace cannot destroy itself -- run 'wk rm ws' on the host"))

    def test_modified_files_in_the_overlay_are_counted_in_a_warning(self):
        for f in ("a.cpp", "b/c with a space.h"):
            self.w.files[os.path.join(self.w.ws_dir(), "changes", f)] = "x"
        self.assertEqual(self.rm()[1].splitlines()[0], "warning: ws has 2 modified file(s) in its overlay")

    def test_what_a_destroy_leaves_is_named_and_the_records_and_alias_stay(self):
        self.w.react(["podman", "rm", "-f"], lambda a, f: Result(0))
        rc, err = self.rm()
        self.assertEqual((rc, said(err)), (1, "  re-run 'wk rm ws' -- what is left is exactly what it will find and retry"))
        self.assertIn("warning: 'ws' was not fully destroyed; still here: the fakebox environment", err.splitlines())
        self.assertEqual((len(self.w.records.list()), "Host wk-ws" in self.w.files[self.conf]), (3, True))


class TestRmAsks(Flow):
    def test_the_question_names_it_with_its_place_and_no_terminal_declines(self):
        self.w.make("a")
        rc, err = self.typed("rm", "a")
        self.assertEqual((rc, said(err), self.w.containers), (1, "error: aborted", {"wk-a"}))
        self.assertIn("    a@fakebox", err.splitlines())

    def test_an_unanswering_machine_keeps_every_record(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        w.unreachable = "Host key verification failed."
        rc, err = self.typed("rm", "ws", w=w)
        self.assertEqual((rc, said(err), [e for e in w.mutations() if w.mutates(e)]),
                         (1, "error: 'ws' has no record here, and fakebox did not answer: Host key verification failed.", []))

    def test_a_refusal_holds_no_lock_afterwards(self):
        self.w.make("a")
        self.w.begin("build", "a", pid=4242)
        self.w.pids.add(4242)
        rc, err = self.typed("rm", "a", "--yes")
        self.assertEqual((rc, said(err), self.w.containers, self.w.lock_files()), (1, "error: 'a' has work running in it, and destroying it under a running job", {"wk-a"}, []))

    def test_several_names_are_asked_once_and_each_goes_through_its_own_places_wk_rm(self):
        """A batch spanning places: the first name's place is not the others'; the worst status wins."""
        self.w.answer(["env", "WK_PLACE=vm"])
        self.w.answer(["env", "WK_PLACE=container"], rc=1)
        self.w.reg.ws_place = {"mac": "vm", "linux": "container"}.get
        rc, _ = self.typed("rm", "mac", "linux", "--yes")
        self.assertEqual((rc, [list(e[1]) for e in self.w.effects if e[0] == "act"]),
                         (1, [["env", "WK_PLACE=vm", WK, "rm", "mac", "--yes"], ["env", "WK_PLACE=container", WK, "rm", "linux", "--yes"]]))


class TestRmAll(Flow):
    def listing(self, *rows):
        self.w.answer([WK, "ls", "--json"], out=json.dumps({"workspaces": [{"place": t, "name": n} for t, n in rows]}))

    def removals(self):
        return [list(e[1][1:2] + e[1][4:5]) for e in self.w.effects if e[0] == "run" and e[1][0] == "env"]

    def test_each_of_wk_ls_s_rows_goes_through_its_places_own_wk_rm_and_the_worst_status_wins(self):
        self.listing(("fakebox", "alpha"), ("here:box2", "beta"), ("box3:extra", "gamma"), ("", "nameless"))
        self.w.answer(["env", "WK_PLACE=box2"], rc=1)
        self.w.answer(["env", "WK_PLACE=fakebox"])
        self.w.answer(["env", "WK_PLACE=box3"])
        rc, _ = self.typed("rm", "--all", "--yes")
        self.assertEqual((rc, self.removals()), (1, [["WK_PLACE=fakebox", "alpha"], ["WK_PLACE=box2", "beta"], ["WK_PLACE=box3", "gamma"]]))

    def test_a_listing_that_does_not_answer_destroys_nothing(self):
        self.w.answer([WK, "ls", "--json"], rc=1)
        rc, err = self.typed("rm", "--all", "--yes")
        self.assertEqual((rc, said(err), self.removals()), (1, "error: 'wk ls --json' did not answer with the workspaces to destroy, so", []))

    def test_a_dry_run_names_each_one_and_the_removal_it_would_run(self):
        self.listing(("fakebox", "alpha"), ("fakebox", "beta"))
        rc, err = self.typed("rm", "--all", "--dry-run")
        self.assertEqual((rc, self.removals()), (0, []))
        self.assertEqual(err.splitlines()[1:], ["    alpha@fakebox", "    beta@fakebox", "destroy them? [y/N]",
                                                "would run: env WK_PLACE=fakebox %s rm alpha --yes" % WK, "would run: env WK_PLACE=fakebox %s rm beta --yes" % WK])

    def test_with_nothing_to_destroy_it_says_so_and_asks_nobody(self):
        self.listing()
        self.assertEqual(self.typed("rm", "--all"), (0, "==> no workspaces -- nothing to destroy\n"))

    def test_declining_destroys_nothing(self):
        self.listing(("fakebox", "alpha"))
        rc, err = self.typed("rm", "--all")
        self.assertEqual((rc, said(err), self.removals()), (1, "error: aborted", []))


class TestNewDryRun(Flow):
    def test_a_dry_run_of_the_front_detaches_nothing_and_names_the_create(self):
        rc, err = self.new("--dry-run")
        self.assertEqual((rc, self.spawned(), self.w.records.list()), (0, [], []))
        self.assertRegex(err, r"would run: .*/wkdev-create .*--name wk-ws")
        self.assertNotIn("detached as pid", err)
