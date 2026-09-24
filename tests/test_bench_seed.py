"""`wk bench seed` and lib/bench.sh's seed_payload (lib/wk/bench/seed.py):
a plan's payload fetched once per upstream commit, pinned without its .git,
against a fake machine and a fake clock; one test drives the bash shim over
a local repository.

Run: python3 -m unittest tests.test_bench_seed -v
"""
import json
import os
import subprocess
import sys

from tests.killpoints import converges
from tests.support import REPO, WkTest, bash, scratch_dir, temp_store
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


def seeder(m, clock=None, env=None):
    store = Store(dict(env or {}, WK_LOCK_DIR="/locks"))
    return seed.Seeder(m, Lock(store, m, clock or FakeClock()), SEEDS)


def lock_path():
    return Store({"WK_LOCK_DIR": "/locks"}).lock_path("bench-seed-jetstream3-%s" % SHA[:12])


def ran(m, word):
    return [e for e in m.effects if e[0] == "run" and word in e[1]]


class TestAPayloadIsSeededOnce(WkTest):
    """`unit bench.seed_from_mirror`: one fetch per upstream commit, whatever
    runs at once -- a second seed of the payload waits on the first one's lock
    and finds it pinned."""

    def test_the_first_seed_clones_and_pins_it_without_its_history(self):
        m = fake()
        with in_process_ok():
            self.assertEqual(seeder(m).seed("jetstream3", PLAN), DEST)
        self.assertEqual(len(ran(m, "clone")), 1)
        self.assertIn(DEST + "/index.html", m.files)
        self.assertFalse(any(p.startswith(DEST + "/.git") for p in m.files), "a pinned payload carries no .git")
        self.assertIn("sha=%s\n" % SHA, m.files[DEST + "/.wk-seeded/origin"])
        self.assertFalse(any("/.tmp-" in p for p in m.files), "the clone's scratch directory is gone")

    def test_a_second_seed_reads_the_pinned_one(self):
        m = fake()
        with in_process_ok():
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

        with in_process_ok():
            self.assertEqual(seeder(m, OtherFinishes()).seed("jetstream3", PLAN), DEST)
        self.assertEqual(ran(m, "clone"), [], "the payload the other seed fetched is the one used")

    def test_the_fetch_holds_the_payloads_lock_and_lets_it_go(self):
        m = fake()
        with in_process_ok():
            seeder(m).seed("jetstream3", PLAN)
        order = [e for e in m.effects if e[0] == "symlink" or (e[0] == "run" and "clone" in e[1])]
        self.assertEqual(order[0], ("symlink", lock_path()))
        self.assertNotIn(lock_path(), m.files)

    def test_a_seed_killed_after_any_effect_and_rerun_converges(self):
        """`killpoints[bench seed]`: nothing a killed seed leaves is trusted, and none of it outlives the re-run."""
        import types

        def world():
            return types.SimpleNamespace(fake=fake())

        def run_once(w):
            with in_process_ok():
                seeder(w.fake).seed("jetstream3", PLAN)

        def payload(w):
            return ({p: t for p, t in w.fake.files.items() if p.startswith(SEEDS + "/")},
                    sorted(d for d in w.fake.dirs if d.startswith(SEEDS + "/")))
        converges(self, world, run_once, payload)

    def test_a_github_tree_pins_the_subdirectory_it_names(self):
        m = fake()
        plan = json.dumps({"github_source": "https://github.com/webkit/MotionMark/tree/be2a5fea89b6ef411b053ebeb95a6302b3dc0ecb/MotionMark"})
        with in_process_ok():
            dest = seeder(m).seed("motionmark1.3.1", plan)
        self.assertEqual(dest, "%s/motionmark1.3.1-%s" % (SEEDS, SHA[:12]))
        self.assertIn(dest + "/index.html", m.files)
        self.assertIn("subdir=MotionMark\n", m.files[dest + "/.wk-seeded/origin"])

    def test_a_ref_the_remote_does_not_list_is_already_a_commit(self):
        m = fake()
        m.answer(["git", "ls-remote"], rc=2)
        plan = json.dumps({"git_repository": {"url": "u", "branch": "fab09aef01c2a5560c22cdc1c1a2451c0d0f4cdc"}})
        with in_process_ok():
            self.assertEqual(seeder(m).seed("octane", plan), "%s/octane-fab09aef01c2" % SEEDS)


