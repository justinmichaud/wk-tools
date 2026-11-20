"""`wk sync` typed into a world of real places: the mirror refresh, the snapshot publish, each workspace's fetch and
wiring check, --fix, the scopes, the broker request and the guests' remount."""

import contextlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from tests import fakes
from tests.fakes import FakeRegistry, Result
from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE
from tests.test_wk_workspace import DRIVERS, WorkspaceTest, said

MAIN_SHA = "a" * 40
FETCHED = "from=mirror\nfetch=0\ncheck=0\n"
STAMP = "20231114T221320Z"   # the world's clock
OLD = "20200101T000000Z"
REMOUNT = "sudo -n /usr/local/libexec/wk-mount-mirror wk-mirror /Volumes/wk-mirror"
CLIENT = str(REPO / "container" / "broker" / "wk-broker-client.py")


def warned(err):
    """The warning a person acts on: its first line."""
    return next((x for x in err.splitlines() if x.startswith("warning: ")), "")


def kind_of(script):
    """What a script handed to `sh -c` is, by the line only it has."""
    for word, mark in (("refresh", "mirror-fetch"), ("fetch", "echo fetch=$rc"), ("check", "problem: no origin remote at all"),
                       ("setup", "WK_HOOK_LEVELS="), ("wire", "pushInsteadOf"), ("pull", "git pull --ff-only")):
        if mark in script:
            return word
    return ""


