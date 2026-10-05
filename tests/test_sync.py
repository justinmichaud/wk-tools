"""`wk sync`: cmd/sync's parse and `--where`, and lib/wk/sync.py's flows over fake places on a fake machine."""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, images, places, pr, sync  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.decl import Decl  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

CMD_SYNC = REPO / "cmd" / "sync"
MAIN_SHA = "a" * 40

cmd = load_cmd("sync")
REAL_MIRROR_BRANCHES = images.mirror_branches


def fake_retarget(driver, ws, src, forks, branches):
    driver.machine.steps.append("UPSTREAMFIX %s" % ws)
    r = driver.machine.fixes.get((ws, "UPSTREAMFIX"))
    return [l for l in r.out.replace("\r", "").splitlines() if l.strip()] if r else []


# The script generators, stood in for by a line naming what each would render: rendering is lib/wk/git.py's,
# tests/test_cli_refspecs.py and tests/test_new_fetch.py run it for real, and what is asked for is this file's.
GENERATORS = {
    (git, "wiring_script"): lambda src, mirror, forks, branches, n="", u="", c="": "WIRING %s %s %s %s %s" % (src, mirror, n, u, c),
    (git, "wiring_check_script"): lambda src, mirror, forks, branches, skip="": "CHECK %s %s %s" % (src, mirror, skip),
    (git, "pr_tool_setup_script"): lambda src, forks: "GITWEBKIT %s" % src,
    (git, "mirror_refresh_script"): lambda mirror, branches: "REFRESH %s" % mirror,
    (git, "REMOTES"): (("origin", "u1"), ("wpe", "u2"), ("fork", "u3"), ("forkwpe", "u4")),
    (images, "mirror_branches"): lambda env=None: ["main"],
    (pr, "retarget"): fake_retarget,
    (pr, "converge"): lambda driver, ws, src, forks: [],
}


class SyncDriver(places.Driver):
    """A place whose environment is the World's: workspaces, states, far side and far `wk` are its answers."""

    def __init__(self, name, root, env, machine, kind):
        super().__init__(name, root, env, machine)
        self.kind = "remote" if kind == "peer" else kind
        self.peer = kind == "peer"
        self.needs_base = kind == "container"
        self.host = name

    def list(self):
        return [(n, "running") for n in self.machine.workspaces.get(self.name, [])]

    def state(self, ws, info=None):
        st = self.machine.states.get(ws, "present")
        if st == "refused":
            raise Refused(1)
        return st

    def src(self, ws):
        return "/src/WebKit"

    def mirror_dir(self):
        return self.machine.mirrors.get(self.name, "/mirror/%s/WebKit.git" % self.name)

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["exec", self.name, ws] + list(argv))

    def act_exec(self, ws, argv):
        return self.machine.act_run(["exec", self.name, ws] + list(argv))

    def wiring_args(self):
        return ("mirror", "/far/mirror", "/far/ssh/config") if self.kind == "remote" else ("", "", "")

    def store_init(self):
        self.machine.mkdir(os.path.join(self.store.store_dir(), "ws"))

    def sync(self, named=False):
        self.machine.steps.append("FURNITURE %s%s" % (self.name, " named" if named else ""))
        if self.name in self.machine.refuse_tools:
            raise Refused(1)
        return True

    def far_side(self):
        return self.machine.far.get(self.name, "none")

    def wk(self, *args, env=None, quiet=False):
        self.machine.steps.append("ASKED %s: wk %s%s" % (self.name, " ".join(args),
                                                         " (no-delegate)" if (env or {}).get("WK_NO_DELEGATE") else ""))
        return self.machine.wk_rc.get(args[1] if len(args) > 1 else "", 0), "said %s\n" % " ".join(args)


class World(Fake):
    """This host: a store in a scratch directory, a mirror the refresh makes, git answering for the snapshot, the
    bridged bash functions answering from `bash`, and each workspace's exec answering from `fetched`."""

    def __init__(self, tmp, kinds=None):
        super().__init__("here")
        self.tmp = Path(tempfile.mkdtemp(dir=str(tmp)))
        self.env = {"HOME": str(self.tmp / "home"), "WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_MARKER": str(self.tmp / "no-marker")}
        self.clock = FakeClock()
        self.dirs.add(self.env["WK_LOCK_DIR"])
        self.workspaces, self.states, self.mirrors, self.far, self.wk_rc = {}, {}, {}, {}, {}
        self.fetched, self.fixes = {}, {}
        self.steps, self.refuse_tools = [], set()
        self.local_store, self.verified = True, set()
        self.heads = ["main"]
        self.refs, self.upstream = "", MAIN_SHA + " refs/heads/main\n"
        self.reg = self.registry(kinds or {"container": "container"})
        self.store = self.reg.store
        self.mirror = self.store.mirror_dir()
        self.react(["sh", "-c"], self._sh)
        self.react(["exec"], self._exec)
        self.react(["git"], self._git)
        self.react(["cp", "-al"], self._cp)
        self.effects = []

    @property
    def fake(self):
        return self

    def registry(self, kinds, guests=False):
        """A SyncDriver of each of `kinds` (name -> kind), but a FakeVm for 'vm' when `guests`."""
        def make(name, env):
            if guests and name == "vm":
                return FakeVm(name, str(REPO), env, self)
            return SyncDriver(name, str(REPO), env, self, kinds[name])
        return FakeRegistry(self.env, self, make, names=list(kinds))

    def lock(self):
        return Lock(self.store, self, self.clock)

    def sync(self, scope="here", only="", place="", fix=False):
        return sync.Sync(self.reg, self.clock, self.lock(), scope, only, place, fix)

    def snapshots_dir(self):
        return self.store.snapshots_dir()

    def publish(self, bid, sha=MAIN_SHA):
        d = os.path.join(self.snapshots_dir(), bid)
        self.dirs.update({self.snapshots_dir(), d, os.path.join(d, "WebKit"), os.path.join(d, "WebKit", ".git")})
        self.files[os.path.join(d, "branch")] = "origin/main\n"
        self.files[os.path.join(d, "sha")] = sha + "\n"

    def complete(self):
        base = self.snapshots_dir()
        ids = sorted({p[len(base) + 1:].split("/")[0] for p in self.files if p.startswith(base + "/")}, reverse=True)
        return [i for i in ids if self.files.get(os.path.join(base, i, "sha"), "").strip()]

    def _sh(self, argv, f):
        text = argv[2]
        if text.startswith("REFRESH "):
            f.dirs.add(text.split(" ", 1)[1])
            f.refs = f.upstream
            return Result(0, "mirror-fetch origin ok\nmirror-fetch wpe FAILED\n")
        if text.startswith("CHECK "):
            return f.base_check
        if text.startswith("WIRING "):
            return f.wire_result
        return Result(127, "", "no sh answer")

    base_check = Result(0)
    wire_result = Result(0)

    def _exec(self, argv, f):
        ws, script = argv[2], argv[-1]
        if script.startswith(("WIRING", "UPSTREAMFIX", "GITWEBKIT")):
            f.steps.append("%s %s" % (script.split()[0], ws))
            return f.fixes.get((ws, script.split()[0]), Result(0, "setup=ok\n" if script.startswith("GITWEBKIT") else ""))
        f.steps.append("FETCH %s" % ws)
        r = f.fetched.get(ws, Result(0, "from=mirror\r\nfetch=0\r\ncheck=0\r\n"))
        return r(argv) if callable(r) else r

    def _git(self, argv, f):
        """A snapshot is on its branch, tracking it, once `verified` names it."""
        tree, args = argv[2], argv[3:]
        if tree == f.mirror and args[:1] == ["for-each-ref"]:
            return Result(0, f.refs)
        if tree.startswith(f.snapshots_dir() + "/") and args[:2] == ["symbolic-ref", "--quiet"]:
            return Result(0, "refs/heads/main\n") if tree[len(f.snapshots_dir()) + 1:].split("/")[0] in f.verified else Result(1)
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return Result(0, "origin/main\n")
        if args[:2] == ["rev-parse", "refs/heads/main"]:
            return Result(0, MAIN_SHA + "\n")
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return Result(0 if args[3][len("refs/heads/"):] in f.heads else 1)
        if args[:2] == ["rev-parse", "--symbolic-full-name"]:
            b = args[2]
            return Result(0, "refs/remotes/%s\n" % b) if b.startswith("origin/") else Result(0, "refs/heads/%s\n" % b)
        if args[:2] == ["symbolic-ref", "--short"]:
            return Result(0, "main\n")
        if args == ["rev-parse", "HEAD"]:
            return Result(0, MAIN_SHA + "\n")
        if argv[1] == "clone":
            f.dirs.update({argv[-1], argv[-1] + "/.git"})
        return Result(0)

    def _cp(self, argv, f):
        src, dst = argv[-2], argv[-1]
        f.dirs.update({dst} | {dst + d[len(src):] for d in f.dirs if d.startswith(src + "/")})
        return Result(0)

    def rel(self, value):
        if isinstance(value, tuple):
            return tuple(self.rel(v) for v in value)
        return value.replace(str(self.tmp), "") if isinstance(value, str) else value

    def state(self):
        """What a sync leaves in the store: a lock is process coordination, not state."""
        store = self.env["WK_STORE"]
        return (sorted(self.rel(p) for p in self.files if p.startswith(store)),
                sorted(self.rel(d) for d in self.dirs if d.startswith(store + "/base")), sorted(self.rel(d) for d in self.dirs if d == self.mirror))

    def work(self):
        return [e for e in self.effects if not (isinstance(e[1], str) and e[1].startswith(self.env["WK_LOCK_DIR"]))]


