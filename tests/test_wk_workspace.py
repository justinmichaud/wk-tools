"""lib/wk/workspace.py: `wk new` and `wk rm` as flows over a fake target on
a fake machine -- every refusal by its words and exit status, the driver's
steps and record, what freshen says, rm's plan and its four answers, and
that either flow killed after any effect and re-run converges on the same
final state, with a dry run printing the same plan and touching nothing.

Run: python3 tests/run.py -k tests.test_wk_workspace
"""
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, record, targets, workspace  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

WK = str(REPO / "wk")
ZED = str(REPO / "cmd" / "zed")
CHECKOUT = "branch=main\nupstream=origin/main\nbehind=0\nhead=abc1234\n"


def _bash_defaults():
    def arch(argv):
        a = argv[-1]
        if a in ("", "native", "arm64"):
            return Result(0, "native\n")
        if a in ("armhf", "arm"):
            return Result(0, "armhf\n")
        return Result(1, "", "error: unknown architecture '%s' (one of: native armhf)\n" % a)

    def pr(argv):
        spec = argv[-1]
        if ":" in spec or spec.isdigit():
            return Result(0)
        return Result(1, "", "error: '%s' is not a PR spec: <user>:<branch>, a pull request number, or wpe:<number>\n" % spec)
    return {"arch_canon": arch, "pr_parse_spec": pr,
            "current_base": Result(0, "main-1\n"), "base_verify": Result(0), "wk_pr_checkout": Result(0, "checked out\n")}


class World(Fake):
    """This host with one container target: podman answers from `containers`, wkdev-create stands in for
    firstrun by writing the ready marker, the bridged bash functions answer from `bash`, and a record
    removed through this machine goes from the record directory too."""

    def __init__(self, tmp, kinds=None, stores=None):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "WK_IN_VM": "1", "WK_TARGET": "fakebox"}
        self.clock = FakeClock()
        self.containers = set()
        self.far = (True, "")
        self.probe = "yes"
        self.checkout = Result(0, CHECKOUT)
        self.bash = _bash_defaults()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.answer(["hostname"], out="here\n")
        self.answer(["sdk-refresh"])
        self.answer([WK, "sync"], out="fetched\n")
        self.answer([ZED])
        self.react(["podman", "container", "exists"], lambda a, f: Result(0 if a[-1] in f.containers else 1))
        self.react(["podman", "inspect"], lambda a, f: Result(0, "running\n") if a[2] in f.containers else Result(125, "", "no such container"))
        self.react(["podman", "rm", "-f"], self._podman_rm)
        self.react(["podman", "unshare", "rm", "-rf"], self._rm_rf)
        self.react(["wkdev-create"], self._wkdev_create)
        self.react(["find"], self._find)
        self.react(["bash", "-c"], self._bash)
        self.react(["exec"], self._exec)
        self.records = record.Records(self.tmp / "store", clock=self.clock, env=self.env, machine=self,
                                      ask_target=lambda n, pid, cap: pid in self.pids)
        self.reg = FakeRegistry(REPO, self.env, self, kinds or {"fakebox": "container"}, stores or {})
        self.target = self.reg.load("fakebox")
        self.lock = Lock(self.target.store, self, self.clock)
        self.effects = []

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
        if path.startswith(str(self.tmp / "store" / "task")) and not act.dry_run():
            record._rmtree(path)

    def _podman_rm(self, argv, f):
        f.containers.discard(argv[-1])
        return Result(0)

    def _rm_rf(self, argv, f):
        p = argv[-1]
        f.files = {k: v for k, v in f.files.items() if k != p and not k.startswith(p + "/")}
        f.dirs = {d for d in f.dirs if d != p and not d.startswith(p + "/")}
        return Result(0)

    def _wkdev_create(self, argv, f):
        f.containers.add(argv[argv.index("--name") + 1])
        home = argv[argv.index("--home") + 1]
        f.dirs.add(home)
        f.files[os.path.join(home, targets.READY_MARKER)] = ""
        return Result(0)

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
        return self.target.store.ws_dir(name)

    def make(self, name="ws", marker=True, base=True):
        d = self.ws_dir(name)
        for sub in ("changes", "overlay-work", "home", "build"):
            self.dirs.add(os.path.join(d, sub))
        self.dirs.add(d)
        self.files[os.path.join(d, "arch")] = "native\n"
        if base:
            self.files[os.path.join(d, "base-id")] = "main-1\n"
        if marker:
            self.files[os.path.join(d, "home", targets.READY_MARKER)] = ""
        self.containers.add("wk-" + name)

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
        """What a flow leaves. A lock file is not in it: one a killed holder left is taken by the next
        taker, and a re-run that cannot take it fails rather than converging."""
        store = str(self.tmp / "store")
        return (sorted(self.containers), sorted(self.rel(p) for p in self.files if p.startswith(store)),
                sorted(self.rel(d) for d in self.dirs if d.startswith(store + "/ws")),
                [(t.field("kind"), t.field("exit")) for t in self.records.list()],
                self.files.get(workspace.sshalias.alias_path(self.env), ""))


