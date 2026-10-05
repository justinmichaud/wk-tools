"""`wk bench seed` (lib/wk/bench/seed.py): a plan's payload fetched once per
upstream commit, pinned without its .git, against a fake machine and a fake clock."""
import contextlib
import io
import json
import os
import sys
import types
from unittest import mock

from tests.fakes import FakeRegistry
from tests.killpoints import converges
from tests.support import REPO, WkTest
from tests.test_bench_report import in_process

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.bench import cli, seed  # noqa: E402
from wk.clock import FakeClock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.store import Store  # noqa: E402

SHA = "0123456789abcdef0123456789abcdef01234567"
SEEDS = "/store/cache/bench"
PLAN = json.dumps({"git_repository": {"url": "https://github.com/WebKit/JetStream.git", "branch": "JetStream3.0"}})
DEST = "%s/jetstream3-%s" % (SEEDS, SHA[:12])


def fake(clone_ok=True):
    m = Fake("here")
    m.answer(["git", "ls-remote"], out="%s\trefs/heads/JetStream3.0\n" % SHA)
    m.answer(["git", "-C"], rc=0)

    def clone(argv, f):
        if not clone_ok:
            return Result(128, "", "fatal: could not read from remote")
        repo = argv[-1]
        f._set_file(repo + "/index.html", "<html>")
        f._set_file(repo + "/MotionMark/index.html", "<html>")
        f._set_file(repo + "/.git/HEAD", "ref")
        return Result(0)

    def mv(argv, f):
        src, dst = argv[1], argv[2]
        for p in [p for p in f.files if p.startswith(src + "/")]:
            f._set_file(dst + p[len(src):], f.files.pop(p))
        for d in [d for d in f.dirs if d == src or d.startswith(src + "/")]:
            f.dirs.discard(d)
            f.dirs.add(dst + d[len(src):])
        return Result(0)

    m.react(["git", "clone"], clone)
    m.react(["mv"], mv)
    return m


def seeder(m, clock=None, mirror=None):
    return seed.Seeder(m, Lock(Store({"WK_LOCK_DIR": "/locks"}), m, clock or FakeClock()), SEEDS, mirror=mirror)


def quiet():
    """Swallows stderr for a block, and hands it back."""
    return contextlib.redirect_stderr(io.StringIO())


def lock_path():
    return Store({"WK_LOCK_DIR": "/locks"}).lock_path("bench-seed-jetstream3-%s" % SHA[:12])


def ran(m, word):
    return [e for e in m.effects if e[0] == "run" and word in e[1]]


