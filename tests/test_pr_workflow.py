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

from tests import fakes
from tests.support import REPO, load_cmd, rand_suffix, stub_path

CMD_PR_MODULE = load_cmd("pr")

from wk import act, decl, dispatch, git, places, pr, sync  # noqa: E402  -- needs CMD_PR_MODULE's sys.path.insert above
from wk.clock import Clock  # noqa: E402
from wk.lock import Lock  # noqa: E402
from wk.machine import Local, Result  # noqa: E402
from tests.fakes import Fake, WsDriver  # noqa: E402
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


class GitWorld(fakes.World):
    def __init__(self):
        super().__init__(name="host")
        self.local = {}            # branch -> sha
        self.remotes = {}          # name -> url
        self.remote_refs = {}      # (remote, branch) -> sha
        self.fetch_shas = {}       # src_ref (or "origin/main") -> sha a fetch brings in
        self.head = None
        self.dirty = 0
        self.upstream = {}         # branch -> {"remote": r, "merge": ref}
        self.pushed = set()        # (fork, branch)
        self.driver = RecordingDriver(self)
        self.react(["exec", "ws", "test", "-d"], lambda a, f: Result(0 if a[4] in f.dirs else 1))
        self.react(["exec", "ws", "git", "-C"], lambda a, f: f.git(a[5:]))

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
        if sub[0] == "push":
            fork, branch = sub[1], sub[2]
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
    """Workspace `ws` on a GitWorld, its mutations kept in order: act_exec prints under a dry run rather than reaching
    the machine's act_run, so the driver records it."""

    def __init__(self, world):
        super().__init__("ws", REPO, {}, world)
        self.mutations = []

    def act_exec(self, ws, argv, person=False):
        self.mutations.append(tuple(argv) + (("as a person",) if person else ()))
        return super().act_exec(ws, argv, person=person)


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
    with mock.patch.object(pr, "pr_open_target", return_value=OPENS):
        pr.pr_open(driver, "ws", False, False)


class BoxKeys:
    """Secrets as `push_from_here` reads it: one fork, and where its private half is."""

    def forks(self):
        return [("fork", "alice/WebKit", "")]

    def push_key_path(self, fork):
        return "/s/push-keys/build_key_%s" % fork


MIRROR = "/h/mirror"
BOX_REF = "refs/wk/push/box/eng/x"


def box_world():
    w = fakes.World()
    w.refs, w.pushed = set(), set()
    w.answer(["git", "init"], 0)
    w.answer(["git", "-C", MIRROR, "config"], 0)
    w.files["/s/push-keys/build_key_fork"] = "KEY\n"

    def git_in_mirror(argv, fake):
        if "fetch" in argv:
            fake.refs.add(argv[-1].split(":", 1)[1])
        elif "update-ref" in argv:
            fake.refs.discard(argv[-1])
        elif "push" in argv:
            fake.pushed.add(argv[-1])
        return Result(0)
    w.react(["git", "-C", MIRROR], git_in_mirror)
    return w


def box_push(w, keys=None):
    """`wk pr open` pushing from a build box through its own mirror."""
    driver = types.SimpleNamespace(here=w, env={"HOME": "/h"}, name="box", ssh_host=lambda _: "box")
    keys = keys or BoxKeys()

    @contextlib.contextmanager
    def held(resource):
        w.effects.append(("hold", resource))
        yield
        w.effects.append(("release", resource))
    with mock.patch.object(pr, "Store", lambda env: mock.Mock(mirror_dir=lambda: MIRROR)), \
            mock.patch.object(pr, "Lock", lambda *a: mock.Mock(held=held)), \
            mock.patch.object(pr.secrets, "Secrets", lambda *a: keys), \
            mock.patch.object(sync, "in_vm", lambda env: False), \
            contextlib.redirect_stderr(io.StringIO()):
        pr.push_from_here(driver, "/src", "fork", "eng/x")


def box_state(w):
    return (set(w.refs), set(w.pushed))


