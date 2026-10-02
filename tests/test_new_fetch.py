"""What a fresh workspace's checkout is: on a branch, tracking it, and as
current as the machine's mirror.

Two commands meet here, and each is driven as itself against real
(local-path) git repositories standing in for the mirror, the snapshot and a
workspace's checkout -- git takes a path as a URL, so nothing here reaches the network,
the store, or a container:

  wk.sync    snapshot_checkout   what a published snapshot's HEAD is: a local
                                 branch of the published branch's name,
                                 tracking it, reset to it (a detached
                                 snapshot is what left every workspace's
                                 `git status` saying "HEAD detached at ...")
             fetch_script        the one fetch a workspace's checkout does,
                                 which is `git fetch --all` as git has that
                                 checkout configured
  wk.git     wiring_script       that configuration: where a fetch of each
                                 remote reads from, and which refs it asks for
  wk.store   Bases.verify        what a snapshot has to be before `wk new`
                                 overlays a workspace on it
  wk.workspace checkout_script   the fast-forward creation does, and the
                                 report of where the checkout ended up (the
                                 rest of creation's fetch is
                                 tests/test_wk_workspace.py's)

What `wk sync` does with them across a fleet is tests/test_sync.py's.

Run: python3 -m unittest tests.test_new_fetch -v
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.support import REPO, bash, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import git, secrets, sync, targets, workspace  # noqa: E402
from wk.clock import Clock  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.store import Bases, Store  # noqa: E402


def _forks():
    """secrets.FORKS, the fork table's one home, as (remote, repo, alias) tuples."""
    return list(secrets.FORKS)

# Nothing here may reach github.com: the wiring points the four remotes at
# their real URLs and rewrites them to a local mirror, so a fetch that ignored
# the rewrite would clone WebKit for real. Port 1 refuses at once.
OFFLINE = {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
           "GIT_TERMINAL_PROMPT": "0"}

GIT_ID = ["-c", "user.email=t@example.com", "-c", "user.name=Test"]