class Recording(World):
    """Every act_run as ("act", argv) in both modes, so a dry run's plan can be held against a wet run's."""

    def act_run(self, argv, **kw):
        self.effects.append(("act", tuple(argv)))
        if act.dry_run():
            return Result(0)
        return super().act_run(argv, **kw)

    def mutations(self):
        return [self.rel(e) for e in self.work() if e[0] in ("act", "write", "mkdir", "remove")]


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-sync-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        osenv = mock.patch.dict(os.environ, {}, clear=False)
        osenv.start()
        self.addCleanup(osenv.stop)
        for v in ("WK_DRY_RUN", "WK_YES", "WK_QUIET", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_CMD", "WK_DEBUG",
                  "WK_BRANCH", "WK_IN_VM", "WK_PLACE", "WK_NAME", "WK_NO_DELEGATE"):
            os.environ.pop(v, None)
        for (module, name), fn in GENERATORS.items():
            p = mock.patch.object(module, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(places.record, "host_name", return_value="here")
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("wk.store.Store.is_local", lambda st: self.w.local_store)
        p.start()
        self.addCleanup(p.stop)
        self.w = self.make_world()

    def make_world(self, kinds=None, cls=World):
        return cls(self.tmp, kinds)

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


class TestParse(SyncTest):
    def parse(self, *args, name=""):
        return cmd.parse(list(args), name)

    def test_each_scope_and_what_it_names(self):
        for args, want in ((( ), ("here", "", "", False)),
                           (("myws",), ("ws", "myws", "", False)),
                           (("--all",), ("all", "", "", False)),
                           (("--mirror",), ("mirror", "", "", False)),
                           (("--on", "moose"), ("place", "", "moose", False)),
                           (("--tools",), ("tools", "", "", False)),
                           (("--tools", "buildbox4"), ("tools", "", "buildbox4", False)),
                           (("--fix",), ("here", "", "", True)),
                           (("myws", "--fix"), ("ws", "myws", "", True))):
            with self.subTest(args=args):
                self.assertEqual(self.parse(*args), want)

    def test_inside_a_workspace_the_dispatchers_name_is_the_workspace(self):
        self.assertEqual(self.parse(name="selftest-ws"), ("ws", "selftest-ws", "", False))

    def test_two_scopes_or_a_name_and_a_scope_are_refused_rather_than_last_one_wins(self):
        for pair in (("--all", "--tools"), ("--tools", "--all"), ("--all", "--on", "moose"),
                     ("--on", "moose", "--all"), ("--mirror", "--all"), ("myws", "--all")):
            with self.subTest(pair=pair):
                self.assertIn("ask for different things -- one at a time", self.refused(lambda: self.parse(*pair)))

    def test_a_second_target_is_refused_rather_than_overwriting_the_first(self):
        self.assertIn("one place at a time (got 'moose' and 'buildbox4')",
                      self.refused(lambda: self.parse("--on", "moose", "--on", "buildbox4")))

    def test_an_invalid_name_is_refused(self):
        self.assertIn("invalid name", self.refused(lambda: self.parse("bad/name")))


class TestWhere(unittest.TestCase):
    """`dispatch.where[sync]`: a workspace's name goes to the machine holding it; every scope flag is this
    host's, so none of them enters the podman VM as a forwarded command."""

    def test_the_answers(self):
        for args, inside, want in (((), False, "host"), (("myws",), False, "workspace"), (("--fix",), False, "host"),
                                   (("myws", "--fix"), False, "workspace"), ((), True, "workspace"), (("--fix",), True, "workspace"),
                                   (("--all",), True, "host"), (("--on=moose",), False, "host"), (("--tools", "buildbox4"), False, "host"),
                                   (("--mirror",), False, "host"), (("--fix", "--all"), False, "host")):
            with self.subTest(args=args, inside=inside):
                self.assertEqual(sync.where(inside, list(args)), want)

    def test_the_dispatcher_asks_cmd_sync_and_nothing_else_decides(self):
        self.assertEqual(Decl(CMD_SYNC).where_for(["myws"]), "dynamic")
        cp = subprocess.run([str(CMD_SYNC), "--where", "myws"], capture_output=True, text=True, timeout=30)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "workspace"), cp.stderr)


class Steps(sync.Sync):
    """The run's order: the mirror, the snapshot and the fetches record themselves and do nothing."""

    def sync_mirror(self):
        self.here.steps.append("MIRROR")
        return True

    def remount_guests(self):
        self.here.steps.append("REMOUNT")
        return 0

    def sync_snapshot(self, driver):
        self.here.steps.append("SNAPSHOT %s" % driver.name)
        if driver.name in self.here.refuse_publish:
            raise Refused(1)

    def base_wiring(self, driver):
        return 0

    def fetch_workspaces(self, driver, names):
        self.here.steps.append("FETCH-IN %s: %s" % (driver.name, " ".join(names)))
        return 0