class TestWhatCannotBeSeeded(WkTest):
    """Nothing to pin is an empty answer, and run-benchmark fetches it itself."""

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
        with in_process_ok() as said:
            self.assertEqual(seeder(m).seed("jetstream3", PLAN), "")
        self.assertIn("could not clone", said.getvalue())
        self.assertFalse(any(p.startswith(SEEDS + "/") for p in m.files))


class TestThePlanIsRead(WkTest):
    """A plan file may hold only another plan's name; the chain is followed, and bounded."""

    def test_a_plan_naming_another_is_followed(self):
        files = {"webkitpy/benchmark_runner/data/plans/speedometer3.plan": "speedometer3.1.plan\n",
                 "webkitpy/benchmark_runner/data/plans/speedometer3.1.plan": PLAN}
        self.assertEqual(seed.plan_json(files.get, "speedometer3"), PLAN)

    def test_a_missing_plan_is_refused_by_name(self):
        with self.assertRaises(Refused):
            with in_process_ok() as said:
                seed.plan_json(lambda p: None, "nosuch")
        self.assertIn("no such plan: nosuch", said.getvalue())

    def test_a_plan_that_never_resolves_is_refused(self):
        with self.assertRaises(Refused):
            with in_process_ok():
                seed.plan_json(lambda p: "loop.plan", "loop")


class TestTheVerb(WkTest):
    """`wk bench seed <ws> <plan>` reads the plan out of the workspace's checkout and prints the pinned directory."""

    class Target:
        def __init__(self):
            self.read = []

        def src(self, ws):
            return "/src/WebKit"

        def exec(self, ws, argv, tty=False, timeout=None):
            self.read.append((ws, tuple(argv)))
            return Result(0, PLAN.replace("\n", "\r\n"))

    class Registry:
        def __init__(self, target, machine, store):
            self.env, self.machine, self.target = {"WK_STORE": store, "WK_LOCK_DIR": "/locks"}, machine, target
            self.store = Store(self.env)

        def ws_target(self, ws):
            return "container"

        def load(self, name):
            return self.target

    def test_the_verb_reads_the_checkout_and_prints_the_payload(self):
        m, target = fake(), self.Target()
        reg = self.Registry(target, m, "/store")
        cp = in_process(cli.Bench(REPO, reg, FakeClock()).seed, "w", "jetstream3", True)
        self.assertEqual(cp.stdout.strip(), "/store/cache/bench/jetstream3-%s" % SHA[:12], cp.stderr)
        self.assertEqual(target.read, [("w", ("cat", "/src/WebKit/Tools/Scripts/webkitpy/benchmark_runner/data/plans/jetstream3.plan"))])

    def test_it_needs_a_workspace_and_a_plan(self):
        b = cli.Bench(REPO, self.Registry(self.Target(), fake(), "/store"), FakeClock())
        with self.assertRaises(Refused):
            with in_process_ok():
                b.seed("w", "", True)


class TestTheBashShim(WkTest):
    """seed_payload, as cmd/pi, build/mac-pgo.sh and the run arm call it: the
    caller's bench_plan_read, then the Python, against a real local repository."""

    def test_it_pins_a_payload_and_prints_where(self):
        with scratch_dir() as tmp, temp_store() as store:
            repo = tmp / "JetStream"
            repo.mkdir()
            (repo / "index.html").write_text("<html>")
            git = ["git", "-C", str(repo), "-c", "user.email=t@example.com", "-c", "user.name=t"]
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(git + ["add", "-A"], check=True)
            subprocess.run(git + ["commit", "-q", "-m", "payload"], check=True)
            plan = json.dumps({"git_repository": {"url": str(repo), "branch": "main"}})
            cp = bash('. lib/common.sh; . lib/store.sh; . lib/bench.sh\n'
                      'bench_plan_read() { printf "%s" "$PLAN"; }\n'
                      'seed_payload jetstream3\n',
                      env={"WK_STORE": store["WK_STORE"], "WK_LOCK_DIR": str(store["path"] / "locks"), "PLAN": plan})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            dest = cp.stdout.strip().splitlines()[-1]
            self.assertTrue(dest.startswith(os.path.join(store["WK_STORE"], "cache", "bench", "jetstream3-")), dest)
            self.assertTrue(os.path.isfile(os.path.join(dest, "index.html")))
            self.assertFalse(os.path.exists(os.path.join(dest, ".git")))


class in_process_ok:
    """Swallows stderr for a block, and hands it back."""

    def __enter__(self):
        import contextlib
        import io
        self.err = io.StringIO()
        self._cm = contextlib.redirect_stderr(self.err)
        self._cm.__enter__()
        return self.err

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        return False