class FakeTarget(targets.Target):
    """The write side over the World's podman, with the record read from the fake's files."""

    def __init__(self, name, root, env, machine, kind):
        super().__init__(name, root, env, machine)
        self.kind = kind
        self.needs_base = kind == "container"
        self.host = "box.example" if kind == "remote" else ""
        self.peer = False

    def ctr(self, ws):
        return "wk-" + ws

    def created(self, ws):
        return self.machine.exists(os.path.join(self.store.ws_dir(ws), "home", targets.READY_MARKER))

    def answers(self):
        return self.machine.far

    def info(self, ws):
        if not self.machine.far[0]:
            return "unreachable"
        r = self.machine.run(["podman", "inspect", self.ctr(ws), "--format", "{{.State.Status}}"])
        if not r.ok:
            return "absent"
        return r.out.strip() if self.created(ws) else "creating"

    def state(self, ws, info=None):
        env = self.info(ws) if info is None else info
        ws_dir = self.store.ws_dir(ws)
        if env in ("creating", "unreachable"):
            return env
        if env == "absent":
            if not self.machine.isdir(ws_dir):
                return "absent"
            return "broken" if self.created(ws) else "creating"
        if self.needs_base and not self.machine.exists(os.path.join(ws_dir, "base-id")):
            return "creating"
        return "present"

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", ws] + list(argv))

    def mirror_dir(self):
        return "/mirror/of/" + self.kind

    def store_init(self):
        self.machine.mkdir(os.path.join(self.store.root(), "ws"))

    def sdk_refresh(self):
        return self.machine.act_run(["sdk-refresh"]).ok

    def create(self, ws, base=None, arch="native"):
        if self.kind == "local":
            act.die("a workspace cannot create a workspace -- run 'wk new %s' on the host" % ws)
        ws_dir = self.store.ws_dir(ws)
        if self.machine.run(["podman", "container", "exists", self.ctr(ws)]).ok:
            act.die("workspace '%s' already exists" % ws)
        for d in ("", "changes", "overlay-work", "home", "build"):
            self.machine.mkdir(os.path.join(ws_dir, d) if d else ws_dir)
        self.machine.write(os.path.join(ws_dir, "arch"), arch + "\n")
        r = self.machine.act_run(["wkdev-create", "--name", self.ctr(ws), "--home", os.path.join(ws_dir, "home")])
        if not r.ok:
            act.die("wkdev-create failed for '%s' (exit %d); what it said is above" % (ws, r.rc), r.rc)
        self.machine.write(os.path.join(ws_dir, "base-id"), (base or "") + "\n")

    def destroy(self, ws):
        if self.kind == "local":
            act.die("a workspace cannot destroy itself -- run 'wk rm %s' on the host" % ws)
        c, ws_dir = self.ctr(ws), self.store.ws_dir(ws)
        if self.machine.run(["podman", "container", "exists", c]).ok:
            self.machine.act_run(["podman", "rm", "-f", c])
        if self.machine.isdir(ws_dir):
            self.machine.act_run(["podman", "unshare", "rm", "-rf", ws_dir])
            self.machine.remove(ws_dir)