class TestWhatEachScopeRuns(SyncTest):
    """`unit sync.scopes`: the tooling copies, this machine's mirror, then each place's snapshot and the fetch
    in its workspaces -- a snapshot is cloned off the mirror and a workspace fetches from it, so either before
    the refresh hands back what the machine already had."""

    KINDS = {"container": "container", "vm": "vm", "buildbox4": "remote"}

    def setUp(self):
        super().setUp()
        self.w = self.make_world(self.KINDS)
        self.w.refuse_publish = set()
        for t in self.KINDS:
            self.w.workspaces[t] = ["%s-ws" % t]

    def steps(self, scope="here", place="", only="", fix=False):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), scope, only, place, fix)
        rc, err = self.stderr(s.run)
        return rc, self.w.steps, err

    def test_bare_is_the_tooling_the_mirror_then_each_target_here(self):
        rc, steps, _ = self.steps()
        self.assertEqual((rc, steps), (0, ["FURNITURE container", "FURNITURE vm", "MIRROR", "REMOUNT", "SNAPSHOT container",
                                           "FETCH-IN container: container-ws", "FETCH-IN vm: vm-ws"]))

    def test_a_named_target_refreshes_its_furniture_before_fetching_in_it(self):
        rc, steps, _ = self.steps("place", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "REMOUNT", "SNAPSHOT container", "FETCH-IN container: container-ws"])

    def test_a_guest_target_gets_the_mirror_and_a_fetch_but_no_snapshot(self):
        rc, steps, _ = self.steps("place", "vm")
        self.assertEqual(steps, ["FURNITURE vm named", "MIRROR", "REMOUNT", "FETCH-IN vm: vm-ws"])

    def test_all_reaches_every_workspace_on_every_place(self):
        rc, steps, _ = self.steps("all")
        self.assertEqual(steps, ["FURNITURE container", "FURNITURE vm", "FURNITURE buildbox4", "MIRROR", "REMOUNT", "SNAPSHOT container",
                                 "FETCH-IN container: container-ws", "FETCH-IN vm: vm-ws", "FETCH-IN buildbox4: buildbox4-ws"])

    def test_tools_refreshes_every_machines_copy_and_publishes_one_snapshot(self):
        rc, steps, _ = self.steps("tools")
        self.assertEqual(steps, ["FURNITURE container", "FURNITURE vm", "FURNITURE buildbox4", "MIRROR", "REMOUNT", "SNAPSHOT container"])
        self.w.steps.clear()
        rc, steps, _ = self.steps("tools", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "REMOUNT", "SNAPSHOT container"])

    def test_a_machine_of_its_own_touches_neither_the_mirror_nor_a_snapshot_here(self):
        rc, steps, _ = self.steps("place", "buildbox4")
        self.assertEqual(steps, ["FURNITURE buildbox4 named", "FETCH-IN buildbox4: buildbox4-ws"])

    def test_an_unknown_target_is_refused_by_name_before_anything_runs(self):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), "place", "", "nosuchthing")
        self.assertIn("unknown place 'nosuchthing'", self.refused(s.run))
        self.assertEqual(self.w.steps, [])

    def test_a_store_in_the_podman_vm_is_asked_with_the_same_scope_word(self):
        self.w.local_store = False
        self.w.far["container"] = "answering"
        _, steps, _ = self.steps("place", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "REMOUNT", "ASKED container: wk sync --on container"])
        self.w.steps.clear()
        _, steps, _ = self.steps("tools", "container", fix=True)
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "REMOUNT", "ASKED container: wk sync --tools container --fix"])

    def test_a_stopped_podman_machine_is_named_and_is_not_a_success(self):
        self.w.local_store = False
        self.w.far["container"] = "stopped"
        rc, steps, err = self.steps("place", "container")
        self.assertEqual((rc, steps), (1, ["FURNITURE container named", "MIRROR", "REMOUNT"]))
        self.assertIn("podman machine is stopped", err)
        self.assertIn("wk start, then  wk sync --on container", err)

    def test_inside_the_podman_vm_the_mirror_is_read_not_refreshed(self):
        self.w.reg.env["WK_IN_VM"] = "1"
        _, steps, _ = self.steps("place", "container")
        self.assertEqual(steps, ["FURNITURE container named", "SNAPSHOT container", "FETCH-IN container: container-ws"])

    def test_a_target_that_did_not_take_the_tooling_fails_the_run_and_the_rest_still_runs(self):
        self.w.refuse_tools.add("vm")
        rc, steps, err = self.steps()
        self.assertEqual(rc, 1)
        self.assertIn("1 place(s) did not take the tooling", err)
        self.assertIn("FETCH-IN vm: vm-ws", steps)

    def test_a_failed_publish_does_not_stop_the_fetches_and_is_not_a_success(self):
        self.w.refuse_publish.add("container")
        rc, steps, _ = self.steps("place", "container")
        self.assertEqual((rc, steps[-1]), (1, "FETCH-IN container: container-ws"))

    def test_one_workspace_fetches_in_that_one_and_refreshes_no_mirror(self):
        self.w.reg.env["WK_PLACE"] = "container"
        rc, steps, _ = self.steps("ws", only="bug-238")
        self.assertEqual(steps, ["FETCH-IN container: bug-238"])

    def test_the_mirror_alone(self):
        rc, steps, _ = self.steps("mirror")
        self.assertEqual((rc, steps), (0, ["MIRROR", "REMOUNT"]))

    def test_inside_a_workspace_a_bare_sync_is_the_mirror_then_this_one_alone_even_when_the_refresh_failed(self):
        Path(self.w.env["WK_MARKER"]).write_text("name=ws\n")
        self.w.reg.env["WK_PLACE"] = "container"
        for asked in (0, 1):
            self.w.steps.clear()
            with mock.patch.object(sync.Sync, "mirror_refresh_request",
                                   side_effect=lambda: self.w.steps.append("ASK BROKER") or asked):
                rc, steps, _ = self.steps("ws", only="ws")
            self.assertEqual((rc, steps), (asked, ["ASK BROKER", "FETCH-IN container: ws"]))

    def test_in_the_podman_vm_or_a_workspace_the_mirror_is_a_request(self):
        self.w.reg.env["WK_IN_VM"] = "1"
        with mock.patch.object(sync.Sync, "mirror_refresh_request", return_value=0) as ask:
            self.assertEqual(self.steps("mirror")[:2] + (ask.called,), (0, [], True))
        self.w.reg.env.pop("WK_IN_VM")
        Path(self.w.env["WK_MARKER"]).write_text("name=ws\n")
        for rc_asked, want in ((0, 0), (1, 1)):
            with mock.patch.object(sync.Sync, "mirror_refresh_request", return_value=rc_asked) as ask:
                rc, steps, _ = self.steps("mirror")
            with self.subTest(rc=rc_asked):
                self.assertEqual((rc, steps, ask.called), (want, [], True))

    def test_the_mirror_and_the_publish_hold_the_store_lock(self):
        lock = self.w.lock()
        held = []
        real = lock.held

        def spy(resource, timeout=600):
            held.append(resource)
            return real(resource, timeout)
        lock.held = spy
        s = Steps(self.w.reg, self.w.clock, lock, "place", "", "container")
        self.stderr(s.run)
        self.assertEqual(held, ["store", "store"])


