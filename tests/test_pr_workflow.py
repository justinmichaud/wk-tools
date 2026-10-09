"""`wk pr` / `wk new --pr`: the spec parser and the mirror fetches underneath
them (lib/wk/pr.py's parse_spec, sync.fetch_into_mirror and fetch_pull_into_mirror)."""
import asyncio
import contextlib
import importlib.util
import io
import os
import shutil
import ssl
import subprocess
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests.fakes import WsDriver
from tests.killpoints import converges
from tests.support import REPO, load_cmd, rand_suffix, run, scratch_dir, stub_path

CMD_PR = REPO / "cmd" / "pr"


CMD_PR_MODULE = load_cmd("pr")

from wk import act, decl, git, places, pr, sync  # noqa: E402  -- needs CMD_PR_MODULE's sys.path.insert above
from wk.clock import Clock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402
from wk.store import Store  # noqa: E402


def _git(*args, cwd, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=check
    )


def _make_repo(dir_, branch, filename="f.txt"):
    """A minimal real git repo with one commit on <branch>. Returns its HEAD sha."""
    dir_.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, cwd=dir_)
    (dir_ / filename).write_text(f"{rand_suffix()}\n")
    _git("add", ".", cwd=dir_)
    _git("-c", "user.email=t@example.com", "-c", "user.name=Test",
         "commit", "-q", "-m", "init", cwd=dir_)
    return _git("rev-parse", "HEAD", cwd=dir_).stdout.strip()


class GitWorld(Fake):

    def __init__(self):
        super().__init__("host")
        self.local = {}            # branch -> sha
        self.remotes = {}          # name -> url
        self.remote_refs = {}      # (remote, branch) -> sha
        self.fetch_shas = {}       # src_ref (or "origin/main") -> sha a fetch brings in
        self.head = None
        self.dirty = 0
        self.upstream = {}         # branch -> {"remote": r, "merge": ref}
        self.pushed = set()        # (fork, branch)
        self.driver = WsDriver("ws", REPO, {}, self)
        self.react(["exec", "ws", "test", "-d"], lambda a, f: Result(0 if a[4] in f.dirs else 1))
        self.react(["exec", "ws", "git", "-C"], lambda a, f: f.git(a[5:]))

    @property
    def fake(self):
        return self

    def _resolve(self, ref):
        if ref.startswith("refs/remotes/"):
            _, _, remote, branch = ref.split("/", 3)
            return self.remote_refs.get((remote, branch), "")
        return ref

    def _fetch(self, sub):
        self.effect(("fetch",) + tuple(sub))
        last = sub[-1]
        if last == "origin" or last.startswith("+refs/heads/*:refs/remotes/"):
            self.remote_refs[("origin", "main")] = self.fetch_shas["origin/main"]
            return Result(0)
        src_ref, _, dest = last.partition(":")
        _, _, remote, branch = dest.split("/", 3)
        self.remote_refs[(remote, branch)] = self.fetch_shas[src_ref]
        return Result(0)

    def git(self, sub):
        if sub[:2] == ["status", "--porcelain"]:
            return Result(0, "M x\n" * self.dirty)
        if sub[:3] == ["rev-parse", "--verify", "--quiet"]:
            b = sub[3][len("refs/heads/"):]
            sha = self.local.get(b, "")
            return Result(0, sha + "\n") if sha else Result(1)
        if sub[:2] == ["config", "--get-regexp"]:
            lines = ["remote.%s.url %s" % (n, u) for n, u in self.remotes.items()]
            return Result(0, "\n".join(lines))
        if sub[:2] == ["remote", "add"]:
            _, name, url = sub[1:]
            self.effect(("remote-add", name))
            self.remotes[name] = url
            return Result(0)
        if sub[:2] == ["remote", "set-url"]:
            _, name, url = sub[1:]
            self.effect(("remote-set-url", name))
            self.remotes[name] = url
            return Result(0)
        if sub[0] == "fetch":
            return self._fetch(sub)
        if sub[:2] == ["rev-list", "--count"]:
            return Result(0, "0\n")
        if sub[:3] == ["show-ref", "--verify", "--quiet"]:
            b = sub[3][len("refs/heads/"):]
            return Result(0) if b in self.local else Result(1)
        if sub[:2] == ["checkout", "--quiet"] and len(sub) > 2 and sub[2] == "-b":
            branch, start = sub[3], sub[4]
            self.effect(("checkout-b", branch))
            self.local[branch] = self._resolve(start)
            self.head = branch
            return Result(0)
        if sub[:2] == ["checkout", "--quiet"]:
            branch = sub[2]
            self.effect(("checkout", branch))
            self.head = branch
            return Result(0)
        if sub[:3] == ["reset", "--hard", "--quiet"]:
            start = sub[3]
            self.effect(("reset", self.head))
            self.local[self.head] = self._resolve(start)
            return Result(0)
        if sub[:3] == ["branch", "--quiet", "--unset-upstream"]:
            branch = sub[3]
            self.effect(("unset-upstream", branch))
            self.upstream.pop(branch, None)
            return Result(0)
        if sub[0] == "config" and sub[1].startswith("branch."):
            key, value = sub[1], sub[2]
            branch, field = key.split(".")[1], key.rsplit(".", 1)[1]
            self.effect(("config", key))
            self.upstream.setdefault(branch, {})[field] = value
            return Result(0)
        if sub[:2] == ["config", "--get-all"] and sub[2].endswith(".fetch"):
            name = sub[2][len("remote."):-len(".fetch")]
            return Result(0, "+refs/heads/*:refs/remotes/%s/*\n" % name) if name in self.remotes else Result(1)
        if sub[0] == "config" and sub[1] == "--get" and sub[2].startswith("remote."):
            name = sub[2][len("remote."):-len(".url")]
            url = self.remotes.get(name)
            return Result(0, url + "\n") if url else Result(1)
        if sub[0] == "symbolic-ref":
            return Result(0, self.head + "\n") if self.head else Result(1)
        if sub[0] == "rev-parse" and sub[1] == "--abbrev-ref":
            up = self.upstream.get(self.head)
            return Result(0, "%s/%s\n" % (up["remote"], up["merge"][len("refs/heads/"):])) if up else Result(1)
        if sub[0] == "push":
            fork, branch = sub[2], sub[3]
            self.effect(("push", fork, branch))
            self.pushed.add((fork, branch))
            self.remote_refs[(fork, branch)] = self.local.get(branch, "")
            return Result(0)
        if sub[0] == "rebase":
            remote, branch = sub[1].split("/", 1)
            sha = self.remote_refs.get((remote, branch))
            self.effect(("rebase", self.head))
            self.local[self.head] = sha
            return Result(0)
        if sub[:2] == ["--no-pager", "log"]:
            return Result(0, "abc1234 message\n")
        raise AssertionError("GitWorld: unhandled git subcommand: %r" % (sub,))