class Host(fakes.World):
    """This host (or, `in_vm`, the podman VM) with a place per `kinds` entry, each the real driver: podman answers from
    `containers`, tart from `guests`, a peer from `peer_ws` and `peer_rc`, git as a mirror at `upstream` and snapshots
    on their branch once `verified`, a workspace from `said[(ws, kind)]`; each step asked of a machine lands in `steps`."""

    def __init__(self, tmp, kinds=None, in_vm=True):
        super().__init__(tmp, WK_MIRROR_BRANCHES="main", WK_CONTAINER_USER="u", WK_SDK="/sdk")
        if in_vm:
            self.env["WK_IN_VM"] = "1"
        self.env["WK_MARKER"] = str(self.tmp / "no-marker")
        os.makedirs(self.env["WK_STORE"])
        kinds = kinds or {"container": "container"}
        self.containers, self.guests, self.peer_ws, self.peer_rc, self.said, self.on_branch = {}, {}, [], {}, {}, {}
        self.heads, self.verified, self.steps = ["main"], set(), []
        self.refs, self.upstream = "", MAIN_SHA + " refs/heads/main\n"
        self.base_check, self.wire_result, self.refresh = Result(0), Result(0), None
        self.reg = FakeRegistry(self.env, self, lambda n, e: DRIVERS[kinds[n]](n, str(REPO), e, self), names=list(kinds))
        self.store = self.reg.store
        self.mirror = self.store.mirror_dir()
        for f in ("github-user", "view/container/github-user"):
            self.files[os.path.join(self.store.keyring_dir(), f)] = "u\n"
        if "vm" in kinds.values():
            self._tart()
        for prefix, fn in ((["find"], lambda a, f: Result(0, a[1] + "\n")), (["podman"], self._podman), (["env"], self._sdk),
                           (["sh", "-c"], lambda a, f: self._sh(a[2])), (["git"], self._git), (["cp", "-al"], self._cp),
                           (["ssh"], self._ssh), (["bash", "-lc"], self._local), (["bash", "-c"], lambda a, f: Result(0)),
                           ([os.path.join(str(REPO), "container", "proxy", "ensure-bridge.sh")], self._local)):
            self.react(prefix, fn)
        self.effects = []

    def _tart(self):
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "tart").write_text("")
        (bindir / "tart").chmod(0o755)
        self.env.update(PATH=str(bindir) + os.pathsep + os.environ["PATH"], WK_VM_STORE=str(self.tmp / "vmstore"))
        t = os.path.realpath(str(bindir / "tart"))
        self.react([t, "list"], lambda a, f: Result(0, json.dumps([{"Name": "wk-" + n, "State": s, "Source": "local"}
                                                                   for n, s in sorted(f.guests.items())])))
        self.react([t, "exec"], self._guest)

    def make(self, name):
        self.containers[name] = "running"
        d = self.store.ws_dir(name)
        self.dirs.add(d)
        self.files[os.path.join(d, "home", ".wk-ready")] = ""
        self.files[os.path.join(d, "base-id")] = OLD + "\n"

    def make_guest(self, name, state="running"):
        self.guests[name] = state
        self.files[os.path.join(self.env["WK_VM_STORE"], "ws", name, ".wk-ready")] = ""

    def inside(self, name="ws"):
        """This process runs in workspace `name`."""
        Path(self.env["WK_MARKER"]).write_text("name=%s\nsrc=/src/WebKit\n" % name)

    def _podman(self, argv, f):
        argv = [a for i, a in enumerate(argv) if a != "-c" and argv[i - 1:i] != ["-c"]]
        if argv[1:3] == ["machine", "ssh"]:   # a macOS host reaches a container's store through the podman VM
            words = shlex.split(argv[-1])
            if words[0] == "test":
                return Result(0 if words[2] in f.dirs or words[2] in f.files else 1)
            if "enter" not in words:
                return Result(1)
            i = words.index("enter")
            return self.in_ws(words[i + 1], words[words.index("--", i) + 1:])
        if argv[1] == "ps":
            return Result(0, "".join("wk-%s\t%s\n" % kv for kv in sorted(f.containers.items())))
        if argv[1] == "inspect":
            n = argv[2][3:]
            return Result(0, f.containers[n] + "\n") if n in f.containers else Result(125, "", "no such container")
        return Result(127, "", "no podman answer")

    def _sdk(self, argv, f):
        if "--name" not in argv:
            return f.refresh or Result(0, "refreshed\n")   # the broker client
        return self.in_ws(argv[argv.index("--name") + 1][3:], list(argv[argv.index("--") + 2:]))

    def _local(self, argv, f):
        return self.in_ws("ws", shlex.split(argv[-1])[1:])

    def _guest(self, argv, f):
        ws, cmd = argv[3][3:], argv[-1]
        if cmd == REMOUNT:
            self.steps.append("remount " + ws)
            return self.said.get((ws, "remount"), Result(0))
        if 'fetch -q "$d/' in cmd:   # the last step of wk-tools' push: the far checkout's HEAD
            self.steps.append("tools " + ws)
            return self.said.get((ws, "tools"), Result(0, MAIN_SHA + "\n"))
        words = shlex.split(cmd)
        return self.in_ws(ws, words) if words[:2] == ["sh", "-c"] and "set -e\nd=$1" not in words[2] else Result(0 if words[0] != "test" else 1)

    def in_ws(self, ws, argv):
        if argv[:2] == ["sh", "-c"]:
            k = kind_of(argv[2])
            if not k:
                return Result(0)
            self.steps.append("%s %s" % (k, ws))
            r = self.said.get((ws, k), Result(0, FETCHED if k == "fetch" else "setup=ok\n" if k == "setup" else ""))
            return r(ws) if callable(r) else r
        b, args = self.on_branch.get(ws), argv[3:]   # git in the checkout: on branch `b`, tracking origin's
        if args[:1] == ["symbolic-ref"]:
            return Result(0, b + "\n") if b else Result(1)
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return Result(0, "origin/%s\n" % b)
        if args[:1] == ["fetch"]:
            self.steps.append("fetch %s from %s" % (args[-1], args[-2]))
        return Result(0)

    def _sh(self, text):
        cd, _, rest = text.partition(" && ")
        if text.startswith("cd ") and "\n" not in cd and rest.startswith(("sh -c ", "git ")):   # a build box that is this machine
            return self.in_ws(os.path.basename(os.path.dirname(shlex.split(cd)[1])), shlex.split(rest))
        if "uname -s" in text:
            return Result(0, LINUX_PROBE)
        if text.startswith("if [ ! -d "):
            d = shlex.split(text)[4]
            return Result(0, "present\n" if d + "/.wk-ready" in self.files else "absent\n" if d not in self.dirs else "creating\n")
        k = kind_of(text)
        if k == "refresh":
            self.steps.append("mirror")
            self.dirs.add(self.mirror)
            self.refs = self.upstream
            return Result(0, "mirror-fetch origin ok\nmirror-fetch wpe FAILED\n")
        if k == "check":
            self.steps.append("check snapshot")
            return self.base_check
        if k == "wire":
            self.steps.append("wire snapshot")
            return self.wire_result
        return Result(127, "", "no sh answer")

    def _ssh(self, argv, f):
        """A peer named by its own host, answering with its own wk."""
        host, cmd = argv[-2], argv[-1]
        words = shlex.split(cmd)
        text = words[2] if words[:2] == ["sh", "-c"] else cmd
        if "uname -s" in text:
            return Result(0, LINUX_PROBE)
        if 'fetch -q "$d/' in text:
            self.steps.append("tools " + host)
            return Result(0, MAIN_SHA + "\n")
        if text.startswith(("test -x", "test -f $HOME/.wk-remote")) or "set -e\nd=$1" in text:
            return Result(0)
        if kind_of(text) in ("refresh", "pull"):
            self.steps.append("%s %s" % (kind_of(text).replace("refresh", "mirror"), host))
            return f.said.get((host, kind_of(text)), Result(0, "mirror-fetch origin ok\n"))
        if "doctor --probe-tools" in text:
            return f.said.get((host, "tools"), Result(0, "sha=%s\ndirty=no\n" % MAIN_SHA))
        args = shlex.split(text.split("&& ", 1)[-1].replace(" 2>&1", ""))
        args = args[next(i for i, a in enumerate(args) if a.endswith("/wk")) + 1:]
        if args == ["ls", "--json"]:
            return Result(0, json.dumps({"workspaces": [{"name": n, "state": "running"} for n in f.peer_ws]}))
        self.steps.append("ask %s: wk %s%s" % (host, " ".join(args), " (no-delegate)" if "WK_NO_DELEGATE=1" in text else ""))
        return Result(f.peer_rc.get(args[1] if len(args) > 1 else "", 0), "said %s\n" % " ".join(args))

    def _git(self, argv, f):
        tree, args = argv[2], argv[3:]
        snaps = self.store.snapshots_dir()
        if tree == f.mirror and args[:1] == ["for-each-ref"]:
            return Result(0, f.refs)
        if tree.startswith(snaps + "/") and args[:2] == ["symbolic-ref", "--quiet"]:
            return Result(0, "refs/heads/main\n") if tree[len(snaps) + 1:].split("/")[0] in f.verified else Result(1)
        if args[:3] == ["rev-parse", "--abbrev-ref", "--symbolic-full-name"]:
            return Result(0, "origin/main\n")
        if args[:3] == ["rev-parse", "--verify", "--quiet"]:
            return Result(0 if args[3][len("refs/heads/"):] in f.heads else 1)
        if args[:2] == ["rev-parse", "--symbolic-full-name"]:
            b = args[2]
            return Result(0, "refs/remotes/%s\n" % b) if b.startswith("origin/") else Result(0, "refs/heads/%s\n" % b)
        if args[:2] == ["symbolic-ref", "--short"]:
            return Result(0, "main\n")
        if args in (["rev-parse", "HEAD"], ["rev-parse", "refs/heads/main"]):
            return Result(0, MAIN_SHA + "\n")
        if args[:3] == ["checkout", "--quiet", "-B"]:
            f.verified.add(tree[len(snaps) + 1:].split("/")[0])
        if argv[1] == "clone":
            f.steps.append("clone")
            f.dirs.update({argv[-1], argv[-1] + "/.git"})
        return Result(0)

    def _cp(self, argv, f):
        src, dst = argv[-2], argv[-1]
        f.steps.append("copy " + os.path.basename(os.path.dirname(src)))
        f.dirs.update({dst} | {dst + d[len(src):] for d in f.dirs if d.startswith(src + "/")})
        return Result(0)

    def current(self):
        """A mirror and a current snapshot, OLD, a workspace was made from."""
        self.dirs.add(self.mirror)
        self.publish(OLD)
        self.verified.add(OLD)

    def snapshot(self, bid=STAMP):
        return os.path.join(self.store.snapshots_dir(), bid)

    def publish(self, bid, sha=MAIN_SHA):
        d = self.snapshot(bid)
        self.dirs.update({self.store.snapshots_dir(), d, os.path.join(d, "WebKit"), os.path.join(d, "WebKit", ".git")})
        self.files[os.path.join(d, "branch")] = "origin/main\n"
        self.files[os.path.join(d, "sha")] = sha + "\n"

    def state(self):
        """What a sync leaves in the store: a lock is process coordination, not state."""
        store = self.env["WK_STORE"]
        return (sorted(self.rel(p) for p in self.files if p.startswith(store)),
                sorted(self.rel(d) for d in self.dirs if d.startswith(store + "/base")), sorted(self.rel(d) for d in self.dirs if d == self.mirror))