class FakeRegistry(targets.Registry):
    def __init__(self, root, env, machine, kinds, stores):
        super().__init__(root, env=env, machine=machine)
        self.kinds = kinds
        self.stores = stores

    def all(self):
        return list(self.kinds)

    def default(self):
        return next(iter(self.kinds))

    def load(self, name):
        if name not in self.kinds:
            raise LookupError("unknown target '%s'.\n    The built-in ones are container, vm, remote and local." % name)
        env = dict(self.env)
        if name in self.stores:
            env["WK_STORE"] = self.stores[name]
        return FakeTarget(name, self.root, env, self.machine, self.kinds[name])


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

    def driver(self, w=None, name="ws", base="", arch="native", target=None):
        w = w or self.w
        return workspace.new_driver(target or w.target, w.records, w.lock, w.clock, name, base, arch)

    def runs(self, w=None, head=None):
        w = w or self.w
        return [e[1] for e in w.effects if e[0] == "run" and (head is None or e[1][0] == head)]

    def lock_takes(self, w=None, heads=()):
        """Each lock taken, as "lock <resource>", in order with the runs whose head is in `heads`."""
        out = []
        for e in (w or self.w).effects:
            if e[0] == "symlink" and not e[1].endswith(".breaking") and ".new." not in e[1]:
                out.append("lock " + os.path.basename(e[1]).split("@")[0])
            elif e[0] == "run" and e[1][0] in heads:
                out.append(e[1][0])
        return out

    def bash_runs(self, w=None, fn=""):
        return [a for a in self.runs(w, "bash") if fn in a[2]]


class TestNewFrontRefusals(WorkspaceTest):
    def test_an_invalid_name_is_refused_before_anything_else(self):
        for bad in ("-x", "", "a b", "a/b"):
            err = self.refused(lambda: self.front(name=bad))
            self.assertIn("invalid name '%s': use [a-zA-Z0-9._-], not starting with '-'" % bad, err)
        self.assertEqual(self.w.effects, [])

    def test_sysroot_is_not_this_flag(self):
        err = self.refused(lambda: self.front(sysroot=True))
        self.assertIn("--sysroot is not implemented", err)
        self.assertIn("'wk build\n    --sysroot', not 'wk new --sysroot'", err)

    def test_pr_needs_a_spec_and_a_valid_one(self):
        self.assertIn("--pr needs a spec: <user>:<branch>, <n>, or wpe:<n>", self.refused(lambda: self.front(pr="")))
        self.assertIn("'nope' is not a PR spec", self.refused(lambda: self.front(pr="nope")))
        self.assertEqual(len(self.bash_runs(fn="pr_parse_spec")), 1)

    def test_pr_and_no_wait_exclude_each_other(self):
        err = self.refused(lambda: self.front(pr="u:b", no_wait=True))
        self.assertIn("--pr needs the workspace to be ready, and --no-wait returns before it is.", err)
        self.assertIn("wk pr ws u:b", err)

    def test_an_unknown_arch_or_target_is_refused_with_the_bridge_or_registry_words(self):
        self.assertIn("unknown architecture 'sparc'", self.refused(lambda: self.front(arch="sparc")))
        self.assertIn("unknown target 'nosuch'.", self.refused(lambda: self.front(target="nosuch")))

    def test_a_non_native_arch_is_container_only(self):
        w = self.make_world(kinds={"fakebox": "vm"})
        err = self.refused(lambda: self.front(w, arch="armhf"))
        self.assertIn("--arch armhf is a container-only capability (this is target 'fakebox')", err)
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
        self.assertIn("workspace 'ws' already exists (target 'fakebox').\n    'wk rm ws' first, or pick another name.", err)
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
        self.assertIn("wk enter ws --zed   /   wk build ws <config>", err)

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
        log = self.w.target.create_log("ws")
        self.assertEqual(self.spawned(), [("spawn", (WK, "new", "ws", "--target", "fakebox", "--arch", "armhf", "--base", "main-2", "--_detached"), log)])
        self.assertIn(("write", log), self.w.effects)
        self.assertIn(("mkdir", os.path.dirname(log)), self.w.effects)
        self.assertIn("creating 'ws' on fakebox, detached as pid 1001 -- this end can go away", err)
        self.assertIn("follow:  tail -f %s" % log, err)
        self.assertIn("state:   wk status ws", err)
        self.assertIn("open it:  wk enter ws --zed   (waits for it to be ready)", err)

    def test_the_store_is_initialised_before_the_detach(self):
        self.stderr(lambda: self.front(no_wait=True))
        kinds = [e[0] for e in self.w.effects if e[0] in ("mkdir", "spawn")]
        self.assertEqual(kinds[0], "mkdir")
        self.assertIn(("mkdir", os.path.join(self.w.env["WK_STORE"], "ws")), self.w.effects)

    def ended(self, status):
        t = self.w.begin(plan=list(workspace.PLAN), log=self.w.target.create_log("ws"))
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
            t = w.begin(plan=list(workspace.PLAN), log=w.target.create_log("ws"))
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
        self.assertIn("wk new ws --target fakebox", err)

    def test_the_wait_gives_up_after_wk_new_timeout_and_undoes_nothing(self):
        self.w.env["WK_NEW_TIMEOUT"] = "3"
        err = self.refused(lambda: self.front())
        self.assertIn("'ws' is still being created after 3s.", err)
        self.assertIn("Nothing here was undone.", err)
        self.assertEqual(self.w.clock.slept, [1, 1, 1])
        self.assertIn(1001, self.w.pids)

    def test_the_log_is_streamed_while_waiting(self):
        log = self.w.target.create_log("ws")
        os.makedirs(os.path.dirname(log))
        Path(log).write_text("driver says hello\n")
        self.ended(0)
        _, err = self.stderr(lambda: self.front())
        self.assertIn("driver says hello", err)


