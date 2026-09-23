"""`wk sync`: cmd/sync's parse and `--where` answer, and lib/wk/sync.py's
flows over fake targets on a fake machine -- what each scope runs and in
what order, the mirror refresh and what it reports, the snapshot publish and
its skip, the fetch in each workspace with the wiring read back (and
re-asserted under --fix), each driver's furniture, a publish killed after any
effect and re-run converging, and a dry run printing the wet run's plan.

Run: python3 tests/run.py -k tests.test_sync
"""

import contextlib
import importlib.machinery
import importlib.util
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

from tests.killpoints import converges
from tests.support import REPO, fake_workspace, run

sys.path.insert(0, str(REPO / "lib"))
from wk import act, shell, sync, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.decl import Decl  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

CMD_SYNC = REPO / "cmd" / "sync"
MAIN_SHA = "a" * 40


def _load_cmd():
    loader = importlib.machinery.SourceFileLoader("wk_cmd_sync", str(CMD_SYNC))
    spec = importlib.util.spec_from_file_location("wk_cmd_sync", str(CMD_SYNC), loader=loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


cmd = _load_cmd()
REAL_MIRROR_BRANCHES = shell.mirror_branches
REAL_WIRING_CHECK = shell.wiring_check_script

# The bash script generators, stood in for by a line naming what each would render: rendering is
# lib/store.sh's and tests/test_new_fetch.py runs it for real, while what is asked for is this file's.
GENERATORS = {
    "wiring_script": lambda root, src, mirror, n="", u="", c="", env=None: "WIRING %s %s %s %s %s" % (src, mirror, n, u, c),
    "wiring_check_script": lambda root, src, mirror, skip="", env=None: "CHECK %s %s %s" % (src, mirror, skip),
    "branch_upstream_fix_script": lambda root, src, env=None: "UPSTREAMFIX %s" % src,
    "gitwebkit_setup_script": lambda root, src, env=None: "GITWEBKIT %s" % src,
    "mirror_refresh_script": lambda root, mirror, env=None: "REFRESH %s" % mirror,
    "wk_remotes": lambda root, env=None: [("origin", "u1"), ("wpe", "u2"), ("fork", "u3"), ("forkwpe", "u4")],
    "mirror_branches": lambda root, env=None: ["main"],
}


class FakeTarget(targets.Target):
    """A target whose environment is the World's: workspaces, states, far side and far `wk` are its answers."""

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
        self.machine.mkdir(os.path.join(self.store.root(), "ws"))

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


class FakeRegistry(targets.Registry):
    def __init__(self, root, env, machine, kinds):
        super().__init__(root, env=env, machine=machine)
        self.kinds = kinds

    def all(self):
        return list(self.kinds)

    def default(self):
        return next(iter(self.kinds))

    def load(self, name):
        if name not in self.kinds:
            raise LookupError("unknown target '%s'.\n    The built-in ones are container, vm, remote and local." % name)
        return FakeTarget(name, self.root, dict(self.env), self.machine, self.kinds[name])


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
        self.local_store, self.verified, self.current = True, set(), ""
        self.heads = ["main"]
        self.reg = FakeRegistry(REPO, self.env, self, kinds or {"container": "container"})
        self.store = self.reg.store
        self.mirror = self.store.mirror()
        self.react(["bash", "-c"], self._bash)
        self.react(["sh", "-c"], self._sh)
        self.react(["exec"], self._exec)
        self.react(["git"], self._git)
        self.react(["cp", "-al"], self._cp)
        self.effects = []

    @property
    def fake(self):
        return self

    def lock(self):
        return Lock(self.store, self, self.clock)

    def sync(self, scope="here", only="", target="", fix=False):
        return sync.Sync(self.reg, self.clock, self.lock(), scope, only, target, fix)

    def base_dir(self):
        return self.store.base_dir()

    def publish(self, bid, sha=MAIN_SHA):
        d = os.path.join(self.base_dir(), bid)
        self.dirs.update({d, os.path.join(d, "WebKit"), os.path.join(d, "WebKit", ".git")})
        self.files[os.path.join(d, "branch")] = "origin/main\n"
        self.files[os.path.join(d, "sha")] = sha + "\n"

    def complete(self):
        base = self.base_dir()
        ids = sorted({p[len(base) + 1:].split("/")[0] for p in self.files if p.startswith(base + "/")}, reverse=True)
        return [i for i in ids if self.files.get(os.path.join(base, i, "sha"), "").strip()]

    def _bash(self, argv, f):
        script = argv[2]
        if "newest_complete_base" in script:
            done = f.complete()
            return Result(0, done[0] + "\n") if done else Result(1)
        if "base_verify" in script:
            return Result(0) if argv[-1] in f.verified else Result(1, "snapshot %s is not on branch main\n" % argv[-1])
        if "current_base" in script:
            return Result(0, f.current + "\n") if f.current else Result(1)
        if "store_is_local" in script:
            return Result(0 if f.local_store else 1)
        if "t_sync_tools" in script:
            return Result(0, "", "pushed into %s\n" % argv[-1])
        return Result(127, "", "no bash answer for: %s" % script[-60:])

    def _sh(self, argv, f):
        text = argv[2]
        if text.startswith("REFRESH "):
            f.dirs.add(text.split(" ", 1)[1])
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
        tree, args = argv[2], argv[3:]
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
                  "WK_BRANCH", "WK_IN_VM", "WK_TARGET", "WK_NAME", "WK_NO_DELEGATE"):
            os.environ.pop(v, None)
        for name, fn in GENERATORS.items():
            p = mock.patch.object(shell, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(targets.record, "host_name", return_value="here")
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
    """cmd/sync's own words: a scope, the workspace named, the target named, --fix."""

    def parse(self, *args, name=""):
        return cmd.parse(list(args), name)

    def test_each_scope_and_what_it_names(self):
        for args, want in ((( ), ("here", "", "", False)),
                           (("myws",), ("ws", "myws", "", False)),
                           (("--all",), ("all", "", "", False)),
                           (("--mirror",), ("mirror", "", "", False)),
                           (("--target", "moose"), ("target", "", "moose", False)),
                           (("--tools",), ("tools", "", "", False)),
                           (("--tools", "buildbox4"), ("tools", "", "buildbox4", False)),
                           (("--fix",), ("here", "", "", True)),
                           (("myws", "--fix"), ("ws", "myws", "", True))):
            with self.subTest(args=args):
                self.assertEqual(self.parse(*args), want)

    def test_inside_a_workspace_the_dispatchers_name_is_the_workspace(self):
        self.assertEqual(self.parse(name="selftest-ws"), ("ws", "selftest-ws", "", False))

    def test_two_scopes_are_refused_rather_than_last_one_wins(self):
        for pair in (("--all", "--tools"), ("--tools", "--all"), ("--all", "--target", "moose"),
                     ("--target", "moose", "--all"), ("--mirror", "--all")):
            with self.subTest(pair=pair):
                self.assertIn("ask for different things -- one at a time", self.refused(lambda: self.parse(*pair)))

    def test_a_name_and_a_scope_together_is_refused(self):
        err = self.refused(lambda: self.parse("myws", "--all"))
        self.assertIn("ask for different things", err)
        self.assertIn("'myws'", err)

    def test_a_second_target_is_refused_rather_than_overwriting_the_first(self):
        self.assertIn("one target at a time (got 'moose' and 'buildbox4')",
                      self.refused(lambda: self.parse("--target", "moose", "--target", "buildbox4")))

    def test_machine_is_a_tombstone_naming_both_replacements(self):
        err = self.refused(lambda: self.parse("--machine"))
        self.assertIn("'wk sync --machine' is gone", err)
        self.assertIn("wk sync --all", err)
        self.assertIn("wk sync --tools", err)

    def test_an_invalid_name_is_refused(self):
        self.assertIn("invalid name", self.refused(lambda: self.parse("bad/name")))


class TestWhere(unittest.TestCase):
    """`dispatch.where[sync]`: a workspace's name goes to the machine holding it; every scope flag is this host's,
    so none of them enters the podman VM as a forwarded command."""

    def test_the_answers(self):
        for args, inside, want in (((), False, "host"), (("myws",), False, "workspace"), (("--fix",), False, "host"),
                                   (("myws", "--fix"), False, "workspace"), ((), True, "workspace"), (("--fix",), True, "workspace"),
                                   (("--all",), True, "host")):
            with self.subTest(args=args, inside=inside):
                self.assertEqual(sync.where(inside, list(args)), want)

    def test_every_scope_flag_is_this_host(self):
        for args in (("--all",), ("--tools",), ("--target", "moose"), ("--tools", "buildbox4"), ("--target=moose",),
                     ("--tools=buildbox4",), ("--machine",), ("--mirror",), ("--tools", "--all"), ("--fix", "--all")):
            with self.subTest(args=args):
                self.assertEqual(sync.where(False, list(args)), "host")

    def test_the_dispatcher_asks_cmd_sync_and_nothing_else_decides(self):
        self.assertEqual(Decl(CMD_SYNC).where_for(["myws"]), "dynamic")
        cp = subprocess.run([str(CMD_SYNC), "--where", "--target=moose"], capture_output=True, text=True, timeout=30)
        self.assertEqual((cp.returncode, cp.stdout.strip()), (0, "host"), cp.stderr)
        cp = subprocess.run([str(CMD_SYNC), "--where", "myws"], capture_output=True, text=True, timeout=30)
        self.assertEqual(cp.stdout.strip(), "workspace")


class Steps(sync.Sync):
    """The run's order: the mirror, the snapshot and the fetches record themselves and do nothing."""

    def sync_mirror(self):
        self.here.steps.append("MIRROR")

    def sync_snapshot(self, target):
        self.here.steps.append("SNAPSHOT %s" % target.name)
        if target.name in self.here.refuse_publish:
            raise Refused(1)

    def base_wiring(self, target):
        return 0

    def fetch_workspaces(self, target, names):
        self.here.steps.append("FETCH-IN %s: %s" % (target.name, " ".join(names)))
        return 0


class TestWhatEachScopeRuns(SyncTest):
    """`unit sync.scopes`: the tooling copies, this machine's mirror, then each target's snapshot and the fetch in
    its workspaces -- a snapshot is cloned off the mirror and a workspace fetches from it, so either before the
    refresh hands back what the machine already had."""

    KINDS = {"container": "container", "vm": "vm", "buildbox4": "remote"}

    def setUp(self):
        super().setUp()
        self.w = self.make_world(self.KINDS)
        self.w.refuse_publish = set()
        for t in self.KINDS:
            self.w.workspaces[t] = ["%s-ws" % t]

    def steps(self, scope="here", target="", only="", fix=False):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), scope, only, target, fix)
        rc, err = self.stderr(s.run)
        return rc, self.w.steps, err

    def test_bare_is_the_tooling_the_mirror_then_each_target_here(self):
        rc, steps, _ = self.steps()
        self.assertEqual((rc, steps), (0, ["FURNITURE container", "FURNITURE vm", "MIRROR", "SNAPSHOT container",
                                           "FETCH-IN container: container-ws", "FETCH-IN vm: vm-ws"]))

    def test_a_named_target_refreshes_its_furniture_before_fetching_in_it(self):
        rc, steps, _ = self.steps("target", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "SNAPSHOT container", "FETCH-IN container: container-ws"])

    def test_a_guest_target_gets_the_mirror_and_a_fetch_but_no_snapshot(self):
        rc, steps, _ = self.steps("target", "vm")
        self.assertEqual(steps, ["FURNITURE vm named", "MIRROR", "FETCH-IN vm: vm-ws"])

    def test_all_reaches_every_workspace_on_every_target(self):
        rc, steps, _ = self.steps("all")
        self.assertEqual(steps, ["FURNITURE container", "FURNITURE vm", "FURNITURE buildbox4", "MIRROR", "SNAPSHOT container",
                                 "FETCH-IN container: container-ws", "FETCH-IN vm: vm-ws", "FETCH-IN buildbox4: buildbox4-ws"])

    def test_tools_refreshes_every_machines_copy_and_publishes_one_snapshot(self):
        rc, steps, _ = self.steps("tools")
        self.assertEqual(steps, ["FURNITURE container", "FURNITURE vm", "FURNITURE buildbox4", "MIRROR", "SNAPSHOT container"])
        self.w.steps.clear()
        rc, steps, _ = self.steps("tools", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "SNAPSHOT container"])

    def test_a_machine_of_its_own_touches_neither_the_mirror_nor_a_snapshot_here(self):
        rc, steps, _ = self.steps("target", "buildbox4")
        self.assertEqual(steps, ["FURNITURE buildbox4 named", "FETCH-IN buildbox4: buildbox4-ws"])

    def test_an_unknown_target_is_refused_by_name_before_anything_runs(self):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), "target", "", "nosuchthing")
        self.assertIn("unknown target 'nosuchthing'", self.refused(s.run))
        self.assertEqual(self.w.steps, [])

    def test_a_store_in_the_podman_vm_is_asked_with_the_same_scope_word(self):
        self.w.local_store = False
        self.w.far["container"] = "answering"
        _, steps, _ = self.steps("target", "container")
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "ASKED container: wk sync --target container"])
        self.w.steps.clear()
        _, steps, _ = self.steps("tools", "container", fix=True)
        self.assertEqual(steps, ["FURNITURE container named", "MIRROR", "ASKED container: wk sync --tools container --fix"])

    def test_a_stopped_podman_machine_is_named_and_is_not_a_success(self):
        self.w.local_store = False
        self.w.far["container"] = "stopped"
        rc, steps, err = self.steps("target", "container")
        self.assertEqual((rc, steps), (1, ["FURNITURE container named", "MIRROR"]))
        self.assertIn("podman machine is stopped", err)
        self.assertIn("wk start, then  wk sync --target container", err)

    def test_inside_the_podman_vm_the_mirror_is_read_not_refreshed(self):
        self.w.reg.env["WK_IN_VM"] = "1"
        _, steps, _ = self.steps("target", "container")
        self.assertEqual(steps, ["FURNITURE container named", "SNAPSHOT container", "FETCH-IN container: container-ws"])

    def test_a_target_that_did_not_take_the_tooling_fails_the_run_and_the_rest_still_runs(self):
        self.w.refuse_tools.add("vm")
        rc, steps, err = self.steps()
        self.assertEqual(rc, 1)
        self.assertIn("1 target(s) did not take the tooling", err)
        self.assertIn("FETCH-IN vm: vm-ws", steps)

    def test_a_failed_publish_does_not_stop_the_fetches_and_is_not_a_success(self):
        self.w.refuse_publish.add("container")
        rc, steps, _ = self.steps("target", "container")
        self.assertEqual((rc, steps[-1]), (1, "FETCH-IN container: container-ws"))

    def test_one_workspace_fetches_in_that_one_and_refreshes_no_mirror(self):
        self.w.reg.env["WK_TARGET"] = "container"
        rc, steps, _ = self.steps("ws", only="bug-238")
        self.assertEqual(steps, ["FETCH-IN container: bug-238"])

    def test_the_mirror_alone(self):
        rc, steps, _ = self.steps("mirror")
        self.assertEqual((rc, steps), (0, ["MIRROR"]))
        self.w.reg.env["WK_IN_VM"] = "1"
        self.assertIn("mounted read-only", self.refused(Steps(self.w.reg, self.w.clock, self.w.lock(), "mirror").run))

    def test_inside_a_workspace_the_mirror_is_a_request(self):
        Path(self.w.env["WK_MARKER"]).write_text("name=ws\n")
        for rc_asked, want in ((0, 0), (1, 1)):
            with mock.patch.object(shell, "mirror_refresh_request", return_value=rc_asked) as ask:
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
        s = Steps(self.w.reg, self.w.clock, lock, "target", "", "container")
        self.stderr(s.run)
        self.assertEqual(held, ["store", "store"])