class RecordingDriver(WsDriver):
    """Workspace `ws` on a GitWorld, its mutations kept in order."""

    def __init__(self, world):
        super().__init__("ws", REPO, {}, world)
        self.mutations = []

    def act_exec(self, ws, argv):
        self.mutations.append(tuple(argv))
        return super().act_exec(ws, argv)


OPENS = ("WebKit/WebKit", "alice:eng/x", "fork", "eng/x")


def checkout_world():
    w = GitWorld()
    for url, out in (("https://github.com/alice/WebKit.git", "b" * 40 + "\trefs/heads/eng/x\n"), ("https://github.com/alice/WPEWebKit.git", "")):
        w.answer(["git", "ls-remote", git.direct_url(url), "refs/heads/eng/x"], out=out)
    w.fetch_shas = {"refs/heads/eng/x": "b" * 40}
    return w


def checkout_state(w):
    return (dict(w.remotes), dict(w.local), w.head, {b: dict(v) for b, v in w.upstream.items()})


def rebase_world():
    w = GitWorld()
    w.local, w.head, w.fetch_shas = {"eng/y": "d" * 40}, "eng/y", {"origin/main": "c" * 40}
    return w


def open_once(driver):
    with mock.patch.object(CMD_PR_MODULE, "pr_open_target", return_value=OPENS):
        CMD_PR_MODULE.pr_open(driver, "ws", False, False)