class TestAPayloadIsSeededOnce(WkTest):

    def test_the_first_seed_clones_and_pins_it_without_its_history(self):
        m = fake()
        with quiet():
            self.assertEqual(seeder(m).seed("jetstream3", PLAN), DEST)
        self.assertEqual(len(ran(m, "clone")), 1)
        self.assertIn(DEST + "/index.html", m.files)
        self.assertFalse(any(p.startswith(DEST + "/.git") for p in m.files), "a pinned payload carries no .git")
        self.assertIn("sha=%s\n" % SHA, m.files[DEST + "/.wk-seeded/origin"])
        self.assertFalse(any("/.tmp-" in p for p in m.files), "the clone's scratch directory is gone")

    def test_a_second_seed_reads_the_pinned_one(self):
        m = fake()
        with quiet():
            seeder(m).seed("jetstream3", PLAN)
            self.assertEqual(seeder(m).seed("jetstream3", PLAN), DEST)
        self.assertEqual(len(ran(m, "clone")), 1)

    def test_a_seed_running_beside_another_waits_for_it_and_fetches_nothing(self):
        m = fake()
        lock = lock_path()
        m.dirs.add("/locks")
        m.files[lock] = "pid=4242 tok=x at=now cmd=wk"
        m.pids.add(4242)

        class OtherFinishes(FakeClock):
            def sleep(self, seconds):
                super().sleep(seconds)
                m.dirs.add(DEST + "/.wk-seeded")
                m.files.pop(lock, None)

        with quiet():
            self.assertEqual(seeder(m, OtherFinishes()).seed("jetstream3", PLAN), DEST)
        self.assertEqual(ran(m, "clone"), [], "the payload the other seed fetched is the one used")

    def test_the_fetch_holds_the_payloads_lock_and_lets_it_go(self):
        m = fake()
        with quiet():
            seeder(m).seed("jetstream3", PLAN)
        order = [e for e in m.effects if e[0] == "symlink" or (e[0] == "run" and "clone" in e[1])]
        self.assertEqual(order[0], ("symlink", lock_path()))
        self.assertNotIn(lock_path(), m.files)

    def test_a_seed_killed_after_any_effect_and_rerun_converges(self):
        def world():
            return types.SimpleNamespace(fake=fake())

        def run_once(w):
            with quiet():
                seeder(w.fake).seed("jetstream3", PLAN)

        def payload(w):
            return ({p: t for p, t in w.fake.files.items() if p.startswith(SEEDS + "/")},
                    sorted(d for d in w.fake.dirs if d.startswith(SEEDS + "/")))
        converges(self, world, run_once, payload)

    def test_a_dry_seed_prints_the_fetch_and_makes_nothing(self):
        m = fake()
        before = (dict(m.files), set(m.dirs))
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), quiet() as err:
            seeder(m).seed("jetstream3", PLAN)
        self.assertIn("would run: git clone -q https://github.com/WebKit/JetStream.git", err.getvalue())
        self.assertIn("would run: mv ", err.getvalue())
        self.assertEqual(ran(m, "clone"), [])
        self.assertEqual(before, (dict(m.files), set(m.dirs) - {"/locks"}), "the lock's directory is no state")

    def test_a_github_tree_pins_the_subdirectory_it_names(self):
        m = fake()
        plan = json.dumps({"github_source": "https://github.com/webkit/MotionMark/tree/be2a5fea89b6ef411b053ebeb95a6302b3dc0ecb/MotionMark"})
        with quiet():
            dest = seeder(m).seed("motionmark1.3.1", plan)
        self.assertEqual(dest, "%s/motionmark1.3.1-%s" % (SEEDS, SHA[:12]))
        self.assertIn(dest + "/index.html", m.files)
        self.assertIn("subdir=MotionMark\n", m.files[dest + "/.wk-seeded/origin"])

    def test_a_ref_the_remote_does_not_list_is_already_a_commit(self):
        m = fake()
        m.answer(["git", "ls-remote"], rc=2)
        plan = json.dumps({"git_repository": {"url": "u", "branch": "fab09aef01c2a5560c22cdc1c1a2451c0d0f4cdc"}})
        with quiet():
            self.assertEqual(seeder(m).seed("octane", plan), "%s/octane-fab09aef01c2" % SEEDS)


class TestWhatCannotBeSeeded(WkTest):

    def test_a_plan_with_no_fetchable_source_warns_and_answers_nothing(self):
        for plan in (json.dumps({"remote_archive": "https://x/y.zip"}), "not json",
                     json.dumps({"github_source": "https://example.com/elsewhere"})):
            with self.subTest(plan=plan):
                m = fake()
                cp = in_process(seeder(m).seed, "octane", plan)
                self.assertIn("cannot pre-seed octane", cp.stderr)
                self.assertEqual(ran(m, "clone"), [])

    def test_a_local_copy_is_not_seeded(self):
        m = fake()
        self.assertEqual(seeder(m).seed("p", json.dumps({"local_copy": "/somewhere"})), "")
        self.assertEqual(ran(m, "ls-remote"), [])

    def test_a_clone_that_fails_leaves_nothing_and_says_so(self):
        m = fake(clone_ok=False)
        with quiet() as said:
            self.assertEqual(seeder(m).seed("jetstream3", PLAN), "")
        self.assertIn("could not clone", said.getvalue())
        self.assertFalse(any(p.startswith(SEEDS + "/") for p in m.files))


MIRROR = "/store/git/WebKit.git"
SUNSPIDER = json.dumps({"github_source": "https://github.com/WebKit/WebKit/tree/%s/PerformanceTests/SunSpider" % SHA})