class TestWhereEachTargetsWorkspacesAreFetched(SyncTest):
    """sync_target: a plain target's workspaces are fetched from here; a peer is asked for each by name and never
    with a scope word -- what a scope means is that machine's copy of wk-tools to decide."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world({"buildbox4": "remote", "moose": "peer"})
        self.w.workspaces = {"buildbox4": ["ws-a", "ws-b"], "moose": ["ws-a", "ws-b"]}

    def route(self, name, fix=False):
        s = Steps(self.w.reg, self.w.clock, self.w.lock(), "target", "", name, fix)
        rc, err = self.stderr(lambda: s.sync_target(self.w.reg.load(name)))
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
            with self.subTest(target=t):
                rc, steps, err = self.route(t)
                self.assertEqual((rc, steps), (0, []))
                self.assertIn("no workspaces on %s" % t, err)


class TestFurniture(SyncTest):
    def test_a_bare_sweep_names_no_target_and_a_named_one_is_named(self):
        """A peer publishes its own snapshot only when it was named, never as part of a sweep (Remote.sync)."""
        self.w = self.make_world({"container": "container", "buildbox4": "remote"})
        self.stderr(self.w.sync("tools").sync_furniture)
        self.assertEqual(self.w.steps, ["FURNITURE container", "FURNITURE buildbox4"])
        self.w.steps.clear()
        rc, err = self.stderr(self.w.sync("tools", target="buildbox4").sync_furniture)
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
        """The branch list is lib/store.sh's wk_mirror_branches, which reads WK_MIRROR_BRANCHES; a branch it
        names that the refresh did not bring is named once, here, rather than in every workspace's fetch."""
        self.w.reg.env["WK_MIRROR_BRANCHES"] = "main webkitglib/2.52"
        with mock.patch.object(shell, "mirror_branches", REAL_MIRROR_BRANCHES):
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