class TestPrOpenFromABox(unittest.TestCase):
    def test_the_push_leaves_the_branch_on_the_fork_and_no_temporary_ref(self):
        w = box_world()
        box_push(w)
        self.assertEqual(box_state(w), (set(), {"%s:refs/heads/eng/x" % BOX_REF}))

    def test_the_temporary_ref_is_locked_from_its_fetch_until_its_delete(self):
        """`wk gc` keeps a ref whose lock is held, so it cannot delete one between this fetch and this push."""
        w = box_world()
        box_push(w)
        locks = [i for i, e in enumerate(w.effects) if e == ("hold", pr.push_lock(BOX_REF)) or e == ("release", pr.push_lock(BOX_REF))]
        work = [i for i, e in enumerate(w.effects) if e[0] == "run"]
        self.assertEqual(2, len(locks))
        self.assertTrue(locks[0] < min(work) and max(work) < locks[1], w.effects)

    def test_the_push_uses_the_forks_private_key_and_no_agent(self):
        w = box_world()
        box_push(w)
        (argv,) = [e[1] for e in w.effects if e[0] == "run" and "push" in e[1]]
        ssh = next(a for a in argv if a.startswith("core.sshCommand=")).split("=", 1)[1]
        self.assertIn("-i /s/push-keys/build_key_fork", ssh)
        self.assertIn("IdentityAgent=none", ssh)
        self.assertNotIn(".pub", ssh)
        self.assertIn("git@github.com:alice/WebKit.git", argv)

    def test_a_machine_with_no_key_refuses_naming_the_remedy_before_any_effect(self):
        w = box_world()
        w.files.clear()
        with self.assertRaises(act.Refused), mock.patch.object(pr, "die", side_effect=act.Refused(1)) as die:
            box_push(w)
        self.assertIn("wk key deploy", die.call_args[0][0])
        self.assertEqual((set(), set()), box_state(w))


class TestPrDryRunEqualsWetRun(unittest.TestCase):
    def test_a_dry_open_pushes_nothing_and_prints_the_pull_request(self):
        with mock.patch("os.execvp") as execvp, \
                mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(io.StringIO()) as err:
            w = GitWorld()
            with self.assertRaises(SystemExit):
                open_once(RecordingDriver(w))
        execvp.assert_not_called()
        self.assertEqual(set(), w.pushed)
        self.assertIn("would run in ws: git -C /src/WebKit push fork eng/x", err.getvalue())
        self.assertIn("would run: gh pr create --repo WebKit/WebKit --head alice:eng/x --fill", err.getvalue())

    def test_every_form_declares_its_dry_run(self):
        for args in (["some-workspace", "42"], ["rebase", "some-workspace"], ["open", "some-workspace"]):
            self.assertTrue(decl.parse("pr", CMD_PR_MODULE.COMMAND, args)[0].dryrun, args)


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


class ScratchTest(fakes.FlowTest):
    def setUp(self):
        super().setUp()
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
                    self.assertEqual(pr.pr_open_target(work), (base, "testuser:eng/x", fork, "eng/x"))

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
                pr.pr_open_target(work)
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
            rc = pr.pr_rebase(driver, "myws")
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

    def _open(self, peer=None):
        driver = rebase_place("", [Result(0)] * 4)   # the push, the fork's refspec, the two tracking writes
        if peer is not None:
            driver.kind, driver.is_here, driver.peer = "remote", lambda: False, peer
        with mock.patch.object(pr, "pr_open_target", return_value=("WebKit/WebKit", "me:b", "fork", "b")), \
                mock.patch.object(pr, "push_from_here", return_value=Result(0)) as from_here, \
                contextlib.redirect_stderr(io.StringIO()):
            pr.pr_open(driver, "myws", draft=False, web=False)
        return driver, from_here.called, calls(driver)

    def test_a_build_box_pushes_from_here_and_a_peer_its_own(self):
        track = [["git", "-C", "/src/WebKit", "config", "--get-all", "remote.fork.fetch"]] + pr.track("/src/WebKit", "fork", "b", "refs/heads/b")
        self.assertEqual(self._open(peer=False)[1:], (True, track))
        self.assertEqual(self._open(peer=True)[1:], (False, [["git", "-C", "/src/WebKit", "push", "fork", "b"]] + track))


class TestPrOpenRefusals(unittest.TestCase):
    def test_refuses_when_the_stored_token_is_dead(self):
        """`wk pr open` needs gh-auth, which a gh whose token the API refuses does not meet."""
        table = decl.parse("pr", CMD_PR_MODULE.COMMAND, ["open", "some-workspace"])[0]
        with stub_path({
            "gh": '#!/bin/sh\ncase "$1 $2" in\n'
                  '"auth status") exit 0 ;;\n'
                  '"api user") echo \'{"message":"Requires authentication"}\'; exit 1 ;;\n'
                  'esac\nexit 0\n',
        }) as binp, mock.patch.dict(os.environ, {"PATH": f"{binp}:{os.environ['PATH']}"}), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            dispatch.check_needs("pr", table)
        self.assertIn("gh auth login", err.getvalue())


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
