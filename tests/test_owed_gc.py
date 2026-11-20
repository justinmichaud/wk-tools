"""`wk gc` typed into a world: what a plain run reclaims, what it names with the flag or command that takes it, what it
keeps, its one question, the podman VM's half on a macOS host, tart's cache, and the dry run."""
import io
import json
import os
import shlex
import unittest
from unittest import mock

from tests import fakes
from tests.fakes import FakeRegistry, Result
from tests.support import REPO
from tests.test_wk_places import LINUX_PROBE
from tests.test_wk_workspace import Flow, said

WK = str(REPO / "wk")
LINUX = os.uname_result(("Linux", "here", "6.8.0", "", "x86_64"))
MAC = os.uname_result(("Darwin", "here", "25.0.0", "", "arm64"))
SLOTS = "/var/wk/slots/"
PUSH_REF = "refs/wk/push/box/eng/x"
GB = 1 << 20   # in kB


class Host(fakes.World):
    """This host, Linux and holding the container store: podman answers from `containers`, `images`, `volumes` and
    `mounting` (what mounts a retired tree), the mirror's git from `refs`, board rpi5's bench system over ssh from `slots`,
    build box `box` from `box_up`; `ls -1t` lists in `order`. Every `du` counts 4 kB."""

    def __init__(self, tmp):
        super().__init__(tmp, WK_CONTAINER_USER="u", WK_SDK="/sdk")
        self.env.update(WK_MACHINES_DIR=str(self.tmp / "machines"), XDG_CONFIG_HOME=str(self.tmp / "config"), XDG_DATA_HOME=str(self.tmp / "data"))
        os.makedirs(self.env["WK_MACHINES_DIR"], exist_ok=True)
        self.store = self.env["WK_STORE"]
        self.containers, self.images, self.volumes, self.mounting, self.refs = set(), [], [], [], set()
        self.slots, self.box_up, self.order = set(), True, {}
        self.answer(["du", "-sk"], out="4\tx\n")
        self.answer(["tailscale"], rc=127)
        self.answer(["sudo", "-n", "true"], rc=1)
        for prefix, fn in ((["podman"], self._podman), (["env"], self._rm), (["ls"], self._ls), (["ssh"], self._ssh),
                           (["git", "-C", self.mirror()], self._git)):
            self.react(prefix, fn)
        self.reg = FakeRegistry(self.env, self)
        self.effects = []

    def p(self, rel):
        return os.path.join(self.store, rel)

    def mirror(self):
        return self.p("git/WebKit.git")

    def conf(self, name, text):
        (self.tmp / "machines" / (name + ".conf")).write_text(text)

    def made(self, *rels):
        self.mkdirs(*(self.p(r) for r in rels))

    def mkdirs(self, *paths):
        for path in paths:
            while path not in ("", "/"):
                self.dirs.add(path)
                path = os.path.dirname(path)

    def lock(self, resource, pid):
        path = os.path.join(self.env["WK_LOCK_DIR"], resource + "@here.lock")
        self.files[path] = "pid=%d tok=x at=t cmd=wk" % pid
        self.links.add(path)
        self.pids.add(pid)

    def publish(self, *bids, complete=True):
        for bid in bids:
            self.made("base/%s/WebKit" % bid)
            if complete:
                self.files[self.p("base/%s/sha" % bid)] = "a" * 40 + "\n"

    def workspace(self, name, ready=True, base="b1"):
        d = self.p("ws/" + name)
        self.mkdirs(d + "/home")
        if base:
            self.files[d + "/base-id"] = base + "\n"
        if ready:
            self.files[d + "/home/.wk-ready"] = ""
        self.containers.add(name)

    def record(self, root, kind, name, pid=999999, log=None):
        d = os.path.join(root, "task", "%s-%s-20260101T000000Z" % (kind, name))
        for k, v in (("plan", "create"), ("kind", kind), ("name", name), ("pid", str(pid)), ("where", "here")) + ((("log", log),) if log else ()):
            self.write_own(os.path.join(d, k), v + "\n")
        if log:
            self.write_own(log, "")

    def _podman(self, a, f):
        a = [x for i, x in enumerate(a) if x != "-c" and a[i - 1:i] != ["-c"]]
        if a[1] == "ps":
            if a[4].startswith("volume="):
                return Result(0, "".join(n + "\n" for n in f.mounting))
            return Result(0, "".join("wk-%s\tUp\n" % n for n in sorted(f.containers)))
        if a[1] == "inspect":
            return Result(0, "running\n") if a[2][3:] in f.containers else Result(125, "", "no such container")
        if a[1] == "images":
            return Result(0, json.dumps(f.images))
        if a[1:3] == ["image", "prune"]:
            f.images = [i for i in f.images if not i.get("Dangling")]
        elif a[1] == "rmi":
            f.images = [i for i in f.images if i["Id"] != a[-1]]
        elif a[1:3] == ["volume", "ls"]:
            return Result(0, "".join(v + "\n" for v in f.volumes))
        elif a[1:3] == ["volume", "prune"]:
            f.volumes = []
        return Result(0)

    def _rm(self, a, f):
        f.containers.discard(a[4])
        f._drop(f.p("ws/" + a[4]))
        return Result(0)

    def _ls(self, a, f):
        d = a[-1]
        if a[1] in ("-1t", "-1At"):
            return Result(0, "".join(n + "\n" for n in f.order.get(d, []) if f.exists(os.path.join(d, n))))
        return Result(0, "".join(n + "\n" for n in f.listdir(d))) if d in f.dirs else Result(2, "", "no such directory")

    def _git(self, a, f):
        if a[3] == "update-ref":
            f.refs.discard(a[-1])
            return Result(0)
        return Result(0, "".join(r + "\n" for r in sorted(f.refs)))

    def _ssh(self, a, f):
        words = shlex.split(a[-1])
        if words[:2] == ["du", "-sk"]:
            return Result(0, "4\t%s\n" % words[2])
        if "rpi5-bench" in a:
            if words[:2] == ["rm", "-rf"]:
                f.effect(("far-remove", "rpi5-bench", words[2]))
                f.slots.discard(os.path.basename(words[2]))
            return Result(0, "".join(s + "\n" for s in sorted(f.slots)))
        if not f.box_up:
            return Result(255, "", "ssh: connect to host box.example port 22: Connection refused\n")
        return Result(0, LINUX_PROBE)

    def taken(self):
        """What the command asked to change, in order: a removed path, an argv run, or (machine, path) removed there."""
        return [self.rel(e[1]) if e[0] == "remove" else self.rel(" ".join(e[1])) if e[0] == "act" else e[1:]
                for e in self.work() if e[0] in ("remove", "act", "far-remove")]

    def state(self):
        locks = self.env["WK_LOCK_DIR"]
        return (sorted(self.rel(p) for p in self.files if not p.startswith(locks)),
                sorted(self.rel(d) for d in self.dirs if not d.startswith(locks) and not d.endswith("/wk/ssh")),   # ssh's ControlPath
                sorted(self.containers), json.dumps(self.images), self.volumes, sorted(self.slots), sorted(self.refs))