def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def _commit(repo, name):
    (Path(repo) / name).write_text(f"{name}\n")
    _git("add", name, cwd=repo)
    _git(*GIT_ID, "commit", "-q", "-m", name, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


class MirrorFixture(unittest.TestCase):
    """A bare "GitHub" repo, a bare mirror wired the way `wk sync` wires one
    (origin's `main` under the mirror's own refs/heads, `wpe` namespaced),
    and helpers to publish snapshots from it."""

    def setUp(self):
        self._scratch = scratch_dir(prefix="wk-new-fetch-")
        self.tmp = self._scratch.__enter__()
        self.addCleanup(self._scratch.__exit__, None, None, None)

        self.upstream = self.tmp / "github.git"
        _git("init", "-q", "--bare", "-b", "main", str(self.upstream), cwd=self.tmp)
        self.seed = self.tmp / "seed"
        _git("clone", "-q", str(self.upstream), str(self.seed), cwd=self.tmp)
        self.sha1 = _commit(self.seed, "a")
        _git("push", "-q", "origin", "main", cwd=self.seed)

        self.mirror = self.tmp / "mirror.git"
        _git("init", "-q", "--bare", str(self.mirror), cwd=self.tmp)
        _git("remote", "add", "origin", str(self.upstream), cwd=self.mirror)
        _git("config", "--unset-all", "remote.origin.fetch", cwd=self.mirror, check=False)
        _git("config", "--add", "remote.origin.fetch",
             "+refs/heads/main:refs/heads/main", cwd=self.mirror)
        _git("config", "remote.origin.tagOpt", "--no-tags", cwd=self.mirror)
        _git("symbolic-ref", "HEAD", "refs/heads/main", cwd=self.mirror)
        self.mirror_fetch()

    def mirror_fetch(self):
        _git("fetch", "--prune", "-q", "origin", cwd=self.mirror)

    def advance_upstream(self, name="b"):
        """One more commit on the upstream's main, and into the mirror."""
        sha = _commit(self.seed, name)
        _git("push", "-q", "origin", "main", cwd=self.seed)
        self.mirror_fetch()
        return sha

    def clone_snapshot(self, dest):
        """What the publish's `git clone <mirror> <tree>` leaves behind,
        with origin pointed at the upstream the way git.wiring_script does."""
        _git("clone", "-q", str(self.mirror), str(dest), cwd=self.tmp)
        _git("remote", "set-url", "origin", str(self.upstream), cwd=dest)
        return dest

    def status_line(self, tree):
        return _git("status", "-sb", cwd=tree).stdout.splitlines()[0]

    def head(self, tree, rev="HEAD"):
        return _git("rev-parse", rev, cwd=tree).stdout.strip()

    def checkout(self, tree, branch="origin/main"):
        """lib/wk/sync.py's snapshot_checkout, run for real: why it refused, or ""."""
        reg = targets.Registry(REPO, env=dict(os.environ, WK_MIRROR_BRANCHES="main"))
        return sync.Sync(reg, Clock(), None, "here").snapshot_checkout(str(tree), branch)

    def wire(self, tree, mirror=None, branches=None):
        """lib/wk/git.py's wiring_script -- the one authority every target
        wires from -- run for real against this fixture's mirror."""
        m = str(self.mirror if mirror is None else mirror)
        script = git.wiring_script(str(tree), m, _forks(), (branches or "main").split())
        out = subprocess.run(["sh", "-c", script], cwd=str(tree),
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        return script

    def check(self, tree, mirror=None, branches=None):
        """wiring_check_script, the other half of the wiring, run for real."""
        m = str(self.mirror if mirror is None else mirror)
        script = git.wiring_check_script(str(tree), m, _forks(), (branches or "main").split(), skip_env=True)
        return subprocess.run(["sh", "-c", script], cwd=str(tree),
                              capture_output=True, text=True)

    def config(self, tree, *args):
        return _git("config", *args, cwd=tree, check=False).stdout.strip()


class TestSnapshotCheckout(MirrorFixture):
    def test_a_published_snapshot_is_on_a_branch_tracking_the_one_it_came_from(self):
        """The defect: a plain `wk new` left git detached. `git clone` leaves
        HEAD on a branch and the publish's checkout is what takes it off again,
        so this is the one place that decides it -- for every workspace
        overlaid on the snapshot."""
        tree = self.clone_snapshot(self.tmp / "base1")
        self.assertEqual(self.checkout(tree), "")
        self.assertEqual(self.status_line(tree), "## main...origin/main")
        self.assertEqual(
            _git("symbolic-ref", "--short", "HEAD", cwd=tree).stdout.strip(), "main")
        self.assertEqual(
            _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                 cwd=tree).stdout.strip(), "origin/main")
        self.assertEqual(self.head(tree), self.sha1)

    def test_a_detached_checkout_is_what_it_replaces(self):
        """Contrast, so the assertion above is about this change and not
        about git: the `checkout --detach origin/main` it replaces leaves
        HEAD on no branch and `@{u}` unresolvable."""
        tree = self.clone_snapshot(self.tmp / "base-detached")
        _git("checkout", "-q", "--detach", "origin/main", cwd=tree)
        self.assertEqual(self.status_line(tree), "## HEAD (no branch)")
        self.assertNotEqual(
            _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}",
                 cwd=tree, check=False).returncode, 0)

    def test_the_next_snapshot_resets_the_branch_forward(self):
        """A snapshot is hardlinked from the last one, so it inherits that
        one's local `main`. Without `-B` resetting it, the branch keeps the
        sha the first clone was taken at while origin/main moves on -- which
        is what `git checkout main` in a workspace would land on."""
        first = self.clone_snapshot(self.tmp / "base1")
        self.checkout(first)
        sha2 = self.advance_upstream()

        # The publish's cp -al path: hardlink, re-point origin at the mirror,
        # fetch, re-wire, check out.
        second = self.tmp / "base2"
        subprocess.run(["cp", "-al", str(first), str(second)], check=True,
                       capture_output=True)
        _git("remote", "set-url", "origin", str(self.mirror), cwd=second)
        _git("fetch", "--all", "--prune", "-q", cwd=second)
        _git("remote", "set-url", "origin", str(self.upstream), cwd=second)
        self.assertEqual(self.checkout(second), "")
        self.assertEqual(self.head(second, "refs/heads/main"), sha2,
                         "the snapshot's own branch did not follow the mirror")
        self.assertEqual(self.status_line(second), "## main...origin/main")

    def test_a_release_branch_keeps_its_own_name(self):
        """WK_BRANCH publishes another branch, and the workspace starts on a
        local branch of that name tracking it -- not on `main`."""
        _git("branch", "-q", "wpe-2.46", cwd=self.seed)
        _git("push", "-q", "origin", "wpe-2.46", cwd=self.seed)
        _git("config", "--add", "remote.origin.fetch",
             "+refs/heads/wpe-2.46:refs/heads/wpe-2.46", cwd=self.mirror)
        self.mirror_fetch()
        tree = self.clone_snapshot(self.tmp / "base-release")
        self.assertEqual(self.checkout(tree, "origin/wpe-2.46"), "")
        self.assertEqual(self.status_line(tree), "## wpe-2.46...origin/wpe-2.46")

    def test_a_branch_that_is_not_remote_tracking_is_refused_by_name(self):
        """A bare `main` resolves to the checkout's own branch, and a sha to
        nothing: either would publish a snapshot with no upstream, so it is
        refused with the spelling to use instead."""
        tree = self.clone_snapshot(self.tmp / "base-bad")
        self.checkout(tree)
        for branch in ("main", self.sha1, "nosuch/branch"):
            why = self.checkout(tree, branch)
            self.assertIn("<remote>/<branch>", why, branch)
            self.assertIn("origin/main", why)


