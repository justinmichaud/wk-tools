"""`wk bench ab` (lib/wk/bench/ab.py) against a Fake world: the refusals, which commits and images an A/B resolves,
the graph it declares -- what each step needs and holds, what runs at once -- and what it costs, a run killed after
any effect and run again, and the report's pairing of rounds (lib/wk/bench/record.py's `paired`).

The world is a Fake machine answering git as a mirror would, `wk sysimage holds` from what the steps built, and
every step's `wk` command by recording what it would leave; each board's rounds are a callable, since a board A/B is
tests/test_bench_board.py's. Rows landed here: `unit ab.plan_and_pairing`, `unit killpoints[bench ab]`,
`unit bench.report_and_cost` (the cost half).

Run: python3 tests/run.py -k test_ab_plan
"""
import concurrent.futures as futures
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from tests.killpoints import converges
from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act, pgo, record as progress, sched, targets  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.bench import ab, record  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402

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


class Reg(targets.Registry):
    def __init__(self, env, fake):
        super().__init__(REPO, env=env, machine=fake)

    def ws_target(self, ws):
        return "container"

    def known(self):
        return ["one", "two"]

    def in_workspace(self):
        return False


def opts(**kw):
    o = dict.fromkeys(("devices", "release", "builder", "bits", "base", "build_on", "rounds", "count", "timeout", "task",
                       "systems", "slot"))
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
            (self.tmp / "machines" / (name + ".conf")).write_text("KIND=board\nNODE_SSH=%s-rescue\n" % name)
        self.fake, self.clock = Fake("here"), FakeClock()
        self.reg = Reg(self.env, self.fake)
        self.mirror = self.reg.store.mirror()
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
            words = words[2:]
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

    def bench(self, ws, plan, o):
        self.fake.write("/measured/%s/%s" % (o["system"], plan), o.get("ab") or o.get("ab_systems"))
        self.benched.append((ws, plan, o))

    def ab(self, spec=HEAD, **kw):
        kw.setdefault("devices", ",".join(self.boards_))
        if not kw.get("systems"):
            kw.setdefault("release", "2.38")
        return ab.AB(REPO, self.reg, self.clock, spec, opts(**kw), boards=self.boards, bench=self.bench, pool=Inline)

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