class Mac(Host):
    """This host a Mac: the container store is the podman VM's, whose wk answers `wk gc --rows` with `vm_rows` and the
    rest with `vm_rc` (each ask in `vm_asked`); its machine is `vm_state`, its disk image `image_kb` big with its
    filesystem using `used_kb`; tart, once `tart()`, lists `guests`; the bench volume is mounted at /vol."""

    def __init__(self, tmp):
        super().__init__(tmp)
        self.env.update(WK_VM_STORE=str(self.tmp / "vmstore"), TART_HOME="/tart", PATH=str(self.tmp / "bin"), WK_BENCH_ROOT="/vol")
        self.vm_rows, self.vm_rc, self.vm_asked, self.vm_state, self.guests = "", 0, [], "running", []
        self.image_kb = self.used_kb = 0
        self.react(["podman", "machine", "inspect"], lambda a, f: Result(0, json.dumps([{"Name": "wk", "State": f.vm_state}])))
        self.react(["podman", "machine", "ssh"], self._vm)
        self.react(["du", "-sk"], lambda a, f: Result(0, "%d\t%s\n" % (f.image_kb if a[2].endswith(".raw") else 4, a[2])))

    def mirror(self):
        return str(self.tmp / "state" / "wk" / "git" / "WebKit.git")

    def tart(self):
        os.makedirs(self.env["PATH"])
        bin = os.path.join(self.env["PATH"], "tart")
        open(bin, "w").close()
        os.chmod(bin, 0o755)
        bin = os.path.realpath(bin)
        self.answer([bin, "prune"])
        self.react([bin, "list"], lambda a, f: Result(0, json.dumps([{"Name": g, "State": "stopped", "Source": "local"} for g in f.guests])))
        return bin

    def disk_image(self, image_kb, used_kb):
        d = self.tmp / "data" / "containers" / "podman" / "machine" / "applehv"
        d.mkdir(parents=True)
        (d / "wk-arm64.raw").write_text("")
        self.image_kb, self.used_kb = image_kb, used_kb

    def _vm(self, a, f):
        text = a[-1]
        if text.startswith("df "):
            return Result(0, "Used\n%d\n" % f.used_kb)
        if "fstrim" in text:
            return Result(0, "/: 1 GiB trimmed\n")
        words = shlex.split(text)
        i = next(n for n, w in enumerate(words) if w.endswith("/wk"))
        f.vm_asked.append(" ".join([w for w in words[:i] if w == "WK_YES=1"] + [w for w in words[i + 1:] if w != "2>&1"]))
        return Result(0, f.vm_rows) if "--rows" in words else Result(f.vm_rc, "")