class Flow(WorkspaceTest):
    world_class = Host
    UNSET = WorkspaceTest.UNSET + ("WK_BRANCH", "WK_IN_VM", "WK_PLACE", "WK_NAME", "WK_NO_DELEGATE", "WK_BROKER_SOCKET",
                                   "XDG_RUNTIME_DIR", "WK_MIRROR_BRANCHES")

    def sync(self, *argv, w=None):
        """`wk sync <argv>`: (exit status, stderr), with the world's effects and steps only this command's."""
        w = w or self.w
        w.effects, w.steps = [], []
        for v in self.UNSET:
            os.environ.pop(v, None)
        rc, _, err = self.wk(w, "sync", *argv)
        return rc, err

    def acts(self):
        return [self.w.rel(e[1]) for e in self.w.effects if e[0] == "act"]


class TestTheCommandLine(Flow):
    def test_two_scopes_or_a_name_and_a_scope_are_refused_in_the_order_typed_before_anything_runs(self):
        for argv, first in ((("--all", "--tools"), "--all and --tools"), (("--tools", "--all"), "--tools and --all"),
                            (("--all", "--on", "moose"), "--all and --on"), (("--on", "moose", "--all"), "--on and --all"),
                            (("--mirror", "--all"), "--mirror and --all"), (("ws", "--all"), "--all and 'ws'")):
            rc, err = self.sync(*argv)
            self.assertEqual((rc, said(err), self.w.steps), (1, "error: %s ask for different things -- one at a time." % first, []), argv)

    def test_a_second_place_is_refused_rather_than_overwriting_the_first(self):
        rc, err = self.sync("--on", "moose", "--on", "buildbox4")
        self.assertEqual((rc, said(err)), (1, "error: one place at a time (got 'moose' and 'buildbox4')"))

    def test_an_unknown_place_is_refused_by_name_before_anything_runs(self):
        rc, err = self.sync("--on", "nosuchthing")
        self.assertEqual((rc, said(err), self.w.steps), (1, "error: unknown place 'nosuchthing'.", []))

    def test_a_name_no_place_holds_is_refused_by_the_dispatcher_before_the_command_reads_it(self):
        for name in ("nope", "bad/name"):
            rc, err = self.sync(name)
            self.assertEqual((rc, warned(err), self.w.steps), (2, "warning: no such workspace: %s -- 'wk ls' lists them" % name, []))


class TestInsideAWorkspace(Flow):
    """In a workspace the mirror is mounted read-only: a bare sync asks the broker for the refresh, then fetches in this
    one; every scope of the host's is refused naming the host command."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"local": "local"}, in_vm=False)
        self.w.inside("ws")
        self.w.env["WK_BROKER_SOCKET"] = "/run/x.sock"
        self.w.answer(["test", "-S"])
        self.w.files[CLIENT] = ""

    def test_a_bare_sync_is_the_brokers_refresh_then_a_fetch_in_this_one_even_when_the_refresh_failed(self):
        for answer, want in ((Result(0), 0), (Result(1), 1)):
            self.w.refresh = answer
            rc, _ = self.sync()
            self.assertEqual((rc, self.w.steps, self.acts()[0]), (want, ["fetch ws"], ("env", "WK_BROKER_SOCKET=/run/x.sock", "python3", CLIENT, "sync")))

    def test_fix_is_this_ones_and_a_scope_is_the_hosts(self):
        self.assertEqual((self.sync("--fix")[0], self.w.steps), (0, ["wire ws", "fetch ws"]))
        for argv in (["--all"], ["--on=moose"], ["--tools"]):
            rc, err = self.sync(*argv)
            self.assertEqual((rc, said(err)), (1, "error: 'wk sync' acts on a host, and this is workspace 'ws'."), argv)

    def test_mirror_alone_is_the_brokers_and_its_status_is_the_answer(self):
        for answer, want in ((Result(0), 0), (Result(3), 1)):
            self.w.refresh = answer
            self.assertEqual((self.sync("--mirror")[0], self.w.steps), (want, []))

    def test_no_broker_is_named_with_the_stage_that_makes_one_and_asks_nothing(self):
        self.w.answer(["test", "-S"], rc=1)
        rc, err = self.sync("--mirror")
        self.assertEqual((rc, warned(err), self.acts()), (1, "warning: no request broker at /run/x.sock, so this machine's mirror was not refreshed.", []))
        self.assertIn("    On the workstation:  ./setup --stage broker   ('wk doctor' says whether it is", err.splitlines())

    def test_a_tree_without_the_client_is_refused_naming_the_tools_sync(self):
        del self.w.files[CLIENT]
        rc, err = self.sync("--mirror")
        self.assertEqual((rc, said(err)), (1, "error: this workspace's copy of wk-tools has no broker client"))
        self.assertIn("    (%s). Refresh it:  wk sync --tools container   on the workstation." % CLIENT, err.splitlines())

    def test_a_macos_guest_asks_at_the_socket_its_start_forwards_into_its_home_and_the_podman_vm_at_the_published_one(self):
        guest, vm = self.make_world(kinds={"local": "local", "vm": "vm"}, in_vm=False), self.make_world()
        guest.inside("ws")
        vm.env["XDG_RUNTIME_DIR"] = "/run/user/501"
        for w, sock in ((guest, "%s/home/.wk-broker.sock" % guest.tmp), (vm, "/run/user/501/wk/broker.sock")):
            w.answer(["test", "-S"], rc=1)
            self.assertEqual(warned(self.sync("--mirror", w=w)[1]), "warning: no request broker at %s, so this machine's mirror was not refreshed." % sock)


FLEET = {"container": "container", "vm": "vm", "moose": "peer"}


class Fleet(Flow):
    """This host: a container `c-ws`, a running guest `g-ws`, and a peer workstation `moose` holding `p-ws`."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds=FLEET, in_vm=False)
        self.w.make("c-ws")
        self.w.make_guest("g-ws")
        self.w.peer_ws = ["p-ws"]