class TestTheRefusals(ABTest):
    """A malformed request is refused before the mirror, a board or GitHub is asked anything."""

    def test_devices_are_required(self):
        self.assertIn("--devices", self.refused(self.world(), devices=""))

    def test_bits_are_32_or_64(self):
        self.assertIn("--bits", self.refused(self.world(), bits="16"))

    def test_rounds_count_and_timeout_are_numbers(self):
        for key in ("rounds", "count", "timeout"):
            with self.subTest(key=key):
                self.assertIn("--" + key, self.refused(self.world(), **{key: "many"}))

    def test_a_plan_is_a_name(self):
        self.assertIn("--plan", self.refused(self.world(), plans=["x;y"]))

    def test_an_unknown_task_is_refused(self):
        self.assertIn("nosuch", self.refused(self.world(), task="nosuch"))

    def test_detach_and_dry_run_exclude_each_other(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.assertIn("--detach", self.refused(self.world(), detach=True))

    def test_a_branch_or_a_commit_needs_the_release_named(self):
        """Only a pull request has a base branch to read the image off."""
        for spec in (HEAD, "alice:feature-x"):
            with self.subTest(spec=spec):
                self.assertIn("--release", self.refused(self.world(), spec, release=""))

    def test_no_mirror_is_refused_naming_wk_sync(self):
        w = self.world()
        w.fake.dirs.discard(w.mirror)
        self.assertIn("wk sync", self.refused(w))

    def test_a_device_that_is_no_board_is_refused(self):
        self.assertIn("nosuch", self.refused(self.world(), devices="nosuch"))

    def test_systems_build_nothing_so_the_build_options_are_refused(self):
        for key in ab.BUILD_ONLY:
            with self.subTest(key=key):
                self.assertIn("--" + key.replace("_", "-"), self.refused(self.world(), "", systems="x,y", **{key: "1"}))

    def test_a_system_id_names_one_board(self):
        w = self.world(boards={"rpi3": R38, "rpi4": R38})
        self.assertIn("--devices", self.refused(w, "", systems="x,y"))

    def test_a_change_holds_no_one_slot(self):
        self.assertIn("--slot", self.refused(self.world(), slot="a"))


class TestTheCommits(ABTest):
    def test_the_base_is_the_merge_base_with_the_images_branch_for_a_commit(self):
        a, _ = self.graph(self.world())
        self.assertEqual((a.head, a.base, a.branch), (HEAD, BASE, "wpe/wpe-2.38"))

    def test_a_pull_requests_base_comes_off_its_own_base_branch(self):
        """The release says which image to measure on; the PR's base branch says where the change begins."""
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
        """WebKit and WPEWebKit are different projects, and a name in both says nothing about which one is meant."""
        self.assertIn("more than one", self.refused(self.world(repos=("WebKit", "WPEWebKit")), "alice:wpe-2.38"))


class TestTheImages(ABTest):
    def test_a_board_with_two_builders_at_one_release_asks_for_the_builder(self):
        """rpi5 at 2.52 has a buildroot image and a yocto one of one width, where naming the width narrows nothing."""
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
    """`unit ab.plan_and_pairing`, the plan half: what each step needs and holds, and so what runs at once."""

    lane = "buildroot-" + R38

    def test_the_steps_declare_what_they_need_and_what_they_hold(self):
        _, s = self.graph(self.world())
        image = "image:%s@tolken" % self.lane
        self.assertEqual(s[image].holds, ("machine:tolken",))
        self.assertEqual(s["slot:%s:base" % self.lane].needs, (image,))
        self.assertEqual(s["deploy:rpi3:base"].holds, ("device:rpi3",))
        self.assertEqual(s["bench:rpi3:speedometer3"].needs, ("deploy:rpi3:base", "deploy:rpi3:pr"))
        self.assertEqual(s["report"].needs, ("bench:rpi3:speedometer3",))

    def test_a_build_is_serialised_by_the_machine_it_runs_on(self):
        _, s = self.graph(self.world())
        for sid, step in s.items():
            if sid.split(":")[0] in BUILDS:
                self.assertTrue(step.holds and step.holds[0].startswith("machine:"), sid)

    def test_the_second_arm_builds_while_the_first_is_on_the_board(self):
        _, s = self.graph(self.world())
        waves = [{x.id for x in wave} for wave in sched.waves(list(s.values()))]
        self.assertTrue([w for w in waves if {"deploy:rpi3:base", "slot:%s:pr" % self.lane} <= w], waves)

    def test_a_buildroot_lane_has_no_toolchain_step(self):
        _, s = self.graph(self.world())
        self.assertFalse([k for k in s if k.startswith("toolchain:")])

    def test_a_yocto_lane_builds_its_cross_toolchain_first(self):
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64")
        lane = "yocto-webkit-2.52-yocto-rpi5-64@tolken"
        self.assertEqual(s["toolchain:" + lane].needs, ("image:" + lane,))
        self.assertIn("--stage toolchain", s["toolchain:" + lane].command)
        for arm in ("base", "pr"):
            self.assertEqual(s["instr:yocto-webkit-2.52-yocto-rpi5-64:" + arm].needs, ("toolchain:" + lane,))

    def test_a_profile_guided_arm_is_the_cycles_phases_not_one_build(self):
        """So one arm's collection on the board and the other arm's instrumented build run at once."""
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64")
        lane = "yocto-webkit-2.52-yocto-rpi5-64"
        self.assertEqual(s["instr:%s:base" % lane].holds, ("machine:tolken",))
        self.assertEqual(s["collect:rpi5:base:speedometer3"].holds, ("device:rpi5",))
        self.assertIn("--collect", s["collect:rpi5:base:jetstream3"].command)
        self.assertEqual(s["mix:%s:base" % lane].needs, tuple("collect:rpi5:base:" + p for p in pgo.BENCHMARKS))
        waves = [{x.id for x in wave} for wave in sched.waves(list(s.values()))]
        self.assertTrue([w for w in waves if {"deploy:rpi5:base-instr", "instr:%s:pr" % lane} <= w], waves)

    def test_two_boards_on_one_machine_build_in_turn(self):
        w = self.world(boards={"rpi4": "webkit-2.52-yocto-rpi4-64", "rpi5": "webkit-2.52-yocto-rpi5-64"})
        _, s = self.graph(w, release="2.52", builder="yocto", devices="rpi4-64,rpi5-64")
        for wave in sched.waves(list(s.values())):
            self.assertLessEqual(len([x for x in wave if x.id.split(":")[0] in BUILDS]), 1, wave)

    def test_build_on_puts_each_arm_on_its_own_machine(self):
        """Two machines are what makes the two arms build at once; each arm's deploy is sent to the machine holding its lane."""
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64", build_on="one,two")
        lane = "yocto-webkit-2.52-yocto-rpi5-64"
        self.assertEqual((s["instr:%s:base" % lane].holds, s["instr:%s:pr" % lane].holds), (("machine:one",), ("machine:two",)))
        first = {x.id for x in sched.waves(list(s.values()))[0]}
        self.assertEqual(first, {"image:%s@one" % lane, "image:%s@two" % lane})
        self.assertTrue(s["deploy:rpi5:pr"].command.startswith("WK_TARGET=two "))

    def test_one_machine_named_for_both_arms_builds_them_in_turn(self):
        _, s = self.graph(self.world(boards=Y52), release="2.52", builder="yocto", devices="rpi5-64", build_on="one")
        for wave in sched.waves(list(s.values())):
            self.assertLessEqual(len([x for x in wave if x.id.split(":")[0] in BUILDS]), 1, wave)

    def test_build_on_names_a_known_machine(self):
        self.assertIn("nosuch", self.refused(self.world(), build_on="nosuch"))

    def test_two_slots_and_two_systems_are_one_board_ab_each(self):
        """A change's arms are two slots on each board's image; --systems names two images holding one slot, and
        nothing is built -- both are one board A/B per board and plan, told apart only by its arms."""
        w = self.world()
        _, s = self.graph(w)
        _, t = self.graph(w, "", systems="sys-a,sys-b", slot="s")
        self.assertEqual(sorted(t), ["bench:rpi3:speedometer3", "report"])
        self.assertEqual([steps["bench:rpi3:speedometer3"].run() for steps in (s, t)], [0, 0])
        (_, _, slots), (_, _, systems) = w.benched
        self.assertEqual((slots["ab"], slots["rounds"]), ("base,pr", "5"))
        self.assertEqual((systems["ab_systems"], systems["slot"]), ("sys-a,sys-b", "s"))

    def test_a_dry_run_creates_no_task_and_runs_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        w = self.world()
        rc, err = self.quiet(w.ab().go)
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.isdir(w.reg.store.bench_dir()))
        self.assertEqual((w.state(), w.benched), ([], []))

    def test_a_dry_run_resolves_the_arms_commits_without_fetching(self):
        """dispatch.dry_run_is_the_recorder: resolving a branch, a pull request or a sha to a commit is a
        read of the mirror the plan is built from, never a fetch into it."""
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
        (t,) = progress.Records(w.reg.store.record_dir(), env=w.env, machine=w.fake).list()
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
        doc = record.task_doc(os.path.join(w.reg.store.bench_dir(), name))
        self.assertEqual((doc["slots"], doc["rounds"], doc["subject"]["head"]), (["base", "pr"], 5, HEAD))

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
        """`unit killpoints[bench ab]`: a re-run names the task the first one made, as the refusal says to."""
        base = os.path.join(self.tmp, "kill")

        def make():
            self.n += 1
            return World(os.path.join(base, str(self.n)))

        def once(w):
            tasks = record.tasks(w.reg.store.bench_dir())
            a = w.ab(task=tasks[0] if tasks else None)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(a.go(), 0, err.getvalue())

        converges(self, make, once, lambda w: w.state(), max_effects=80)


class TestTheCost(ABTest):
    """`unit bench.report_and_cost`, the cost half: a plan's cost is stated from measured leg times before it runs."""

    def leg(self, w, task, run, secs, count="", machine="rpi3", plan="speedometer3", ok=True):
        d = os.path.join(w.reg.store.bench_dir(), task, "runs", run)
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

    def test_a_leg_at_another_count_scales_and_one_at_the_default_stands_only_for_it(self):
        w = self.world()
        self.leg(w, "t1", "r", 100, count="2")
        self.leg(w, "t2", "r", 999)
        self.assertEqual(ab.leg_seconds(w.reg.store.bench_dir(), "rpi3", "speedometer3", "4"), [200.0])
        self.assertEqual(ab.leg_seconds(w.reg.store.bench_dir(), "rpi3", "speedometer3", ""), [999.0])

    def test_nothing_measured_is_an_unknown_cost(self):
        a, _ = self.graph(self.world())
        self.assertEqual(a.cost(), {("rpi3", "speedometer3"): (12, None, 0)})


class TestThePairing(unittest.TestCase):
    """`unit ab.plan_and_pairing`, the pairing half: only rounds both arms finished on one payload pin are compared."""

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


if __name__ == "__main__":
    unittest.main()