BOX = "/state/wk/remote/box"
SEED = "cache/bench/.tmp-speedometer3-aaaaaaaaaaaa"


def _box(w, record=None, pid=999999):
    w.conf("box", "kind=build\nhost=box.example\nroot=/r\n")
    if record:
        root = str(w.tmp) + BOX
        w.record(root, record, "a", pid, log="%s/%s.log" % (root, record))


def _payloads(w):
    w.made(*("cache/bench/speedometer3-" + c * 12 for c in "ab"))
    w.order[w.p("cache/bench")] = ["speedometer3-" + "b" * 12, "speedometer3-" + "a" * 12]


def _runners(w):
    w.made("cache/bench-runner/new", "cache/bench-runner/old")
    w.order[w.p("cache/bench-runner")] = ["new", "old"]


def _board(slot):
    def make(w):
        w.conf("rpi5", "kind=board\nbench_ssh=rpi5-bench\n")
        w.slots = {"a", slot}
    return make


def _push_ref(w):
    w.mkdirs(w.mirror())
    w.refs = {PUSH_REF}


RM = "env WK_PLACE=container %s rm %%s --yes" % WK
# kind: (make, what takes it ("" a plain run), the row a plain run prints, what the run that takes it removes)
KINDS = {
    "snapshot": (lambda w: w.publish("b1", "b2"), "", "4.0K snapshot b1, no workspace on it goes", ["/store/base/b1"]),
    "unfinished snapshot": (lambda w: (w.publish("b1"), w.publish("b2", complete=False)), "", "4.0K snapshot b2, no workspace on it goes",
                            ["/store/base/b2"]),
    "mirror": (lambda w: (w.publish("b1"), w.mkdirs(w.mirror())), "--purge-mirror",
               "8.0K the mirror and every snapshot kept -- 'wk gc --purge-mirror' takes it", ["/store/base", "/store/git/WebKit.git"]),
    "seed": (lambda w: w.made(SEED), "", "4.0K abandoned payload seed .tmp-speedometer3-aaaaaaaaaaaa goes", ["/store/" + SEED]),
    "payload": (_payloads, "", "4.0K superseded speedometer3 payload speedometer3-aaaaaaaaaaaa goes",
                ["/store/cache/bench/speedometer3-aaaaaaaaaaaa"]),
    "runner": (_runners, "", "4.0K benchmark runner tree old goes", ["/store/cache/bench-runner/old"]),
    "build-output": (lambda w: w.made("cache/yocto"), "--purge-builds",
                     "4.0K yocto build output /store/cache/yocto kept -- 'wk gc --purge-builds' takes it", ["/store/cache/yocto"]),
    "dangling-images": (lambda w: w.images.append({"Id": "d1", "Dangling": True, "Size": 4096}), "",
                        "4.0K 1 dangling container image(s) goes", ["podman image prune -f"]),
    "container-image": (lambda w: w.images.append({"Id": "t1", "Names": ["ghcr.io/igalia/wkdev:old"], "Size": 4096, "Containers": 0}),
                        "--purge-images", "4.0K container image ghcr.io/igalia/wkdev:old, no container on it "
                        "kept -- 'wk gc --purge-images' takes it", ["podman rmi t1"]),
    "dangling-volumes": (lambda w: w.volumes.append("v1"), "", "?? 1 container volume(s) no container mounts goes", ["podman volume prune -f"]),
    "retired": (lambda w: w.made("cache/ccache", "skills"), "", "4.0K /store/cache/ccache, which no workspace made now mounts goes",
                ["/store/cache/ccache", "/store/skills"]),
    "board-slot-copy": (_board("a.part"), "", "4.0K rpi5: slot copy a.part, its deploy never finished goes", [("rpi5-bench", SLOTS + "a.part")]),
    "board-slot-instrumented": (_board("a-instr"), "--purge-rubble",
                                "4.0K rpi5: instrumented slot a-instr kept -- 'wk gc --purge-rubble' takes it", [("rpi5-bench", SLOTS + "a-instr")]),
    "remote-mirror": (_box, "wk machine rm box", "4.0K box: its store's mirror kept -- 'wk machine rm box' takes it", []),
    "box-record": (lambda w: _box(w, "build"), "--purge-rubble", "4.0K box: a build record of 'a' kept here, not on the box "
                   "kept -- 'wk gc --purge-rubble' takes it", [BOX + "/task/build-a-20260101T000000Z", BOX + "/build.log"]),
    "box-creation-record": (lambda w: _box(w, "new"), "--purge-rubble", "4.0K box: a new record of 'a' kept here, not on the box "
                            "kept -- 'wk gc --purge-rubble' takes it", [BOX + "/task/new-a-20260101T000000Z", BOX + "/new.log"]),
    "push-ref": (_push_ref, "", "0B %s, left in the mirror by a killed box push goes" % PUSH_REF,
                 ["git -C /store/git/WebKit.git update-ref -d " + PUSH_REF]),
    "half-made": (lambda w: w.workspace("hm", ready=False, base=""), "--purge-rubble", "?? workspace 'hm' on container, half-made and "
                  "nothing creating it kept -- 'wk gc --purge-rubble' takes it", [RM % "hm"]),
    "selftest-ws": (lambda w: w.workspace("wk-test-abc123", base="b2"), "", "?? workspace 'wk-test-abc123' on container, left by a "
                    "selftest goes", [RM % "wk-test-abc123"]),
    "creation-record": (lambda w: w.record(w.store, "new", "gone"), "", "?? creation record of 'gone' on container, its workspace gone goes",
                        ["/store/task/new-gone-20260101T000000Z", "/store/log/new-gone.log"]),
}