class WorkspaceFixture(MirrorFixture):
    """A snapshot published from the mirror, plus a workspace checkout that
    is a copy of it -- what a container's overlay is, without the overlay."""

    def setUp(self):
        super().setUp()
        self.base = self.clone_snapshot(self.tmp / "base")
        self.wire(self.base)
        assert self.checkout(self.base) == ""
        self.ws = self.tmp / "ws"
        subprocess.run(["cp", "-a", str(self.base), str(self.ws)], check=True,
                       capture_output=True)

    def fetch_script(self):
        return sync.fetch_script(str(self.ws), "")

    def run_fetch(self):
        return subprocess.run(["sh", "-c", self.fetch_script()], cwd=str(self.tmp),
                              capture_output=True, text=True,
                              env={**os.environ, **OFFLINE})


class TestWsFetchScript(WorkspaceFixture):
    """The fetch itself, run for real in a checkout wired the way every target
    wires one: `git fetch --all --prune`, against remotes whose URLs are
    github.com and whose fetches are rewritten to this machine's mirror."""

    def test_it_reads_the_mirror_although_the_remotes_name_github(self):
        """The rewrite, end to end: the environment cannot reach github.com
        (OFFLINE), the remote URLs are github.com's, and origin/main still
        arrives -- from the mirror."""
        sha2 = self.advance_upstream()
        cp = self.run_fetch()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.head(self.ws, "refs/remotes/origin/main"), sha2)
        self.assertEqual(self.config(self.ws, "remote.origin.url"),
                         "https://github.com/WebKit/WebKit.git",
                         "git-webkit reads this to find the project; the rewrite "
                         "must not have replaced it")

    def test_a_person_typing_git_fetch_origin_gets_the_same_read(self):
        """Not just `wk sync`: the defect was a bare `git fetch origin` in the
        workspace taking half a minute over the network."""
        sha2 = self.advance_upstream()
        out = _git("fetch", "origin", cwd=self.ws, check=False)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(self.head(self.ws, "refs/remotes/origin/main"), sha2)

    def test_no_tags_are_followed(self):
        """Measured cost, not a preference: following tags re-negotiates every
        tag the source has. The mirror here has one and the workspace ends
        with none."""
        _git("tag", "some-release", self.sha1, cwd=self.seed)
        _git("push", "-q", "origin", "some-release", cwd=self.seed)
        _git("fetch", "-q", "--tags", "origin", "refs/heads/main:refs/heads/main",
             cwd=self.mirror)
        self.advance_upstream()
        _git("tag", "-d", "some-release", cwd=self.ws, check=False)

        cp = self.run_fetch()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(_git("for-each-ref", "refs/tags", cwd=self.ws).stdout, "")

    def test_origin_is_narrowed_to_the_branches_the_mirror_carries(self):
        """A remote-tracking ref per branch of WebKit/WebKit (~920 of them) is
        what git's default refspec writes, and what made the fetch slow."""
        for extra in ("safari-1-branch", "safari-2-branch"):
            _git("branch", "-q", extra, cwd=self.seed)
        _git("push", "-q", "origin", "safari-1-branch", "safari-2-branch", cwd=self.seed)
        _git("fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/heads/*",
             cwd=self.mirror)

        cp = self.run_fetch()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        refs = _git("for-each-ref", "--format=%(refname)", "refs/remotes/origin",
                    cwd=self.ws).stdout.split()
        self.assertIn("refs/remotes/origin/main", refs)
        self.assertNotIn("refs/remotes/origin/safari-1-branch", refs)

    def test_a_ref_the_mirror_cannot_answer_fails_the_script(self):
        """The defect: `git fetch --all` said `fatal: couldn't find remote
        ref`, the script exited 0 on the `from=` line after it, and `wk new`
        reported the fetch ok in a workspace whose `git-webkit setup` had
        died on the same ref."""
        _git("config", "--add", "remote.origin.fetch",
             "+refs/heads/webkitglib/9.9:refs/remotes/origin/webkitglib/9.9",
             cwd=self.ws)
        cp = self.run_fetch()
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("couldn't find remote ref", cp.stderr)
        self.assertIn("from=mirror", cp.stdout,
                      "which source it read is still reported")

    def test_it_is_one_fetch_of_every_remote_git_has(self):
        """No second refspec list in the script: what is asked for lives in the
        checkout's own config, so `wk sync` and a person's `git fetch --all`
        are the same fetch."""
        script = self.fetch_script()
        self.assertIn("git fetch --all --prune --quiet", script)
        for word in ("refs/heads", "refs/remotes", "--no-tags", "github.com"):
            self.assertNotIn(word, script, script)