class TestWhatEachScopeRuns(Fleet):
    """`unit sync.scopes`: the tooling copies, this machine's mirror, then each place's snapshot and the fetch in its
    workspaces -- a snapshot is cloned off the mirror and a workspace fetches from it."""

    def test_each_scope_and_the_steps_it_runs_in_order(self):
        """A guest gets no snapshot; a machine of its own neither the mirror nor a snapshot here; --tools no fetch."""
        mirror, snap = ["mirror", "remount g-ws"], ["clone", "wire snapshot", "check snapshot"]
        fetch = {"c": ["fetch c-ws"], "g": ["fetch g-ws"], "p": ["ask moose: wk sync p-ws (no-delegate)"]}
        for argv, want in ((["--mirror"], mirror), ([], ["tools g-ws"] + mirror + snap + fetch["c"] + fetch["g"]),
                           (["--on", "container"], mirror + snap + fetch["c"]),
                           (["--on", "vm"], ["tools g-ws"] + mirror + fetch["g"]),
                           (["--all"], ["tools g-ws", "pull moose"] + mirror + snap + fetch["c"] + fetch["g"] + fetch["p"]),
                           (["--tools"], ["tools g-ws", "pull moose"] + mirror + snap),
                           (["--tools", "container"], mirror + snap),
                           (["--on", "moose"], ["pull moose", "ask moose: wk sync --tools (no-delegate)"] + fetch["p"])):
            w = self.make_world(kinds=FLEET, in_vm=False)
            w.make("c-ws")
            w.make_guest("g-ws")
            w.peer_ws = ["p-ws"]
            rc, err = self.sync(*argv, w=w)
            self.assertEqual((rc, w.steps), (0, want), argv)
        self.assertIn("==> 'wk status' compares every copy against this one", err.splitlines())

    def test_a_place_that_did_not_take_the_tooling_fails_the_run_and_the_rest_still_runs(self):
        self.w.said[("moose", "pull")] = Result(1, "", "not a fast-forward\n")
        rc, err = self.sync("--all")
        self.assertEqual((rc, warned(err), self.w.steps[-1]), (1, "warning: 1 place(s) did not take the tooling -- see above",
                                                               "ask moose: wk sync p-ws (no-delegate)"))
        self.assertIn("  moose                    git pull --ff-only failed there", err.splitlines())

    def test_a_failed_publish_does_not_stop_the_fetches_and_is_not_a_success(self):
        self.w.react(["git", "clone"], lambda a, f: Result(128, "", "fatal: no space"))
        rc, err = self.sync("--on", "container")
        self.assertEqual((rc, said(err), self.w.steps[-1]), (1, "error: could not make snapshot %s: fatal: no space" % STAMP, "fetch c-ws"))
        self.assertNotIn(self.w.snapshot(), self.w.dirs)

    def test_the_mirror_and_the_publish_hold_the_store_lock(self):
        self.sync("--on", "container")
        held, out = False, []
        for e in self.w.effects:
            if e[0] in ("symlink", "remove") and e[1].endswith("/store@here.lock"):
                held = e[0] == "symlink"
            elif e[0] == "act" and e[1][:2] in (("sh", "-c"), ("git", "clone")) and kind_of(e[1][-1]) in ("refresh", ""):
                out.append((e[1][1], held))
        self.assertEqual(out[:2], [("-c", True), ("clone", True)])

    def test_a_container_copies_no_tooling_a_running_guest_gets_its_copy_and_one_not_running_is_skipped(self):
        self.w.make_guest("down", "stopped")
        rc, err = self.sync("--tools")
        self.assertEqual((rc, [s for s in self.w.steps if s.startswith("tools")]), (0, ["tools g-ws"]))
        for line in ("==> nothing to copy: a container bind-mounts this checkout (%s) at /opt/wk-tools;" % REPO,
                     "  %-24s not running -- skipped" % "down", "  %-24s ok" % "g-ws"):
            self.assertIn(line, err.splitlines())
        self.w.said[("g-ws", "tools")] = Result(0, "f00d\n")
        self.assertEqual(warned(self.sync("--tools", "vm")[1]), "warning: wk-tools at /Users/admin/wk-tools did not end at %s (it answers 'f00d')" % MAIN_SHA)


class TestThePodmanVM(Flow):
    """Inside the podman VM the mirror is the host's, read and not refreshed; a macOS host whose store is the VM's asks
    the VM's own wk with the same scope word."""

    def test_the_mirror_is_read_not_refreshed_and_the_refresh_alone_is_a_request(self):
        self.w.dirs.add(self.w.mirror)
        self.w.make("c-ws")
        self.assertEqual((self.sync("--on", "container")[0], self.w.steps), (0, ["clone", "wire snapshot", "check snapshot", "fetch c-ws"]))
        self.w.answer(["test", "-S"])
        self.w.files[CLIENT] = ""
        self.assertEqual((self.sync("--mirror")[0], self.w.steps, self.acts()[0][-2:]), (0, [], (CLIENT, "sync")))

    def test_one_workspace_fetches_in_that_one_and_refreshes_no_mirror(self):
        self.w.make("bug-238")
        self.assertEqual((self.sync("bug-238")[0], self.w.steps), (0, ["fetch bug-238"]))

    @unittest.skipUnless(sys.platform == "darwin", "a store only the podman VM can write is a macOS host's")
    def test_a_store_in_the_podman_vm_is_asked_with_the_same_scope_word_and_a_stopped_machine_is_named(self):
        for state, rc, asked, line in (("running", 0, ["sync --tools container --fix 2>&1"], ""),
                                       ("stopped", 1, [], "warning: the podman machine is stopped, so container's snapshot was not published and its")):
            w = self.make_world(in_vm=False)
            os.rmdir(w.env["WK_STORE"])
            w.answer(["podman", "machine", "inspect"], out=json.dumps([{"State": state}]))
            w.react(["podman", "machine", "ssh", "wk", "--"], lambda a, f: (f.steps.append(a[-1].split("/wk ", 1)[-1]), Result(0))[1])
            got, err = self.sync("--tools", "container", "--fix", w=w)
            self.assertEqual((got, w.steps[1:], warned(err)), (rc, asked, line), state)