class TestTheRefreshAskedOfTheBroker(SyncTest):
    """In a workspace the mirror is mounted read-only, so the refresh is one request to the broker, which runs
    `wk sync --mirror` outside; with no broker listening it says how to open the door and what ran instead."""

    def ask(self, sock=True, rc=0):
        self.w.answer(["test", "-S"], rc=0 if sock else 1)
        self.w.answer(["env"], rc=rc, out="refreshed\n")
        return self.stderr(self.w.sync("mirror").mirror_refresh_request)

    def client_runs(self):
        return [e[1] for e in self.w.effects if e[0] == "run" and e[1][0] == "env"]

    def test_no_broker_is_named_with_the_stage_that_makes_one_and_asks_nothing(self):
        Path(self.w.env["WK_MARKER"]).write_text("name=ws\n")
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=False):
            rc, err = self.ask(sock=False)
        self.assertEqual(rc, 1)
        self.assertIn("no request broker at /run/wk/broker.sock", err)
        self.assertIn("./setup --stage broker", err)
        self.assertEqual(self.client_runs(), [])

    def test_a_macos_guest_asks_at_the_socket_its_start_forwards_into_its_home(self):
        Path(self.w.env["WK_MARKER"]).write_text("name=ws\n")
        with mock.patch.object(Store, "macos_host", new_callable=mock.PropertyMock, return_value=True):
            err = self.ask(sock=False)[1]
        self.assertIn("no request broker at %s/.wk-broker.sock" % Store(self.w.env).home(), err)

    def test_the_podman_vm_asks_at_the_socket_the_broker_publishes_into_it(self):
        self.w.env.update({"WK_IN_VM": "1", "XDG_RUNTIME_DIR": "/run/user/501"})
        self.assertIn("no request broker at /run/user/501/wk/broker.sock", self.ask(sock=False)[1])

    def test_the_request_is_the_client_asking_for_sync_and_its_status_is_the_answer(self):
        client = str(REPO / "container" / "broker" / "wk-broker-client.py")
        self.w.files[client] = ""
        self.w.env["WK_BROKER_SOCKET"] = "/run/x.sock"
        self.assertEqual(self.ask(), (0, "refreshed\n"))
        self.assertEqual(self.client_runs(), [("env", "WK_BROKER_SOCKET=/run/x.sock", "python3", client, "sync")])
        self.assertEqual(self.ask(rc=3)[0], 1)

    def test_a_tree_without_the_client_is_refused_naming_the_tools_sync(self):
        self.w.answer(["test", "-S"])
        self.assertIn("wk sync --tools container", self.refused(self.w.sync("mirror").mirror_refresh_request))


class TestWhereEachPlacesWorkspacesAreFetched(SyncTest):
    """sync_place: a plain place's workspaces are fetched from here; a peer is asked for each by name and never
    with a scope word -- what a scope means is that machine's copy of wk-tools to decide."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world({"buildbox4": "remote", "moose": "peer"})
        self.w.workspaces = {"buildbox4": ["ws-a", "ws-b"], "moose": ["ws-a", "ws-b"]}

    def route(self, name, fix=False):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), "place", "", name, fix)
        rc, err = self.stderr(lambda: s.sync_place(self.w.reg.load(name)))
        return rc, self.w.steps, err

    def test_a_plain_target_fetches_in_its_workspaces_from_here(self):
        self.assertEqual(self.route("buildbox4")[:2], (0, ["FETCH-IN buildbox4: ws-a ws-b"]))

    def test_a_peer_is_asked_for_each_workspace_by_name(self):
        rc, steps, err = self.route("moose")
        self.assertEqual(steps, ["ASKED moose: wk sync ws-a (no-delegate)", "ASKED moose: wk sync ws-b (no-delegate)"])
        self.assertIn("said sync ws-a", err)

    def test_fix_travels_to_the_peer_and_an_old_copy_is_named(self):
        self.w.wk_rc["ws-b"] = 2
        rc, steps, err = self.route("moose", fix=True)
        self.assertEqual(steps[0], "ASKED moose: wk sync ws-a --fix (no-delegate)")
        self.assertEqual(rc, 1)
        self.assertIn("moose did not fetch in 'ws-b'", err)
        self.assertIn("wk sync --tools moose", err)

    def test_a_peer_that_did_not_fetch_is_not_a_success(self):
        self.w.wk_rc["ws-a"] = 1
        rc, _, err = self.route("moose")
        self.assertEqual(rc, 1)
        self.assertNotIn("older than", err)

    def test_no_workspaces_says_so(self):
        self.w.workspaces = {}
        for t in ("buildbox4", "moose"):
            with self.subTest(driver=t):
                rc, steps, err = self.route(t)
                self.assertEqual((rc, steps), (0, []))
                self.assertIn("no workspaces on %s" % t, err)


class TestFurniture(SyncTest):
    def test_a_named_sweep_says_status_compares_every_copy(self):
        self.w = self.make_world({"container": "container", "buildbox4": "remote"})
        rc, err = self.stderr(self.w.sync("tools", place="buildbox4").sync_furniture)
        self.assertEqual((rc, self.w.steps), (0, ["FURNITURE buildbox4 named"]))
        self.assertIn("'wk status' compares every copy against this one", err)


class TestTheMirror(SyncTest):
    """The refresh, what it reports, and the reading taken after it."""

    def mirror(self, heads=("main",)):
        self.w.heads = list(heads)
        return self.stderr(self.w.sync("mirror").sync_mirror)[1]

    def test_the_first_refresh_makes_it_and_reports_each_upstream(self):
        err = self.mirror()
        self.assertIn(("run", ("sh", "-c", "REFRESH %s" % self.w.mirror)), self.w.effects)
        self.assertIn(("mkdir", os.path.dirname(self.w.mirror)), self.w.effects)
        self.assertIn("creating bare mirror", err)
        self.assertIn("fetching origin wpe fork forkwpe (origin: main)", err)
        self.assertIn("  origin   ok", err)
        self.assertIn("  wpe      FAILED (continuing)", err)
        self.assertNotIn("creating bare mirror", self.mirror())

    def test_no_main_at_all_is_fatal(self):
        self.w.heads = []
        self.assertIn("main was not fetched", self.refused(self.w.sync("mirror").sync_mirror))

    def test_a_refresh_that_failed_is_named(self):
        self.w.react(["sh", "-c"], lambda a, f: Result(1, "", "boom"))
        self.assertIn("the mirror refresh did not finish", self.mirror())

    def test_wk_mirror_branches_carries_the_extra_branches(self):
        self.w.reg.env["WK_MIRROR_BRANCHES"] = "main webkitglib/2.52"
        with mock.patch.object(images, "mirror_branches", REAL_MIRROR_BRANCHES):
            err = self.mirror(heads=("main",))
            self.assertIn("(origin: main webkitglib/2.52)", err)
            self.assertIn("advertises no webkitglib/2.52", err)
            self.assertIn("CFG_BRANCH", err)
            self.assertNotIn("advertises no", self.mirror(heads=("main", "webkitglib/2.52")))

    def test_stages_are_timed_under_wk_debug(self):
        os.environ["WK_DEBUG"] = "1"
        self.assertRegex(self.mirror(), r"stage mirror fetch: \d+s")
        os.environ.pop("WK_DEBUG")
        self.assertNotIn("stage mirror fetch", self.mirror())


class FakeVm(places.Vm):
    """The guest driver with tart's listing and the ssh into a guest answered by the World."""

    def list(self):
        return sorted(self.machine.guests.items())

    def exec(self, ws, argv, tty=False, timeout=None):
        return self.machine.run(["guest", ws] + list(argv))