class TestWiringWithNoMirror(MirrorFixture):
    """A checkout on a machine that keeps no mirror -- a build box cloning from
    the reference its admins refresh (Remote.mirror_dir) -- is
    wired to the upstreams themselves, and still never asks origin for more
    than the branches this tooling carries."""

    def test_no_fetch_is_rewritten_and_origin_is_still_narrowed(self):
        """The push rewrite stays: which deploy key ssh offers a fork does not
        depend on whether the machine keeps a mirror."""
        tree = self.clone_snapshot(self.tmp / "no-mirror")
        self.wire(tree, mirror="")
        self.assertEqual(self.config(tree, "--get-regexp", r"^url\..*\.insteadof$"), "")
        self.assertEqual(
            _git("remote", "get-url", "--push", "fork", cwd=tree).stdout.strip(),
            "git@github-webkit:justinmichaud/WebKit.git")
        self.assertEqual(self.config(tree, "--get-all", "remote.origin.fetch"),
                         "+refs/heads/main:refs/remotes/origin/main")
        self.assertEqual(self.config(tree, "--get-all", "remote.wpe.fetch"),
                         "+refs/heads/*:refs/remotes/wpe/*")
        self.assertEqual(self.config(tree, "remote.wpe.tagOpt"), "--no-tags")

    def test_re_wiring_it_with_a_mirror_leaves_one_rewrite_per_remote(self):
        """Idempotence across a change of answer: `wk sync --fix` after a
        machine grows a mirror must not leave the old rewrite beside the new
        one, which is two sources claiming the same URL."""
        tree = self.clone_snapshot(self.tmp / "regrown")
        self.wire(tree, mirror="/gone/WebKit.git")
        self.wire(tree)
        self.assertEqual(
            self.config(tree, "--get-all", f"url.{self.mirror}.insteadOf").split(),
            ["https://github.com/WebKit/WebKit.git",
             "https://github.com/WebPlatformForEmbedded/WPEWebKit.git",
             "https://github.com/justinmichaud/WebKit.git",
             "https://github.com/justinmichaud/WPEWebKit.git"])
        self.assertEqual(self.config(tree, "--get-regexp", r"url\./gone/"), "")
        self.assertEqual(self.config(tree, "--get-all", "remote.origin.fetch"),
                         "+refs/heads/main:refs/remotes/origin/main")


class TestTheWiringCheck(MirrorFixture):
    """wk_wiring_check_script is what `wk sync` reads back from a checkout and what
    `--fix` re-asserts against: it has to pass on a freshly wired one and name
    each fault on a checkout wired before this -- every workspace on the fleet
    is one of those until it is fixed."""

    def test_a_freshly_wired_checkout_passes(self):
        tree = self.clone_snapshot(self.tmp / "wired")
        self.wire(tree)
        out = self.check(tree)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_the_url_the_remote_records_is_read_from_config_not_from_git_remote(self):
        """`git remote get-url` applies the rewrite and would call every remote
        wrong; `git config remote.<r>.url` is the recorded value, and the one
        git-webkit reads."""
        tree = self.clone_snapshot(self.tmp / "wired2")
        self.wire(tree)
        self.assertEqual(
            _git("remote", "get-url", "origin", cwd=tree).stdout.strip(),
            str(self.mirror), "the rewrite is what a fetch resolves to")
        out = self.check(tree)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_a_checkout_wired_before_this_is_named_fault_by_fault(self):
        tree = self.clone_snapshot(self.tmp / "old")
        self.wire(tree)
        _git("config", "--replace-all", "remote.origin.fetch",
             "+refs/heads/*:refs/remotes/origin/*", cwd=tree)
        _git("config", "--unset", "remote.wpe.tagOpt", cwd=tree)
        _git("config", "--remove-section", f"url.{self.mirror}", cwd=tree)

        out = self.check(tree)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("origin asks for +refs/heads/*:refs/remotes/origin/*", out.stdout)
        self.assertIn("wpe follows tags", out.stdout)
        for remote in ("origin", "wpe", "fork", "forkwpe"):
            self.assertIn(f"problem: {remote} is not rewritten to {self.mirror}",
                          out.stdout)

    def test_a_mirror_without_a_branch_this_tree_declares_is_a_fault_of_its_own(self):
        """Declaring a branch (an image configuration's CFG_BRANCH) wires
        every workspace to ask origin for it; the mirror carries it only
        once it has been refreshed. Between the two, `git fetch --all` in
        the workspace -- which is the fetch `git-webkit setup` does -- dies
        on that one ref, so the checkout is named for what it is missing."""
        tree = self.clone_snapshot(self.tmp / "gap")
        self.wire(tree, branches="main webkitglib/9.9")
        out = self.check(tree, branches="main webkitglib/9.9")
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn(f"the mirror {self.mirror} carries no refs/heads/webkitglib/9.9",
                      out.stdout)
        self.assertIn("wk sync --mirror", out.stdout)
        self.assertEqual(out.stdout.count("problem:"), 1,
                         "the branch the mirror does carry is not a fault")

    def test_a_checkout_that_trusts_ctime_is_a_fault(self):
        tree = self.clone_snapshot(self.tmp / "ctime")
        self.wire(tree)
        _git("config", "--unset", "core.trustctime", cwd=tree)
        out = self.check(tree)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn("problem: git trusts ctime here", out.stdout)

    def test_a_checkout_with_no_mirror_is_checked_against_the_upstreams(self):
        """No rewrite is expected of it, and origin is still narrowed."""
        tree = self.clone_snapshot(self.tmp / "no-mirror-check")
        self.wire(tree, mirror="")
        out = self.check(tree, mirror="")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