def mirror_fake(has_sha=True):
    m = fake()
    m.answer(["git", "--git-dir=" + MIRROR, "rev-parse"], rc=0 if has_sha else 128, out=SHA + "\n" if has_sha else "")

    def archive(argv, f):
        repo = argv[-1].rsplit(" -C ", 1)[1].strip("'")
        f._set_file(repo + "/PerformanceTests/SunSpider/sunspider.html", "<html>")
        return Result(0)
    m.react(["bash", "-c"], archive)
    return m


class TestWebKitsOwnPayloadComesFromTheMirror(WkTest):

    def test_it_is_archived_out_of_the_mirror_and_nothing_is_cloned(self):
        m = mirror_fake()
        with quiet():
            dest = seeder(m, mirror=MIRROR).seed("sunspider", SUNSPIDER)
        self.assertEqual(dest, "%s/sunspider-%s" % (SEEDS, SHA[:12]))
        self.assertIn(dest + "/sunspider.html", m.files)
        self.assertEqual([], ran(m, "clone") + ran(m, "ls-remote"))
        self.assertIn("git --git-dir=%s archive %s PerformanceTests/SunSpider" % (MIRROR, SHA), ran(m, "bash")[0][1][-1])
        self.assertIn("url=%s\n" % MIRROR, m.files[dest + "/.wk-seeded/origin"])

    def test_a_commit_the_mirror_lacks_is_refused_naming_the_mirror_refresh(self):
        m = mirror_fake(has_sha=False)
        with quiet() as said, self.assertRaises(Refused):
            seeder(m, mirror=MIRROR).seed("sunspider", SUNSPIDER)
        self.assertIn("wk sync --mirror", said.getvalue())
        self.assertEqual([], ran(m, "clone"))
        self.assertFalse(any(p.startswith(SEEDS + "/") for p in m.files))

    def test_another_repository_is_still_cloned(self):
        m = mirror_fake()
        with quiet():
            seeder(m, mirror=MIRROR).seed("jetstream3", PLAN)
        self.assertEqual(1, len(ran(m, "clone")))


class TestThePlanIsRead(WkTest):

    def test_a_plan_naming_another_is_followed(self):
        files = {"webkitpy/benchmark_runner/data/plans/speedometer3.plan": "speedometer3.1.plan\n",
                 "webkitpy/benchmark_runner/data/plans/speedometer3.1.plan": PLAN}
        self.assertEqual(seed.plan_json(files.get, "speedometer3"), PLAN)

    def test_a_missing_plan_is_refused_by_name(self):
        with self.assertRaises(Refused):
            with quiet() as said:
                seed.plan_json(lambda p: None, "nosuch")
        self.assertIn("no such plan: nosuch", said.getvalue())

    def test_a_plan_that_never_resolves_is_refused(self):
        with self.assertRaises(Refused):
            with quiet():
                seed.plan_json(lambda p: "loop.plan", "loop")


class TestTheVerb(WkTest):

    class Driver:
        def __init__(self):
            self.read = []

        def src(self, ws):
            return "/src/WebKit"

        def exec(self, ws, argv, tty=False, timeout=None):
            self.read.append((ws, tuple(argv)))
            return Result(0, PLAN.replace("\n", "\r\n"))

    @staticmethod
    def registry(driver, machine, store):
        return FakeRegistry({"WK_STORE": store, "WK_LOCK_DIR": "/locks"}, machine, lambda n, e: driver,
                            ws_place=lambda ws: "container")

    def test_the_verb_reads_the_checkout_and_prints_the_payload(self):
        m, driver = fake(), self.Driver()
        reg = self.registry(driver, m, "/store")
        cp = in_process(cli.Bench(REPO, reg, FakeClock()).seed, "w", "jetstream3", True)
        self.assertEqual(cp.stdout.strip(), "/store/cache/bench/jetstream3-%s" % SHA[:12], cp.stderr)
        self.assertEqual(driver.read, [("w", ("cat", "/src/WebKit/Tools/Scripts/webkitpy/benchmark_runner/data/plans/jetstream3.plan"))])

    def test_it_needs_a_workspace_and_a_plan(self):
        b = cli.Bench(REPO, self.registry(self.Driver(), fake(), "/store"), FakeClock())
        with self.assertRaises(Refused):
            with quiet():
                b.seed("w", "", True)