def rows(err):
    """The rows as a person reads them, their column padding closed up."""
    return [" ".join(line.split()) for line in err.splitlines() if line[:2] == line[9:11] == "  "]


class GcTest(Flow):
    """`wk gc` on a Linux host (`mac`: a Mac), --yes unless `ask`."""

    def make_world(self, mac=False, *kinds):
        p = mock.patch("os.uname", return_value=MAC if mac else LINUX)
        p.start()
        self.addCleanup(p.stop)
        w = (Mac if mac else Host)(self.tmp)
        for k in kinds:
            KINDS[k][0](w)
        return w

    def gc(self, *flags, w=None, ask=False):
        rc, err = self.typed("gc", *flags, *(() if ask else ("--yes",)), w=w)
        return rc, (w or self.w).rel(err)

    def assertGone(self, w, removed, before):
        """Each path `removed` names, and each board slot, is gone; what an argv took changed the world."""
        left = [p for p in removed if isinstance(p, str) and p.startswith("/") and w.exists(str(w.tmp) + p)]
        left += [p for _, p in (r for r in removed if isinstance(r, tuple)) if os.path.basename(p) in w.slots]
        self.assertEqual((left, w.state() != before), ([], True))


def rubble_world(case):
    """A store holding one of each kind of rubble a plain run takes."""
    return case.make_world(False, "snapshot", "seed", "payload", "runner", "board-slot-copy", "selftest-ws")