class TestHowAForkIsPushedTo(MirrorFixture):
    """Two constraints at once. ssh picks a fork's deploy key by host alias, so
    a push has to resolve to `git@github-webkit:...`; and git-webkit's
    install-hooks reads `git config --get-regexp 'remote.+url'` and takes any
    host that is not github.com for a GitHub instance of its own, whose
    credentials it then hunts for in a keyring -- which is what stops
    `git-webkit setup --defaults` dead. So a fork records only its github.com
    URL and the alias is a `url.<alias>.pushInsteadOf` rewrite, which git
    applies only to a remote that has no explicit pushurl of its own."""

    ALIAS = "git@github-webkit:justinmichaud/WebKit.git"
    RECORDED = "https://github.com/justinmichaud/WebKit.git"

    def test_the_recorded_url_is_github_com_and_the_push_resolves_to_the_alias(self):
        tree = self.clone_snapshot(self.tmp / "push-wired")
        self.wire(tree)
        self.assertEqual(self.config(tree, "--get", "remote.fork.url"),
                         self.RECORDED)
        self.assertEqual(self.config(tree, "--get", "remote.fork.pushurl"), "",
                         "an explicit pushurl is what makes git skip the rewrite")
        self.assertEqual(
            _git("remote", "get-url", "--push", "fork", cwd=tree).stdout.strip(),
            self.ALIAS, "git applies pushInsteadOf when it resolves a push")
        out = self.check(tree)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_no_remote_url_names_a_host_git_webkit_would_read_as_another_github(self):
        tree = self.clone_snapshot(self.tmp / "push-hosts")
        self.wire(tree)
        rows = _git("config", "--get-regexp", "remote.+url", cwd=tree).stdout.splitlines()
        self.assertTrue(rows)
        for row in rows:
            url = row.split(" ", 1)[1]
            if url.startswith("no-push://"):
                continue
            self.assertRegex(url, r"^(https://github\.com/|git@github\.com:)", row)

    def test_a_checkout_carrying_the_alias_in_its_push_url_is_named_and_converged(self):
        """Every checkout on the fleet wired before this carries it, and
        `wk sync --fix` is the wiring run again."""
        tree = self.clone_snapshot(self.tmp / "push-old")
        self.wire(tree)
        _git("config", "remote.fork.pushurl", self.ALIAS, cwd=tree)
        _git("config", "--remove-section", f"url.{self.ALIAS}", cwd=tree)

        out = self.check(tree)
        self.assertNotEqual(out.returncode, 0, out.stdout)
        self.assertIn(f"problem: fork records {self.ALIAS} as a push URL",
                      out.stdout)

        self.wire(tree)
        self.assertEqual(self.config(tree, "--get", "remote.fork.pushurl"), "")
        self.assertEqual(
            _git("remote", "get-url", "--push", "fork", cwd=tree).stdout.strip(),
            self.ALIAS)
        out = self.check(tree)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)