class Creates(World):
    """A host whose spawned driver makes the workspace at once and records it done."""

    def spawn(self, argv, log):
        pid = super().spawn(argv, log)
        self.make(argv[2])
        self.begin(name=argv[2], plan=list(workspace.PLAN), log=log, pid=pid).end(0)
        return pid


class TestNewFrontTail(WorkspaceTest):
    def make_world(self, **kw):
        return Creates(self.tmp, **kw)

    def test_the_hints_follow_readiness_and_no_agent_is_started(self):
        """Remote Control is a `wk ai claude` session's own, so `wk new` runs no agent and judges no login."""
        rc, err = self.stderr(lambda: self.front())
        self.assertEqual(rc, 0)
        self.assertEqual(self.runs(head=WK), [])
        self.assertEqual(self.bash_runs(fn="wk_cred_check"), [])
        self.assertIn("workspace 'ws' ready", err)
        self.assertIn("wk ai claude ws      sandboxed agent\n", err)
        self.assertIn("wk enter ws          shell", err)
        self.assertNotIn("remote control", err.lower())

    def test_a_remote_target_names_its_missing_sandbox(self):
        w = self.make_world(kinds={"fakebox": "remote"})
        _, err = self.stderr(lambda: self.front(w))
        self.assertEqual(self.runs(w, head=WK), [])
        self.assertIn("no sandbox on a shared machine, so 'wk ai claude' and 'wk doctor <ws>' refuse.", err)
        self.assertIn("ssh://box.example/src/WebKit", err)

    def test_the_vm_and_armhf_hints(self):
        w = self.make_world(kinds={"fakebox": "vm"})
        _, err = self.stderr(lambda: self.front(w))
        self.assertIn("wk vm start ws       boot it", err)
        w = self.make_world()
        _, err = self.stderr(lambda: self.front(w, arch="armhf"))
        self.assertIn("workspace 'ws' ready (armhf)", err)
        self.assertIn("armhf: native 32-bit, no GPU.", err)

    def test_a_pr_is_checked_out_through_the_bridge_once_ready_and_its_failure_is_the_commands(self):
        rc, _ = self.stderr(lambda: self.front(pr="u:b"))
        self.assertEqual(rc, 0)
        checkout = self.bash_runs(fn="wk_pr_checkout")
        self.assertEqual(len(checkout), 1)
        self.assertEqual(checkout[0][-2:], ("ws", "u:b"))
        self.assertIn("load_target 'fakebox'", checkout[0][2])
        w = self.make_world()
        w.bash["wk_pr_checkout"] = Result(3, "", "no branch\n")
        self.assertIn("no branch", self.refused(lambda: self.front(w, pr="u:b"), 3))

    def test_zed_opens_the_checkout_and_its_failure_only_warns(self):
        self.stderr(lambda: self.front(zed=True))
        self.assertEqual(self.runs(head=ZED), [(ZED, "ws")])
        w = self.make_world()
        w.answer([ZED], rc=1)
        rc, err = self.stderr(lambda: self.front(w, zed=True))
        self.assertEqual(rc, 0)
        self.assertIn("'ws' is there; opening it in Zed is what failed (above) -- 'wk zed ws' retries", err)


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
        self.assertIn("stopping the new in 'ws' (pid 4242 on here)", err)
        self.assertIn("stopped 'ws's new and recorded it as cancelled", err)
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
        self.assertIn("pid 4242 did not stop on TERM after 2s -- killing it", err)
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
        return [e for e in w.work() if e[0] in ("write", "mkdir", "remove") or (e[0] == "run" and e[1][0] in ("sdk-refresh", "wkdev-create", WK))]

    def test_the_steps_land_in_order_and_the_record_ends_0(self):
        rc, err = self.stderr(lambda: self.driver())
        self.assertEqual(rc, 0)
        heads = [e[1][0] if e[0] == "run" else e[0] for e in self.acts()]
        self.assertEqual(heads, ["sdk-refresh", "mkdir", "mkdir", "mkdir", "mkdir", "mkdir", "write", "wkdev-create", "write", WK])
        ws = self.w.ws_dir()
        self.assertEqual(self.w.files[os.path.join(ws, "base-id")], "main-1\n")
        self.assertIn("wk-ws", self.w.containers)
        (t,) = self.w.records.list()
        self.assertEqual((t.field("kind"), t.verdict(), t.plan()), ("new", "ok", list(workspace.PLAN)))
        self.assertEqual([s for _, s in t.steps()], ["done"] * 6 + ["running"])
        self.assertEqual(t.field("kill"), "wk new ws --kill")
        self.assertEqual(self.w.lock_files(), [])
        self.assertIn("workspace 'ws' created", err)
        self.assertIn("'ws' is on main at abc1234, up to date with origin/main", err)

    def test_the_sdk_is_refreshed_under_its_lock_before_the_store_lock_and_the_create(self):
        """A container's image tag comes from the SDK checkout on disk, so it is refreshed for every new
        workspace; the refresh is a network fetch, so no `wk sync` waits on it behind the store lock."""
        self.stderr(lambda: self.driver())
        self.assertEqual(self.lock_takes(heads=("sdk-refresh", "wkdev-create")),
                         ["lock sdk", "sdk-refresh", "lock ws-ws", "lock store", "wkdev-create"])
        for kind in ("vm", "remote"):
            w = self.make_world(kinds={"fakebox": kind})
            self.stderr(lambda: self.driver(w))
            self.assertEqual(self.lock_takes(w, heads=("sdk-refresh",)), ["lock ws-ws"], kind)

    def test_a_present_broken_or_unreachable_workspace_is_refused_and_the_record_says_refused(self):
        cases = []
        w = self.make_world()
        w.make()
        cases.append((w, "workspace 'ws' already exists (target 'fakebox').\n    'wk rm ws' first, or pick another name."))
        w = self.make_world()
        w.make()
        w.containers.clear()
        cases.append((w, "'ws' is a record without an environment: creation finished, and the\n    fakebox side of it is gone -- something outside wk removed it."))
        w = self.make_world()
        w.far = (False, "Connection refused")
        cases.append((w, "cannot reach the machine behind target 'fakebox', so whether 'ws' is\n    already there cannot be known"))
        for w, words in cases:
            err = self.refused(lambda: self.driver(w))
            self.assertIn(words, err)
            (t,) = w.records.list()
            self.assertEqual(t.verdict(), "refused")
            self.assertEqual(t.stage(), ["checking"])
            self.assertEqual([a for a in self.runs(w) if a[0] == "wkdev-create"], [])
            self.assertEqual(w.lock_files(), [])

    def test_a_half_made_workspace_is_wiped_and_remade(self):
        self.w.make(marker=False, base=False)
        conf = self.w.alias()
        rc, err = self.stderr(lambda: self.driver())
        self.assertEqual(rc, 0)
        self.assertIn("'ws' exists but was never finished -- destroying it and starting again", err)
        self.assertIn("(an interrupted 'wk new' leaves this; nothing in it is worth keeping)", err)
        heads = [a[:3] for a in self.runs() if a[0] == "wkdev-create" or a[:2] in (("podman", "rm"), ("podman", "unshare"))]
        self.assertEqual(heads, [("podman", "rm", "-f"), ("podman", "unshare", "rm"), ("wkdev-create", "--name", "wk-ws")])
        self.assertNotIn("Host wk-ws", self.w.files[conf])
        self.assertIn("Host other", self.w.files[conf])
        self.assertEqual(self.w.records.list()[0].verdict(), "ok")

    def test_a_half_made_workspace_the_destroy_leaves_is_a_refusal_not_a_create_over_it(self):
        self.w.make(marker=False, base=False)
        self.w.react(["podman", "rm", "-f"], lambda a, f: Result(0))
        err = self.refused(lambda: self.driver())
        self.assertIn("could not destroy the half-made workspace 'ws'; still here: the fakebox environment\n"
                      "    'wk rm ws' retries exactly that, then 'wk new ws'", err)
        self.assertEqual([a for a in self.runs() if a[0] == "wkdev-create"], [])
        self.assertEqual(self.w.records.list()[0].verdict(), "failed")
        self.assertEqual(self.w.lock_files(), [])

    def test_a_creation_that_died_after_the_marker_is_remade_not_refused(self):
        self.w.make()
        self.w.begin(plan=list(workspace.PLAN))
        self.assertEqual(workspace.creation_state(self.w.target, self.w.records, "ws"), "creating")
        rc, err = self.stderr(lambda: self.driver())
        self.assertEqual(rc, 0)
        self.assertIn("'ws' exists but was never finished -- destroying it and starting again", err)
        self.assertEqual([t.verdict() for t in self.w.records.list()], ["ok"])
        w = self.make_world()
        w.make()
        w.begin(plan=list(workspace.PLAN)).end(0)
        self.assertEqual(workspace.creation_state(w.target, w.records, "ws"), "present")
        self.assertIn("already exists", self.refused(lambda: self.driver(w)))

    def test_no_base_snapshot_refuses_naming_wk_sync_and_creates_nothing(self):
        self.w.bash["current_base"] = Result(1)
        err = self.refused(lambda: self.driver())
        self.assertIn("no base snapshot this machine can build a workspace from:  wk sync\n    publishes one.", err)
        self.assertEqual([a for a in self.runs() if a[0] == "wkdev-create"], [])
        self.assertNotIn(self.w.ws_dir(), self.w.dirs)
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["base"]))
        self.assertEqual(self.w.lock_files(), [])

    def test_a_given_base_is_verified_and_its_refusal_is_the_reason(self):
        self.w.bash["base_verify"] = Result(1, "snapshot main-9 does not exist\n")
        err = self.refused(lambda: self.driver(base="main-9"))
        self.assertIn("snapshot main-9 does not exist", err)
        self.assertEqual(self.bash_runs(fn="current_base"), [])
        self.assertEqual(self.bash_runs(fn="base_verify")[0][-1], "main-9")
        w = self.make_world(kinds={"fakebox": "vm"})
        self.stderr(lambda: self.driver(w))
        self.assertEqual(self.bash_runs(w, fn="base_verify") + self.bash_runs(w, fn="current_base"), [])

    def test_a_workspace_that_never_initialises_is_refused_after_wk_ready_timeout(self):
        def no_firstrun(argv, f):
            f.containers.add(argv[argv.index("--name") + 1])
            return Result(0)
        self.w.react(["wkdev-create"], no_firstrun)
        self.w.env["WK_READY_TIMEOUT"] = "2"
        self.w.target = self.w.reg.load("fakebox")
        err = self.refused(lambda: self.driver())
        self.assertIn("'ws' was created but never finished initialising", err)
        self.assertIn("wk new ws    (destroys it and retries)", err)
        self.assertEqual(self.w.clock.slept, [1, 1])
        (t,) = self.w.records.list()
        self.assertEqual((t.verdict(), t.stage()), ("failed", ["init"]))

    def test_a_create_that_fails_ends_the_record_with_its_status(self):
        self.w.react(["wkdev-create"], lambda a, f: Result(125, "", "podman: boom\n"))
        err = self.refused(lambda: self.driver(), 125)
        self.assertIn("wkdev-create failed for 'ws' (exit 125)", err)
        self.assertEqual(self.w.records.list()[0].field("exit"), "125")

    def test_a_dry_run_records_and_waits_for_nothing_and_prints_the_plan(self):
        self.dry_run()
        before = (dict(self.w.files), set(self.w.dirs), set(self.w.containers))
        rc, err = self.stderr(lambda: self.driver())
        self.assertEqual(rc, 0)
        self.assertEqual((self.w.files, self.w.dirs, self.w.containers), before)
        self.assertEqual(self.w.records.list(), [])
        self.assertIn("would run: wkdev-create --name wk-ws", err)
        self.assertIn("would run: %s sync ws" % WK, err)
        self.assertNotIn("created", err)
        self.assertEqual(self.w.clock.slept, [])