class TestReclaimsOrNames(GcTest):
    def test_every_kind_is_taken_by_a_plain_run_or_named_with_what_takes_it(self):
        for kind, (_, taker, row, removed) in KINDS.items():
            with self.subTest(kind=kind):
                w = self.make_world(False, kind)
                before = w.state()
                rc, err = self.gc(w=w)
                self.assertEqual((rc, [r for r in rows(err) if r == row]), (0, [row]), err)
                if taker:
                    self.assertEqual((w.taken(), w.state()), ([], before), err)
                if taker.startswith("--"):
                    rc, err = self.gc(taker, w=w)
                    self.assertEqual(rc, 0, err)
                self.assertEqual(w.taken(), removed, err)
                if removed:
                    self.assertGone(w, removed, before)

    def test_a_box_record_whose_driver_still_runs_here_is_named_with_its_kill_and_refuses_the_flag(self):
        _box(self.w, "build", pid=4242)
        self.w.pids.add(4242)
        before, why = self.w.state(), "kept -- its detached run, pid 4242, still runs here: 'kill 4242' ends it"
        rc, err = self.gc()
        self.assertEqual((rc, rows(err)[0]), (0, "4.0K box: a build record of 'a' kept here, not on the box " + why))
        rc, err = self.gc("--purge-rubble")
        self.assertEqual((rc, said(err), self.w.taken(), self.w.state()),
                         (1, "error: box: a build record of 'a' kept here, not on the box: " + why, [], before))

    def test_a_box_that_does_not_answer_is_named_not_looked_at(self):
        _box(self.w)
        self.w.box_up = False
        self.assertEqual(self.gc(), (0, "==> what wk left on this machine, and what takes it\n       ??  box: its store's mirror"
                                        "                        not looked at -- box did not answer: connect to host box.example port 22: "
                                        "Connection refused\n==> nothing to reclaim\n"))

    def test_rows_prints_each_row_tab_separated_and_takes_nothing(self):
        w = self.make_world(False, "half-made", "seed")
        rc, out, _ = self.wk(w, "gc", "--rows")
        self.assertEqual((rc, out.splitlines(), w.taken()), (0, [
            "4\tseed\tabandoned payload seed .tmp-speedometer3-aaaaaaaaaaaa\t\t\tt",
            "??\thalf-made\tworkspace 'hm' on container, half-made and nothing creating it\t--purge-rubble\t\tt"], []))
        self.assertEqual(said(self.typed("gc", "--rows", "--purge-rubble")[1]),
                         "error: --rows prints what 'wk gc' would take, and takes no --purge flag")


class TestAMeasurementIsNeverRubble(GcTest):
    def task(self, *workspaces):
        d = self.w.p("bench/20260101T000000Z-rpi5-a")
        self.w.write_own(d + "/task.json", "{}")
        for i, ws in enumerate(workspaces):
            self.w.write_own(d + "/runs/r%d/env.json" % i, json.dumps({"workspace": ws}))

    def test_no_flag_takes_it_and_gc_names_the_move_into_its_workspace(self):
        self.task("bug-238")
        before = self.w.state()
        for flags in ((), ("--purge-rubble",), ("--purge-rubble", "--purge-builds", "--purge-images")):
            with self.subTest(flags=flags):
                rc, err = self.gc(*flags)
                self.assertEqual((rc, rows(err), self.w.taken(), self.w.state()), (0, [
                    "4.0K /store/bench/20260101T000000Z-rpi5-a: a task outside any workspace kept -- a measurement is never rubble; "
                    "once moved into its workspace every command reads it: mkdir -p /store/ws/bug-238/bench && "
                    "mv /store/bench/20260101T000000Z-rpi5-a /store/ws/bug-238/bench/"], [], before))

    def test_a_task_whose_runs_name_no_one_workspace_names_the_placeholder(self):
        self.task("a", "b")
        self.assertIn("mv /store/bench/20260101T000000Z-rpi5-a /store/ws/<workspace>/bench/", rows(self.gc()[1])[0])