class TestTheSnapshot(SyncTest):
    """sync_snapshot: a `--shared` clone of the mirror (or a hardlinked copy of the last snapshot), wired, fetched,
    on a branch tracking the one it was published from, clean, with `sha` the completion marker written last."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(cls=Recording)

    def publish_raw(self):
        self.w.sync("target", target="container").sync_snapshot(self.w.reg.load("container"))

    def publish(self):
        return self.stderr(self.publish_raw)[1]

    def acts(self):
        return [e[1] for e in self.w.effects if e[0] == "act"]

    def test_no_mirror_is_refused_naming_the_host(self):
        self.assertIn("'wk sync' on the host makes it", self.refused(self.publish_raw))

    def test_the_first_snapshot_is_a_shared_clone_on_its_branch_with_the_sha_last(self):
        self.w.dirs.add(self.w.mirror)
        err = self.publish()
        new = os.path.join(self.w.base_dir(), self.w.clock.stamp())
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
        """Or `wk new` refuses forever while `wk sync` reports nothing to do."""
        self.w.dirs.add(self.w.mirror)
        self.w.publish("20200101T000000Z")
        self.publish()
        self.assertEqual(self.acts()[0], ("cp", "-al", self.w.store.base_path("20200101T000000Z"),
                                          os.path.join(self.w.base_dir(), self.w.clock.stamp(), "WebKit")))
        self.assertEqual(self.w.complete()[0], self.w.clock.stamp())

    def test_another_branch_is_published_even_when_main_is_current(self):
        self.w.dirs.add(self.w.mirror)
        self.w.publish("20200101T000000Z")
        self.w.verified.add("20200101T000000Z")
        self.w.reg.env["WK_BRANCH"] = "origin/wpe-2.46"
        err = self.publish()
        self.assertIn("tracking origin/wpe-2.46", err)
        self.assertEqual(self.w.files[os.path.join(self.w.base_dir(), self.w.clock.stamp(), "branch")], "origin/wpe-2.46\n")

    def test_a_branch_that_is_not_remote_tracking_is_refused_and_the_half_publish_removed(self):
        self.w.dirs.add(self.w.mirror)
        self.w.reg.env["WK_BRANCH"] = "main"
        err = self.refused(self.publish_raw)
        self.assertIn("<remote>/<branch>", err)
        self.assertIn("origin/main", err)
        self.assertNotIn(os.path.join(self.w.base_dir(), self.w.clock.stamp()), self.w.dirs)

    def test_a_failed_clone_is_refused_and_removed(self):
        self.w.dirs.add(self.w.mirror)
        self.w.react(["git", "clone"], lambda a, f: Result(128, "", "fatal: no space"))
        self.assertIn("fatal: no space", self.refused(self.publish_raw))
        self.assertNotIn(os.path.join(self.w.base_dir(), self.w.clock.stamp()), self.w.dirs)

    def test_a_publish_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[sync]`: the store's mirror refresh and publish, the flow `wk sync --tools` runs."""
        def world():
            w = self.make_world()
            w.publish("20190101T000000Z", sha="b" * 40)
            return w

        def run_once(w):
            with contextlib.redirect_stderr(io.StringIO()):
                w.sync("tools", target="container").run()
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
        self.stderr(wet.sync("target", target="container", fix=True).run)
        dry = world()
        before = dry.state()
        os.environ["WK_DRY_RUN"] = "1"
        rc, err = self.stderr(dry.sync("target", target="container", fix=True).run)
        self.assertEqual(dry.mutations(), wet.mutations())
        self.assertGreaterEqual(len(dry.mutations()), 10)
        self.assertEqual(dry.state(), before)


