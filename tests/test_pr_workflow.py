"""`wk pr` / `wk new --pr`: the spec parser and the mirror fetches underneath
them (lib/wk/pr.py's parse_spec, mirror_fetch and mirror_fetch_pull).

The fetches run against temporary git repositories standing in for a fork,
an upstream, and the mirror -- git accepts a plain path as a URL, so no
network and no real WK_STORE is ever touched. WK_LOCK_DIR is pointed at a
scratch directory too: the fetches take the real 'store' lock name (the same
one `wk sync` takes), and without this a test run here would contend with a
real `wk sync` on this machine, or vice versa.

'wk pr open' -- the fifth form, which pushes a branch to its fork and opens
it with `gh pr create` -- is covered further down by TestPrOpenTarget and
TestPrOpenGhArgs (pr_open_target/pr_open_gh_args, imported straight out of
the Python cmd/pr) and by TestPrOpenRefusals (the two refusals that happen
before either of those ever runs, through the real dispatcher).

Run: python3 -m unittest tests.test_pr_workflow -v
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.killpoints import converges
from tests.support import REPO, fake_workspace, rand_suffix, run, scratch_dir, stub_path

PRELUDE = f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n. lib/store.sh\n'

CMD_PR = REPO / "cmd" / "pr"


def _load_cmd_pr():
    """cmd/pr as a module, the way its own `#!/usr/bin/env python3` runs it --
    a real file with no extension needs its loader spelled out."""
    loader = importlib.machinery.SourceFileLoader("wk_cmd_pr", str(CMD_PR))
    spec = importlib.util.spec_from_file_location("wk_cmd_pr", str(CMD_PR), loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# The real git.REMOTES and wk_push_forks: pr_open_target never fetches or pushes over them (it
# only reads remote *names* and URLs already configured in the test's own local-path repo).
CMD_PR_MODULE = _load_cmd_pr()

from wk import act, decl, git, pr, targets  # noqa: E402  -- needs CMD_PR_MODULE's sys.path.insert above
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
    """`killpoints[pr]`: a git+gh model behind pr.checkout/pr_rebase/pr_open,
    just real enough that a kill can land between any two of their mutations
    and a rerun has real state to converge from -- not a real git process,
    since what is under test is `wk pr`'s own recovery, not git's."""

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
        self.opened = 0
        self.target = SimTarget(self)

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
        """One `git -C <src>` subcommand, as pr.checkout/pr_rebase/pr_open shape it:
        a read answers from state, a write goes through `effect()` first."""
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


class SimTarget(targets.Target):
    """A workspace whose checkout is a GitWorld: `src`/`mirror_dir`/`exec` are
    pr.py's whole contract with a target, so this is the smallest thing that
    satisfies it."""

    def __init__(self, world, src="/src/WebKit", mirror=""):
        super().__init__("ws", REPO, {}, world)
        self.world = world
        self._src = src
        self._mirror = mirror

    def src(self, ws):
        return self._src

    def mirror_dir(self):
        return self._mirror

    def exec(self, ws, argv, tty=False, timeout=None):
        if argv[:2] == ["test", "-d"]:
            return Result(0 if argv[2] in self.world.dirs else 1)
        assert argv[:2] == ["git", "-C"], argv
        return self.world.git(argv[3:])


class RecordingTarget(SimTarget):
    """Every act_exec call, wet or dry -- act_exec's own dry-run gate decides
    whether `exec` (and so a GitWorld mutation) ever runs, so this is the one
    place a dry run's plan and a wet run's argv are the same list to compare."""

    def __init__(self, world, **kw):
        super().__init__(world, **kw)
        self.mutations = []

    def act_exec(self, ws, argv):
        self.mutations.append(tuple(argv))
        return super().act_exec(ws, argv)


class TestPrCheckoutKillPoints(unittest.TestCase):
    """`killpoints[pr]`: pr.checkout onto a fork's branch -- the path that adds
    a remote -- killed after any effect and rerun converging. The add is its
    own fire-and-forget act_exec precisely so a kill between it and the url
    fix-up cannot strand a half-wired remote: `_source`'s own config read
    finds the remote either way and skips re-adding it."""

    URL = "https://github.com/alice/WebKit.git"
    WPE_URL = "https://github.com/alice/WPEWebKit.git"

    def make_world(self):
        w = GitWorld()
        w.answer(["git", "ls-remote", git.direct_url(self.URL), "refs/heads/eng/x"], out="b" * 40 + "\trefs/heads/eng/x\n")
        w.answer(["git", "ls-remote", git.direct_url(self.WPE_URL), "refs/heads/eng/x"], out="")
        w.fetch_shas = {"refs/heads/eng/x": "b" * 40}
        return w

    def run_once(self, w):
        with contextlib.redirect_stderr(io.StringIO()):
            pr.checkout(w.target, w, "ws", "alice:eng/x")

    def state(self, w):
        return (dict(w.remotes), dict(w.local), w.head, {b: dict(v) for b, v in w.upstream.items()})

    def test_a_checkout_killed_after_any_effect_and_rerun_converges(self):
        converges(self, self.make_world, self.run_once, self.state)


class TestPrRebaseKillPoints(unittest.TestCase):
    """`killpoints[pr]`: pr_rebase's fetch and rebase, killed after either and
    rerun converging on the same rebased tip."""

    def make_world(self):
        w = GitWorld()
        w.local = {"eng/y": "d" * 40}
        w.head = "eng/y"
        w.fetch_shas = {"origin/main": "c" * 40}
        return w

    def run_once(self, w):
        with contextlib.redirect_stderr(io.StringIO()):
            CMD_PR_MODULE.pr_rebase(w.target, "ws")

    def state(self, w):
        return dict(w.local)

    def test_a_rebase_killed_after_any_effect_and_rerun_converges(self):
        converges(self, self.make_world, self.run_once, self.state)


class TestPrOpenKillPoints(unittest.TestCase):
    """`killpoints[pr]`: 'wk pr open's push and the `gh pr create` after it,
    killed after either and rerun converging -- a second push is a
    fast-forward no-op and a second `gh pr create` a harmless retry with
    GitHub, neither a half-made thing `wk` owns the recovery of."""

    def make_world(self):
        return GitWorld()

    def run_once(self, w):
        def fake_run(argv, **kw):
            if argv[:1] == ["gh"]:
                w.effect(("gh",) + tuple(argv))
                w.opened += 1
            return subprocess.CompletedProcess(argv, 0)
        with mock.patch.object(CMD_PR_MODULE, "pr_open_target",
                               return_value=("WebKit/WebKit", "alice:eng/x", "fork", "eng/x")), \
                mock.patch.object(CMD_PR_MODULE.subprocess, "run", fake_run), \
                contextlib.redirect_stderr(io.StringIO()):
            CMD_PR_MODULE.pr_open(w.target, "ws", False, False)

    def state(self, w):
        return (set(w.pushed), w.opened > 0)

    def test_an_open_killed_after_any_effect_and_rerun_converges(self):
        converges(self, self.make_world, self.run_once, self.state)


class TestPrCheckoutDryRunEqualsWetRun(unittest.TestCase):
    """cmd/pr declares 'dryrun' for the plain checkout form because every
    mutation in pr.checkout goes through Target.act_exec -- the one place
    --dry-run intercepts it -- so a dry run's plan is the wet run's argv
    list, in order, and a dry run touches no state. 'rebase' and 'open' stay
    undeclared: their mutations run through plain exec (pr_rebase's fetch and
    rebase, pr_open's push and `gh pr create`), so nothing would stop them."""

    URL = "https://github.com/alice/WebKit.git"
    WPE_URL = "https://github.com/alice/WPEWebKit.git"

    def _world(self):
        w = GitWorld()
        w.answer(["git", "ls-remote", git.direct_url(self.URL), "refs/heads/eng/x"], out="b" * 40 + "\trefs/heads/eng/x\n")
        w.answer(["git", "ls-remote", git.direct_url(self.WPE_URL), "refs/heads/eng/x"], out="")
        w.fetch_shas = {"refs/heads/eng/x": "b" * 40}
        return w, RecordingTarget(w)

    def _state(self, w):
        return (dict(w.remotes), dict(w.local), w.head, {b: dict(v) for b, v in w.upstream.items()})

    def test_a_dry_run_is_the_wet_runs_plan_and_touches_nothing(self):
        wet_world, wet_target = self._world()
        with contextlib.redirect_stderr(io.StringIO()):
            pr.checkout(wet_target, wet_world, "ws", "alice:eng/x")

        dry_world, dry_target = self._world()
        before = self._state(dry_world)
        with mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"}), contextlib.redirect_stderr(io.StringIO()):
            pr.checkout(dry_target, dry_world, "ws", "alice:eng/x")

        self.assertEqual(dry_target.mutations, wet_target.mutations)
        self.assertGreaterEqual(len(dry_target.mutations), 6)
        self.assertEqual(self._state(dry_world), before)

    def test_the_declaration_only_covers_the_path_that_honours_it(self):
        """rebase/open's mutations bypass act_exec (plain Target.exec), so the
        decl's per-sub override must turn dryrun back off for both -- the
        invariant CLAUDE.md holds cmd/pr to: declare it only where every path
        honours it."""
        d = decl.Decl(CMD_PR)
        self.assertTrue(d.honours_dryrun(["some-workspace", "42"]))
        self.assertFalse(d.honours_dryrun(["rebase", "some-workspace"]))
        self.assertFalse(d.honours_dryrun(["open", "some-workspace"]))


class TestPrParseSpec(unittest.TestCase):
    """parse_spec maps the three spellings 'wk pr' accepts."""

    def _fields(self, spec):
        got = pr.parse_spec(spec)
        return [got[k] for k in ("kind", "user", "branch", "remote", "n")]

    def _refused(self, spec):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.parse_spec(spec)
        return err.getvalue()

    def test_user_branch(self):
        """'user:branch' is a fork's branch"""
        self.assertEqual(self._fields("alice:eng/frame-fix"), ["user", "alice", "eng/frame-fix", "", ""])

    def test_bare_number(self):
        """a bare number is a pull request against origin (WebKit/WebKit)"""
        self.assertEqual(self._fields("1234"), ["pull", "", "", "origin", "1234"])

    def test_wpe_number(self):
        """'wpe:<n>' is a pull request against the wpe remote"""
        self.assertEqual(self._fields("wpe:5678"), ["pull", "", "", "wpe", "5678"])

    def test_wpe_non_numeric_falls_back_to_a_fork_spec(self):
        """'wpe:somebranch' is not wpe:<n> (non-digits), so it is a fork spec
        for a user literally named 'wpe' -- the same disambiguation
        <user>:<branch> already gets, not a second special case"""
        self.assertEqual(self._fields("wpe:somebranch"), ["user", "wpe", "somebranch", "", ""])

    def test_garbage_is_refused(self):
        self.assertIn("not a PR spec", self._refused("not-a-spec"))

    def test_bad_pull_number_is_refused(self):
        """digits followed by anything else is refused rather than silently truncated"""
        self.assertIn("not a pull request number", self._refused("1234x"))
        self.assertIn("not a pull request number", self._refused("wpe:12x"))

    def test_an_empty_half_is_refused(self):
        self.assertIn("expected <user>:<branch>", self._refused(":b"))


class TestMirrorFetch(unittest.TestCase):
    """mirror_fetch and mirror_fetch_pull, against real (local-path) git
    repositories standing in for a fork and an upstream."""

    def setUp(self):
        self._scratch = scratch_dir(prefix="wk-pr-test-")
        self.tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)
        self.env = {"WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "", "HOME": str(self.tmp)}
        self.store = Store(self.env)
        self.here = Local()

    def fetch(self, fn, *args):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            fn(self.here, self.store, Lock(self.store, self.here, Clock()), *args)
        return err.getvalue()

    def mirror_rev(self, ref):
        return _git("rev-parse", ref, cwd=self.store.mirror()).stdout.strip()

    def test_a_fork_branch_lands_under_pr_and_the_second_fetch_is_a_no_op(self):
        fork = self.tmp / "fork"
        sha = _make_repo(fork, "eng-test")
        err = self.fetch(pr.mirror_fetch, str(fork), "refs/heads/eng-test", "refs/remotes/pr/alice/WebKit/eng-test")
        self.assertIn("creating bare mirror", err)
        self.assertEqual(self.mirror_rev("refs/remotes/pr/alice/WebKit/eng-test"), sha)
        self.assertEqual(_git("config", "gc.auto", cwd=self.store.mirror()).stdout.strip(), "0")
        before = _git("count-objects", "-v", cwd=self.store.mirror()).stdout
        self.assertNotIn("creating bare mirror",
                         self.fetch(pr.mirror_fetch, str(fork), "refs/heads/eng-test", "refs/remotes/pr/alice/WebKit/eng-test"))
        self.assertEqual(_git("count-objects", "-v", cwd=self.store.mirror()).stdout, before)

    def test_a_pull_request_lands_as_refs_pull_n_head(self):
        origin = self.tmp / "origin"
        sha = _make_repo(origin, "main")
        _git("update-ref", "refs/pull/7/head", sha, cwd=origin)
        self.fetch(pr.mirror_fetch_pull, "origin", "7", (("origin", str(origin)),))
        self.assertEqual(self.mirror_rev("refs/remotes/pr/" + pr.pull_refname("origin", "7")), sha)

    def test_an_unknown_remote_is_refused_by_name(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.mirror_fetch_pull(self.here, self.store, None, "nosuchremote", "1")
        self.assertIn("no such upstream remote", err.getvalue())

    def test_a_failed_fetch_is_a_refusal_naming_the_ref(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.mirror_fetch(self.here, self.store, Lock(self.store, self.here, Clock()),
                            str(self.tmp / "nowhere"), "refs/heads/x", "refs/remotes/pr/x")
        self.assertIn("could not fetch refs/heads/x", err.getvalue())


class TestMirrorFetchIsARecorderUnderADryRun(unittest.TestCase):
    """dispatch.dry_run_is_the_recorder: mirror_fetch's git init/config/fetch all run through
    act_run, so a dry run leaves the mirror untouched even if a caller reaches this directly;
    resolved_or_planned is the one place that decides whether to, and never calls it under one."""

    def setUp(self):
        self._scratch = scratch_dir(prefix="wk-pr-dryrun-")
        self.tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)
        self.env = {"WK_STORE": str(self.tmp / "store"), "WK_LOCK_DIR": str(self.tmp / "locks"),
                    "XDG_STATE_HOME": str(self.tmp / "state"), "WK_IN_VM": "", "HOME": str(self.tmp)}
        self.store = Store(self.env)
        self.here = Fake("here")
        p = mock.patch.dict(os.environ, {"WK_DRY_RUN": "1"})
        p.start()
        self.addCleanup(p.stop)

    def test_mirror_fetch_runs_no_git_under_a_dry_run(self):
        with contextlib.redirect_stderr(io.StringIO()):
            pr.mirror_fetch(self.here, self.store, Lock(self.store, self.here, Clock()),
                            "https://example/x.git", "refs/heads/b", "refs/remotes/pr/b")
        self.assertEqual([e for e in self.here.effects if e[0] == "run"], [])

    def test_resolved_or_planned_answers_from_the_mirror_already_there(self):
        mirror = self.store.mirror()
        self.here.dirs.add(mirror)
        sha = "c" * 40
        self.here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet", "refs/x^{commit}"], out=sha + "\n")
        called = []
        got = pr.resolved_or_planned(self.here, mirror, "refs/x", "x", lambda: called.append(1))
        self.assertEqual((got, called), (sha, []))

    def test_resolved_or_planned_plans_a_fetch_it_has_not_made_yet(self):
        mirror = self.store.mirror()
        self.here.dirs.add(mirror)
        self.here.answer(["git", "-C", mirror, "rev-parse", "--verify", "--quiet"], rc=1)
        called = []
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = pr.resolved_or_planned(self.here, mirror, "refs/x", "x from y", lambda: called.append(1))
        self.assertEqual((got, called), (pr.PLANNED_COMMIT, []))
        self.assertIn("would fetch x from y into the mirror", err.getvalue())


class TestPrOpenTarget(unittest.TestCase):
    """pr_open_target: which project a branch belongs to and what it opens
    as, read off a real (local-path) git checkout -- no network, no gh."""

    def setUp(self):
        self._scratch = scratch_dir(prefix="wk-pr-open-test-")
        self.tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)

    def _tracked_branch(self, project, remote_name, fork_remote, branch, user="testuser"):
        """A working checkout with <branch> checked out tracking
        <remote_name>/main (an 'upstream' repo whose basename is <project>
        -- 'WebKit' or 'WPEWebKit', the same suffix wk_push_forks matches),
        plus a <fork_remote> remote pointing at a github fork. Mirrors how
        `wk pr rebase` leaves a branch tracking origin/main or wpe/main."""
        upstream = self.tmp / project
        _make_repo(upstream, "main")
        work = self.tmp / f"work-{rand_suffix()}"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", remote_name, str(upstream), cwd=work)
        _git("fetch", "-q", remote_name, cwd=work)
        _git("checkout", "-q", "-b", branch, f"{remote_name}/main", cwd=work)
        _git("remote", "add", fork_remote, f"https://github.com/{user}/{project}.git", cwd=work)
        return work

    def _target(self, src):
        return CMD_PR_MODULE.pr_open_target(src)

    def test_webkit_branch(self):
        """a branch tracking origin/main opens against WebKit/WebKit, head
        <fork's github user>:<branch>, pushed to the 'fork' remote"""
        work = self._tracked_branch("WebKit", "origin", "fork", "eng/my-feature")
        self.assertEqual(
            self._target(work),
            ("WebKit/WebKit", "testuser:eng/my-feature", "fork", "eng/my-feature"),
        )

    def test_wpe_branch(self):
        """a branch tracking wpe/main opens against WPEWebKit's real owner
        (WebPlatformForEmbedded, not the fork's), pushed to 'forkwpe'"""
        work = self._tracked_branch("WPEWebKit", "wpe", "forkwpe", "eng/wpe-feature")
        self.assertEqual(
            self._target(work),
            ("WebPlatformForEmbedded/WPEWebKit", "testuser:eng/wpe-feature", "forkwpe", "eng/wpe-feature"),
        )

    def _branch_tracking_its_fork(self, project, fork_remote, branch, user="testuser"):
        """What `wk pr <user>:<branch>` leaves behind: the branch tracks the
        *fork* it came from, not an upstream's main."""
        fork = self.tmp / f"{project}-fork-{rand_suffix()}"
        _make_repo(fork, branch)
        work = self.tmp / f"work-{rand_suffix()}"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        _git("remote", "add", fork_remote, str(fork), cwd=work)
        _git("fetch", "-q", fork_remote, cwd=work)
        _git("checkout", "-q", "-b", branch, f"{fork_remote}/{branch}", cwd=work)
        # The URL the real wiring records, which is what names the project.
        _git("remote", "set-url", fork_remote, f"https://github.com/{user}/{project}.git", cwd=work)
        return work

    def test_a_branch_tracking_the_fork_opens_against_the_project(self):
        """Not against the fork itself: `wk pr` leaves the branch tracking
        `fork/<branch>`, and a pull request against that is one against you."""
        work = self._branch_tracking_its_fork("WebKit", "fork", "eng/my-feature")
        self.assertEqual(
            self._target(work),
            ("WebKit/WebKit", "testuser:eng/my-feature", "fork", "eng/my-feature"),
        )

    def test_a_wpe_branch_tracking_its_fork_opens_against_wpewebkit(self):
        work = self._branch_tracking_its_fork("WPEWebKit", "forkwpe", "eng/wpe-feature")
        self.assertEqual(
            self._target(work),
            ("WebPlatformForEmbedded/WPEWebKit", "testuser:eng/wpe-feature",
             "forkwpe", "eng/wpe-feature"),
        )

    def test_refuses_on_main(self):
        """opening 'main' itself as a pull request is refused by name"""
        work = self.tmp / "on-main"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        with self.assertRaises(CMD_PR_MODULE.PrOpenError) as ctx:
            self._target(work)
        self.assertIn("cannot open 'main'", str(ctx.exception))

    def test_refuses_detached_head(self):
        """a detached HEAD has no branch to push, so it is refused by name"""
        work = self.tmp / "detached"
        _make_repo(work, "main")
        _git("checkout", "-q", "--detach", "HEAD", cwd=work)
        with self.assertRaises(CMD_PR_MODULE.PrOpenError) as ctx:
            self._target(work)
        self.assertIn("detached HEAD", str(ctx.exception))

    def test_refuses_dirty_tree(self):
        """an uncommitted change is named before anything is pushed, the
        same rule the PR-checkout form applies (cmd/pr's own header)"""
        work = self._tracked_branch("WebKit", "origin", "fork", "eng/dirty")
        (work / "untracked.txt").write_text("scratch\n")
        with self.assertRaises(CMD_PR_MODULE.PrOpenError) as ctx:
            self._target(work)
        msg = str(ctx.exception)
        self.assertIn("uncommitted changes", msg)
        self.assertIn("git", msg)

    def test_refuses_a_branch_with_no_upstream(self):
        """a fresh local branch with no tracking ref cannot be told apart
        as WebKit's or WPEWebKit's, and is refused rather than guessed at"""
        work = self.tmp / "no-upstream"
        _make_repo(work, "main")
        _git("checkout", "-q", "-b", "eng/untracked", cwd=work)
        with self.assertRaises(CMD_PR_MODULE.PrOpenError) as ctx:
            self._target(work)
        self.assertIn("no upstream", str(ctx.exception))


class TestPrOpenGhArgs(unittest.TestCase):
    """pr_open_gh_args: the exact argv 'gh pr create' gets -- what a stub gh
    actually receives in TestPrOpenRefusals-style use."""

    def _args(self, base, head, *flags):
        return CMD_PR_MODULE.pr_open_gh_args(base, head, *flags)

    def test_plain(self):
        """no flags: --repo, --head, --fill, nothing else"""
        self.assertEqual(
            self._args("WebKit/WebKit", "alice:eng/x"),
            ["--repo", "WebKit/WebKit", "--head", "alice:eng/x", "--fill"],
        )

    def test_draft_and_web_pass_through_in_order(self):
        self.assertEqual(
            self._args("WebKit/WebKit", "alice:eng/x", "--draft", "--web"),
            ["--repo", "WebKit/WebKit", "--head", "alice:eng/x", "--fill", "--draft", "--web"],
        )

    def test_unknown_flag_is_dropped(self):
        """anything but --draft/--web is not gh's business and is not passed"""
        self.assertEqual(
            self._args("WebKit/WebKit", "alice:eng/x", "--bogus"),
            ["--repo", "WebKit/WebKit", "--head", "alice:eng/x", "--fill"],
        )


class _FakeRebaseTarget:
    """A duck-typed Target: pr_rebase only ever calls src/mirror_dir/exec on
    it, so a fake answering those three, from a scripted list of Results, is
    the whole of what a unit test needs -- no container, guest or ssh driver."""

    def __init__(self, mirror, responses):
        self._mirror = mirror
        self._responses = list(responses)
        self.calls = []

    def src(self, ws):
        return "/src/WebKit"

    def mirror_dir(self):
        return self._mirror

    def exec(self, ws, argv, tty=False, timeout=None):
        self.calls.append(argv)
        return self._responses.pop(0)


class TestPrRebase(unittest.TestCase):
    """pr_rebase: fetch from the mirror when the target has one and it is
    there, the network otherwise, then rebase -- one round trip per fact,
    each through Target.exec, none of it inline bash."""

    def _run(self, mirror, responses):
        target = _FakeRebaseTarget(mirror, responses)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = CMD_PR_MODULE.pr_rebase(target, "myws")
        return rc, target.calls, err.getvalue()

    def test_fetches_from_the_mirror_when_it_is_there(self):
        rc, calls, _ = self._run("/store/git/WebKit.git", [
            Result(0),                       # test -d <mirror>
            Result(0),                       # git fetch <mirror>
            Result(0),                       # git rebase origin/main
            Result(0, "abc1234 c\n"),        # git log --oneline -1
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["test", "-d", "/store/git/WebKit.git"])
        self.assertIn("/store/git/WebKit.git", calls[1])
        self.assertIn("+refs/heads/*:refs/remotes/origin/*", calls[1])
        self.assertEqual(calls[2], ["git", "-C", "/src/WebKit", "rebase", "origin/main"])

    def test_fetches_from_origin_with_no_mirror(self):
        rc, calls, _ = self._run("", [
            Result(0),                       # git fetch origin
            Result(0),                       # git rebase origin/main
            Result(0, "abc1234 c\n"),        # git log --oneline -1
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0], ["git", "-C", "/src/WebKit", "fetch", "--prune", "--quiet", "origin"])

    def test_fetches_from_origin_when_the_mirror_is_not_there(self):
        rc, calls, _ = self._run("/store/git/WebKit.git", [
            Result(1),                       # test -d <mirror> -- not there
            Result(0),                       # git fetch origin
            Result(0),                       # git rebase origin/main
            Result(0, "abc1234 c\n"),
        ])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[1], ["git", "-C", "/src/WebKit", "fetch", "--prune", "--quiet", "origin"])

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


class TestPrOpenStatus(unittest.TestCase):
    """'wk pr open' ends with gh's own exit status: a PR gh did not create is not a success."""

    def _open(self, gh_rc):
        target = _FakeRebaseTarget("", [Result(0)])   # the push
        ran = []

        def fake_run(argv, **kw):
            ran.append(argv)
            return subprocess.CompletedProcess(argv, gh_rc if argv[:3] == ["gh", "pr", "create"] else 0)
        with mock.patch.object(CMD_PR_MODULE, "pr_open_target", return_value=("WebKit/WebKit", "me:b", "fork", "b")), \
                mock.patch.object(CMD_PR_MODULE.subprocess, "run", fake_run), contextlib.redirect_stderr(io.StringIO()):
            rc = CMD_PR_MODULE.pr_open(target, "myws", draft=False, web=False)
        self.assertEqual(ran[-1][:3], ["gh", "pr", "create"])
        return rc

    def test_gh_failing_is_the_commands_failure(self):
        self.assertEqual(self._open(1), 1)
        self.assertEqual(self._open(0), 0)


class TestPrOpenRefusals(unittest.TestCase):
    """'wk pr open', through the real dispatcher: the two refusals that
    must happen before any git or gh runs. cmd/pr declares 'sub open
    where=host needs=gh,gh-auth' rather than re-checking either itself
    (CLAUDE.md: a concern the dispatcher already owns is a bug to re-decide
    in a command, even when it decides the same) -- so what is under test
    here is the declaration wired to the framework the rest of `wk` reuses
    (cmd/push, cmd/key, cmd/bench's own 'sub ... where=host' subverbs)."""

    def test_refuses_inside_a_workspace(self):
        """where=host + in_workspace is refused by the dispatcher itself,
        before cmd/pr runs at all -- the same mechanism 'wk push status'
        and 'wk bench stage' rely on inside a workspace."""
        with fake_workspace() as ws:
            cp = ws.run("pr", "open")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("host", cp.stdout)

    def test_refuses_without_gh_login(self):
        """a stub gh that cannot call the API is refused naming 'gh auth
        login' -- check_needs('gh-auth'), the same check cmd/key relies on.
        This fires before cmd/pr looks for a workspace at all, so none of
        --draft/--web/a real name is needed to reach it."""
        with stub_path({
            "gh": '#!/bin/sh\ncase "$1 $2" in\n"api user") exit 1 ;;\nesac\nexit 0\n',
        }) as binp:
            cp = run("pr", "open", "some-workspace",
                     env={"PATH": f"{binp}:{os.environ['PATH']}"})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("gh auth login", cp.stdout)

    def test_refuses_when_the_stored_token_is_dead(self):
        """`gh auth status` exits 0 for an account whose token has expired
        or been revoked -- it answers "is an account configured", not "can
        this machine call the API" -- so gh_authenticated (lib/wk/shell.py)
        asks the API instead, and the refusal comes before the command
        starts rather than part-way through its own report."""
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
    """`git-webkit pr` inside a workspace is the one thing that has to reach
    GitHub's API, and it authenticates with GITHUB_COM_USERNAME/GITHUB_COM_TOKEN
    from its environment (webkitcorepy; the keyring is unusable in a container).
    A token in the environment is one the agent in that workspace can read, so
    the workspace holds a placeholder and the injector outside it puts the real
    token in the Authorization header.

    This drives the whole mechanism for real -- a TLS handshake against the
    injector's own leaf certificate, a requests-shaped request head over the
    wire, and a fake upstream that reports what arrived -- with no network and
    no GitHub. INJECT_PORT is pointed at that upstream, which is the one thing
    a local run cannot do by configuration.
    """

    @classmethod
    def setUpClass(cls):
        import importlib.util
        import shutil
        if not shutil.which("openssl"):
            raise unittest.SkipTest("needs the openssl CLI")
        path = REPO / "container" / "proxy" / "github-inject.py"
        spec = importlib.util.spec_from_file_location("wkinject_e2e", str(path))
        cls.m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.m)

    def _run(self, token):
        """Returns (what the upstream received, what the client got back)."""
        import asyncio
        import ssl
        import tempfile
        import unittest.mock

        m = self.m
        d = Path(tempfile.mkdtemp(prefix="wk-test-inject-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        chain = m.ensure_certs(str(d / "certs"), str(d / "ca.pem"))
        pat = d / "pat"
        if token:
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

            # The upstream leg of the injector verifies against the system
            # trust store, which cannot know about a certificate made two
            # lines ago; pointing it at this CA keeps the leg verified -- and
            # the name it verifies stays api.github.com, which is why the
            # connection is redirected by address below rather than by name.
            client_ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            m.INJECT_PORT = port
            # No standing read token here: this drives the write half, whose
            # only credential is the switch's.
            injector = m.Injector(str(pat), str(d / "read-pat"), str(d / "bz-key"),
                                  client_ctx)

            srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            srv_ctx.load_cert_chain(chain, str(d / "certs" / "leaf.key"))
            sock = str(d / "inject.sock")
            server = await asyncio.start_unix_server(injector.handle, path=sock,
                                                     ssl=srv_ctx)

            # The workspace's side: it trusts the CA (REQUESTS_CA_BUNDLE,
            # container/proxy/ensure-bridge.sh) and sends the placeholder.
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

        # The injector connects to api.github.com by name, because that is the
        # only host it ever talks to; the fake upstream is on loopback. Sending
        # the connection there by address keeps the certificate name -- and so
        # the verification -- exactly as it is in production.
        real_open = asyncio.open_connection

        async def to_loopback(host, port, **kw):
            return await real_open("127.0.0.1", port, **kw)

        with unittest.mock.patch.object(asyncio, "open_connection", to_loopback):
            got = asyncio.run(asyncio.wait_for(main(), 30))
        return seen.get("head", b""), got

    def test_the_real_token_arrives_and_the_placeholder_never_does(self):
        head, got = self._run("ghp-not-a-real-token")
        self.assertIn(b"Authorization: Bearer ghp-not-a-real-token", head)
        self.assertNotIn(b"Basic", head)
        self.assertNotIn(b"wk-injects-this", head)
        self.assertIn(b"200 OK", got)
        # The request itself is otherwise untouched.
        self.assertIn(b"GET /user HTTP/1.1", head)
        self.assertIn(b"Accept: application/vnd.github.v3+json", head)

    def test_with_the_switch_off_the_call_goes_unauthenticated(self):
        """`wk push off` removes the token file, so GitHub answers for itself:
        401 on an endpoint that needs an account. Nothing is fabricated here,
        and nothing the workspace sent is forwarded."""
        head, got = self._run("")
        self.assertNotIn(b"Authorization", head)
        self.assertNotIn(b"wk-injects-this", head)
        self.assertIn(b"GET /user HTTP/1.1", head)
        self.assertIn(b"200 OK", got)

    def test_the_workspace_is_told_to_send_that_placeholder(self):
        text = (REPO / "container" / "proxy" / "ensure-bridge.sh").read_text()
        self.assertIn("export GITHUB_COM_TOKEN=wk-injects-this", text)


@unittest.skip(
    "a real end-to-end run ('wk new wk-test-<rnd>' then 'wk pr <it> <spec>' "
    "against a local fork) needs an isolated WK_STORE, but on this machine "
    "'where=workspace' commands for a container workspace are forwarded "
    "whole into the podman VM (forward_to_vm, wk:522-596) and only a fixed "
    "list of variables crosses that ssh (WK_IN_VM/WK_DEBUG/WK_QUIET/WK_YES/WK_FORCE/"
    "WK_ROW_LABEL/WK_HOST_SELF/WK_CONFIG -- WK_STORE is not "
    "one of them), so there is no way to point the forwarded command at a "
    "scratch mirror without writing PR refs into this machine's real one. "
    "TestPrParseSpec and TestMirrorFetch above cover the same code with a "
    "real (if local-path) fork/origin/mirror instead."
)
class TestPrEndToEnd(unittest.TestCase):
    def test_new_then_pr_against_a_local_fork(self):
        pass