class TestPrKillPoints(unittest.TestCase):
    def test_a_checkout_a_rebase_and_an_open_killed_after_any_effect_and_rerun_converge(self):
        for name, make, flow, state in (
                ("checkout", checkout_world, lambda w: pr.checkout(w.driver, w, "ws", "alice:eng/x"), checkout_state),
                ("rebase", rebase_world, lambda w: CMD_PR_MODULE.pr_rebase(w.driver, "ws"), lambda w: dict(w.local)),
                ("open", GitWorld, lambda w: open_once(w.driver), lambda w: (set(w.pushed), any(e[0] == "exec" for e in w.effects)))):
            with self.subTest(flow=name), contextlib.redirect_stderr(io.StringIO()):
                converges(self, make, flow, state)


class BoxKeys:
    """Secrets as `push_from_here` reads it: one fork, and where its private half is."""

    def forks(self):
        return [("fork", "alice/WebKit", "")]

    def push_key_path(self, fork):
        return "/s/push-keys/build_key_%s" % fork


class TestPrOpenFromABoxKillPoints(unittest.TestCase):

    MIRROR = "/h/mirror"
    REF = "refs/wk/push/box/eng/x"

    def make_world(self):
        w = Fake("here")
        w.fake, w.refs, w.pushed = w, set(), set()
        w.answer(["git", "init"], 0)
        w.answer(["git", "-C", self.MIRROR, "config"], 0)
        w.files["/s/push-keys/build_key_fork"] = "KEY\n"

        def git_in_mirror(argv, fake):
            if "fetch" in argv:
                fake.refs.add(argv[-1].split(":", 1)[1])
            elif "update-ref" in argv:
                fake.refs.discard(argv[-1])
            elif "push" in argv:
                fake.pushed.add(argv[-1])
            return Result(0)
        w.react(["git", "-C", self.MIRROR], git_in_mirror)
        return w

    def run_once(self, w, keys=None):
        driver = types.SimpleNamespace(here=w, env={"HOME": "/h"}, name="box", ssh_host=lambda _: "box")
        keys = keys or BoxKeys()

        @contextlib.contextmanager
        def held(resource):
            w.effects.append(("hold", resource))
            yield
            w.effects.append(("release", resource))
        with mock.patch.object(CMD_PR_MODULE, "Store", lambda env: mock.Mock(mirror_dir=lambda: self.MIRROR)), \
                mock.patch.object(CMD_PR_MODULE, "Lock", lambda *a: mock.Mock(held=held)), \
                mock.patch.object(CMD_PR_MODULE.secrets, "Secrets", lambda *a: keys), \
                mock.patch.object(sync, "in_vm", lambda env: False), \
                contextlib.redirect_stderr(io.StringIO()):
            CMD_PR_MODULE.push_from_here(driver, "/src", "fork", "eng/x")

    def state(self, w):
        return (set(w.refs), set(w.pushed))

    def test_a_push_from_a_box_killed_after_any_effect_and_rerun_converges(self):
        converges(self, self.make_world, self.run_once, self.state)
        w = self.make_world()
        self.run_once(w)
        self.assertEqual(self.state(w), (set(), {"%s:refs/heads/eng/x" % self.REF}))

    def test_the_temporary_ref_is_locked_from_its_fetch_until_its_delete(self):
        """`wk gc` keeps a ref whose lock is held, so it cannot delete one between this fetch and this push."""
        w = self.make_world()
        self.run_once(w)
        locks = [i for i, e in enumerate(w.effects) if e == ("hold", pr.push_lock(self.REF)) or e == ("release", pr.push_lock(self.REF))]
        work = [i for i, e in enumerate(w.effects) if e[0] == "run"]
        self.assertEqual(2, len(locks))
        self.assertTrue(locks[0] < min(work) and max(work) < locks[1], w.effects)

    def test_the_push_uses_the_forks_private_key_and_no_agent(self):
        w = self.make_world()
        self.run_once(w)
        (argv,) = [e[1] for e in w.effects if e[0] == "run" and "push" in e[1]]
        ssh = next(a for a in argv if a.startswith("core.sshCommand=")).split("=", 1)[1]
        self.assertIn("-i /s/push-keys/build_key_fork", ssh)
        self.assertIn("IdentityAgent=none", ssh)
        self.assertNotIn(".pub", ssh)
        self.assertIn("git@github.com:alice/WebKit.git", argv)

    def test_a_machine_with_no_key_refuses_naming_the_remedy_before_any_effect(self):
        w = self.make_world()
        w.files.clear()
        with self.assertRaises(act.Refused), mock.patch.object(CMD_PR_MODULE, "die", side_effect=act.Refused(1)) as die:
            self.run_once(w)
        self.assertIn("wk key deploy", die.call_args[0][0])
        self.assertEqual((set(), set()), self.state(w))