class TestAPeersWorkspaces(Flow):
    """A peer is asked for each of its workspaces by name and never with a scope word -- what a scope means is that
    machine's copy of wk-tools to decide."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"moose": "peer"}, in_vm=False)
        self.w.peer_ws = ["ws-a", "ws-b"]

    def test_each_one_is_asked_by_name(self):
        rc, err = self.sync("--on", "moose")
        self.assertEqual((rc, self.w.steps[2:]), (0, ["ask moose: wk sync ws-a (no-delegate)", "ask moose: wk sync ws-b (no-delegate)"]))
        self.assertIn("said sync ws-a", err.splitlines())

    def test_fix_travels_to_it_one_that_did_not_fetch_is_no_success_and_a_usage_error_names_an_old_copy(self):
        self.w.peer_rc.update({"ws-a": 1, "ws-b": 2})
        rc, err = self.sync("--on", "moose", "--fix")
        self.assertEqual((rc, self.w.steps[2], warned(err)), (1, "ask moose: wk sync ws-a --fix (no-delegate)", "warning: moose did not fetch in 'ws-a' -- see above"))
        self.assertEqual(err.splitlines()[-2:], ["warning: moose did not fetch in 'ws-b' -- see above; a usage error is a copy of wk-tools older than 'wk sync --fix':",
                                                 "    wk sync --tools moose"])

    def test_no_workspaces_says_so_on_a_peer_or_a_place_fetched_from_here(self):
        self.w.peer_ws = []
        rc, err = self.sync("--on", "moose")
        self.assertEqual((rc, self.w.steps[2:], said(err)), (0, [], "==> no workspaces on moose"))
        w = self.make_world()
        w.dirs.add(w.mirror)
        self.assertEqual(said(self.sync("--on", "container", w=w)[1]), "==> no workspaces on container")


class TestTheMirror(Flow):
    """`wk sync --mirror` on the host: the refresh, what it reports, and the reading taken after it."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(in_vm=False)

    def test_the_first_refresh_makes_it_and_reports_each_upstream(self):
        rc, err = self.sync("--mirror")
        self.assertEqual((rc, self.w.steps, ("mkdir", os.path.dirname(self.w.mirror)) in self.w.effects), (0, ["mirror"], True))
        self.assertEqual(err.splitlines(), ["==> creating bare mirror (first run: this clones all of WebKit)", "==> fetching origin wpe fork forkwpe (origin: main)",
                                            "  origin   ok", "  wpe      FAILED (continuing)"])
        self.assertNotIn("==> creating bare mirror (first run: this clones all of WebKit)", self.sync("--mirror")[1].splitlines())

    def test_no_main_at_all_is_fatal(self):
        self.w.heads = []
        self.assertEqual(said(self.sync("--mirror")[1]), "error: main was not fetched into the mirror; nothing can be published from it")

    def test_a_refresh_that_failed_is_named(self):
        self.w.react(["sh", "-c"], lambda a, f: Result(1, "", "boom"))
        self.assertEqual(warned(self.sync("--mirror")[1]), "warning: the mirror refresh did not finish")

    def test_wk_mirror_branches_carries_the_extra_branches(self):
        self.w.env["WK_MIRROR_BRANCHES"] = "main webkitglib/2.52"
        rc, err = self.sync("--mirror")
        self.assertIn("==> fetching origin wpe fork forkwpe (origin: main webkitglib/2.52)", err.splitlines())
        self.assertEqual(warned(err), "warning: origin advertises no webkitglib/2.52, so every workspace wired to ask for it fails its")
        self.w.heads.append("webkitglib/2.52")
        self.assertEqual(warned(self.sync("--mirror")[1]), "")

    def test_stages_are_timed_under_wk_debug(self):
        self.w.env["WK_DEBUG"] = "1"
        self.assertRegex(self.sync("--mirror")[1], r"stage mirror fetch: \d+s")
        del self.w.env["WK_DEBUG"]
        self.assertNotIn("stage mirror fetch", self.sync("--mirror")[1])