class TestFreshen(WorkspaceTest):
    def freshen(self, w=None):
        w = w or self.w
        return self.stderr(lambda: workspace.freshen(w.target, "ws", w))[1]

    def test_where_a_fetch_comes_from_is_the_workspaces_own_answer(self):
        self.assertEqual([workspace.fetch_from(p) for p in ("yes", "no", "", "wkdev-enter: no such container")],
                         ["mirror", "network", "unreachable", "unreachable"])

    def test_a_mirror_in_reach_is_fetched_with_wk_sync(self):
        err = self.freshen()
        self.assertEqual(self.runs(head=WK), [(WK, "sync", "ws")])
        self.assertIn("'ws' is on main at abc1234, up to date with origin/main", err)
        probe = [a for a in self.runs(head="exec")][0]
        self.assertIn("'/mirror/of/container'", probe[-1])

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

    def test_the_checkout_script_enters_the_quoted_source_and_never_resets(self):
        script = workspace.checkout_script("/src/We bKit")
        self.assertTrue(script.startswith("cd '/src/We bKit' || exit 2\n"))
        self.assertIn("git merge --ff-only --quiet", script)
        self.assertNotIn("reset", script)

    def test_a_dry_run_names_the_fetch_and_asks_the_workspace_nothing(self):
        self.dry_run()
        err = self.freshen()
        self.assertIn("would run: %s sync ws" % WK, err)
        self.assertEqual(self.runs(head="exec"), [])