class TestTheGuestsRemount(SyncTest):
    """`unit sync.guest_remount`: after the refresh, each running guest mounts the mirror's own share afresh,
    since its old mount keeps the inode of every ref git renamed over; a guest that cannot is named with the
    restart."""

    def setUp(self):
        super().setUp()
        self.w.reg = self.w.registry({"container": "container", "vm": "vm"}, guests=True)
        self.w.guests = {"up-a": "running", "down": "stopped", "up-b": "running"}
        self.w.answer(["guest"])

    def remounts(self):
        return [e[1][1] for e in self.w.effects if e[0] == "run" and e[1][0] == "guest"]

    def test_each_running_guest_remounts_and_a_stopped_one_does_not(self):
        rc, _ = self.stderr(self.w.sync("mirror").run)
        self.assertEqual((rc, self.remounts()), (0, ["up-a", "up-b"]))

    def test_a_refresh_that_moved_no_ref_remounts_nothing(self):
        self.stderr(self.w.sync("mirror").run)
        self.w.effects = []
        rc, _ = self.stderr(self.w.sync("mirror").run)
        self.assertEqual((rc, self.remounts()), (0, []))
        self.w.upstream = "f" * 40 + " refs/heads/main\n"
        self.stderr(self.w.sync("mirror").run)
        self.assertEqual(self.remounts(), ["up-a", "up-b"])

    def test_the_remount_is_the_mirror_tag_alone_never_agent_rw(self):
        self.stderr(self.w.sync("mirror").run)
        asked = [e[1][2:] for e in self.w.effects if e[0] == "run" and e[1][0] == "guest"]
        self.assertEqual(asked, [("sudo", "-n", places.GUEST_MOUNT_MIRROR, places.MIRROR_TAG, places.GUEST_MIRROR_MOUNT)] * 2)
        self.assertNotIn(places.GUEST_SHARES, " ".join(" ".join(a) for a in asked))

    def test_the_remount_converges_from_a_share_mounted_or_not(self):
        bindir, at = self.tmp / "bin", str(self.tmp / "mnt")
        bindir.mkdir()
        stubs = {"mount": '[ -f "$STATE" ] && echo "%s on %s (virtiofs, local)"\nexit 0' % (places.MIRROR_TAG, at),
                 "umount": '[ "$1" = "%s" ] && [ -f "$STATE" ] && rm "$STATE"' % at,
                 "mount_virtiofs": '[ "$1 $2" = "%s %s" ] && [ ! -f "$STATE" ] && touch "$STATE"' % (places.MIRROR_TAG, at)}
        for stub, body in stubs.items():
            (bindir / stub).write_text("#!/bin/sh\n%s\n" % body)
            os.chmod(bindir / stub, 0o755)
        for mounted in (True, False):
            with self.subTest(mounted=mounted):
                state = self.tmp / "mounted"
                if mounted:
                    state.write_text("")
                elif state.exists():
                    state.unlink()
                cp = subprocess.run([str(REPO / "vm" / "mount-mirror.sh"), places.MIRROR_TAG, at], capture_output=True,
                                    text=True, timeout=30,
                                    env=dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), STATE=str(state)))
                self.assertEqual((cp.returncode, state.exists()), (0, True), cp.stderr)

    def test_a_dry_run_lists_each_remount_and_runs_none(self):
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.stderr(self.w.sync("mirror").run)
        self.assertEqual((rc, self.remounts()), (0, []))
        for ws in ("up-a", "up-b"):
            self.assertIn("would run in %s: sudo -n %s" % (ws, places.GUEST_MOUNT_MIRROR), err)
        self.assertNotIn("would run in down", err)

    def test_a_busy_share_is_named_with_the_restart_and_the_refresh_still_succeeds(self):
        self.w.react(["guest", "up-a"], lambda a, f: Result(16, "", "umount(/Volumes/wk-mirror): Resource busy\n"))
        rc, err = self.stderr(self.w.sync("mirror").run)
        self.assertEqual((rc, self.remounts()), (0, ["up-a", "up-b"]))
        self.assertIn("'up-a' could not remount the mirror share (umount(/Volumes/wk-mirror): Resource busy)", err)
        self.assertIn("wk stop up-a, then  wk start up-a", err)
        self.assertNotIn("'up-b' could not", err)

    def test_no_guest_target_here_remounts_nothing(self):
        self.w.reg = self.w.registry({"container": "container"}, guests=True)
        rc, _ = self.stderr(self.w.sync("mirror").run)
        self.assertEqual((rc, self.remounts()), (0, []))