class TestTheGuestsRemount(Flow):
    """`unit sync.guest_remount`: after the refresh, each running guest mounts the mirror's own share afresh, since its
    old mount keeps the inode of every ref git renamed over; a guest that cannot is named with the restart."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"container": "container", "vm": "vm"}, in_vm=False)
        for g, st in (("up-a", "running"), ("down", "stopped"), ("up-b", "running")):
            self.w.make_guest(g, st)

    def test_each_running_guest_remounts_and_a_stopped_one_does_not_and_a_refresh_that_moved_no_ref_remounts_nothing(self):
        moved = (0, ["mirror", "remount up-a", "remount up-b"])
        self.assertEqual((self.sync("--mirror")[0], self.w.steps), moved)
        self.assertEqual((self.sync("--mirror")[0], self.w.steps), (0, ["mirror"]))
        self.w.upstream = "f" * 40 + " refs/heads/main\n"
        self.assertEqual((self.sync("--mirror")[0], self.w.steps), moved)

    def test_a_dry_run_lists_each_remount_and_runs_none(self):
        rc, err = self.sync("--mirror", "--dry-run")
        self.assertEqual((rc, self.w.steps), (0, []))
        lines = err.splitlines()
        self.assertEqual([l for l in lines if l.startswith("would run in ")], ["would run in up-a: " + REMOUNT, "would run in up-b: " + REMOUNT])

    def test_a_busy_share_is_named_with_the_restart_and_the_refresh_still_succeeds(self):
        self.w.said[("up-a", "remount")] = Result(16, "", "umount(/Volumes/wk-mirror): Resource busy\n")
        rc, err = self.sync("--mirror")
        self.assertEqual((rc, self.w.steps, warned(err)), (0, ["mirror", "remount up-a", "remount up-b"],
                         "warning: 'up-a' could not remount the mirror share (umount(/Volumes/wk-mirror): Resource busy), so it reads the mirror as it"))
        self.assertIn("    was before this refresh and every fetch in it fails:  wk stop up-a, then  wk start up-a", err.splitlines())

    def test_no_guest_place_here_remounts_nothing(self):
        w = self.make_world(in_vm=False)
        self.assertEqual((self.sync("--mirror", w=w)[0], w.steps), (0, ["mirror"]))

    def test_the_remount_converges_from_a_share_mounted_or_not(self):
        bindir, at = self.tmp / "bin", str(self.tmp / "mnt")
        bindir.mkdir()
        stubs = {"mount": '[ -f "$STATE" ] && echo "wk-mirror on %s (virtiofs, local)"\nexit 0' % at,
                 "umount": '[ "$1" = "%s" ] && [ -f "$STATE" ] && rm "$STATE"' % at,
                 "mount_virtiofs": '[ "$1 $2" = "wk-mirror %s" ] && [ ! -f "$STATE" ] && touch "$STATE"' % at}
        for stub, body in stubs.items():
            (bindir / stub).write_text("#!/bin/sh\n%s\n" % body)
            os.chmod(bindir / stub, 0o755)
        for mounted in (True, False):
            state = self.tmp / ("mounted-%s" % mounted)
            if mounted:
                state.write_text("")
            cp = subprocess.run([str(REPO / "vm" / "mount-mirror.sh"), "wk-mirror", at], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]), STATE=str(state)))
            self.assertEqual((cp.returncode, state.exists()), (0, True), cp.stderr)


class TestTheSnapshot(Flow):
    """`wk sync --tools container` in the podman VM publishes a snapshot: a `--shared` clone of the mirror (or a
    hardlinked copy of the last one), wired, fetched, on a branch tracking the one it was published from, clean, with
    `sha` the completion marker written last."""

    def setUp(self):
        super().setUp()
        self.w.dirs.add(self.w.mirror)

    def published(self):
        return self.sync("--tools", "container")

    def test_no_mirror_is_refused_naming_the_host_and_reads_no_snapshot(self):
        self.w.dirs.discard(self.w.mirror)
        rc, err = self.published()
        self.assertEqual((rc, said(err), self.w.steps), (1, "error: no mirror at %s to publish a snapshot from -- 'wk sync' on the host makes it" % self.w.mirror, []))

    def test_the_first_snapshot_is_a_shared_clone_on_its_branch_with_the_sha_last(self):
        rc, err = self.published()
        tree, m = self.w.rel(self.w.snapshot() + "/WebKit"), self.w.rel(self.w.mirror)
        acts = [a for a in self.acts() if a[:2] != ("sh", "-c")]
        self.assertEqual((rc, acts), (0, [("git", "clone", "--quiet", "--shared", m, tree)] + [("git", "-C", tree) + a for a in (
            ("fetch", "--all", "--prune", "--quiet"), ("checkout", "--quiet", "-B", "main", "refs/remotes/origin/main"),
            ("branch", "--quiet", "--set-upstream-to", "origin/main", "main"), ("reset", "--hard", "--quiet"), ("clean", "-qfdx"))]))
        self.assertEqual(self.w.steps[:2], ["clone", "wire snapshot"])
        writes = [e[1] for e in self.w.effects if e[0] == "write"]
        self.assertEqual((writes, self.w.files[self.w.snapshot() + "/sha"]), ([self.w.snapshot() + "/branch", self.w.snapshot() + "/sha"], MAIN_SHA + "\n"))
        self.assertIn("==> checked out on branch main (tracking origin/main)", err.splitlines())
        self.assertIn("==> published base %s (%s)" % (STAMP, MAIN_SHA[:10]), err.splitlines())

    def test_a_verified_current_snapshot_is_left_alone(self):
        self.w.current()
        self.published()
        self.assertEqual((self.acts(), [e for e in self.w.effects if e[0] == "write"], self.w.snapshot() in self.w.dirs), ([], [], False))

    def test_a_refused_snapshot_with_the_current_sha_is_republished_from_it(self):
        self.w.publish(OLD)
        self.published()
        self.assertEqual((self.w.steps[0], self.acts()[0][-2:]), ("copy " + OLD, (self.w.rel(self.w.store.snapshot_tree(OLD)), self.w.rel(self.w.snapshot() + "/WebKit"))))
        self.assertEqual(self.w.files[self.w.snapshot() + "/sha"], MAIN_SHA + "\n")

    def test_another_branch_is_published_even_when_main_is_current(self):
        self.w.current()
        self.w.env["WK_BRANCH"] = "origin/wpe-2.46"
        rc, err = self.published()
        self.assertIn("==> checked out on branch main (tracking origin/wpe-2.46)", err.splitlines())
        self.assertEqual(self.w.files[self.w.snapshot() + "/branch"], "origin/wpe-2.46\n")

    def test_a_branch_that_is_not_remote_tracking_is_refused_and_the_half_publish_removed(self):
        self.w.env["WK_BRANCH"] = "main"
        rc, err = self.published()
        self.assertEqual((rc, said(err)), (1, "error: 'main' is not a branch this mirror carries: a snapshot is published from a"))
        self.assertIn("    remote-tracking branch, <remote>/<branch> (origin/main, wpe/wpe-2.46). Only main", err.splitlines())
        self.assertNotIn(self.w.snapshot(), self.w.dirs)

    def test_a_failed_clone_is_refused_and_removed(self):
        self.w.react(["git", "clone"], lambda a, f: Result(128, "", "fatal: no space"))
        rc, err = self.published()
        self.assertEqual((rc, said(err), self.w.snapshot() in self.w.dirs), (1, "error: could not make snapshot %s: fatal: no space" % STAMP, False))


class TestTheBaseWiring(Flow):
    """The current snapshot is where every future workspace gets its remotes from: read back on every sync of its
    place, and re-wired under --fix."""

    def setUp(self):
        super().setUp()
        self.w.current()

    def test_a_right_one_says_nothing(self):
        rc, err = self.sync("--tools", "container")
        self.assertEqual((rc, self.w.steps, warned(err)), (0, ["check snapshot"], ""))

    def test_a_wrong_one_is_named_with_its_problems_and_the_remedy(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.sync("--tools", "container")
        self.assertEqual((rc, warned(err)), (1, "warning: the snapshot %s is wired wrong, and every new workspace starts from it:" % OLD))
        self.assertEqual(err.splitlines()[-2:], ["    - origin is /x", "  re-assert it:  wk sync --on container --fix"])

    def test_fix_re_wires_it_and_a_wiring_that_fails_is_not_a_success(self):
        self.w.base_check = Result(1, "problem: origin is /x\n")
        rc, err = self.sync("--tools", "container", "--fix")
        self.assertEqual((rc, self.w.steps, said(err)), (0, ["check snapshot", "wire snapshot"], "==> re-wired the snapshot %s" % OLD))
        self.w.wire_result = Result(1)
        self.assertEqual(self.sync("--tools", "container", "--fix")[0], 1)


class TestTheFetch(Flow):
    """One fetch per workspace, all at once, one line each in the order named -- every line derived from what that
    fetch said, since `wk sync --all` is often the only thing anybody reads about a machine's workspaces."""

    def setUp(self):
        super().setUp()
        self.w.current()

    def fetch(self, *names, fix=False):
        for n in names:
            self.w.make(n)
        return self.sync(*(["--on", "container"] if len(names) > 1 else [names[0]]), *(["--fix"] if fix else []))

    def row(self, err, ws):
        return next(l for l in err.splitlines() if l.startswith("  %-24s " % ws))

    def test_the_script_is_one_fetch_and_the_report_names_the_mirror(self):
        rc, err = self.fetch("one")
        self.assertEqual((rc, self.row(err, "one")), (0, "  %-24s ok  (%s)" % ("one", self.w.mirror)))
        script = next(e[1][-1] for e in self.w.effects if e[0] == "run" and e[1][0] == "env")
        fetch = script.split("(\n", 1)[0]
        for part in ("cd /src/WebKit || exit 1", "git fetch --all --prune --quiet", "url.%s.insteadOf" % self.w.mirror):
            self.assertIn(part, fetch)
        self.assertFalse("github.com" in fetch)
        self.assertIn("==> fetching in 1 workspace(s) -- origin wpe fork forkwpe", err.splitlines())

    def test_a_dry_run_prints_the_fetch_and_judges_nothing(self):
        self.w.make("one")
        rc, err = self.sync("one", "--dry-run")
        self.assertEqual((rc, self.w.steps), (0, []))
        self.assertIn("would run in one: sh -c 'cd /src/WebKit || exit 1", err.splitlines())

    def test_the_listing_is_in_the_order_asked_for_not_finished_in(self):
        import time
        self.w.said[("a-slow", "fetch")] = lambda ws: (time.sleep(0.3), Result(0, FETCHED))[1]
        self.w.said[("c-bad", "fetch")] = Result(0, "from=mirror\nfetch=1\ncheck=0\n")
        self.w.make("a-slow")
        self.w.make("c-bad")
        self.w.containers["b-gone"] = "exited"
        self.w.react(["podman", "inspect", "wk-b-gone"], lambda a, f: Result(125))
        rc, err = self.sync("--on", "container")
        rows = [l.split()[0] for l in err.splitlines() if l.startswith("  ") and not l.startswith("    ") and l.split()[0] in ("a-slow", "b-gone", "c-bad")]
        self.assertEqual((rc, rows, warned(err)), (1, ["a-slow", "b-gone", "c-bad"], "warning: 1 workspace(s) did not fetch -- 'wk sync <ws> --fix' re-asserts the wiring it"))
        self.assertEqual((self.row(err, "b-gone"), self.row(err, "c-bad")), ("  %-24s absent -- skipped" % "b-gone", "  %-24s FAILED (continuing)" % "c-bad"))

    def test_they_run_at_once(self):
        met, broken = threading.Barrier(3, timeout=10), []

        def wait(ws):
            try:
                met.wait()
            except threading.BrokenBarrierError:
                broken.append(ws)
            return Result(0, FETCHED)
        for n in ("a", "b", "c"):
            self.w.said[(n, "fetch")] = wait
        self.fetch("a", "b", "c")
        self.assertEqual(broken, [], "the fetches ran in turn")

    def test_the_one_named_not_being_there_is_a_refusal_not_a_skip(self):
        self.w.dirs.add(self.w.store.ws_dir("one"))
        rc, err = self.sync("one")
        self.assertEqual((rc, said(err), self.w.steps), (1, "error: workspace 'one' is not there to fetch in", []))

    def test_a_machine_that_does_not_answer_is_a_skip(self):
        w = self.make_world(kinds={"buildbox4": "remote"}, in_vm=False)
        w.env["WK_REMOTE_STORE"] = str(w.tmp / "rstore")
        os.makedirs(str(w.tmp / "rstore" / "ws" / "one"))
        w.react(["ssh"], lambda a, f: Result(255, "", "Connection refused\n"))
        rc, err = self.sync("--on", "buildbox4", w=w)
        self.assertEqual((rc, self.row(err, "one")), (1, "  %-24s unreachable -- skipped" % "one"))

    def test_a_cloned_repo_reads_no_mirror_and_fix_points_its_origin_back(self):
        self.w.make("tools")
        self.w.files[os.path.join(self.w.store.ws_dir("tools"), "home", ".wk-workspace")] = "repo=wk-tools\n"
        self.w.answer(["git", "-c", "safe.directory=*", "-C", str(REPO), "remote", "get-url", "origin"], out="https://github.com/u/wk-tools.git\n")
        self.w.said[("tools", "fetch")] = Result(0, "from=github\nfetch=0\ncheck=0\n")
        rc, err = self.sync("tools", "--fix")
        self.assertEqual((rc, self.w.steps, self.row(err, "tools")), (0, ["fetch tools"], "  %-24s ok  (over the network: this checkout reads no mirror)" % "tools"))
        set_url = [e[1][-4:] for e in self.w.effects if e[0] == "run" and "set-url" in e[1]]
        self.assertEqual(set_url, [("remote", "set-url", "origin", "https://github.com/u/wk-tools.git"),
                                   ("set-url", "--push", "origin", "git@github-wk-tools:u/wk-tools.git")])

    def test_the_source_reported_is_the_one_the_checkout_answered_with(self):
        self.w.said[("one", "fetch")] = Result(0, "from=github\nfetch=0\ncheck=0\n")
        rc, err = self.fetch("one")
        self.assertEqual(self.row(err, "one"), "  %-24s ok  (over the network: this checkout reads no mirror, though this machine keeps %s -- wk sync one --fix)" % ("one", self.w.mirror))

    def test_what_falls_short_is_named_under_the_workspace_and_fails_the_run(self):
        """A checkout that names no source is no local read, a check that never reported is wired wrong, an exec that
        answered nothing failed, and a failed fetch names the problem the check found."""
        ok, failed = "  %-24s ok  (%s)" % ("one", self.w.mirror), "  %-24s FAILED (continuing)" % "one"
        wired = "warning: 1 workspace(s) fetched but are wired wrong (above) -- 'wk sync <ws> --fix' re-asserts the wiring"
        unfetched = "warning: 1 workspace(s) did not fetch -- 'wk sync <ws> --fix' re-asserts the wiring it"
        for out, lines, warning in ((Result(0, "fetch=0\ncheck=0\n"), ["  %-24s ok  (it did not say which source it read)" % "one"], ""),
                                    (Result(0, "from=mirror\nfetch=0\n"), [ok + " -- wired wrong:", "    - the wiring check did not report (it never reached its end)"], wired),
                                    (Result(1, "", "no such container"), [failed], unfetched),
                                    (Result(0, "from=mirror\nfetch=0\nproblem: origin accepts a push (x)\ncheck=1\n"), [ok + " -- wired wrong:", "    - origin accepts a push (x)"], wired),
                                    (Result(0, "from=mirror\nfetch=1\nproblem: the mirror carries no refs/heads/x\ncheck=1\n"), [failed, "    - the mirror carries no refs/heads/x"], unfetched)):
            self.w.said[("one", "fetch")] = out
            rc, err = self.fetch("one")
            i = err.splitlines().index(lines[0])
            self.assertEqual((rc if warning else 1, err.splitlines()[i:i + len(lines)], warned(err)), (1, lines, warning), out.out)

    def test_fix_re_asserts_the_wiring_the_upstream_and_git_webkit_before_the_fetch(self):
        self.w.on_branch["one"] = "eng/x"
        rc, err = self.fetch("one", fix=True)
        self.assertEqual((rc, self.w.steps), (0, ["wire one", "fetch eng/x from fork", "setup one", "fetch one"]))
        i = err.splitlines().index("  %-24s ok  (%s)" % ("one", self.w.mirror))
        self.assertEqual(err.splitlines()[i + 1:i + 4], ["    re-wired", "    retargeted: eng/x now tracks fork/eng/x", "    git-webkit: setup=ok"])

    def test_a_git_webkit_setup_that_failed_names_both_remedies(self):
        self.w.said[("one", "setup")] = Result(1, "setup=failed\n", "HTTP 401\n")
        rc, err = self.fetch("one", fix=True)
        self.assertEqual((rc, err.splitlines()[-4:-1]), (1, ["    re-wired", "    HTTP 401", "    'git-webkit setup' did not finish (setup=failed); 'wk sync one --fix' again"]))

    def test_a_wiring_that_did_not_take_is_named_and_the_fetch_still_runs(self):
        self.w.said[("one", "wire")] = Result(1)
        rc, err = self.fetch("one", fix=True)
        self.assertEqual((rc, self.w.steps), (1, ["wire one", "fetch one"]))
        self.assertIn("    could not re-wire 'one'", err.splitlines())

    def test_a_guest_gets_git_webkit_set_up_and_a_build_box_of_the_persons_own_does_not(self):
        w = self.make_world(kinds={"vm": "vm"}, in_vm=False)
        w.make_guest("one")
        self.assertEqual((self.sync("one", "--fix", w=w)[0], w.steps), (0, ["wire one", "setup one", "fetch one"]))
        w = self.make_world(kinds={"buildbox4": "remote"}, in_vm=False)
        w.env.update(WK_REMOTE_LOCAL="1", WK_REMOTE_ROOT="/far")
        w.dirs.add("/far/ws/one")
        w.files["/far/ws/one/.wk-ready"] = ""
        self.assertEqual((self.sync("one", "--fix", w=w)[0], w.steps), (0, ["wire one", "fetch one"]))
        wired = next(e[1][-1] for e in w.effects if e[0] == "run" and kind_of(e[1][-1]) == "wire")
        self.assertIn("git remote add mirror /far/mirror 2>/dev/null || git remote set-url mirror /far/mirror", wired)