class TestWhatAPlainRunKeeps(GcTest):
    def kept(self, *flags, w=None):
        """A run that takes nothing and changes nothing: its exit status and rows."""
        w = w or self.w
        before = w.state()
        rc, err = self.gc(*flags, w=w)
        self.assertEqual((w.taken(), w.state()), ([], before), err)
        return rc, rows(err)

    def test_the_shared_ccache_and_skills_are_kept_while_a_container_mounts_them(self):
        w = self.make_world(False, "retired")
        w.mounting = ["wk-old"]
        self.assertEqual(self.kept(w=w)[1][0], "4.0K /store/cache/ccache, which no workspace made now mounts "
                                               "kept -- wk-old mount it: 'wk rm' and 'wk new' remake each without it")

    def test_a_workspace_a_live_selftest_made_is_kept(self):
        w = self.make_world(False, "selftest-ws")
        w.lock("selftest", 4242)
        self.assertEqual(self.kept(w=w), (0, ["?? workspace 'wk-test-abc123' on container, left by a selftest kept -- the live "
                                              "'wk selftest' running here made it"]))

    def test_a_seed_whose_lock_is_held_is_being_made_and_kept(self):
        w = self.make_world(False, "seed")
        w.lock("bench-seed-speedometer3-aaaaaaaaaaaa", 4242)
        self.assertEqual(self.kept(w=w), (0, []))

    def test_a_workspace_still_being_created_is_not_rubble(self):
        w = self.make_world(False, "half-made")
        w.record(w.store, "new", "hm", pid=4242)
        w.pids.add(4242)
        self.assertEqual(self.kept("--purge-rubble", w=w), (0, []))

    def test_a_finished_workspace_and_its_pinned_snapshot_are_not_rubble(self):
        self.w.publish("b1", "b2")
        self.w.workspace("done", base="b1")
        self.assertEqual(self.kept(), (0, ["4.0K every snapshot kept -- every workspace is overlaid on a snapshot here: 'wk rm' done first"]))
        self.assertEqual(said(self.gc("--purge-mirror")[1]), "error: every snapshot: kept -- every workspace is overlaid on a snapshot "
                                                             "here: 'wk rm' done first")

    def test_an_unpinned_workspace_keeps_every_snapshot_and_says_why(self):
        w = self.make_world(False, "snapshot")
        w.workspace("nopin", base="")
        self.assertEqual(self.kept(w=w)[1][0], "4.0K snapshots kept -- nopin never recorded a snapshot, so any snapshot may be under it: "
                                               "'wk new <name>' remakes one, 'wk rm <name>' removes it")

    def test_a_push_ref_a_live_pr_open_holds_is_kept_and_named(self):
        w = self.make_world(False, "push-ref")
        w.lock("push-ref-box%eng%x", 4242)
        self.assertEqual(self.kept(w=w)[1][-1], "0B %s, left in the mirror by a killed box push kept -- the 'wk pr open' (pid 4242) "
                                                "pushing it holds it" % PUSH_REF)


class TestWhatWasNotLookedAt(GcTest):
    def test_a_failed_image_listing_is_named_not_reported_as_nothing(self):
        self.w.answer(["podman", "images"], rc=125, err="cannot connect to podman")
        self.assertEqual(rows(self.gc()[1]), ["?? container images not looked at -- 'podman images' failed: cannot connect to podman"])

    def test_a_failed_ref_listing_in_the_mirror_is_named(self):
        w = self.make_world(False, "push-ref")
        w.answer(["git", "-C", w.mirror(), "for-each-ref"], rc=128, err="fatal: not a git repository")
        self.assertEqual(rows(self.gc(w=w)[1])[-1], "?? box pushes' temporary refs in the mirror not looked at -- git for-each-ref "
                                                    "failed: fatal: not a git repository")


class Terminal(io.StringIO):
    def isatty(self):
        return True