class TestPrDryRunEqualsWetRun(unittest.TestCase):

    def dry_and_wet(self, make, verb):
        wet_world, dry_world = make(), make()
        wet_place, dry_place = RecordingDriver(wet_world), RecordingDriver(dry_world)
        with contextlib.redirect_stderr(io.StringIO()):
            verb(wet_place)
            with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}):
                verb(dry_place)
        self.assertTrue(wet_place.mutations)
        self.assertEqual(dry_place.mutations, wet_place.mutations)
        return wet_world, dry_world

    def test_a_dry_checkout_is_the_wet_runs_plan_and_touches_nothing(self):
        _, dry = self.dry_and_wet(checkout_world, lambda t: pr.checkout(t, t.machine, "ws", "alice:eng/x"))
        self.assertEqual(checkout_state(dry), checkout_state(checkout_world()))

    def test_a_dry_rebase_is_the_wet_runs_plan_and_touches_nothing(self):
        wet, dry = self.dry_and_wet(rebase_world, lambda t: CMD_PR_MODULE.pr_rebase(t, "ws"))
        self.assertEqual(({"eng/y": "c" * 40}, {"eng/y": "d" * 40}), (wet.local, dry.local))

    def test_a_dry_open_pushes_nothing_and_prints_the_pull_request(self):
        with mock.patch.object(CMD_PR_MODULE.os, "execvp") as execvp, \
                mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(io.StringIO()) as err:
            w = GitWorld()
            with self.assertRaises(SystemExit):
                open_once(RecordingDriver(w))
        execvp.assert_not_called()
        self.assertEqual(set(), w.pushed)
        self.assertIn("would run in ws: git -C /src/WebKit push -u fork eng/x", err.getvalue())
        self.assertIn("would run: gh pr create --repo WebKit/WebKit --head alice:eng/x --fill", err.getvalue())

    def test_every_form_declares_its_dry_run(self):
        d = decl.Decl(CMD_PR)
        for args in (["some-workspace", "42"], ["rebase", "some-workspace"], ["open", "some-workspace"]):
            self.assertTrue(d.honours_dryrun(args), args)


class TestPrParseSpec(unittest.TestCase):
    def test_the_three_spellings(self):
        for spec, want in (("alice:eng/frame-fix", ["user", "alice", "eng/frame-fix", "", ""]),
                           ("1234", ["pull", "", "", "origin", "1234"]),
                           ("wpe:5678", ["pull", "", "", "wpe", "5678"]),
                           ("wpe:somebranch", ["user", "wpe", "somebranch", "", ""])):
            got = pr.parse_spec(spec)
            self.assertEqual([got[k] for k in ("kind", "user", "branch", "remote", "n")], want, spec)

    def test_a_bad_spec_is_refused(self):
        for spec, why in (("not-a-spec", "not a PR spec"), ("1234x", "not a pull request number"),
                          ("wpe:12x", "not a pull request number"), (":b", "expected <user>:<branch>")):
            with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
                pr.parse_spec(spec)
            self.assertIn(why, err.getvalue(), spec)


def scratch_store(tmp):
    return Store({"WK_STORE": str(tmp / "store"), "WK_LOCK_DIR": str(tmp / "locks"),
                  "XDG_STATE_HOME": str(tmp / "state"), "WK_IN_VM": "", "HOME": str(tmp)})


class ScratchTest(unittest.TestCase):
    def setUp(self):
        scratch = scratch_dir(prefix="wk-pr-test-")
        self.tmp = scratch.__enter__()
        self.addCleanup(scratch.__exit__, None, None, None)
        self.store = scratch_store(self.tmp)


