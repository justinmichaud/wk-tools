"""`wk bench ab` (lib/wk/bench/ab.py) against a Fake world: refusals, commits, images, the graph, its cost, the arms' distance, the kill, and the pairing."""
import concurrent.futures as futures
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, images, pgo, record as progress, sched  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import ab, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result, Ssh  # noqa: E402

HEAD, BASE, TIP = "a" * 40, "b" * 40, "c" * 40
WK = str(REPO / "wk")
R38 = "wpewebkit-2.38-buildroot-rpi3-32"
Y52 = {"rpi5": "webkit-2.52-yocto-rpi5-64"}
BUILDS = ("image", "toolchain", "instr", "mix", "slot")


class Inline:
    """An executor that runs each step as it is submitted: one order of effects, so a kill point is one place."""

    def __init__(self, max_workers=None):
        pass

    def submit(self, fn, *args):
        f = futures.Future()
        try:
            f.set_result(fn(*args))
        except BaseException as e:   # noqa: B902 -- a Killed is what the kill-point test is after
            f.set_exception(e)
        return f

    def map(self, fn, items):
        return [fn(x) for x in items]

    def shutdown(self, wait=True):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Far(Fake):
    """This host, whose podman machine and build boxes run their shell on the scratch tree the store paths name; a
    peer's shell runs in `peer_env`, the environment its own wk has."""

    peer_env = None

    def run(self, argv, input=None, timeout=None, stream=False):
        if argv[:3] == ["podman", "machine", "ssh"] or argv[:1] == ["ssh"]:
            self.record_run(argv)
            remote = argv[-1] if argv[0] == "podman" else shlex.split(argv[-1])[-1]
            env = self.peer_env if argv[:1] == ["ssh"] and "peer1" in argv else None
            cp = subprocess.run(["sh", "-c", remote], input=input or "", capture_output=True, text=True, env=env)
            return Result(cp.returncode, cp.stdout, cp.stderr)
        return super().run(argv, input, timeout, stream)


class Reg(FakeRegistry):
    """The container place as a macOS host has it: its store in the podman machine, reached over `podman machine ssh`."""

    remote = {}

    def load(self, name):
        if name in self.remote:
            return types.SimpleNamespace(kind="remote", results=lambda ws: self.remote[name])
        t = super().load(name)
        if t.kind == "container":
            t.is_here = lambda: False
        return t

    def known(self):
        return ["one", "two", "peer1"]


def opts(**kw):
    o = dict.fromkeys(("devices", "release", "builder", "bits", "base", "build_on", "rounds", "count", "timeout", "task",
                       "systems", "slot", "workspace"))
    o.update(plans=[], detach=False)
    o.update(kw)
    return o