class TestOneQuestion(GcTest):
    def test_nothing_to_take_asks_nothing(self):
        rc, err = self.gc(ask=True)
        self.assertEqual((rc, said(err), self.w.taken()), (0, "==> nothing to reclaim", []))

    def test_declining_changes_nothing(self):
        w = self.make_world(False, "snapshot")
        before = w.state()
        rc, err = self.gc(w=w, ask=True)
        self.assertEqual((rc, said(err), w.taken(), w.state()), (1, "error: nothing was changed", [], before))

    def test_it_is_asked_once_on_a_terminal_and_a_yes_takes_every_row(self):
        w = self.make_world(False, "snapshot", "seed")
        with mock.patch("sys.stdin", Terminal("y\n")):
            rc, err = self.gc(w=w, ask=True)
        self.assertEqual((rc, err.count("[y/N]"), w.taken()), (0, 1, ["/store/base/b1", "/store/" + SEED]))

    def test_a_flag_refused_by_a_row_refuses_the_run_before_anything_goes(self):
        w = self.make_world(False, "mirror", "seed")
        w.workspace("live")
        before = w.state()
        rc, err = self.gc("--purge-mirror", w=w)
        self.assertEqual((rc, said(err), w.taken(), w.state()), (1, "error: the mirror and every snapshot: kept -- every workspace "
                                                                    "is overlaid on a snapshot here: 'wk rm' live first", [], before))

    def test_a_failing_removal_ends_none_of_the_rest(self):
        w = self.make_world(False, "dangling-images", "seed")
        w.answer(["podman", "image", "prune"], rc=1)
        rc, err = self.gc(w=w)
        self.assertEqual((rc, said(err), w.taken(), w.exists(w.p(SEED))), (1, "==> gc complete", ["podman image prune -f", "/store/" + SEED], False))
        self.assertIn("warning: could not reclaim 1 dangling container image(s)", err)

    def test_a_dry_run_says_what_would_go_and_changes_nothing(self):
        w = self.make_world(False, "snapshot", "board-slot-copy")
        before = w.state()
        rc, err = self.typed("gc", "--dry-run", w=w)
        self.assertEqual((rc, w.state()), (0, before), err)
        self.assertIn("would ask: remove the 2 item(s) marked 'goes' (8.0K at least)? [y/N]", err)
        self.assertIn("would remove on rpi5-bench: /var/wk/slots/a.part", err)