class TestMirrorFetch(ScratchTest):

    def setUp(self):
        super().setUp()
        self.here = Local()

    def fetch(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            fn(self.here, self.store, Lock(self.store, self.here, Clock()), *args)
        return err.getvalue()

    def mirror_rev(self, ref):
        return _git("rev-parse", ref, cwd=self.store.mirror_dir()).stdout.strip()

    def test_a_fork_branch_lands_under_pr_and_the_second_fetch_is_a_no_op(self):
        fork = self.tmp / "fork"
        sha = _make_repo(fork, "eng-test")
        err = self.fetch(sync.fetch_into_mirror, str(fork), "refs/heads/eng-test", "refs/remotes/pr/alice/WebKit/eng-test")
        self.assertIn("creating bare mirror", err)
        self.assertEqual(self.mirror_rev("refs/remotes/pr/alice/WebKit/eng-test"), sha)
        self.assertEqual(_git("config", "gc.auto", cwd=self.store.mirror_dir()).stdout.strip(), "0")
        before = _git("count-objects", "-v", cwd=self.store.mirror_dir()).stdout
        self.assertNotIn("creating bare mirror",
                         self.fetch(sync.fetch_into_mirror, str(fork), "refs/heads/eng-test", "refs/remotes/pr/alice/WebKit/eng-test"))
        self.assertEqual(_git("count-objects", "-v", cwd=self.store.mirror_dir()).stdout, before)

    def test_a_pull_request_lands_as_refs_pull_n_head(self):
        origin = self.tmp / "origin"
        sha = _make_repo(origin, "main")
        _git("update-ref", "refs/pull/7/head", sha, cwd=origin)
        self.fetch(sync.fetch_pull_into_mirror, "origin", "7", (("origin", str(origin)),))
        self.assertEqual(self.mirror_rev("refs/remotes/pr/" + pr.pull_refname("origin", "7")), sha)

    def test_an_unknown_remote_is_refused_by_name(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            sync.fetch_pull_into_mirror(self.here, self.store, None, "nosuchremote", "1")
        self.assertIn("no such upstream remote", err.getvalue())

    def test_a_failed_fetch_is_a_refusal_naming_the_ref(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            sync.fetch_into_mirror(self.here, self.store, Lock(self.store, self.here, Clock()),
                            str(self.tmp / "nowhere"), "refs/heads/x", "refs/remotes/pr/x")
        self.assertIn("could not fetch refs/heads/x", err.getvalue())


class TestMirrorFetchIsARecorderUnderADryRun(ScratchTest):

    def setUp(self):
        super().setUp()
        self.here = Fake("here")
        p = mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"})
        p.start()
        self.addCleanup(p.stop)

    def test_fetch_into_mirror_runs_no_git_under_a_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()):
            sync.fetch_into_mirror(self.here, self.store, Lock(self.store, self.here, Clock()),
                            "https://example/x.git", "refs/heads/b", "refs/remotes/pr/b")
        self.assertEqual([e for e in self.here.effects if e[0] == "run"], [])

    def test_resolved_or_planned_answers_from_the_mirror_already_there(self):
        mirror = self.store.mirror_dir()
        self.here.dirs.add(mirror)
        sha = "c" * 40
        self.here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/x^{commit}"], out=sha + "\n")
        called = []
        got = pr.resolved_or_planned(self.here, mirror, "refs/x", "x", lambda: called.append(1))
        self.assertEqual((got, called), (sha, []))

    def test_resolved_or_planned_plans_a_fetch_it_has_not_made_yet(self):
        mirror = self.store.mirror_dir()
        self.here.dirs.add(mirror)
        self.here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet"], rc=1)
        called = []
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = pr.resolved_or_planned(self.here, mirror, "refs/x", "x from y", lambda: called.append(1))
        self.assertEqual((got, called), (pr.PLANNED_COMMIT, []))
        self.assertIn("would fetch x from y into the mirror", err.getvalue())


class TestPrOpenTarget(ScratchTest):
    def _checkout(self, project, remote, fork_remote, branch, tracks_fork):
        upstream = self.tmp / f"{project}-{rand_suffix()}" / project
        _make_repo(upstream, branch if tracks_fork else "main")
        work = self.tmp / f"work-{rand_suffix()}"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        source = fork_remote if tracks_fork else remote
        _git("remote", "add", source, str(upstream), cwd=work)
        _git("fetch", "-q", source, cwd=work)
        _git("checkout", "-q", "-b", branch, f"{source}/{branch if tracks_fork else 'main'}", cwd=work)
        url = f"https://github.com/testuser/{project}.git"
        _git("remote", "set-url" if tracks_fork else "add", fork_remote, url, cwd=work)
        return work

    def test_which_project_a_branch_opens_against(self):
        for project, remote, fork, base in (("WebKit", "origin", "fork", "WebKit/WebKit"),
                                            ("WPEWebKit", "wpe", "forkwpe", "WebPlatformForEmbedded/WPEWebKit")):
            for tracks_fork in (False, True):
                with self.subTest(project=project, tracks_fork=tracks_fork):
                    work = self._checkout(project, remote, fork, "eng/x", tracks_fork)
                    self.assertEqual(CMD_PR_MODULE.pr_open_target(work), (base, "testuser:eng/x", fork, "eng/x"))

    def test_what_cannot_be_opened_is_refused_by_name(self):
        main = self.tmp / "on-main"
        main.mkdir()
        _git("init", "-q", "-b", "main", cwd=main)
        detached = self.tmp / "detached"
        _make_repo(detached, "main")
        _git("checkout", "-q", "--detach", "HEAD", cwd=detached)
        dirty = self._checkout("WebKit", "origin", "fork", "eng/dirty", False)
        (dirty / "untracked.txt").write_text("scratch\n")
        loose = self.tmp / "no-upstream"
        _make_repo(loose, "main")
        _git("checkout", "-q", "-b", "eng/untracked", cwd=loose)
        for work, why in ((main, "cannot open 'main'"), (detached, "detached HEAD"),
                          (dirty, "uncommitted changes"), (loose, "no upstream")):
            with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()) as err:
                CMD_PR_MODULE.pr_open_target(work)
            self.assertIn(why, err.getvalue())


def rebase_place(mirror, responses):
    """Workspace `myws` whose commands answer `responses` in turn."""
    fake, queue = Fake(), list(responses)
    fake.react(["exec", "myws"], lambda a, f: queue.pop(0))
    return WsDriver("ws", str(REPO), {}, fake, kind=places.Driver.kind, mirror=mirror)


def calls(driver):
    return [list(e[1][2:]) for e in driver.machine.effects if e[0] == "run" and e[1][:2] == ("exec", "myws")]


class TestPrRebase(unittest.TestCase):

    def _run(self, mirror, responses):
        driver = rebase_place(mirror, responses)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = CMD_PR_MODULE.pr_rebase(driver, "myws")
        return rc, calls(driver), err.getvalue()

    def test_it_fetches_from_the_mirror_only_when_it_is_there(self):
        ok = [Result(0), Result(0), Result(0, "abc1234 c\n")]   # fetch, rebase, log
        for mirror, there, source in (("/m.git", [Result(0)], "/m.git"), ("", [], "origin"), ("/m.git", [Result(1)], "origin")):
            rc, calls, _ = self._run(mirror, there + ok)
            self.assertEqual(rc, 0)
            self.assertIn(source, calls[len(there)])
            self.assertEqual(calls[len(there) + 1], ["git", "-C", "/src/WebKit", "rebase", "origin/main"])

    def test_a_failed_fetch_never_rebases(self):
        rc, calls, err = self._run("", [Result(1, "", "network unreachable\n")])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 1, "the rebase must not run over an unfetched checkout")
        self.assertIn("the rebase stopped", err)

    def test_a_rebase_conflict_is_reported_not_swallowed(self):
        rc, calls, err = self._run("", [
            Result(0),                       # fetch ok
            Result(1, "", "CONFLICT\n"),      # rebase stops
        ])
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 2, "nothing past the failed rebase runs")
        self.assertIn("CONFLICT\n", err)
        self.assertLess(err.index("CONFLICT"), err.index("the rebase stopped"))
        self.assertIn("git rebase --continue", err)


