"""`wk pr` / `wk new --pr`: the spec parser and the mirror fetches underneath
them (lib/store.sh's pr_parse_spec, mirror_fetch_pr, mirror_fetch_pull).

The unit tests below source lib/common.sh and lib/store.sh directly (the
`bash -c '. lib/store.sh'` idiom cmd/selftest itself uses to lift a function
out of a file) and run against temporary git repositories standing in for a
fork, an upstream, and the mirror -- git accepts a plain path as a URL, so no
network and no real WK_STORE is ever touched. WK_LOCK_DIR is pointed at a
scratch directory too: mirror_fetch_pr/mirror_fetch_pull take the real
'store' lock name (the same one `wk sync` takes), and without this a test run
here would contend with a real `wk sync` on this machine, or vice versa.

The end-to-end path -- a real `wk new`, then `wk pr <it> <spec>` against a
local fork -- is not exercised here; see
TestPrEndToEnd.test_new_then_pr_against_a_local_fork for why.

'wk pr open' -- the fifth form, which pushes a branch to its fork and opens
it with `gh pr create` -- is covered further down by TestPrOpenTarget and
TestPrOpenGhArgs (pr_open_target/pr_open_gh_args, lifted out of cmd/pr the
same way _lift lifts a function from admin/wk-card-priv in
tests/test_wifi_seed.py, and run against a real, if local-path, git repo:
no network and no gh) and by TestPrOpenRefusals (the two refusals that
happen before either of those ever runs, through the real dispatcher).

Run: python3 -m unittest tests.test_pr_workflow -v
"""
import os
import shlex
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WK, bash, fake_workspace, rand_suffix, run, scratch_dir, stub_path

PRELUDE = f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n. lib/store.sh\n'

CMD_PR = REPO / "cmd" / "pr"


def _lift(path, func):
    """A function's body, sed'd out of a shell file -- see tests/test_wifi_seed.py's
    _lift, which this mirrors. pr_open_target/pr_open_gh_args are written as
    plain functions in cmd/pr precisely so they can be lifted and called
    directly, against a temporary repo or a stub gh, without running the
    rest of the file (which needs a real workspace)."""
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"{func} not found in {path}"
    return text


# lib/store.sh's real wk_remotes/wk_push_forks, unmodified: pr_open_target
# never fetches or pushes over them (it only reads remote *names* it is
# handed and URLs already configured in the test's own local-path repo), so
# there is nothing here for a fake to stand in for, unlike TestMirrorFetch's
# wk_remotes override.
OPEN_PRELUDE = (
    f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n. lib/store.sh\n'
    + _lift(CMD_PR, "pr_open_target")
    + "\n"
    + _lift(CMD_PR, "pr_open_gh_args")
    + "\n"
)


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


class TestPrParseSpec(unittest.TestCase):
    """pr_parse_spec maps the three spellings 'wk pr' accepts."""

    def _fields(self, spec):
        cp = bash(PRELUDE + (
            f'pr_parse_spec {spec!r}\n'
            'printf "%s|%s|%s|%s|%s" "$PR_KIND" "$PR_USER" "$PR_BRANCH" "$PR_REMOTE" "$PR_N"\n'
        ))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip().split("|")

    def test_user_branch(self):
        """'user:branch' parses to PR_KIND=user with PR_USER/PR_BRANCH set"""
        kind, user, branch, remote, n = self._fields("alice:eng/frame-fix")
        self.assertEqual((kind, user, branch, remote, n), ("user", "alice", "eng/frame-fix", "", ""))

    def test_bare_number(self):
        """a bare number parses to PR_KIND=pull against origin (WebKit/WebKit)"""
        kind, user, branch, remote, n = self._fields("1234")
        self.assertEqual((kind, user, branch, remote, n), ("pull", "", "", "origin", "1234"))

    def test_wpe_number(self):
        """'wpe:<n>' parses to PR_KIND=pull against the wpe remote"""
        kind, user, branch, remote, n = self._fields("wpe:5678")
        self.assertEqual((kind, user, branch, remote, n), ("pull", "", "", "wpe", "5678"))

    def test_wpe_non_numeric_falls_back_to_a_fork_spec(self):
        """'wpe:somebranch' is not wpe:<n> (non-digits), so it is a fork spec
        for a user literally named 'wpe' -- the same disambiguation
        <user>:<branch> already gets, not a second special case"""
        kind, user, branch, remote, n = self._fields("wpe:somebranch")
        self.assertEqual((kind, user, branch, remote, n), ("user", "wpe", "somebranch", "", ""))

    def test_garbage_is_refused(self):
        """a spec with no colon and not all digits is refused by name"""
        cp = bash(PRELUDE + 'pr_parse_spec "not-a-spec" 2>&1; echo "exit=$?"')
        self.assertIn("not a PR spec", cp.stdout)

    def test_bad_pull_number_is_refused(self):
        """digits followed by anything else is refused rather than silently truncated"""
        cp = bash(PRELUDE + 'pr_parse_spec "1234x" 2>&1; echo "exit=$?"')
        self.assertIn("not a pull request number", cp.stdout)