class TestTheBaseWiring(SyncTest):
    """The current snapshot is where every future workspace gets its remotes from: read back on every sync of
    its target, and re-wired under --fix."""

    def setUp(self):
        super().setUp()
        self.w.current = "20200101T000000Z"
        self.w.publish(self.w.current)

    def wiring(self, fix=False):
        s = self.w.sync("target", target="container", fix=fix)
        return self.stderr(lambda: s.base_wiring(self.w.reg.load("container")))

    def test_a_right_one_says_nothing(self):
        self.assertEqual(self.wiring(), (0, ""))

    def test_a_wrong_one_is_named_with_its_problems_and_the_remedy(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.wiring()
        self.assertEqual(rc, 1)
        self.assertIn("the base snapshot 20200101T000000Z is wired wrong", err)
        self.assertIn("    - origin is /x", err)
        self.assertIn("wk sync --target container --fix", err)

    def test_fix_re_wires_it(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.wiring(fix=True)
        self.assertEqual(rc, 0)
        self.assertIn("re-wired the base snapshot", err)
        self.assertIn(("sh", "-c", "WIRING %s %s   " % (self.w.store.base_path(self.w.current), self.w.mirror)),
                      [e[1] for e in self.w.effects if e[0] == "run"])
        self.w.wire_result = Result(1)
        self.assertEqual(self.wiring(fix=True)[0], 1)

    def test_no_snapshot_is_nothing_to_read(self):
        self.w.current = ""
        self.assertEqual(self.wiring(), (0, ""))

    def test_a_check_script_bash_could_not_render_is_a_refusal_not_an_empty_check_that_passes(self):
        with mock.patch.object(shell, "wiring_check_script", REAL_WIRING_CHECK), mock.patch.object(shell, "ask", return_value=None):
            err = self.refused(lambda: self.w.sync("target", target="container").base_wiring(self.w.reg.load("container")))
        self.assertIn("the bash function wk_wiring_check_script failed (its error is above)", err)
        self.assertNotIn(("sh", "-c", ""), [e[1] for e in self.w.effects if e[0] == "run"])


class TestTheFetch(SyncTest):
    """One fetch per workspace, all at once, one line each in the order named -- every line derived from what that
    fetch said, since `wk sync --all` is often the only thing anybody reads about a machine's workspaces. The
    wiring check runs in the same round trip (`unit remotes.wiring[<target>]` for what it reports)."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world({"container": "container", "buildbox4": "remote", "vm": "vm"})

    def fetch_raw(self, *names, scope="all", fix=False, target="container"):
        s = self.w.sync(scope, only=names[0] if scope == "ws" else "", fix=fix)
        return s.fetch_workspaces(self.w.reg.load(target), list(names))

    def fetch(self, *names, **kw):
        return self.stderr(lambda: self.fetch_raw(*names, **kw))

    def test_the_script_is_one_fetch_and_the_report_names_the_mirror(self):
        rc, err = self.fetch("one")
        self.assertEqual(rc, 0)
        self.assertIn("  %-24s ok  (/mirror/container/WebKit.git)" % "one", err)
        script = next(e[1][-1] for e in self.w.effects if e[1][:1] == ("exec",))
        self.assertIn("cd '/src/WebKit'", script)
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
        seen, lock = [], threading.Lock()

        def wait(argv):
            with lock:
                seen.append(argv[2])
            for _ in range(50):
                if len(seen) == 3:
                    break
                time.sleep(0.01)
            return Result(0, "from=mirror\nfetch=0\ncheck=0\n")
        self.w.fetched = {n: wait for n in ("a", "b", "c")}
        start = time.monotonic()
        self.fetch("a", "b", "c")
        self.assertLess(time.monotonic() - start, 0.4)

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
        self.fetch("one", fix=True, target="buildbox4")
        self.assertEqual(self.w.steps, ["WIRING one", "UPSTREAMFIX one", "FETCH one"])
        wired = next(e[1][-1] for e in self.w.effects if e[1][:1] == ("exec",))
        self.assertEqual(wired, "WIRING /src/WebKit /mirror/buildbox4/WebKit.git mirror /far/mirror /far/ssh/config")

    def test_fix_runs_git_webkit_in_a_guest_too(self):
        self.fetch("one", fix=True, target="vm")
        self.assertIn("GITWEBKIT one", self.w.steps)

    def test_a_git_webkit_setup_that_failed_names_both_remedies(self):
        self.w.fixes = {("one", "GITWEBKIT"): Result(1, "setup=failed\n", "HTTP 401\n")}
        rc, err = self.fetch("one", fix=True)
        self.assertEqual(rc, 1)
        self.assertIn("-- wired wrong:", err)
        self.assertIn("    HTTP 401", err)
        self.assertIn("'git-webkit setup' did not finish (setup=failed)", err)
        self.assertIn("wk push on", err)
        self.assertIn("wk sync one --fix", err)

    def test_a_wiring_that_did_not_take_is_named_and_the_fetch_still_runs(self):
        self.w.fixes = {("one", "WIRING"): Result(1)}
        rc, err = self.fetch("one", fix=True)
        self.assertEqual((rc, self.w.steps), (1, ["WIRING one", "FETCH one"]))
        self.assertIn("could not re-wire 'one'", err)


class TestEachDriversFurniture(SyncTest):
    """Target.sync, the half of `wk sync --tools` each driver answers for itself."""

    def test_a_container_mounts_the_tooling_and_copies_nothing(self):
        t = targets.Container("container", str(REPO), dict(self.w.env), self.w)
        ok, err = self.stderr(lambda: t.sync())
        self.assertEqual((ok, self.w.effects), (True, []))
        self.assertIn("nothing to copy", err)

    def test_a_containers_exec_already_carries_the_injected_credential(self):
        """Why --fix runs `git-webkit setup` through the plain exec and not a second bridge."""
        t = targets.Container("container", str(REPO), dict(self.w.env), self.w)
        t.act_exec("ws", ["sh", "-c", "GITWEBKIT /src/WebKit"])
        argv = next(e[1] for e in self.w.effects if e[0] == "run")
        self.assertEqual(argv[argv.index("--") + 1:], ("/opt/wk-tools/container/proxy/ensure-bridge.sh", "sh", "-c", "GITWEBKIT /src/WebKit"))

    def test_a_guest_gets_its_copy_pushed_and_one_not_running_is_skipped(self):
        class Guests(targets.Vm):
            def list(self):
                return [("mya", "running"), ("myb", "stopped")]

            def info(self, ws):
                return "running" if ws == "mya" else "stopped"
        t = Guests("vm", str(REPO), dict(self.w.env), self.w)
        ok, err = self.stderr(lambda: t.sync())
        self.assertTrue(ok)
        self.assertIn("mya" + " " * 22 + "ok", err)
        self.assertIn("myb" + " " * 22 + "not running -- skipped", err)
        pushed = [e[1] for e in self.w.effects if e[0] == "run" and e[1][:1] == ("bash",)]
        self.assertEqual(len(pushed), 1)
        self.assertIn("load_target 'vm'", pushed[0][2])
        self.assertIn("t_sync_tools", pushed[0][2])
        self.assertEqual(pushed[0][-1], "mya")
        self.assertEqual([e for e in self.w.effects if e[1][:1] != ("bash",)], [], "a guest's mirror is the host's")
        self.w.react(["bash", "-c"], lambda a, f: Result(1))
        self.assertFalse(self.stderr(lambda: t.sync())[0])


class TestRemoteFurniture(SyncTest):
    """Remote.sync. A build box is pushed this tree's HEAD and has its mirror refreshed; a peer is a workstation
    under git, so its tooling is pulled, and only once that pull has converged -- and the peer was named -- is it
    asked to publish its own snapshot."""

    def remote(self, peer=False, reference=""):
        env = dict(self.w.env, WK_REMOTE_LOCAL="1", WK_REMOTE_TOOLS="/far/wk-tools", WK_REMOTE_ROOT="/far/wk",
                   WK_REMOTE_REFERENCE=reference)
        if peer:
            env["WK_REMOTE_PEER"] = "1"
        return targets.Remote("apeer" if peer else "box", str(REPO), env, self.w)

    def answer_versions(self, theirs, mine="sha=abc\ndirty=no\n"):
        self.w.answer(["env", "WK_ROOT=%s" % REPO], out=mine)
        self.w.react(["sh", "-c"], lambda a, f: self._far(a, theirs))

    def _far(self, argv, theirs):
        text = argv[2]
        if "git pull --ff-only" in text:
            return Result(0, "Already up to date.\n")
        if text.endswith("/cmd/version'") or text.endswith("/cmd/version"):
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
        self.assertIn("pulled, still DIFFERS (0000stale000, this machine has abc)", err)
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
        self.w.answer(["bash", "-c"], err="pushed\n")
        self.w.answer(["git", "-C", str(REPO), "rev-parse", "HEAD"], out="f00d\n")
        ok, err = self.stderr(lambda: self.remote().sync())
        self.assertTrue(ok, err)
        self.assertIn("box" + " " * 22 + "pushed f00d", err)
        self.assertIn("the WebKit mirror on box is up to date", err)

    def test_a_build_box_cloning_from_its_admins_reference_keeps_no_mirror(self):
        self.answer_versions("")
        self.w.answer(["bash", "-c"], rc=1)
        ok, err = self.stderr(lambda: self.remote(reference="/srv/WebKit").sync())
        self.assertFalse(ok)
        self.assertIn("clone from /srv/WebKit", err)
        self.assertNotIn("mirror on box", err)


class TestWhyAPeerIsBehind(SyncTest):
    def test_each_reason(self):
        self.w = Fake("here")
        self.assertIn("not a git checkout", targets.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-parse", "--git-dir"], out=".git\n")
        self.w.answer(["git", "-C", "/t", "rev-parse", "--abbrev-ref", "HEAD"], out="eng/x\n")
        self.assertIn("branch 'eng/x' has no upstream here", targets.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-parse", "--abbrev-ref", "@{upstream}"], out="origin/main\n")
        self.w.answer(["git", "-C", "/t", "rev-list"], out="2\n")
        self.assertIn("2 commit(s) ahead of origin/main", targets.tools_why_behind(self.w, "/t"))
        self.w.answer(["git", "-C", "/t", "rev-list"], out="0\n")
        self.assertEqual(targets.tools_why_behind(self.w, "/t"), "")


class TestProcess(unittest.TestCase):
    """What the command says as a process."""

    def test_help_mentions_the_timing_and_the_branch(self):
        out = run("sync", "-h").stdout
        self.assertIn("WK_DEBUG", out)
        self.assertIn("WK_BRANCH", out)
        self.assertIn("--fix", out)

    def test_an_unknown_target_is_refused_by_name(self):
        for args in (("--target", "nosuchthing"), ("--tools", "nosuchthing"), ("--target=nosuchthing",)):
            cp = run("sync", *args, env={"WK_DRY_RUN": "1"})
            self.assertNotEqual(cp.returncode, 0, f"{args} was accepted")
            self.assertIn("unknown target 'nosuchthing'", cp.stdout)

    def test_wk_remotes_is_a_tombstone_naming_sync_fix(self):
        cp = run("remotes", "--fix")
        self.assertEqual(cp.returncode, 1, cp.stdout)
        self.assertIn("'wk remotes' is merged into sync: wk sync [<workspace>] --fix", cp.stdout)


# A broker that answers one request and records it: enough for container/broker/wk-broker-client.py to speak to.
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


class TestSyncInsideWorkspace(unittest.TestCase):
    """Inside a workspace `wk sync` is the machine's mirror, asked for over the broker socket, then a fetch in
    this workspace from it; with no broker listening the fetch still runs and the miss is reported. The scope
    flags answer `host` to the dispatcher and are refused before cmd/sync starts."""

    def _refused(self, *args):
        with fake_workspace() as ws:
            cp = ws.run("sync", *args)
        self.assertNotEqual(cp.returncode, 0, f"'wk sync {' '.join(args)}' was accepted inside a workspace")
        self.assertIn("acts on a host, and this is workspace 'selftest-ws'", cp.stdout)
        self.assertIn(f"From the host:  wk sync {' '.join(args)}", cp.stdout)

    def test_the_scope_flags_are_refused_naming_the_host_invocation(self):
        self._refused("--all")
        self._refused("--tools")
        self._refused("--target", "container")

    def test_a_different_workspaces_name_is_refused_by_the_dispatcher(self):
        with fake_workspace() as ws:
            cp = ws.run("sync", "someotherws")
        self.assertEqual(cp.returncode, 2, cp.stdout)
        self.assertIn("unexpected argument: someotherws", cp.stdout)

    def test_an_unrecognised_flag_is_refused_as_unknown_not_as_host_only(self):
        with fake_workspace() as ws:
            cp = ws.run("sync", "--bogus")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("unknown option: --bogus", cp.stdout)
        self.assertNotIn("acts on a host", cp.stdout)

    def _checkout(self, ws):
        """A bare repo standing in for upstream with one commit on main, and the workspace's checkout with it as
        origin -- real git, no network, and not wired the way wk wires one, so the report names that."""
        bare, seed = ws.tmp / "origin.git", ws.tmp / "seed"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(bare)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "--quiet", str(bare), str(seed)], check=True, capture_output=True)
        (seed / "file.txt").write_text("hello\n")
        subprocess.run(["git", "-C", str(seed), "add", "file.txt"], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "-c", "user.email=t@t.example", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "main"], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", str(seed), "rev-parse", "main"], capture_output=True, text=True, check=True).stdout.strip()
        src = ws.ws_dir / "WebKit"
        subprocess.run(["git", "init", "--quiet", "-b", "main", str(src)], check=True, capture_output=True)
        subprocess.run(["git", "-C", str(src), "remote", "add", "origin", str(bare)], check=True, capture_output=True)
        return src, sha

    def fetched(self, src):
        return subprocess.run(["git", "-C", str(src), "rev-parse", "refs/remotes/origin/main"], capture_output=True, text=True).stdout.strip()

    def test_bare_sync_asks_the_machine_to_refresh_its_mirror_then_fetches_here(self):
        with fake_workspace() as ws:
            src, sha = self._checkout(ws)
            with stub_broker(ws.tmp) as (sock, record):
                cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(sock)})
            self.assertEqual(json.loads(record.read_text()), {"verb": "sync", "args": {}}, record.read_text())
            self.assertEqual(self.fetched(src), sha, cp.stdout)
            self.assertIn("selftest-ws", cp.stdout)
            self.assertIn("-- wired wrong:", cp.stdout)
            self.assertIn("origin is %s" % (ws.tmp / "origin.git"), cp.stdout)
            self.assertEqual(cp.returncode, 1, cp.stdout)
            self.assertNotIn("Permission denied", cp.stdout)

    def test_with_no_broker_the_fetch_still_runs_and_the_miss_is_reported(self):
        with fake_workspace() as ws:
            src, sha = self._checkout(ws)
            cp = ws.run("sync", env={"WK_BROKER_SOCKET": str(ws.tmp / "nothing.sock")})
            self.assertEqual(cp.returncode, 1, cp.stdout)
            self.assertIn("mirror was not", cp.stdout)
            self.assertIn("./setup --stage broker", cp.stdout)
            self.assertEqual(self.fetched(src), sha)


if __name__ == "__main__":
    unittest.main()