class TestTheSnapshot(SyncTest):
    """sync_snapshot: a `--shared` clone of the mirror (or a hardlinked copy of the last snapshot), wired,
    fetched, on a branch tracking the one it was published from, clean, with `sha` the completion marker
    written last."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(cls=Recording)

    def publish_raw(self):
        self.w.sync("place", place="container").sync_snapshot(self.w.reg.load("container"))

    def publish(self):
        return self.stderr(self.publish_raw)[1]

    def acts(self):
        return [e[1] for e in self.w.effects if e[0] == "act"]

    def test_no_mirror_is_refused_naming_the_host(self):
        self.assertIn("'wk sync' on the host makes it", self.refused(self.publish_raw))

    def test_the_first_snapshot_is_a_shared_clone_on_its_branch_with_the_sha_last(self):
        self.w.dirs.add(self.w.mirror)
        err = self.publish()
        new = os.path.join(self.w.snapshots_dir(), self.w.clock.stamp())
        tree, m = new + "/WebKit", self.w.mirror
        self.assertEqual(self.acts(), [
            ("git", "clone", "--quiet", "--shared", m, tree),
            ("sh", "-c", "WIRING %s %s   " % (tree, m)),
            ("git", "-C", tree, "fetch", "--all", "--prune", "--quiet"),
            ("git", "-C", tree, "checkout", "--quiet", "-B", "main", "refs/remotes/origin/main"),
            ("git", "-C", tree, "branch", "--quiet", "--set-upstream-to", "origin/main", "main"),
            ("git", "-C", tree, "reset", "--hard", "--quiet"),
            ("git", "-C", tree, "clean", "-qfdx")])
        self.assertEqual([e[1] for e in self.w.effects if e[0] == "write"], [new + "/branch", new + "/sha"])
        self.assertEqual(self.w.files[new + "/sha"], MAIN_SHA + "\n")
        self.assertIn("checked out on branch main (tracking origin/main)", err)
        self.assertIn("published base %s (%s)" % (self.w.clock.stamp(), MAIN_SHA[:10]), err)

    def test_a_verified_current_snapshot_is_left_alone(self):
        self.w.dirs.add(self.w.mirror)
        self.w.publish("20200101T000000Z")
        self.w.verified.add("20200101T000000Z")
        self.publish()
        self.assertEqual((self.acts(), [e for e in self.w.effects if e[0] == "write"]), ([], []))
        self.assertEqual(self.w.complete(), ["20200101T000000Z"])

    def test_a_refused_snapshot_with_the_current_sha_is_republished_from_it(self):
        self.w.dirs.add(self.w.mirror)
        self.w.publish("20200101T000000Z")
        self.publish()
        self.assertEqual(self.acts()[0], ("cp", "-al", self.w.store.snapshot_tree("20200101T000000Z"),
                                          os.path.join(self.w.snapshots_dir(), self.w.clock.stamp(), "WebKit")))
        self.assertEqual(self.w.complete()[0], self.w.clock.stamp())

    def test_another_branch_is_published_even_when_main_is_current(self):
        self.w.dirs.add(self.w.mirror)
        self.w.publish("20200101T000000Z")
        self.w.verified.add("20200101T000000Z")
        self.w.reg.env["WK_BRANCH"] = "origin/wpe-2.46"
        err = self.publish()
        self.assertIn("tracking origin/wpe-2.46", err)
        self.assertEqual(self.w.files[os.path.join(self.w.snapshots_dir(), self.w.clock.stamp(), "branch")], "origin/wpe-2.46\n")

    def test_a_branch_that_is_not_remote_tracking_is_refused_and_the_half_publish_removed(self):
        self.w.dirs.add(self.w.mirror)
        self.w.reg.env["WK_BRANCH"] = "main"
        err = self.refused(self.publish_raw)
        self.assertIn("<remote>/<branch>", err)
        self.assertIn("origin/main", err)
        self.assertNotIn(os.path.join(self.w.snapshots_dir(), self.w.clock.stamp()), self.w.dirs)

    def test_a_failed_clone_is_refused_and_removed(self):
        self.w.dirs.add(self.w.mirror)
        self.w.react(["git", "clone"], lambda a, f: Result(128, "", "fatal: no space"))
        self.assertIn("fatal: no space", self.refused(self.publish_raw))
        self.assertNotIn(os.path.join(self.w.snapshots_dir(), self.w.clock.stamp()), self.w.dirs)

    def test_a_publish_killed_after_any_effect_and_rerun_converges(self):
        def world():
            w = self.make_world()
            w.publish("20190101T000000Z", sha="b" * 40)
            return w

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.sync("tools", place="container").run()
        converges(self, world, run_once, World.state)
        w = world()
        run_once(w)
        self.assertEqual(w.complete(), [w.clock.stamp(), "20190101T000000Z"])

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        def world():
            w = self.make_world(cls=Recording)
            w.dirs.add(w.mirror)
            w.publish("20190101T000000Z", sha="b" * 40)
            w.workspaces["container"] = ["ws"]
            return w
        wet = world()
        self.stderr(wet.sync("place", place="container", fix=True).run)
        dry = world()
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.stderr(dry.sync("place", place="container", fix=True).run)
        self.assertEqual(dry.mutations(), wet.mutations())
        self.assertGreaterEqual(len(dry.mutations()), 10)
        self.assertEqual(dry.state(), before)


class TestTheBaseWiring(SyncTest):
    """The current snapshot is where every future workspace gets its remotes from: read back on every sync of its
    place, and re-wired under --fix."""

    def setUp(self):
        super().setUp()
        self.current = "20200101T000000Z"
        self.w.publish(self.current)
        self.w.verified.add(self.current)

    def wiring(self, fix=False):
        s = self.w.sync("place", place="container", fix=fix)
        return self.stderr(lambda: s.base_wiring(self.w.reg.load("container")))

    def test_a_right_one_says_nothing(self):
        self.assertEqual(self.wiring(), (0, ""))

    def test_a_wrong_one_is_named_with_its_problems_and_the_remedy(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.wiring()
        self.assertEqual(rc, 1)
        self.assertIn("the snapshot 20200101T000000Z is wired wrong", err)
        self.assertIn("    - origin is /x", err)
        self.assertIn("wk sync --on container --fix", err)

    def test_fix_re_wires_it(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.wiring(fix=True)
        self.assertEqual(rc, 0)
        self.assertIn("re-wired the snapshot", err)
        self.assertIn(("sh", "-c", "WIRING %s %s   " % (self.w.store.snapshot_tree(self.current), self.w.mirror)),
                      [e[1] for e in self.w.effects if e[0] == "run"])
        self.w.wire_result = Result(1)
        self.assertEqual(self.wiring(fix=True)[0], 1)

    def test_no_snapshot_is_nothing_to_read(self):
        self.w.verified.clear()
        self.assertEqual(self.wiring(), (0, ""))


class TestTheFetch(SyncTest):
    """One fetch per workspace, all at once, one line each in the order named -- every line derived from what
    that fetch said, since `wk sync --all` is often the only thing anybody reads about a machine's workspaces."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world({"container": "container", "buildbox4": "remote", "vm": "vm"})

    def fetch_raw(self, *names, scope="all", fix=False, place="container"):
        s = self.w.sync(scope, only=names[0] if scope == "ws" else "", fix=fix)
        return s.fetch_workspaces(self.w.reg.load(place), list(names))

    def fetch(self, *names, **kw):
        return self.stderr(lambda: self.fetch_raw(*names, **kw))

    def test_the_script_is_one_fetch_and_the_report_names_the_mirror(self):
        rc, err = self.fetch("one")
        self.assertEqual(rc, 0)
        self.assertIn("  %-24s ok  (/mirror/container/WebKit.git)" % "one", err)
        script = next(e[1][-1] for e in self.w.effects if e[1][:1] == ("exec",))
        self.assertIn("cd /src/WebKit || exit 1", script)
        self.assertIn("git fetch --all --prune --quiet", script)
        self.assertIn("url./mirror/container/WebKit.git.insteadOf", script)
        self.assertIn("CHECK /src/WebKit /mirror/container/WebKit.git", script)
        self.assertNotIn("github.com", script)
        self.assertIn("fetching in 1 workspace(s) -- origin wpe fork forkwpe", err)

    def test_the_listing_is_in_the_order_asked_for_not_finished_in(self):
        def slow(argv):
            time.sleep(0.3)
            return Result(0, "from=mirror\nfetch=0\ncheck=0\n")
        self.w.fetched = {"slow": slow, "bad": Result(0, "from=mirror\nfetch=1\ncheck=0\n")}
        self.w.states = {"gone": "absent"}
        rc, err = self.fetch("slow", "gone", "bad")
        rows = [l.split()[0] for l in err.splitlines() if l.startswith("  ") and not l.startswith("    ")]
        self.assertEqual(rows, ["slow", "gone", "bad"])
        self.assertIn("absent -- skipped", err)
        self.assertIn("bad" + " " * 22 + "FAILED (continuing)", err)
        self.assertIn("1 workspace(s) did not fetch", err)
        self.assertEqual(rc, 1)

    def test_they_run_at_once(self):
        met, broken = threading.Barrier(3, timeout=10), []

        def wait(argv):
            try:
                met.wait()
            except threading.BrokenBarrierError:
                broken.append(argv[2])
            return Result(0, "from=mirror\nfetch=0\ncheck=0\n")
        self.w.fetched = {n: wait for n in ("a", "b", "c")}
        self.fetch("a", "b", "c")
        self.assertEqual(broken, [], "the fetches ran in turn")

    def test_the_one_named_being_absent_is_a_refusal_not_a_skip(self):
        self.w.states = {"one": "absent"}
        self.assertIn("workspace 'one' is not there to fetch in", self.refused(lambda: self.fetch_raw("one", scope="ws")))

    def test_a_machine_that_does_not_answer_is_a_skip(self):
        self.w.states = {"one": "refused"}
        self.assertIn("unreachable -- skipped", self.fetch("one")[1])

    def test_the_source_reported_is_the_one_the_checkout_answered_with(self):
        self.w.fetched = {"one": Result(0, "from=github\nfetch=0\ncheck=0\n")}
        err = self.fetch("one")[1]
        self.assertIn("over the network: this checkout reads no mirror, though this machine keeps /mirror/container/WebKit.git", err)
        self.assertIn("wk sync one --fix", err)
        self.w.mirrors["container"] = ""
        err = self.fetch("one")[1]
        self.assertIn("over the network: this checkout reads no mirror)", err)
        self.assertIn(r"'^url\..*\.insteadof$'", next(e[1][-1] for e in reversed(self.w.effects) if e[1][:1] == ("exec",)))

    def test_a_checkout_that_says_nothing_is_not_reported_as_a_local_read(self):
        self.w.fetched = {"one": Result(0, "fetch=0\n")}
        self.assertIn("did not say which source", self.fetch("one")[1])

    def test_a_check_that_never_reported_is_wired_wrong_not_right(self):
        self.w.fetched = {"one": Result(0, "from=mirror\nfetch=0\n")}
        rc, err = self.fetch("one")
        self.assertEqual(rc, 1)
        self.assertIn("-- wired wrong:\n    - the wiring check did not report (it never reached its end)", err)

    def test_an_exec_that_answered_nothing_is_a_failure(self):
        self.w.fetched = {"one": Result(1, "", "no such container")}
        rc, err = self.fetch("one")
        self.assertEqual(rc, 1)
        self.assertIn("FAILED (continuing)", err)

    def test_a_deviation_in_the_wiring_is_reported_under_the_workspace(self):
        self.w.fetched = {"one": Result(0, "from=mirror\nfetch=0\nproblem: origin accepts a push (x)\ncheck=1\n")}
        rc, err = self.fetch("one")
        self.assertEqual(rc, 1)
        self.assertIn("ok  (/mirror/container/WebKit.git) -- wired wrong:", err)
        self.assertIn("    - origin accepts a push (x)", err)
        self.assertIn("1 workspace(s) fetched but are wired wrong", err)
        self.assertIn("'wk sync <ws> --fix' re-asserts the wiring", err)

    def test_a_failed_fetch_names_the_problem_the_check_found(self):
        self.w.fetched = {"one": Result(0, "from=mirror\nfetch=1\nproblem: the mirror carries no refs/heads/x\ncheck=1\n")}
        err = self.fetch("one")[1]
        self.assertIn("FAILED (continuing)\n    - the mirror carries no refs/heads/x", err)

    def test_fix_re_asserts_the_wiring_the_upstream_and_git_webkit_before_the_fetch(self):
        self.w.fixes = {("one", "UPSTREAMFIX"): Result(0, "retargeted: eng/x now tracks fork/eng/x\r\n")}
        rc, err = self.fetch("one", fix=True)
        self.assertEqual(rc, 0)
        self.assertEqual(self.w.steps, ["WIRING one", "UPSTREAMFIX one", "GITWEBKIT one", "FETCH one"])
        self.assertIn("    re-wired\n    retargeted: eng/x now tracks fork/eng/x\n    git-webkit: setup=ok", err)

    def test_fix_wires_from_the_driver_and_leaves_a_machine_of_the_persons_own_its_setup(self):
        self.fetch("one", fix=True, place="buildbox4")
        self.assertEqual(self.w.steps, ["WIRING one", "UPSTREAMFIX one", "FETCH one"])
        wired = next(e[1][-1] for e in self.w.effects if e[1][:1] == ("exec",))
        self.assertEqual(wired, "WIRING /src/WebKit /mirror/buildbox4/WebKit.git mirror /far/mirror /far/ssh/config")
        self.w.steps.clear()
        self.fetch("one", fix=True, place="vm")
        self.assertIn("GITWEBKIT one", self.w.steps)

    def test_a_git_webkit_setup_that_failed_names_both_remedies(self):
        self.w.fixes = {("one", "GITWEBKIT"): Result(1, "setup=failed\n", "HTTP 401\n")}
        rc, err = self.fetch("one", fix=True)
        self.assertEqual(rc, 1)
        self.assertIn("-- wired wrong:", err)
        self.assertIn("    HTTP 401", err)
        self.assertIn("'git-webkit setup' did not finish (setup=failed)", err)
        self.assertIn("wk key push on", err)
        self.assertIn("wk sync one --fix", err)

    def test_a_wiring_that_did_not_take_is_named_and_the_fetch_still_runs(self):
        self.w.fixes = {("one", "WIRING"): Result(1)}
        rc, err = self.fetch("one", fix=True)
        self.assertEqual((rc, self.w.steps), (1, ["WIRING one", "FETCH one"]))
        self.assertIn("could not re-wire 'one'", err)