class TestAPeersTooling(Flow):
    """A peer pulls its own wk-tools, and publishes its own snapshot only once it matches this checkout and was named."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(kinds={"moose": "peer"}, in_vm=False)

    def test_a_copy_that_still_differs_is_asked_for_nothing_more_and_why_is_named(self):
        self.w.said[("moose", "tools")] = Result(0, "sha=0000stale0000\ndirty=no\n")
        self.w.answer(["git", "-C", str(REPO), "status"], out=" M wk\n")
        rc, err = self.sync("--tools", "moose")
        self.assertEqual((rc, self.w.steps), (1, ["pull moose"]))
        self.assertIn("  %-24s pulled, still DIFFERS (0000stale000, this machine has %s+dirty)" % ("moose", MAIN_SHA[:12]), err.splitlines())
        self.assertIn(" " * 27 + "this machine has uncommitted changes -- a peer pulls from the upstream, so commit and push them first", err.splitlines())

    def test_each_reason_this_machine_is_not_what_a_peer_pulls(self):
        up, ahead = ("rev-parse", "--abbrev-ref", "@{upstream}"), ("rev-list",)
        for answers, why in ((((("rev-parse", "--git-dir"), 128, ""),), "this copy of wk-tools is not a git checkout, so nothing can pull from it"),
                             (((("rev-parse", "--abbrev-ref", "HEAD"), 0, "eng/x\n"),), "branch 'eng/x' has no upstream here, so there is nothing for a peer to pull from"),
                             (((up, 0, "origin/main\n"), (ahead, 0, "2\n")), "this machine is 2 commit(s) ahead of origin/main -- push them, then re-run"),
                             (((up, 0, "origin/main\n"), (ahead, 128, "")), "git could not count this machine's commits past origin/main")):
            w = self.make_world(kinds={"moose": "peer"}, in_vm=False)
            w.said[("moose", "tools")] = Result(0, "sha=0000stale0000\ndirty=no\n")
            for args, rc, out in answers:
                w.answer(["git", "-C", str(REPO)] + list(args), rc=rc, out=out)
            rc, err = self.sync("--tools", "moose", w=w)
            self.assertIn(" " * 27 + why, err.splitlines(), why)

    def test_a_converged_copy_named_by_this_run_publishes_its_own_snapshot(self):
        rc, err = self.sync("--tools", "moose")
        self.assertEqual((rc, self.w.steps), (0, ["pull moose", "ask moose: wk sync --tools (no-delegate)"]))
        self.assertIn("  %-24s pulled, in sync" % "moose", err.splitlines())

    def test_a_converged_copy_not_named_keeps_its_store_untouched(self):
        rc, err = self.sync("--tools")
        self.assertEqual((rc, self.w.steps), (0, ["pull moose"]))
        self.assertIn("  name it for those:  wk sync --tools moose", err.splitlines())

    def test_a_pull_that_failed_is_named(self):
        self.w.said[("moose", "pull")] = Result(1, "", "not a fast-forward\n")
        rc, err = self.sync("--tools", "moose")
        self.assertEqual((rc, self.w.steps), (1, ["pull moose"]))
        self.assertIn("  %-24s git pull --ff-only failed there" % "moose", err.splitlines())


class TestABuildBoxsTooling(Flow):
    def test_it_is_pushed_this_head_and_its_mirror_refreshed(self):
        w = self.make_world(kinds={"buildbox4": "remote"}, in_vm=False)
        rc, err = self.sync("--tools", "buildbox4", w=w)
        self.assertEqual((rc, w.steps), (0, ["tools buildbox4", "mirror buildbox4"]))
        self.assertIn("  %-24s pushed %s" % ("buildbox4", MAIN_SHA), err.splitlines())
        self.assertIn("==> the WebKit mirror on buildbox4 is up to date", err.splitlines())


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