class TestMirrorFetch(unittest.TestCase):
    """mirror_fetch_pr and mirror_fetch_pull, against real (local-path) git
    repositories standing in for a fork and an upstream."""

    def setUp(self):
        self._scratch = scratch_dir(prefix="wk-pr-test-")
        self.tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)
        self.store = self.tmp / "store"
        self.locks = self.tmp / "locks"
        self.store.mkdir()
        self.locks.mkdir()

    def _env(self, extra=None):
        e = {"WK_STORE": str(self.store), "WK_LOCK_DIR": str(self.locks)}
        if extra:
            e.update(extra)
        return e

    def test_mirror_fetch_pr_lands_the_ref_and_is_a_no_op_the_second_time(self):
        """mirror_fetch_pr lands a fork branch under refs/remotes/pr/... and a
        second call fetches no new objects"""
        fork = self.tmp / "fork"
        sha = _make_repo(fork, "eng-test")

        script = PRELUDE + f'''
mirror_fetch_pr {str(fork)!r} eng-test alice/WebKit/eng-test >/dev/null
git -C "$(wk_mirror)" rev-parse refs/remotes/pr/alice/WebKit/eng-test
before=$(git -C "$(wk_mirror)" count-objects -v)
mirror_fetch_pr {str(fork)!r} eng-test alice/WebKit/eng-test >/dev/null
after=$(git -C "$(wk_mirror)" count-objects -v)
if [ "$before" = "$after" ]; then echo NOOP; else echo "CHANGED:$before//$after"; fi
'''
        cp = bash(script, env=self._env())
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = cp.stdout.strip().splitlines()
        self.assertEqual(lines[0], sha, "the mirror's ref did not land at the fork's head")
        self.assertEqual(lines[1], "NOOP", f"a second fetch was not a no-op: {lines[1]}")

    def test_mirror_fetch_pull_lands_refs_pull_n_head(self):
        """mirror_fetch_pull fetches refs/pull/<n>/head from the named
        upstream remote into refs/remotes/pr/<remote>/<n>"""
        origin = self.tmp / "origin"
        sha = _make_repo(origin, "main")
        _git("update-ref", "refs/pull/7/head", sha, cwd=origin)

        # wk_remotes is redefined after sourcing store.sh, the same way a
        # test lifts any other function -- 'origin' now means this local
        # repo rather than the real upstream, and nothing else changes.
        script = PRELUDE + f'''
wk_remotes() {{ printf "origin %s\\nwpe %s\\n" {str(origin)!r} {str(origin)!r}; }}
mirror_fetch_pull origin 7 >/dev/null
git -C "$(wk_mirror)" rev-parse refs/remotes/pr/origin/7
'''
        cp = bash(script, env=self._env())
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), sha)

    def test_mirror_fetch_pull_unknown_remote_is_refused(self):
        """mirror_fetch_pull against a remote wk_remotes does not know is refused by name"""
        cp = bash(PRELUDE + 'mirror_fetch_pull nosuchremote 1 2>&1; echo "exit=$?"',
                   env=self._env())
        self.assertIn("no such upstream remote", cp.stdout)


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
        cp = bash(OPEN_PRELUDE + f'pr_open_target {shlex.quote(str(src))}\n')
        return cp

    def test_webkit_branch(self):
        """a branch tracking origin/main opens against WebKit/WebKit, head
        <fork's github user>:<branch>, pushed to the 'fork' remote"""
        work = self._tracked_branch("WebKit", "origin", "fork", "eng/my-feature")
        cp = self._target(work)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(
            cp.stdout.strip().split("\t"),
            ["WebKit/WebKit", "testuser:eng/my-feature", "fork", "eng/my-feature"],
        )

    def test_wpe_branch(self):
        """a branch tracking wpe/main opens against WPEWebKit's real owner
        (WebPlatformForEmbedded, not the fork's), pushed to 'forkwpe'"""
        work = self._tracked_branch("WPEWebKit", "wpe", "forkwpe", "eng/wpe-feature")
        cp = self._target(work)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(
            cp.stdout.strip().split("\t"),
            ["WebPlatformForEmbedded/WPEWebKit", "testuser:eng/wpe-feature", "forkwpe", "eng/wpe-feature"],
        )

    def test_refuses_on_main(self):
        """opening 'main' itself as a pull request is refused by name"""
        work = self.tmp / "on-main"
        work.mkdir()
        _git("init", "-q", "-b", "main", cwd=work)
        cp = self._target(work)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("cannot open 'main'", cp.stderr)

    def test_refuses_detached_head(self):
        """a detached HEAD has no branch to push, so it is refused by name"""
        work = self.tmp / "detached"
        _make_repo(work, "main")
        _git("checkout", "-q", "--detach", "HEAD", cwd=work)
        cp = self._target(work)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("detached HEAD", cp.stderr)

    def test_refuses_dirty_tree(self):
        """an uncommitted change is named before anything is pushed, the
        same rule the PR-checkout form applies (cmd/pr's own header)"""
        work = self._tracked_branch("WebKit", "origin", "fork", "eng/dirty")
        (work / "untracked.txt").write_text("scratch\n")
        cp = self._target(work)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("uncommitted changes", cp.stderr)
        self.assertIn("git", cp.stderr)

    def test_refuses_a_branch_with_no_upstream(self):
        """a fresh local branch with no tracking ref cannot be told apart
        as WebKit's or WPEWebKit's, and is refused rather than guessed at"""
        work = self.tmp / "no-upstream"
        _make_repo(work, "main")
        _git("checkout", "-q", "-b", "eng/untracked", cwd=work)
        cp = self._target(work)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no upstream", cp.stderr)