class TestEachDriversFurniture(SyncTest):
    def test_a_container_mounts_the_tooling_and_copies_nothing(self):
        t = places.Container("container", str(REPO), dict(self.w.env), self.w)
        ok, err = self.stderr(lambda: t.sync())
        self.assertEqual((ok, self.w.effects), (True, []))
        self.assertIn("nothing to copy", err)

    def test_a_guest_gets_its_copy_pushed_and_one_not_running_is_skipped(self):
        class Guests(places.Vm):
            pushed, result = [], True

            def list(self):
                return [("mya", "running"), ("myb", "stopped")]

            def info(self, ws):
                return "running" if ws == "mya" else "stopped"

            def sync_tools(self, ws):
                self.pushed.append(ws)
                return self.result
        t = Guests("vm", str(REPO), dict(self.w.env), self.w)
        ok, err = self.stderr(lambda: t.sync())
        self.assertTrue(ok)
        self.assertIn("mya" + " " * 22 + "ok", err)
        self.assertIn("myb" + " " * 22 + "not running -- skipped", err)
        self.assertEqual(t.pushed, ["mya"])
        self.assertEqual(self.w.effects, [], "a guest's mirror is the host's")
        t.result = False
        self.assertFalse(self.stderr(lambda: t.sync())[0])


class TestRemoteFurniture(SyncTest):
    def remote(self, peer=False, reference=""):
        env = dict(self.w.env, WK_REMOTE_LOCAL="1", WK_REMOTE_TOOLS="/far/wk-tools", WK_REMOTE_ROOT="/far/wk",
                   WK_REMOTE_REFERENCE=reference)
        if peer:
            env["WK_REMOTE_PEER"] = "1"
        return places.Remote("apeer" if peer else "box", str(REPO), env, self.w)

    def answer_versions(self, theirs):
        self.w.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="abc\n")
        self.w.answer(["git", "-C", str(REPO), "status"], out="")
        self.w.react(["sh", "-c"], lambda a, f: self._far(a, theirs))

    def _far(self, argv, theirs):
        text = argv[2]
        if "git pull --ff-only" in text:
            return Result(0, "Already up to date.\n")
        if text.endswith(" doctor --probe-tools"):
            return Result(0, theirs)
        if "sync --tools" in text:
            self.w.steps.append("ASKED: %s" % text)
            return Result(0, "published\n")
        if "motd" in text:
            return Result(0, "")
        if "echo \"$HOME\"" in text:
            return Result(0, "/far\nLinux\n4\n0.1 0 0\n===MEM===\nMemAvailable: 1024 kB\n===IONICE===\nno\n")
        return Result(0, "mirror-fetch origin ok\n")

    def test_a_copy_that_still_differs_is_asked_for_nothing_more(self):
        self.answer_versions("sha=0000stale0000\ndirty=no\n")
        self.w.answer(["git", "-C", str(REPO), "rev-parse", "--git-dir"], out=".git\n")
        self.w.answer(["git", "-C", str(REPO), "rev-parse", "--abbrev-ref", "@{upstream}"], out="origin/main\n")
        self.w.answer(["git", "-C", str(REPO), "status"], out=" M wk\n")
        ok, err = self.stderr(lambda: self.remote(peer=True).sync(named=True))
        self.assertFalse(ok)
        self.assertIn("pulled, still DIFFERS (0000stale000, this machine has abc+dirty)", err)
        self.assertIn("uncommitted changes -- a peer pulls from origin/main", err)
        self.assertEqual(self.w.steps, [])

    def test_a_converged_copy_named_by_this_run_publishes_its_own_snapshot(self):
        self.answer_versions("sha=abc\ndirty=no\n")
        ok, err = self.stderr(lambda: self.remote(peer=True).sync(named=True))
        self.assertTrue(ok, err)
        self.assertIn("pulled, in sync", err)
        self.assertEqual(len(self.w.steps), 1)
        self.assertIn("WK_NO_DELEGATE=1 /far/wk-tools/wk sync --tools", self.w.steps[0])

    def test_a_converged_copy_not_named_keeps_its_store_untouched(self):
        self.answer_versions("sha=abc\ndirty=no\n")
        ok, err = self.stderr(lambda: self.remote(peer=True).sync(named=False))
        self.assertTrue(ok)
        self.assertEqual(self.w.steps, [])
        self.assertIn("wk sync --tools apeer", err)

    def test_a_pull_that_failed_is_named(self):
        self.w.react(["sh", "-c"], lambda a, f: Result(1, "", "not a fast-forward"))
        ok, err = self.stderr(lambda: self.remote(peer=True).sync())
        self.assertFalse(ok)
        self.assertIn("git pull --ff-only failed there", err)

    def test_a_build_box_is_pushed_head_and_its_mirror_refreshed(self):
        self.answer_versions("")
        self.w.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="f00d\n")
        ok, err = self.stderr(lambda: self.remote().sync())
        self.assertTrue(ok, err)
        self.assertIn("box" + " " * 22 + "pushed f00d", err)
        self.assertIn("the WebKit mirror on box is up to date", err)

    def test_a_build_box_cloning_from_its_admins_reference_keeps_no_mirror(self):
        self.answer_versions("")
        with mock.patch.object(places.Remote, "sync_tools", return_value=False):
            ok, err = self.stderr(lambda: self.remote(reference="/srv/WebKit").sync())
        self.assertFalse(ok)
        self.assertIn("clone from /srv/WebKit", err)
        self.assertNotIn("mirror on box", err)