class TestPrOpen(unittest.TestCase):
    """It pushes from where the key is, then ends as gh: the process becomes `gh pr create`, so a PR gh did not create
    is not a success."""

    def _open(self, peer=None, draft=False):
        driver = rebase_place("", [Result(0)])   # the push
        if peer is not None:
            driver.kind, driver.is_local, driver.peer = "remote", False, peer
        with mock.patch.object(CMD_PR_MODULE, "pr_open_target", return_value=("WebKit/WebKit", "me:b", "fork", "b")), \
                mock.patch.object(CMD_PR_MODULE, "push_from_here", return_value=Result(0)) as from_here, \
                contextlib.redirect_stderr(io.StringIO()):
            CMD_PR_MODULE.pr_open(driver, "myws", draft=draft, web=False)
        return driver, from_here.called, calls(driver)

    def test_the_command_execs_into_gh(self):
        driver = self._open(draft=True)[0]
        self.assertEqual([("exec", ("gh", "pr", "create", "--repo", "WebKit/WebKit", "--head", "me:b", "--fill", "--draft"), None)],
                         [e for e in driver.here.effects if e[0] == "exec"])

    def test_a_build_box_pushes_from_here_and_a_peer_its_own(self):
        self.assertEqual(self._open(peer=False)[1:], (True, []))
        self.assertEqual(self._open(peer=True)[1:], (False, [["git", "-C", "/src/WebKit", "push", "-u", "fork", "b"]]))