class TestRecordHelpers(WorkspaceTest):
    def test_live_lines_name_every_live_job_and_skip_dead_pids(self):
        self.w.begin("build", "ws1", pid=4242)
        self.w.begin("agent-forward", "ws1", pid=4243, kill="wk push off")
        self.w.begin("test", "ws1", pid=4244)
        self.w.begin("build", "ws2", pid=4245)
        self.w.pids.update({4242, 4243, 4245})
        self.assertEqual(workspace.live_task_lines(self.w.records, "ws1"),
                         "    agent-forward (pid 4243 on here)  stop it:  wk push off\n"
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
        self.w.dirs.add(self.w.ws_dir())
        self.w.far = (False, "down")
        t, what = self.plan()
        self.assertEqual((t.name, what), ("fakebox", "workspace"))

    def test_an_environment_with_no_directory_is_a_workspace(self):
        self.w.containers.add("wk-ws")
        self.assertEqual(self.plan()[1], "workspace")

    def test_only_a_creation_record_is_a_record_on_the_target_that_holds_it(self):
        self.w.begin()
        self.assertEqual(self.plan()[1], "record")
        other = str(self.tmp / "other-store")
        w = self.make_world(kinds={"fakebox": "container", "box2": "remote"}, stores={"box2": other})
        record.Records(other, clock=w.clock, env=dict(w.env, WK_STORE=other), machine=w).begin(
            "new", "here", "ws", "wk new ws --kill", "/nolog", ["a"], pid=1)
        t, what = self.plan(w)
        self.assertEqual((t.name, what), ("box2", "record"))

    def test_nothing_anywhere_is_1_and_a_machine_that_did_not_answer_is_2_with_ssh_words(self):
        self.refused(lambda: self.plan(), 1)
        self.w.far = (False, "Host key verification failed.")
        err = self.refused(lambda: self.plan(), 2)
        self.assertIn("'ws' has no record here, and fakebox did not answer: Host key verification failed.", err)
        self.assertIn("re-run once fakebox answers.", err)

    def test_a_name_on_two_targets_is_refused_by_the_registry(self):
        class Two(FakeRegistry):
            def ws_target(self, ws):
                raise LookupError("workspace '%s' exists on targets: a b -- this cannot be\n    resolved; remove one, or set WK_TARGET" % ws)
        self.w.reg = Two(REPO, self.w.env, self.w, {"fakebox": "container"}, {})
        self.assertIn("exists on targets: a b", self.refused(lambda: self.plan()))


class TestRmOne(WorkspaceTest):
    def setUp(self):
        super().setUp()
        self.w.make()
        self.conf = self.w.alias()
        self.clog = self.w.target.create_log("ws")
        self.w.files[self.clog] = "log\n"
        self.w.begin().end(0)
        self.w.begin("build", pid=99).end(0)

    def rm(self, w=None, what="workspace", name="ws"):
        w = w or self.w
        return workspace.rm_one(w.target, w.records, w.lock, name, what)

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
        self.assertIn("workspace 'ws' destroyed", err)
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

    def test_a_dry_run_prints_the_removals_and_removes_nothing(self):
        self.dry_run()
        before = (dict(self.w.files), set(self.w.dirs), set(self.w.containers), len(self.w.records.list()))
        rc, err = self.stderr(self.rm)
        self.w.lock.release_all()
        self.assertEqual(rc, 0)
        self.assertEqual((self.w.files, self.w.dirs, self.w.containers, len(self.w.records.list())), before)
        self.assertIn("would run: podman rm -f wk-ws", err)
        self.assertIn("would run: podman unshare rm -rf %s" % self.w.ws_dir(), err)
        self.assertNotIn("destroyed", err)
        planned = [e for e in self.w.work() if e[0] in ("write", "remove")]
        self.assertEqual(planned[:3], [("remove", self.w.ws_dir()), ("write", self.conf), ("remove", self.clog)])
        self.assertEqual(len(planned), 5)
        self.assertTrue(all(p.startswith(str(self.w.tmp / "store" / "task")) for _, p in planned[3:]))


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
        self.w.far = (False, "Host key verification failed.")
        rc, err = self.stderr(lambda: self.names("ws"))
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
        doc = {"workspaces": [{"target": t, "name": n} for t, n in rows]}
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
        self.assertIn("would run: env WK_TARGET=fakebox %s rm alpha --yes" % WK, err)
        self.assertIn("rm beta --yes", err)

    def test_with_nothing_to_destroy_it_says_so_and_asks_nobody(self):
        self.listing()
        rc, err = self.stderr(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual(rc, 0)
        self.assertIn("no workspaces -- nothing to destroy", err)
        self.assertNotIn("destroy them?", err)

    def test_each_removal_goes_through_its_targets_own_wk_rm_and_the_worst_status_wins(self):
        os.environ["WK_YES"] = "1"
        self.listing(("fakebox", "alpha"), ("box2", "beta"))
        calls = []

        def fake_act(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if "alpha" in argv else 1)
        with mock.patch.object(act, "act", fake_act):
            rc, _ = self.stderr(lambda: workspace.rm_all(self.w.reg, self.w.records))
        self.assertEqual(rc, 1)
        self.assertEqual(calls, [["env", "WK_TARGET=fakebox", WK, "rm", "alpha", "--yes"], ["env", "WK_TARGET=box2", WK, "rm", "beta", "--yes"]])

    def test_declining_destroys_nothing(self):
        self.listing(("fakebox", "alpha"))
        with mock.patch.object(act, "act") as acted:
            self.refused(lambda: workspace.rm_all(self.w.reg, self.w.records))
        acted.assert_not_called()


class Recording(World):
    """Records every act_run as ("act", argv) in both modes, so a dry run's plan can be held against a wet run's effects."""

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def mutations(self):
        return [self.rel(e) for e in self.work() if e[0] in ("act", "write", "mkdir", "remove", "kill", "spawn")]


class TestKillPoints(WorkspaceTest):
    def test_new_killed_after_any_effect_and_rerun_converges(self):
        def run_once(w):
            # each run is its own process: nothing the killed one held in memory survives it
            w.lock = Lock(w.target.store, w, w.clock)
            with contextlib.redirect_stderr(io.StringIO()):
                self.driver(w)
        converges(self, self.make_world, run_once, World.state)

    def rm_world(self, cls=World):
        w = cls(self.tmp)
        w.make()
        w.alias()
        w.files[w.target.create_log("ws")] = "log\n"
        w.files[os.path.join(w.ws_dir(), "changes", "a.cpp")] = "x"
        w.begin().end(0)
        w.begin("build", pid=99).end(0)
        return w

    def test_rm_killed_after_any_effect_and_rerun_converges(self):
        os.environ["WK_YES"] = "1"

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                workspace.rm_names(w.reg, w.records, ["ws"])
        converges(self, self.rm_world, run_once, World.state)
        w = self.rm_world()
        run_once(w)
        self.assertEqual(w.state()[:4], ([], [], [], []))

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet = Recording(self.tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            self.driver(wet)
        dry = Recording(self.tmp)
        before = dry.state()
        self.dry_run()
        with contextlib.redirect_stderr(io.StringIO()):
            self.driver(dry)
        self.assertEqual(dry.mutations(), wet.mutations())
        self.assertGreaterEqual(len(dry.mutations()), 9)
        self.assertEqual(dry.state(), before)
        self.assertEqual(dry.pids, set())

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
        self.assertIn("would run: wkdev-create --name wk-ws", err)
        self.assertNotIn("detached as pid", err)
        self.assertEqual(self.w.records.list(), [])


if __name__ == "__main__":
    unittest.main()