class TestWhyAPeerIsBehind(SyncTest):
    def test_each_reason(self):
        self.w = Fake("here")
        self.assertIn("not a git checkout", places.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-parse", "--git-dir"], out=".git\n")
        self.w.answer(["git", "-C", "/t", "rev-parse", "--abbrev-ref", "HEAD"], out="eng/x\n")
        self.assertIn("branch 'eng/x' has no upstream here", places.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-parse", "--abbrev-ref", "@{upstream}"], out="origin/main\n")
        self.w.answer(["git", "-C", "/t", "rev-list"], out="2\n")
        self.assertIn("2 commit(s) ahead of origin/main", places.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-list"], out="0\n")
        self.assertEqual(places.tools_why_behind(self.w, "/t"), "")

    def test_a_count_git_could_not_make_is_said_not_read_as_in_sync(self):
        self.w = Fake("here")
        self.w.answer(["git", "-C", "/t", "rev-parse", "--git-dir"], out=".git\n")
        self.w.answer(["git", "-C", "/t", "rev-parse", "--abbrev-ref", "@{upstream}"], out="origin/main\n")
        self.w.answer(["git", "-C", "/t", "rev-list"], rc=128)
        self.assertEqual(places.tools_why_behind(self.w, "/t"), "git could not count this machine's commits past origin/main")


STUB_BROKER = """
import json, os, socket, sys
sock, record = sys.argv[1], sys.argv[2]
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.bind(sock)
s.listen(1)
sys.stderr.write("ready\\n"); sys.stderr.flush()
c, _ = s.accept()
line = b""
while not line.endswith(b"\\n"):
    chunk = c.recv(65536)
    if not chunk:
        break
    line += chunk
open(record, "w").write(line.decode())
c.sendall(json.dumps({"event": "done", "ok": True, "request": "stub"}).encode() + b"\\n")
c.close()
"""


@contextlib.contextmanager
def stub_broker(tmp):
    sock, record = tmp / "broker.sock", tmp / "request.json"
    script = tmp / "stub-broker.py"
    script.write_text(STUB_BROKER)
    p = subprocess.Popen([sys.executable, str(script), str(sock), str(record)], stderr=subprocess.PIPE, text=True)
    p.stderr.readline()
    try:
        yield sock, record
    finally:
        p.kill()
        p.wait()


class TestTheBrokerClient(unittest.TestCase):
    def test_the_client_asks_for_sync_and_the_brokers_verdict_is_its_status(self):
        with tempfile.TemporaryDirectory() as tmp, stub_broker(Path(tmp)) as (sock, record):
            cp = subprocess.run([sys.executable, str(REPO / "container" / "broker" / "wk-broker-client.py"), "sync"],
                                env=dict(os.environ, WK_BROKER_SOCKET=str(sock)), capture_output=True, text=True)
            self.assertEqual(json.loads(record.read_text()), {"verb": "sync", "args": {}})
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_no_broker_is_status_2(self):
        cp = subprocess.run([sys.executable, str(REPO / "container" / "broker" / "wk-broker-client.py"), "sync"],
                            env=dict(os.environ, WK_BROKER_SOCKET="/nonexistent/broker.sock"), capture_output=True, text=True)
        self.assertEqual(cp.returncode, 2)

if __name__ == "__main__":
    unittest.main()