class World:
    """This host with a mirror holding HEAD, BASE and the branch tip TIP, the boards of `boards` booted into their images."""

    def __init__(self, tmp, boards=None, ahead=1, pr_base="", repos=("WebKit",)):
        self.tmp, self.boards_ = Path(tmp), dict(boards or {"rpi3": R38})
        self.env = {"WK_STORE": str(self.tmp / "store"), "XDG_STATE_HOME": str(self.tmp / "state"), "HOME": str(self.tmp),
                    "XDG_CONFIG_HOME": str(self.tmp / "config"), "WK_MACHINES_DIR": str(self.tmp / "machines")}
        (self.tmp / "machines").mkdir(parents=True, exist_ok=True)
        for name in self.boards_:
            (self.tmp / "machines" / (name + ".conf")).write_text("kind=board\nssh=%s-rescue\n" % name)
        self.fake, self.clock = Far("here"), FakeClock()
        self.reg = Reg(self.env, self.fake, ws_place=lambda ws: "container", in_workspace=lambda: False)
        self.mirror = self.reg.store.mirror_dir()
        self.fake.dirs.add(self.mirror)
        self.ahead, self.benched = ahead, []
        self.refs = {"refs/remotes/pr/origin/990": HEAD, "refs/remotes/pr/wpe/990": HEAD, "refs/remotes/pr/alice/WebKit/feature-x": HEAD, HEAD: HEAD, BASE: BASE}
        f, g = self.fake, ["git", "-C", self.mirror]
        f.answer(["hostname", "-s"], out="tolken\n")
        f.answer(g + ["cat-file", "-e"])
        f.answer(g + ["fetch", "--quiet"])
        f.answer(g + ["merge-base"], out=BASE + "\n")
        f.answer(g + ["log"], out="a subject\n")
        f.answer(g + ["rev-parse", "--short=12"], out=BASE[:12] + "\n")
        f.react(g + ["rev-parse", "--verify", "--quiet"], self.rev)
        f.react(g + ["rev-list", "--count"], lambda argv, fk: Result(0, "%d\n" % (self.ahead if argv[-1] == BASE + ".." + HEAD else 0)))
        f.react(["git", "ls-remote"], lambda argv, fk: Result(0, (HEAD + "\trefs/heads/x\n") if any(argv[2].endswith("/" + r) for r in repos)
                                                              and argv[-1].endswith(("feature-x", "wpe-2.38")) else ""))
        f.answer(["gh", "pr", "view"], out=pr_base + "\n", rc=0 if pr_base else 1)
        f.react(["sh", "-c", sched.LOGGED], self.logged)
        f.react([WK, "sysimage", "holds"], self.holds)
        for name in images.names(self.env):
            if images.image_ws(name, self.env):
                os.makedirs(os.path.join(self.reg.store.records_dir(), "ws", images.image_ws(name, self.env)))

    def peer(self):
        """peer1, a workstation with its own wk: what it holds is in its own store, which only its wk knows."""
        store = self.tmp / "peer1"
        (self.tmp / "machines" / "peer1.conf").write_text("kind=peer\ndriver=remote\nhost=peer1\npeer=1\ntools=%s\n" % REPO)
        (store / "machines").mkdir(parents=True)
        self.fake.peer_env = {"PATH": os.environ["PATH"], "HOME": str(store), "SHELL": "/bin/sh", "WK_PLACE": "local",
                              "WK_LOCAL_STORE": str(store), "XDG_STATE_HOME": str(store / "state"),
                              "XDG_CONFIG_HOME": str(store / "config"), "WK_MACHINES_DIR": str(store / "machines")}
        return store / "ws" / ("buildroot-" + R38) / "bench"

    def home(self):
        """Where the A/B keeps its task: the first device's image workspace."""
        return os.path.join(self.reg.store.records_dir(), "ws", "buildroot-" + R38, "bench")

    def rev(self, argv, fk):
        ref = argv[-1][:-len("^{commit}")]
        sha = self.refs.get(ref) or (TIP if ref.startswith("refs/remotes/") and "/pr/" not in ref else "")
        return Result(0 if sha else 1, sha + "\n" if sha else "")

    @staticmethod
    def key(words):
        w = lambda flag: words[words.index(flag) + 1] if flag in words else ""   # noqa: E731
        if words[:2] == ["sysimage", "build"]:
            stage = w("--stage") or "image"
            return "%s/%s/%s" % (stage, words[2], w("--slot")) if stage == "pgo-mix" else "%s/%s" % (stage, words[2])
        if words[:2] == ["sysimage", "webkit"]:
            return "slot/%s/%s/%s" % (words[2], w("--slot"), w("--commit"))
        if words[:2] == ["bench", "deploy"]:
            return "deploy/%s/%s" % (words[3], w("--slot"))
        return "collect/%s/%s/%s" % (w("--system"), w("--slot"), words[3])

    def logged(self, argv, fk):
        words = argv[5:]
        if words[0] == "env":
            words = [w for w in words[1:] if "=" not in w]
        if words[1:3] == ["bench", "run"] and "--task" in words:
            flags = dict(zip(words[7::2], words[8::2]))
            o = {k[2:].replace("-", "_"): v for k, v in flags.items()}
            fk._set_file("/measured/%s/%s" % (words[6], words[4]), o.get("ab") or o.get("ab_systems"))
            self.benched.append((words[3], words[4], o))
        elif words[1:3] != ["bench", "report"]:
            if words[1:3] == ["sysimage", "build"]:   # the image step makes its workspace
                os.makedirs(os.path.join(self.reg.store.records_dir(), "ws", words[words.index("--workspace") + 1]), exist_ok=True)
            fk._set_file("/state/" + self.key(words[1:]), "1")
        return Result(0)

    def holds(self, argv, fk):
        words, w = argv[1:], lambda flag: argv[argv.index(flag) + 1]   # noqa: E731
        spec = words[2]
        key = ("toolchain/" + spec if "--toolchain" in words else "slot/%s/%s/%s" % (spec, w("--slot"), w("--commit"))
               if "--slot" in words else "image/" + spec)
        return Result(0, "yes\n" if "/state/" + key in fk.files else "no\n")

    def boards(self, name):
        return "bench %s-0123abcd" % self.boards_.get(name, "sysa"), ["base"]

    def ab(self, spec=HEAD, **kw):
        kw.setdefault("devices", ",".join(self.boards_))
        if not kw.get("systems"):
            kw.setdefault("release", "2.38")
        return ab.AB(REPO, self.reg, self.clock, spec, opts(**kw), boards=self.boards, pool=Inline)

    def state(self):
        return sorted(k for k in self.fake.files if k.startswith(("/state/", "/measured/")))


class ABTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-ab-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved = dict(os.environ)
        for v in ("WK_DRY_RUN", "WK_FORCE", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_DEVICE_HELD"):
            os.environ.pop(v, None)
        os.environ["WK_YES"] = "1"
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        self.addCleanup(act._forced.clear)
        self.n = 0

    def world(self, **kw):
        self.n += 1
        return World(os.path.join(self.tmp, "w%d" % self.n), **kw)

    def quiet(self, fn, *args):
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                return fn(*args), err.getvalue()
            except Refused as e:
                return e, err.getvalue()

    def graph(self, w, spec=HEAD, **kw):
        a = w.ab(spec, **kw)
        rc, err = self.quiet(a.resolve)
        self.assertIsNone(rc, err)
        return a, {s.id: s for s in a.steps()}

    def refused(self, w, spec=HEAD, **kw):
        rc, err = self.quiet(w.ab(spec, **kw).go)
        self.assertIsInstance(rc, Refused, err)
        return err


class TestBoardStateNamesAConfigProblem(ABTest):

    def test_a_board_machines_does_not_name_is_refused_not_called_unreachable(self):
        w = self.world()
        a = w.ab(devices="ghost")
        rc, err = self.quiet(a.board_state, "ghost")
        self.assertIsInstance(rc, Refused, err)
        self.assertIn("ghost", err)
        self.assertIn("names no board", err)


class TestMachineKindNamesAMalformedConf(ABTest):

    def test_a_malformed_conf_is_refused_not_treated_as_not_a_mac(self):
        w = self.world()
        (Path(w.env["WK_MACHINES_DIR"]) / "broken.conf").write_text("not a keyvalue line\n")
        rc, err = self.quiet(ab.machine_kind, REPO, w.env, "broken")
        self.assertIsInstance(rc, Refused, err)
        self.assertIn("not a KEY=value line", err)


class TestTheRefusals(ABTest):

    def test_a_bad_option_is_refused_by_name(self):
        for kw, named in (({"devices": ""}, "--devices"), ({"bits": "16"}, "--bits"), ({"rounds": "many"}, "--rounds"),
                          ({"count": "many"}, "--count"), ({"timeout": "many"}, "--timeout"), ({"plans": ["x;y"]}, "--plan"),
                          ({"task": "nosuch"}, "nosuch"), ({"devices": "nosuch"}, "nosuch"), ({"slot": "a"}, "--slot"),
                          ({"build_on": "nosuch"}, "nosuch")):
            with self.subTest(kw=kw):
                self.assertIn(named, self.refused(self.world(), **kw))

    def test_detach_and_dry_run_exclude_each_other(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.assertIn("--detach", self.refused(self.world(), detach=True))

    def test_a_branch_or_a_commit_needs_the_release_named(self):
        for spec in (HEAD, "alice:feature-x"):
            with self.subTest(spec=spec):
                self.assertIn("--release", self.refused(self.world(), spec, release=""))

    def test_no_mirror_is_refused_naming_wk_sync(self):
        w = self.world()
        w.fake.dirs.discard(w.mirror)
        self.assertIn("wk sync", self.refused(w))

    def test_systems_build_nothing_so_the_build_options_are_refused(self):
        for key in ab.BUILD_ONLY:
            with self.subTest(key=key):
                self.assertIn("--" + key.replace("_", "-"), self.refused(self.world(), "", systems="x,y", **{key: "1"}))

    def test_a_system_id_names_one_board(self):
        w = self.world(boards={"rpi3": R38, "rpi4": R38})
        self.assertIn("--devices", self.refused(w, "", systems="x,y"))

    def test_the_task_needs_a_workspace_to_live_in(self):
        w = self.world()
        w.fake._set_file("/state/image/" + R38, "1")   # held, so the step that would make the workspace is not run
        shutil.rmtree(os.path.dirname(w.home()))
        self.assertIn("no workspace 'buildroot-%s' is at" % R38, self.refused(w))
        self.assertIn("--workspace <ws>", self.refused(self.world(), "", systems="x,y"))
        self.assertIn("no workspace 'gone' is at", self.refused(self.world(), "", systems="x,y", workspace="gone"))
        self.assertIn("--workspace", self.refused(self.world(), workspace="buildroot-" + R38))


class TestTheCommits(ABTest):
    def test_the_base_is_the_merge_base_with_the_images_branch_for_a_commit(self):
        a, _ = self.graph(self.world())
        self.assertEqual((a.head, a.base, a.branch), (HEAD, BASE, "wpe/wpe-2.38"))

    def test_a_pull_requests_base_comes_off_its_own_base_branch(self):
        w = self.world(boards=Y52, pr_base="feature-x")
        a, _ = self.graph(w, "990", release="2.52", builder="yocto", devices="rpi5-64")
        self.assertEqual((a.head, a.branch), (HEAD, "origin/feature-x"))
        fetched = [e[1] for e in w.fake.effects if e[0] == "run" and "fetch" in e[1]]
        self.assertIn("+refs/heads/feature-x:refs/remotes/origin/feature-x", [argv[-1] for argv in fetched])

    def test_a_pull_request_reads_its_release_off_its_base_branch(self):
        a, _ = self.graph(self.world(pr_base="wpe-2.38"), "wpe:990", release="")
        self.assertEqual(a.devices[0].profile, R38)

    def test_a_pull_request_whose_base_branch_names_no_release_is_refused(self):
        self.assertIn("--release", self.refused(self.world(pr_base="main"), "990", release=""))

    def test_a_forks_branch_is_measured_at_its_head(self):
        a, steps = self.graph(self.world(), "alice:feature-x")
        self.assertEqual(a.head, HEAD)
        self.assertIn("--commit %s --slot pr" % HEAD, steps["slot:buildroot-%s:pr" % R38].command)

    def test_a_branch_no_repository_of_that_user_has_is_refused_by_name(self):
        err = self.refused(self.world(), "alice:no-such-branch")
        self.assertIn("https://github.com/alice/WebKit.git", err)

    def test_a_branch_two_of_the_users_repositories_carry_is_refused(self):
        self.assertIn("more than one", self.refused(self.world(repos=("WebKit", "WPEWebKit")), "alice:wpe-2.38"))


class TestTheImages(ABTest):
    def test_a_board_with_two_builders_at_one_release_asks_for_the_builder(self):
        err = self.refused(self.world(boards=Y52), release="2.52", devices="rpi5-64")
        self.assertIn("--builder", err)
        self.assertNotIn("rpi5-64 or rpi5-64", err)

    def test_a_board_with_two_widths_at_one_release_asks_for_the_width(self):
        err = self.refused(self.world(boards={"rpi4": "webkit-2.52-yocto-rpi4-64"}), release="2.52", builder="yocto", devices="rpi4")
        self.assertIn("rpi4-32 or rpi4-64", err)

    def test_a_device_carries_its_own_width(self):
        w = self.world(boards={"rpi3": "webkit-2.52-yocto-rpi3-32", "rpi4": "webkit-2.52-yocto-rpi4-64"})
        a, _ = self.graph(w, release="2.52", builder="yocto", devices="rpi3-32,rpi4-64")
        self.assertEqual([d.profile for d in a.devices], ["webkit-2.52-yocto-rpi3-32", "webkit-2.52-yocto-rpi4-64"])


class TestTheGraph(ABTest):

    ws = "buildroot-" + R38

    def test_the_steps_declare_what_they_need_and_what_they_hold(self):
        _, s = self.graph(self.world())
        image = "image:%s@tolken" % self.ws
        self.assertEqual(s[image].holds, ("machine:tolken",))
        self.assertEqual(s["slot:%s:base" % self.ws].needs, (image,))
        self.assertEqual(s["deploy:rpi3:base"].holds, ("device:rpi3",))
        self.assertEqual(s["bench:rpi3:speedometer3"].needs, ("task", "deploy:rpi3:base", "deploy:rpi3:pr"))
        self.assertEqual(s["report"].needs, ("bench:rpi3:speedometer3",))

    def test_a_build_is_serialised_by_the_machine_it_runs_on(self):
        _, s = self.graph(self.world())
        for sid, step in s.items():
            if sid.split(":")[0] in BUILDS:
                self.assertTrue(step.holds and step.holds[0].startswith("machine:"), sid)

    def test_the_second_arm_builds_while_the_first_is_on_the_board(self):
        _, s = self.graph(self.world())
        waves = [{x.id for x in wave} for wave in sched.waves(list(s.values()))]
        self.assertTrue([w for w in waves if {"deploy:rpi3:base", "slot:%s:pr" % self.ws} <= w], waves)

    def test_a_buildroot_image_has_no_toolchain_step(self):
        _, s = self.graph(self.world())
        self.assertFalse([k for k in s if k.startswith("toolchain:")])

    def test_a_yocto_image_builds_its_cross_toolchain_first(self):
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64")
        ws = "yocto-webkit-2.52-yocto-rpi5-64@tolken"
        self.assertEqual(s["toolchain:" + ws].needs, ("image:" + ws,))
        self.assertIn("--stage toolchain", s["toolchain:" + ws].command)
        for arm in ("base", "pr"):
            self.assertEqual(s["instr:yocto-webkit-2.52-yocto-rpi5-64:" + arm].needs, ("toolchain:" + ws,))

    def test_a_profile_guided_arm_is_the_cycles_phases_not_one_build(self):
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64")
        ws = "yocto-webkit-2.52-yocto-rpi5-64"
        self.assertEqual(s["instr:%s:base" % ws].holds, ("machine:tolken",))
        self.assertEqual(s["collect:rpi5:base:speedometer3"].holds, ("device:rpi5",))
        self.assertIn("--collect", s["collect:rpi5:base:jetstream3"].command)
        self.assertEqual(s["mix:%s:base" % ws].needs, tuple("collect:rpi5:base:" + p for p in pgo.BENCHMARKS))
        waves = [{x.id for x in wave} for wave in sched.waves(list(s.values()))]
        self.assertTrue([w for w in waves if {"deploy:rpi5:base-instr", "instr:%s:pr" % ws} <= w], waves)

    def test_builds_on_one_machine_run_in_turn(self):
        two = {"rpi4": "webkit-2.52-yocto-rpi4-64", "rpi5": "webkit-2.52-yocto-rpi5-64"}
        for boards, kw in ((two, {"devices": "rpi4-64,rpi5-64"}), (Y52, {"devices": "rpi5-64", "build_on": "one"})):
            with self.subTest(boards=sorted(boards)):
                _, s = self.graph(self.world(boards=boards), release="2.52", builder="yocto", **kw)
                for wave in sched.waves(list(s.values())):
                    self.assertLessEqual(len([x for x in wave if x.id.split(":")[0] in BUILDS]), 1, wave)

    def test_build_on_puts_each_arm_on_its_own_machine(self):
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64", build_on="one,two")
        ws = "yocto-webkit-2.52-yocto-rpi5-64"
        self.assertEqual((s["instr:%s:base" % ws].holds, s["instr:%s:pr" % ws].holds), (("machine:one",), ("machine:two",)))
        first = {x.id for x in sched.waves(list(s.values()))[0]}
        self.assertEqual(first, {"image:%s@one" % ws, "image:%s@two" % ws})
        self.assertTrue(s["deploy:rpi5:pr"].command.startswith("WK_PLACE=two "))

    def test_two_slots_and_two_systems_are_one_board_ab_each(self):
        w = self.world()
        _, s = self.graph(w)
        _, t = self.graph(w, "", systems="sys-a,sys-b", slot="s", workspace="buildroot-" + R38)
        self.assertEqual(sorted(t), ["bench:rpi3:speedometer3", "report", "task"])
        self.assertEqual([steps["bench:rpi3:speedometer3"].run() for steps in (s, t)], [0, 0])
        (_, _, slots), (_, _, systems) = w.benched
        self.assertEqual((slots["ab"], slots["rounds"]), ("base,pr", "5"))
        self.assertEqual((systems["ab_systems"], systems["slot"]), ("sys-a,sys-b", "s"))

    def test_a_dry_run_creates_no_task_and_runs_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        w = self.world()
        shutil.rmtree(os.path.dirname(w.home()))
        rc, err = self.quiet(w.ab().go)
        self.assertEqual(rc, 0, err)
        self.assertIn("in workspace buildroot-%s" % R38, err, "a dry run names the workspace the task would live in")
        self.assertFalse(os.path.isdir(os.path.dirname(w.home())))
        self.assertEqual((w.state(), w.benched), ([], []))

    def test_a_dry_run_resolves_the_arms_commits_without_fetching(self):
        os.environ["WK_DRY_RUN"] = "1"
        w = self.world(boards=Y52, pr_base="feature-x")
        a, _ = self.graph(w, "990", release="2.52", builder="yocto", devices="rpi5-64")
        self.assertEqual((a.head, a.branch), (HEAD, "origin/feature-x"))
        self.assertFalse([e for e in w.fake.effects if e[0] == "run" and "fetch" in e[1]])


class TestARun(ABTest):
    def run_ab(self, w, **kw):
        rc, err = self.quiet(w.ab(**kw).go)
        self.assertEqual(rc, 0, err)
        return err

    def task(self, w):
        (t,) = progress.Records(w.reg.store.records_dir(), env=w.env, machine=w.fake).list()
        return t

    def test_every_step_runs_and_the_record_names_the_kill_and_the_subject(self):
        w = self.world()
        self.run_ab(w)
        self.assertIn("/state/deploy/rpi3/pr", w.state())
        self.assertIn("/measured/rpi3/speedometer3", w.state())
        t = self.task(w)
        name = t.field("name")
        self.assertEqual(t.field("kill"), "wk bench ab %s --kill" % name)
        self.assertTrue(t.field("subject"))
        self.assertEqual({state for _, state in t.steps()}, {"done"})
        self.assertEqual(t.field("exit"), "0")
        doc = record.task_doc(os.path.join(w.home(), name))
        self.assertEqual((doc["slots"], doc["rounds"], doc["subject"]["head"]), (["base", "pr"], 5, HEAD))
        self.assertEqual(doc["restart"], doc["commands"][0] + " --task " + name, "a restart is the request itself, into this task")
        stepped = [e[1] for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c") and "bench" in e[1]]
        self.assertTrue(stepped and all("WK_TASK_HELD=" + name in argv for argv in stepped), "a step is told the A/B holds its task")

    def test_a_first_ab_records_its_task_once_the_image_step_has_made_the_workspace(self):
        w = self.world()
        shutil.rmtree(os.path.dirname(w.home()))
        self.run_ab(w)
        self.assertEqual(record.tasks(w.home()), [self.task(w).field("name")])

    def test_a_task_in_the_podman_machines_store_is_written_through_it(self):
        w = self.world()
        self.run_ab(w)
        wrote = [e[1] for e in w.fake.effects if e[0] == "run" and e[1][:3] == ("podman", "machine", "ssh") and "task.json" in e[1][-1]]
        self.assertTrue(wrote, "task.json is written through the podman machine")

    def test_a_task_on_a_build_box_is_written_there_and_its_run_sent_there(self):
        w = self.world()
        far = Path(w.tmp, "far", "ws", "buildroot-" + R38)
        far.mkdir(parents=True)
        w.reg.remote = {"one": (Ssh("one", via=w.fake), str(far / "bench"))}
        self.run_ab(w, build_on="one")
        name = self.task(w).field("name")
        self.assertEqual(record.tasks(str(far / "bench")), [name])
        self.assertEqual(record.tasks(w.home()), [], "nothing is written into the workspace of the same name here")
        bench = [e[1] for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c") and "run" in e[1] and "--ab" in e[1]]
        self.assertTrue(bench and all("WK_PLACE=one" in argv for argv in bench), bench)

    def test_a_task_in_a_peers_workspace_is_written_where_its_own_wk_holds_it(self):
        w = self.world()
        bench = w.peer()
        bench.parent.mkdir(parents=True)
        self.run_ab(w, build_on="peer1")
        self.assertEqual(record.tasks(str(bench)), [self.task(w).field("name")])

    def test_a_step_already_done_is_not_run_again(self):
        w = self.world()
        w.fake._set_file("/state/image/" + R38, "1")
        self.run_ab(w)
        built = [e[1] for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c") and "build" in e[1]]
        self.assertEqual(built, [])

    def test_each_board_ab_records_into_this_task(self):
        w = self.world()
        self.run_ab(w)
        ((ws, plan, o),) = w.benched
        self.assertEqual((ws, plan, o["task"]), ("buildroot-" + R38, "speedometer3", self.task(w).field("name")))

    def test_a_failed_step_stops_what_needed_it_and_the_run_is_refused(self):
        w = self.world()
        w.fake.react(["sh", "-c", sched.LOGGED], lambda argv, fk: Result(1) if "deploy" in argv else w.logged(argv, fk))
        rc, err = self.quiet(w.ab().go)
        self.assertIsInstance(rc, Refused, err)
        self.assertEqual(w.benched, [])
        self.assertEqual(self.task(w).field("exit"), "1")

    def test_slots_that_do_not_hold_the_commits_after_the_builds_are_refused(self):
        w = self.world()
        w.fake.react([WK, "sysimage", "holds"], lambda argv, fk: Result(0, "no\n"))
        rc, err = self.quiet(w.ab().go)
        self.assertIsInstance(rc, Refused, err)

    def test_a_detached_run_hands_this_ab_with_its_task_to_a_process_of_its_own(self):
        w = self.world()
        self.run_ab(w, detach=True, plans=["jetstream3"])
        ((_, argv, log),) = [e for e in w.fake.effects if e[0] == "spawn"]
        name = os.path.basename(os.path.dirname(log))
        self.assertEqual(list(argv[:4]), [WK, "bench", "ab", HEAD])
        self.assertEqual(list(argv[-3:]), ["--yes", "--task", name])
        self.assertIn("jetstream3", argv)
        self.assertEqual(w.state(), [])

    def test_a_run_killed_after_any_effect_and_run_again_converges(self):
        base = os.path.join(self.tmp, "kill")

        def make():
            self.n += 1
            return World(os.path.join(base, str(self.n)))

        def once(w):
            tasks = record.tasks(w.home())
            a = w.ab(task=tasks[0] if tasks else None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.go(), 0, err.getvalue())

        converges(self, make, once, lambda w: w.state(), max_effects=80)


class TestTheCost(ABTest):

    def leg(self, w, task, run, secs, count="", machine="rpi3", plan="speedometer3", ok=True, home=None):
        d = os.path.join(home or w.home(), task, "runs", run)
        os.makedirs(d)
        Path(d, "env.json").write_text(json.dumps({"machine": machine, "plan": plan, "count": count, "wall_time_s": secs}))
        if ok:
            Path(d, "result.json").write_text("{}")
        record.task_write(os.path.dirname(os.path.dirname(d)), ["task=" + task, "requested=x", "devices=rpi3=p", "plans=" + plan,
                                                                 "rounds=1", "slots=a"], [])

    def test_the_legs_are_a_warmup_per_arm_and_each_round_one_leg_per_arm(self):
        self.assertEqual((ab.legs_per_plan(5, False), ab.legs_per_plan(5, True)), (12, 22))

    def test_a_plans_cost_is_its_legs_times_the_median_measured_leg(self):
        w = self.world()
        for i, secs in enumerate((100, 300, 200)):
            self.leg(w, "t%d" % i, "r", secs)
        self.leg(w, "t9", "r", 5000, ok=False)
        self.leg(w, "t8", "r", 5000, machine="rpi4")
        a, _ = self.graph(w)
        self.assertEqual(a.cost(), {("rpi3", "speedometer3"): (12, 12 * 200.0, 3)})

    def test_legs_measured_in_a_workspace_on_a_build_box_count(self):
        w = self.world()
        far = Path(w.tmp, "far", "ws", "buildroot-" + R38, "bench")
        far.mkdir(parents=True)
        w.reg.remote = {"one": (Ssh("one", via=w.fake), str(far))}
        self.leg(w, "t1", "r", 100, home=str(far))
        a, _ = self.graph(w, build_on="one")
        self.assertEqual(a.cost(), {("rpi3", "speedometer3"): (12, 12 * 100.0, 1)})

    def test_legs_measured_in_a_peers_workspace_count_and_a_dry_run_states_them(self):
        w = self.world()
        far = w.peer()
        far.mkdir(parents=True)
        self.leg(w, "t1", "r", 100, home=str(far))
        os.environ["WK_DRY_RUN"] = "1"
        a, _ = self.graph(w, build_on="peer1")
        self.assertEqual(a.cost(), {("rpi3", "speedometer3"): (12, 12 * 100.0, 1)})

    def test_a_leg_at_another_count_scales_and_one_at_the_default_stands_only_for_it(self):
        w = self.world()
        self.leg(w, "t1", "r", 100, count="2")
        self.leg(w, "t2", "r", 999)
        self.assertEqual(ab.leg_seconds(w.reg, [("buildroot-" + R38, "")], "rpi3", "speedometer3", "4"), [200.0])
        self.assertEqual(ab.leg_seconds(w.reg, [("buildroot-" + R38, "")], "rpi3", "speedometer3", ""), [999.0])

    def test_a_settle_or_warmup_leg_is_no_measured_leg_and_stands_for_no_cost(self):
        w = self.world()
        self.leg(w, "t1", "r", 100)
        for kind in ("settle", "evidence"):
            self.leg(w, "t1", "w-" + kind, 900)
            env = Path(w.home(), "t1", "runs", "w-" + kind, "env.json")
            env.write_text(json.dumps(dict(json.loads(env.read_text()), warmup=True, warmup_kind=kind)))
        self.assertEqual(ab.leg_seconds(w.reg, [("buildroot-" + R38, "")], "rpi3", "speedometer3", ""), [100.0])

    def test_nothing_measured_is_an_unknown_cost(self):
        a, _ = self.graph(self.world())
        self.assertEqual(a.cost(), {("rpi3", "speedometer3"): (12, None, 0)})


class TestThePairing(unittest.TestCase):

    def run_(self, state="ok", runner="r1", copy="/p"):
        return {"state": state, "dir": "/runs/%s-%s" % (runner, state), "env": {"runner_sha": runner, "local_copy": copy}}

    def test_a_round_both_arms_finished_on_one_pin_is_compared(self):
        a, b, dropped = record.paired({1: {"a": self.run_(), "b": self.run_()}}, ["base", "pr"])
        self.assertEqual((len(a), len(b), dropped), (1, 1, []))

    def test_a_round_one_arm_did_not_finish_is_dropped(self):
        for arms in ({"a": self.run_()}, {"a": self.run_(), "b": self.run_("failed")}):
            with self.subTest(arms=sorted(arms)):
                a, b, dropped = record.paired({1: arms}, ["base", "pr"])
                self.assertEqual((a, b, len(dropped)), ([], [], 1))

    def test_arms_on_two_payload_pins_are_not_compared(self):
        for other in ({"runner": "r2"}, {"copy": "/q"}):
            with self.subTest(other=other):
                a, _, dropped = record.paired({1: {"a": self.run_(), "b": self.run_(**other)}, 2: {"a": self.run_(), "b": self.run_()}},
                                              ["base", "pr"])
                self.assertEqual((len(a), len(dropped)), (1, 1))


class TestTheArmsMayDifferByOneCommit(ABTest):

    def test_one_commit_apart_or_none_is_allowed(self):
        for ahead in (0, 1):
            with self.subTest(ahead=ahead):
                os.environ["WK_DRY_RUN"] = "1"
                rc, err = self.quiet(self.world(ahead=ahead).ab().go)
                self.assertEqual(rc, 0, err)

    def test_two_commits_apart_is_refused_given_or_guessed(self):
        for base in ("", BASE):
            with self.subTest(base=base):
                os.environ["WK_DRY_RUN"] = "1"
                w = self.world(ahead=2)
                err = self.refused(w, base=base)
                self.assertIn("--base", err)
                self.assertFalse([e for e in w.fake.effects if e[0] == "run" and e[1][:2] == ("sh", "-c")])

    def test_force_crosses_it(self):
        os.environ.update(WK_DRY_RUN="1", WK_FORCE="1")
        rc, err = self.quiet(self.world(ahead=2).ab().go)
        self.assertEqual(rc, 0, err)
        self.assertTrue(act._forced)


class TestTheKill(ABTest):

    def record(self, w, pid):
        recs = progress.Records(w.reg.store.records_dir(), clock=w.clock, env=w.env, machine=w.fake)
        return recs.begin("ab", "here", "t1", "wk bench ab t1 --kill", "/x/ab.log", ["wk sysimage build"], pid=pid)

    def kill(self, w, task="t1"):
        return self.quiet(ab.kill, w.reg, w.clock, task)

    def test_the_process_and_its_steps_go_and_the_record_says_cancelled(self):
        w = self.world()
        w.fake.pids |= {4000, 4001}
        w.fake.answer(["sh", "-c"], out="4001\n4000\n")
        t = self.record(w, 4000)
        rc, err = self.kill(w)
        self.assertEqual(rc, 0, err)
        self.assertEqual((w.fake.pids, t.field("exit")), ({os.getpid()}, "cancelled"))

    def test_a_process_that_ignores_term_is_killed_after_the_bound(self):
        w = self.world()
        w.fake.pids.add(4000)
        w.fake.answer(["sh", "-c"], out="4000\n")
        w.fake.kill = lambda pid, sig=15: (w.fake.pids.discard(pid) if sig == 9 else None) or True
        w.env["WK_KILL_WAIT"] = "3"
        t = self.record(w, 4000)
        rc, err = self.kill(w)
        self.assertEqual(rc, 0, err)
        self.assertEqual((t.field("exit"), w.clock.slept.count(1) >= 3), ("cancelled", True))

    def test_no_record_is_a_refusal_naming_where_the_tasks_are_listed(self):
        rc, err = self.kill(self.world(), "nosuch")
        self.assertIsInstance(rc, Refused)
        self.assertIn("wk bench ls", err)

    def test_a_finished_task_is_not_killed(self):
        w = self.world()
        w.fake.pids.add(4000)
        self.record(w, 4000).end(0)
        rc, err = self.kill(w)
        self.assertIsInstance(rc, Refused)
        self.assertEqual(w.fake.pids, {4000, os.getpid()})


if __name__ == "__main__":
    unittest.main()
