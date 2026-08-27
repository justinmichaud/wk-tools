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

Run: python3 -m unittest tests.test_pr_workflow -v
"""
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, WK, bash, rand_suffix, scratch_dir

PRELUDE = f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n. lib/store.sh\n'


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