class TestPrOpenGhArgs(unittest.TestCase):
    """pr_open_gh_args: the exact argv 'gh pr create' gets, one token per
    line -- what a stub gh actually receives in TestPrOpenRefusals-style use."""

    def _args(self, base, head, *flags):
        script = OPEN_PRELUDE + 'pr_open_gh_args {} {} {}\n'.format(
            shlex.quote(base), shlex.quote(head),
            " ".join(shlex.quote(f) for f in flags),
        )
        cp = bash(script)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.splitlines()

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
        this machine call the API" -- so gh_authenticated asks the API
        instead (lib/common.sh), and the refusal comes before the command
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


@unittest.skip(
    "a real end-to-end run ('wk new wk-test-<rnd>' then 'wk pr <it> <spec>' "
    "against a local fork) needs an isolated WK_STORE, but on this machine "
    "'where=workspace' commands for a container workspace are forwarded "
    "whole into the podman VM (forward_to_vm, wk:522-596) and only a fixed "
    "list of variables crosses that ssh (WK_IN_VM/WK_DEBUG/WK_YES/WK_FORCE/"
    "WK_EXPECT_TREE/WK_ROW_LABEL/WK_HOST_SELF/WK_CONFIG -- WK_STORE is not "
    "one of them), so there is no way to point the forwarded command at a "
    "scratch mirror without writing PR refs into this machine's real one. "
    "TestPrParseSpec and TestMirrorFetch above cover the same code with a "
    "real (if local-path) fork/origin/mirror instead."
)
class TestPrEndToEnd(unittest.TestCase):
    def test_new_then_pr_against_a_local_fork(self):
        pass