class TestPrOpenRefusals(unittest.TestCase):

    def test_refuses_when_the_stored_token_is_dead(self):
        with stub_path({
            "gh": '#!/bin/sh\ncase "$1 $2" in\n'
                  '"auth status") exit 0 ;;\n'
                  '"api user") echo \'{"message":"Requires authentication"}\'; exit 1 ;;\n'
                  'esac\nexit 0\n',
        }) as binp:
            cp = run("pr", "open", "some-workspace",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("gh auth login", cp.stdout)


class TestGitWebkitPrThroughTheInjector(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("needs the openssl CLI")
        path = REPO / "container" / "proxy" / "github-inject.py"
        spec = importlib.util.spec_from_file_location("wkinject_e2e", str(path))
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def _run(self, token):
        m = self.m
        d = Path(tempfile.mkdtemp(prefix="wk-test-inject-"))
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        chain = m.ensure_certs(str(d / "certs"), str(d / "ca.pem"))
        pat = d / "pat"
        pat.write_text(token + "\n")

        seen = {}

        async def upstream(reader, writer):
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                head += chunk
            seen["head"] = head
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"
                         b"Connection: close\r\n\r\nok")
            await writer.drain()
            writer.close()

        async def main():
            up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            up_ctx.load_cert_chain(chain, str(d / "certs" / "leaf.key"))
            up = await asyncio.start_server(upstream, host="127.0.0.1", port=0,
                                            ssl=up_ctx)
            port = up.sockets[0].getsockname()[1]

            client_ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            m.INJECT_PORT = port
            injector = m.Injector(str(pat), str(d / "read-pat"), str(d / "bz-key"),
                                  m.Holder(str(d / "claude-login")), client_ctx)

            srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            srv_ctx.load_cert_chain(chain, str(d / "certs" / "leaf.key"))
            sock = str(d / "inject.sock")
            server = await asyncio.start_unix_server(injector.handle, path=sock,
                                                     ssl=srv_ctx)

            ws_ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            reader, writer = await asyncio.open_unix_connection(
                sock, ssl=ws_ctx, server_hostname="api.github.com")
            writer.write(
                b"GET /user HTTP/1.1\r\n"
                b"Host: api.github.com\r\n"
                b"User-Agent: python-requests/2.31.0\r\n"
                b"Accept: application/vnd.github.v3+json\r\n"
                b"Authorization: Basic d2s6d2staW5qZWN0cy10aGlz\r\n"
                b"Connection: keep-alive\r\n\r\n")
            await writer.drain()
            body = await asyncio.wait_for(reader.read(4096), 10)
            writer.close()
            server.close()
            up.close()
            return body

        # The injector dials api.github.com by name; the fake upstream is on loopback.
        real_open = asyncio.open_connection

        async def to_loopback(host, port, **kw):
            return await real_open("127.0.0.1", port, **kw)

        with mock.patch.object(asyncio, "open_connection", to_loopback):
            got = asyncio.run(asyncio.wait_for(main(), 30))
        return seen.get("head", b""), got

    def test_the_real_token_arrives_and_the_placeholder_never_does(self):
        head, got = self._run("ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", head)
        self.assertNotIn(b"Basic", head)
        self.assertNotIn(b"wk-injects-this", head)
        self.assertIn(b"200 OK", got)
        self.assertIn(b"GET /user HTTP/1.1", head)
        self.assertIn(b"Accept: application/vnd.github.v3+json", head)