class TestTheStaleRewritesTheWiringClearsFirst(MirrorFixture):
    """The wiring rewrites URLs through `url.<base>.insteadOf`, so a mirror
    that moves leaves a section pointing at a path that is gone. Every local
    one goes before the current ones are written; a rewrite the person set in
    their own global config is theirs."""

    def test_local_rewrites_go_and_a_global_one_stays(self):
        tree = self.clone_snapshot(self.tmp / "stale")
        gitconfig = self.tmp / "global-gitconfig"
        gitconfig.write_text(
            "[url \"/somewhere/else.git\"]\n\tinsteadOf = https://example.invalid/x.git\n")
        env = dict(os.environ, GIT_CONFIG_GLOBAL=str(gitconfig))

        for stale in ("/gone/one.git", "/gone/two.git"):
            _git("config", "--local", "--add", f"url.{stale}.insteadOf",
                 "https://github.com/WebKit/WebKit.git", cwd=tree)
        _git("config", "--local", "url.git@old-alias:x/y.git.pushInsteadOf",
             "git@github.com:x/y.git", cwd=tree)

        script = git.wiring_script(str(tree), str(self.mirror), _forks(), ["main"])
        run = subprocess.run(["sh", "-c", script], cwd=str(tree), env=env,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

        local = subprocess.run(
            ["git", "config", "--local", "--name-only", "--get-regexp", r"^url\."],
            cwd=str(tree), env=env, capture_output=True, text=True).stdout
        for gone in ("/gone/one.git", "/gone/two.git", "old-alias"):
            self.assertNotIn(gone, local)
        self.assertIn(str(self.mirror), local)
        self.assertEqual(
            subprocess.run(["git", "config", "--global", "--get",
                            "url./somewhere/else.git.insteadOf"],
                           cwd=str(tree), env=env, capture_output=True,
                           text=True).stdout.strip(),
            "https://example.invalid/x.git")


class TestWhatFirstRunSaysAboutTheMirror(unittest.TestCase):
    """container/firstrun.sh wires the checkout from `python3 -m wk.git
    wiring-script` and the mirror the target hands it in WK_MIRROR. A wiring
    that fails and an answer of "no mirror on this target" both leave fetches
    reading github.com, so the log has to tell them apart: the first is a fault
    with a name, the second is what a machine keeping no mirror looks like."""

    # The wiring half of the block, taken from the file and run: the half below
    # it is `git-webkit setup`, which needs the injector.
    _FIRSTRUN = (REPO / "container" / "firstrun.sh").read_text()
    BLOCK = _FIRSTRUN[_FIRSTRUN.index('if [ -d "$SRC/.git" ]'):
                      _FIRSTRUN.index("    # Through ensure-bridge.sh")]

    HARNESS = """
set -u
SRC=%s
log()  { printf '[firstrun] %%s\\n' "$*"; }
warn() { printf '[firstrun] warning: %%s\\n' "$*"; }
_git_py() { %s; }
"""

    def _run(self, wiring_body, mirror=""):
        self.assertIn("_git_py wiring-script", self.BLOCK)
        with scratch_dir(prefix="wk-firstrun-") as d:
            (d / ".git").mkdir()
            cp = bash(self.HARNESS % (repr(str(d)), wiring_body)
                      + self.BLOCK + "\nfi\n", env={"WK_MIRROR": mirror} if mirror else None)
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            return cp.stdout + cp.stderr

    def test_a_wiring_that_failed_is_a_warning_that_names_it(self):
        out = self._run("return 1")
        self.assertIn("wk.git wiring-script failed", out)
        self.assertIn("could not wire the checkout's remotes", out)

    def test_no_mirror_on_the_target_is_stated_and_is_not_a_warning(self):
        out = self._run("printf 'true\\n'")
        self.assertIn("no mirror on this target", out)
        self.assertNotIn("warning", out)
        self.assertIn("fetches read github.com", out)

    def test_a_mirror_is_named_in_the_line_that_says_what_was_wired(self):
        out = self._run("printf 'true\\n'", mirror="/mirror/WebKit.git")
        self.assertIn("fetches read /mirror/WebKit.git", out)
        self.assertNotIn("warning", out)


class TestPublishingOverADetachedSnapshot(MirrorFixture):
    """The defect, at its root: `wk new` on moose kept leaving HEAD detached
    because the snapshots it built from were published before a snapshot was
    checked out onto a branch, and every later snapshot is a hardlinked copy of
    the one before -- so the detached HEAD is inherited, publish after publish,
    and every workspace overlaid on one starts detached. Publishing over one
    converges it."""

    def test_the_publish_puts_the_hardlinked_copy_back_on_its_branch(self):
        first = self.clone_snapshot(self.tmp / "base1")
        self.wire(first)
        _git("checkout", "-q", "--detach", "origin/main", cwd=first)
        self.assertEqual(self.status_line(first), "## HEAD (no branch)")

        sha2 = self.advance_upstream()
        second = self.tmp / "base2"
        subprocess.run(["cp", "-al", str(first), str(second)], check=True,
                       capture_output=True)
        self.wire(second)
        _git("fetch", "--all", "--prune", "-q", cwd=second)
        self.assertEqual(self.checkout(second), "")
        self.assertEqual(self.status_line(second), "## main...origin/main")
        self.assertEqual(self.head(second), sha2)


class StoreFixture(MirrorFixture):
    """A $WK_STORE with published snapshots under base/<id>/, the way `wk sync`
    leaves them: the tree, the `branch` it was published from, and the `sha`
    completion marker written last."""

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        (self.store / "base").mkdir(parents=True)

    def publish(self, bid, detached=False, branch_file="origin/main"):
        d = self.store / "base" / bid
        d.mkdir()
        tree = self.clone_snapshot(d / "WebKit")
        self.wire(tree)
        if detached:
            _git("checkout", "-q", "--detach", "origin/main", cwd=tree)
        else:
            assert self.checkout(tree) == ""
        if branch_file is not None:
            (d / "branch").write_text(branch_file + "\n")
        (d / "sha").write_text(self.head(tree) + "\n")
        return d

    def bases(self):
        return Bases(Store({"WK_STORE": str(self.store)}), Local())


class TestBaseVerify(StoreFixture):
    """Bases.verify, which `wk new` asks before overlaying a workspace on a
    snapshot and Bases.current asks before offering one."""

    def test_a_snapshot_on_its_branch_verifies(self):
        self.publish("20260101T000000Z")
        self.assertEqual(self.bases().verify("20260101T000000Z"), "", "nothing is said about a good one")

    def test_a_detached_snapshot_is_refused_and_names_wk_sync(self):
        self.publish("20260101T000000Z", detached=True)
        why = self.bases().verify("20260101T000000Z")
        self.assertIn("is not on branch main", why)
        self.assertIn("wk sync", why)

    def test_a_snapshot_that_records_no_branch_is_refused(self):
        """Published before a snapshot recorded one: whether its HEAD is that
        branch cannot be known, so it is not handed out."""
        self.publish("20260101T000000Z", branch_file=None)
        self.assertIn("does not record the branch", self.bases().verify("20260101T000000Z"))

    def test_a_branch_that_tracks_nothing_is_refused(self):
        d = self.publish("20260101T000000Z")
        _git("branch", "--unset-upstream", cwd=d / "WebKit")
        self.assertIn("tracking origin/main", self.bases().verify("20260101T000000Z"))

    def test_current_base_skips_one_it_would_refuse(self):
        """`wk new` takes the newest publishable snapshot, and a detached one
        is not publishable: the older good one is what it gets, and a store
        with nothing but bad ones answers with nothing at all."""
        self.publish("20260101T000000Z")
        self.publish("20260102T000000Z", detached=True)
        self.assertEqual(self.bases().current(), "20260101T000000Z")

    def test_a_store_of_only_detached_snapshots_offers_none(self):
        self.publish("20260102T000000Z", detached=True)
        self.assertEqual(self.bases().current(), "")


class TestAHardLinkedSnapshotStaysClean(MirrorFixture):
    """The publish hard-links the last snapshot, and link() moves the ctime of every file -- in that snapshot and in
    every workspace overlaid on it. A wired checkout does not trust ctime, so its index still vouches for each file."""

    def link_every_file(self, tree):
        dest = self.tmp / "links"
        dest.mkdir()
        for i, f in enumerate(p for p in Path(tree).iterdir() if p.is_file()):
            os.link(f, dest / str(i))

    def stat_dirty(self, tree, *config):
        return _git(*config, "diff-files", "--name-only", cwd=tree).stdout.split()

    def test_linking_the_files_dirties_nothing_in_a_wired_checkout(self):
        tree = self.clone_snapshot(self.tmp / "linked")
        self.wire(tree)
        _git("update-index", "--refresh", "-q", cwd=tree, check=False)
        time.sleep(1.1)   # git compares ctime to the second
        self.link_every_file(tree)
        self.assertEqual(self.stat_dirty(tree), [])
        self.assertEqual(self.stat_dirty(tree, "-c", "core.trustctime=true"), ["a"],
                         "the contrast: trusting ctime, the same link stat-dirties the file")


class TestNewCheckoutScript(WorkspaceFixture):
    """`wk new`'s fast-forward and its report, run against the workspace
    checkout copied off the snapshot."""

    def _run(self):
        out = subprocess.run(["sh", "-c", workspace.checkout_script(str(self.ws))], cwd=str(self.tmp),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return dict(line.split("=", 1) for line in out.stdout.split())

    def test_a_snapshot_behind_the_mirror_is_fast_forwarded_onto_it(self):
        sha2 = self.advance_upstream()
        self.run_fetch()
        got = self._run()
        self.assertEqual(got["branch"], "main")
        self.assertEqual(got["upstream"], "origin/main")
        self.assertEqual(got["behind"], "1")
        self.assertEqual(got["moved"], "1")
        self.assertEqual(self.head(self.ws), sha2)
        self.assertEqual(
            _git("status", "--porcelain", cwd=self.ws).stdout, "",
            "the fast-forward left the tree dirty")

    def test_a_current_checkout_moves_nothing_and_says_so(self):
        got = self._run()
        self.assertEqual(got["behind"], "0")
        self.assertNotIn("moved", got)
        self.assertEqual(self.head(self.ws), self.sha1)

    def test_the_index_is_refreshed_before_the_person_runs_git_status(self):
        """The checkout is a copy of the snapshot, so no entry's stat data matches its file until a status
        re-reads it: creation pays that, even when nothing moved."""
        self.assertNotEqual(_git("diff-files", "--name-only", cwd=self.ws).stdout, "")
        self._run()
        self.assertEqual(_git("diff-files", "--name-only", cwd=self.ws).stdout, "")

    def test_a_detached_checkout_reports_the_sha_and_nothing_else(self):
        _git("checkout", "-q", "--detach", "HEAD", cwd=self.ws)
        got = self._run()
        self.assertIn("detached", got)
        self.assertNotIn("branch", got)

    def test_a_branch_with_no_upstream_stops_there(self):
        _git("checkout", "-q", "-b", "eng/local", cwd=self.ws)
        got = self._run()
        self.assertEqual(got["branch"], "eng/local")
        self.assertNotIn("upstream", got)

    def test_a_diverged_branch_is_left_where_it_is(self):
        """`git merge --ff-only` and not a reset: a commit the upstream does
        not have is never discarded by creation."""
        self.advance_upstream()
        self.run_fetch()
        mine = _commit(self.ws, "mine")
        got = self._run()
        self.assertEqual(got["moved"], "refused")
        self.assertEqual(self.head(self.ws), mine)


class TestTheAliasIsResolvedByTheResolver(MirrorFixture):
    """The other half of the check: whether an ssh alias reaches github.com.
    Which file holds the Host block is ssh's business -- a container's own
    ~/.ssh/config is one `Include /secrets/ssh_config` line
    (container/firstrun.sh) -- so a check that reads the file itself calls
    every fork alias unresolved and `wk sync --fix` re-wires on every run.
    `ssh -G` is the resolver, and it honours Include.

    ssh reads the per-user config from the passwd entry rather than $HOME, so
    these drive it through the `-F` file `core.sshCommand` names, which is
    how a build box is wired (git.wiring_script).
    """

    def aliases(self):
        return [alias for _, _, alias in _forks()]

    def ssh_config(self, name, body):
        p = self.tmp / name
        p.write_text(body)
        p.chmod(0o600)
        return p

    def resolved(self, tree, config):
        _git("config", "core.sshCommand", f"ssh -F {config}", cwd=tree)
        script = git.wiring_check_script(str(tree), str(self.mirror), _forks(), ["main"])
        return subprocess.run(["sh", "-c", script], cwd=str(tree),
                              capture_output=True, text=True)

    def test_a_config_that_only_includes_the_host_blocks_resolves(self):
        tree = self.clone_snapshot(self.tmp / "alias-include")
        self.wire(tree)
        inc = self.ssh_config("included-ssh-config", "".join(
            f"Host {a}\n    HostName github.com\n" for a in self.aliases()))
        out = self.resolved(tree, self.ssh_config("outer-ssh-config",
                                                  f"Include {inc}\n"))
        self.assertEqual(0, out.returncode, out.stdout + out.stderr)

    def test_a_config_that_names_no_alias_names_each_one_as_a_problem(self):
        tree = self.clone_snapshot(self.tmp / "alias-nothing")
        self.wire(tree)
        out = self.resolved(tree, self.ssh_config("empty-ssh-config", ""))
        self.assertNotEqual(0, out.returncode, out.stdout)
        for alias in self.aliases():
            with self.subTest(alias=alias):
                self.assertIn(f"problem: the ssh alias {alias} resolves to "
                              f"{alias}, not github.com", out.stdout)

    def test_the_check_reads_no_ssh_config_file_of_its_own(self):
        """One resolver, and it is ssh's: a second reader of the file is what
        cannot see an Include."""
        script = git.wiring_check_script("/nowhere", str(self.mirror), _forks(), ["main"])
        self.assertNotIn(".ssh/config", script)
        self.assertIn("ssh -G", script)


if __name__ == "__main__":
    unittest.main()