class TestTheVmHalf(GcTest):
    """On a Mac the podman VM's own wk answers for the container store, and tart's pulled-image cache is pruned to its
    budget (caches only) while the golden base is named with the command that erases it."""

    def setUp(self):
        super().setUp()
        self.w = self.make_world(mac=True)

    def test_the_blocks_the_images_filesystem_freed_are_a_row_fstrim_takes(self):
        self.w.disk_image(3 * GB, GB)
        rc, err = self.gc()
        self.assertEqual((rc, rows(err), self.w.taken()), (0, ["2.0G the podman machine's disk image, holding blocks its filesystem "
                                                               "freed goes"], ["podman machine ssh wk -- sudo fstrim -av"]))

    def test_under_a_gigabyte_freed_is_no_row(self):
        self.w.disk_image(GB + 10, GB)
        rc, err = self.gc()
        self.assertEqual((rc, rows(err), self.w.taken()), (0, [], []))

    def test_the_vms_rows_are_asked_once_and_its_wk_takes_them_with_the_answer(self):
        self.w.vm_rows = "4\tsnapshot\tsnapshot b1\t\t\tt\n"
        with mock.patch("sys.stdin", Terminal("y\n")):
            rc, err = self.gc(ask=True)
        self.assertEqual((rc, rows(err), err.count("[y/N]"), self.w.vm_asked), (0, ["4.0K podman VM: snapshot b1 goes"], 1,
                                                                                    ["gc --rows", "gc --yes"]))

    def mirror_kept(self, rc, said_, asked):
        self.w.mkdirs(self.w.mirror())
        before = self.w.state()
        got, err = self.gc("--purge-mirror")
        self.assertEqual((got, said(err), self.w.vm_asked, self.w.taken(), self.w.state()), (rc, said_, asked, [], before))
        return err

    def test_the_hosts_mirror_is_kept_while_the_vm_has_a_workspace_on_it(self):
        self.w.vm_rows = "4\tmirror\tevery snapshot\t--purge-mirror\tkept -- 'wk rm' a first\tt\n"
        self.mirror_kept(1, "error: the mirror and every snapshot: kept -- 'wk rm' a first", ["WK_YES=1 gc --rows"])

    def test_a_failing_vm_half_keeps_the_mirror_it_borrows(self):
        self.w.vm_rows, self.w.vm_rc = "4\tsnapshot\tsnapshot b1\t\t\tt\n", 3
        err = self.mirror_kept(3, "==> gc complete", ["WK_YES=1 gc --rows", "WK_YES=1 gc --yes --purge-mirror"])
        self.assertIn("warning: the podman VM's half exited 3, so the mirror it borrows is kept", err)

    def test_a_guest_keeps_the_mirror_it_clones_from(self):
        self.w.tart()
        self.w.guests = ["wk-g1"]
        self.mirror_kept(1, "error: the mirror and every snapshot: kept -- guest(s) g1 clone from it: 'wk rm' them first", ["WK_YES=1 gc --rows"])

    def test_a_stopped_vm_is_named_and_keeps_the_mirror(self):
        self.w.vm_state = "stopped"
        self.mirror_kept(1, "error: the mirror and every snapshot: not looked at -- the podman machine is stopped: 'wk start', then "
                            "'wk gc' again", [])
        self.assertEqual(rows(self.gc()[1]), [
            "4.0K the mirror and every snapshot not looked at -- the podman machine is stopped: 'wk start', then 'wk gc' again",
            "?? the container store in the podman VM not looked at -- the podman machine is stopped: 'wk start', then 'wk gc' again"])

    def test_tart_prunes_its_cache_to_the_declared_budget_or_the_default(self):
        for env, budget in (({"WK_TART_CACHE_GB": "7"}, "7"), ({}, "20")):
            with self.subTest(budget=budget):
                w = self.make_world(mac=True)
                w.env.update(env)
                tart = w.tart()
                w.mkdirs("/tart/cache")
                rc, err = self.gc(w=w)
                self.assertEqual((rc, rows(err), w.taken()), (0, ["4.0K tart's pulled-image cache, down to %s GB goes" % budget],
                                                              ["%s prune --space-budget %s" % (w.rel(tart), budget)]))

    def test_a_failing_tart_is_reported_and_the_rest_still_goes(self):
        tart = self.w.tart()
        self.w.answer([tart, "prune"], rc=1)
        self.w.mkdirs("/tart/cache")
        KINDS["seed"][0](self.w)
        rc, err = self.gc()
        self.assertEqual((rc, self.w.taken(), self.w.exists(self.w.p(SEED))),
                         (1, ["/store/" + SEED, self.w.rel(tart) + " prune --space-budget 20"], False), err)
        self.assertIn("warning: could not reclaim tart's pulled-image cache, down to 20 GB", err)

    def test_without_tart_there_is_nothing_to_trim(self):
        self.w.mkdirs("/tart/cache")
        rc, err = self.gc()
        self.assertEqual((rc, rows(err), self.w.taken()), (0, [], []))

    def test_the_golden_base_is_named_never_taken(self):
        self.w.tart()
        self.w.guests = ["wk-base"]
        rc, err = self.gc("--purge-builds", "--purge-rubble", "--purge-images")
        self.assertEqual((rc, rows(err), self.w.taken()), (0, ["4.0K golden base 'wk-base' kept -- 'wk sysimage build macos-guest-base "
                                                               "--rm' takes it"], []))

    def test_on_the_bench_volume_an_unfinished_stage_goes_and_a_finished_one_but_the_newest_is_named(self):
        self.w.files["/etc/wk-image"] = "id=mac\n"
        self.w.made(*("/vol/staged/2026010%d" % n for n in (1, 2, 3)))
        for n in (1, 2):
            self.w.files["/vol/staged/2026010%d/stage.json" % n] = "{}"
        rc, err = self.gc()
        self.assertEqual((rc, rows(err), self.w.taken()), (0, [
            "4.0K finished stage 20260101 kept -- 'wk gc --purge-builds' takes it", "4.0K stage 20260103, never finished goes"],
            ["/vol/staged/20260103"]))
        rc, err = self.gc("--purge-builds")
        self.assertEqual((rc, self.w.taken(), "/vol/staged/20260102" in self.w.dirs), (0, ["/vol/staged/20260101"], True))


class TestNotReachableFromTheCli(unittest.TestCase):
    """`wk doctor`'s disk section renders these rows with its own word for what a plain run takes; it reads this host
    through a Doctor and a Gc it builds itself (lib/wk/disk.py machine_report), so no world reaches it."""

    def test_wk_doctor_renders_the_rows_with_what_takes_each(self):
        from wk import rubble   # noqa: E402 -- tests.support put lib on the path
        lines = [rubble.line(rubble.row(k, "x", None, flag, lambda: None), (), "a plain 'wk gc' takes it")
                 for k, flag in (("half-made", "--purge-rubble"), ("seed", ""))]
        self.assertEqual([" ".join(l.split()) for l in lines], ["?? x kept -- 'wk gc --purge-rubble' takes it", "?? x a plain 'wk gc' takes it"])


if __name__ == "__main__":
    unittest.main()
