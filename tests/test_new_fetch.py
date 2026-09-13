"""What a fresh workspace's checkout is: on a branch, tracking it, and as
current as the machine's mirror.

Two commands meet here, and each is driven as itself against real
(local-path) git repositories standing in for the mirror, the snapshot and a
workspace's checkout -- git takes a path as a URL, so nothing here reaches the network,
the store, or a container:

  cmd/sync   snapshot_checkout   what a published snapshot's HEAD is: a local
                                 branch of the published branch's name,
                                 tracking it, reset to it (a detached
                                 snapshot is what left every workspace's
                                 `git status` saying "HEAD detached at ...")
             ws_fetch_script     the one fetch a workspace's checkout does,
                                 which is `git fetch --all` as git has that
                                 checkout configured
  lib/store  wk_wiring_script    that configuration: where a fetch of each
                                 remote reads from, and which refs it asks for
             base_verify         what a snapshot has to be before `wk new`
                                 overlays a workspace on it
  cmd/new    new_fetch_from      where a fresh workspace can be brought up to
                                 date from, decided from what the workspace
                                 itself answered
             new_checkout_script the fast-forward creation does, and the
                                 report of where the checkout ended up
             new_freshen         the two together, with the messages -- run
                                 with `t_exec` standing in for a container
                                 exec, the way the fetch actually reaches in

Both files carry the same `functions` seam: sourced with `functions` they
define their helpers and return before doing anything. sync_workspaces, which
is what `wk sync <ws>` and `wk new` both reach, lives below that seam and is
lifted with sed the way tests/test_sync.py lifts sync_target.

Run: python3 -m unittest tests.test_new_fetch -v
"""
import os
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, bash, func_body, scratch_dir

SYNC_FUNCS = f'set -euo pipefail\ncd "{REPO}"\n. cmd/sync functions\n'
STORE_FUNCS = f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n. lib/store.sh\n'

# Nothing here may reach github.com: the wiring points the four remotes at
# their real URLs and rewrites them to a local mirror, so a fetch that ignored
# the rewrite would clone WebKit for real. Port 1 refuses at once.
OFFLINE = {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1",
           "GIT_TERMINAL_PROMPT": "0"}
NEW_FUNCS = f'set -euo pipefail\ncd "{REPO}"\n. cmd/new functions\n'

GIT_ID = ["-c", "user.email=t@example.com", "-c", "user.name=Test"]


def _lift(*funcs):
    """Functions from cmd/sync below its `functions` seam. The fetch loop and
    the one job it runs come together: par_run names the job to run it."""
    text = (REPO / "cmd" / "sync").read_text()
    return "".join("%s() {%s}\n" % (n, func_body(text, n)) for n in funcs)


def _git(*args, cwd, check=True):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, check=check)


def _commit(repo, name):
    (Path(repo) / name).write_text(f"{name}\n")
    _git("add", name, cwd=repo)
    _git(*GIT_ID, "commit", "-q", "-m", name, cwd=repo)
    return _git("rev-parse", "HEAD", cwd=repo).stdout.strip()


class MirrorFixture(unittest.TestCase):
    """A bare "GitHub" repo, a bare mirror wired the way cmd/sync wires one
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
        """What cmd/sync's `git clone "$MIRROR" "$NEW_TREE"` leaves behind,
        with origin pointed at the upstream the way wk_wiring_script does."""
        _git("clone", "-q", str(self.mirror), str(dest), cwd=self.tmp)
        _git("remote", "set-url", "origin", str(self.upstream), cwd=dest)
        return dest

    def status_line(self, tree):
        return _git("status", "-sb", cwd=tree).stdout.splitlines()[0]

    def head(self, tree, rev="HEAD"):
        return _git("rev-parse", rev, cwd=tree).stdout.strip()

    def checkout(self, tree, branch="origin/main"):
        """cmd/sync's snapshot_checkout, run for real."""
        return bash(SYNC_FUNCS + f'snapshot_checkout {str(tree)!r} {branch!r}')

    def wire(self, tree, mirror=None):
        """lib/store.sh's wk_wiring_script -- the one authority every target
        wires from -- run for real against this fixture's mirror."""
        m = str(self.mirror if mirror is None else mirror)
        cp = bash(STORE_FUNCS + f'wk_wiring_script {str(tree)!r} {m!r}')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        out = subprocess.run(["sh", "-c", cp.stdout], cwd=str(tree),
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        return cp.stdout

    def check(self, tree, mirror=None):
        """wk_wiring_check_script, the other half of the wiring, run for real."""
        m = str(self.mirror if mirror is None else mirror)
        cp = bash(STORE_FUNCS + f'wk_wiring_check_script {str(tree)!r} {m!r} skip-env')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return subprocess.run(["sh", "-c", cp.stdout], cwd=str(tree),
                              capture_output=True, text=True)

    def config(self, tree, *args):
        return _git("config", *args, cwd=tree, check=False).stdout.strip()


class TestSnapshotCheckout(MirrorFixture):
    def test_a_published_snapshot_is_on_a_branch_tracking_the_one_it_came_from(self):
        """The defect: a plain `wk new` left git detached. `git clone` leaves
        HEAD on a branch and cmd/sync's checkout is what takes it off again,
        so this is the one place that decides it -- for every workspace
        overlaid on the snapshot."""
        tree = self.clone_snapshot(self.tmp / "base1")
        cp = self.checkout(tree)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "", "nothing is printed when it worked")
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

        # cmd/sync's cp -al path: hardlink, re-point origin at the mirror,
        # fetch, re-wire, check out.
        second = self.tmp / "base2"
        subprocess.run(["cp", "-al", str(first), str(second)], check=True,
                       capture_output=True)
        _git("remote", "set-url", "origin", str(self.mirror), cwd=second)
        _git("fetch", "--all", "--prune", "-q", cwd=second)
        _git("remote", "set-url", "origin", str(self.upstream), cwd=second)
        cp = self.checkout(second)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
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
        cp = self.checkout(tree, "origin/wpe-2.46")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.status_line(tree), "## wpe-2.46...origin/wpe-2.46")

    def test_a_branch_that_is_not_remote_tracking_is_refused_by_name(self):
        """A bare `main` resolves to the checkout's own branch, and a sha to
        nothing: either would publish a snapshot with no upstream, so it is
        refused with the spelling to use instead."""
        tree = self.clone_snapshot(self.tmp / "base-bad")
        self.checkout(tree)
        for branch in ("main", self.sha1, "nosuch/branch"):
            cp = self.checkout(tree, branch)
            self.assertNotEqual(cp.returncode, 0, f"{branch}: {cp.stdout}")
            self.assertIn("<remote>/<branch>", cp.stdout)
            self.assertIn("origin/main", cp.stdout)


class TestSnapshotPublishCallSite(MirrorFixture):
    """The two lines in cmd/sync's publish that use it: the refusal is
    relayed, the half-published directory is removed, and what it says it
    checked out is read back off the tree rather than assumed."""

    def _publish(self, tree, branch):
        lines = [l.strip() for l in (REPO / "cmd" / "sync").read_text().splitlines()
                 if l.strip().startswith("_why=$(snapshot_checkout")
                 or l.strip().startswith('info "checked out on branch')]
        self.assertEqual(len(lines), 2, lines)
        setup = (f"NEW_TREE={str(tree)!r}\n"
                 f"NEW_DIR={str(Path(tree).parent / (Path(tree).name + '-dir'))!r}\n"
                 'mkdir -p "$NEW_DIR"\n'
                 f"BRANCH={branch!r}\n")
        return bash(SYNC_FUNCS + setup + "\n".join(lines) + "\n")

    def test_it_says_which_branch_the_snapshot_was_left_on(self):
        tree = self.clone_snapshot(self.tmp / "base")
        cp = self._publish(tree, "origin/main")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("checked out on branch main (tracking origin/main)", cp.stderr)

    def test_a_branch_the_mirror_does_not_carry_stops_the_publish(self):
        tree = self.clone_snapshot(self.tmp / "base")
        cp = self._publish(tree, "origin/nosuch")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("<remote>/<branch>", cp.stderr)
        self.assertFalse((self.tmp / "base-dir").exists(),
                         "the half-published snapshot directory was left behind")


class WorkspaceFixture(MirrorFixture):
    """A snapshot published from the mirror, plus a workspace checkout that
    is a copy of it -- what a container's overlay is, without the overlay."""

    def setUp(self):
        super().setUp()
        self.base = self.clone_snapshot(self.tmp / "base")
        self.wire(self.base)
        cp = self.checkout(self.base)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        self.ws = self.tmp / "ws"
        subprocess.run(["cp", "-a", str(self.base), str(self.ws)], check=True,
                       capture_output=True)

    def fetch_script(self):
        cp = bash(SYNC_FUNCS + f'ws_fetch_script {str(self.ws)!r}')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout

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
    the reference its admins refresh (targets/remote.sh, t_mirror_dir) -- is
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
        """Idempotence across a change of answer: `wk remotes --fix` after a
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


class TestSyncWorkspacesUsesTheScript(unittest.TestCase):
    """cmd/sync's sync_workspaces is what `wk sync <ws>` and `wk new` both
    reach; this is the wiring between it and ws_fetch_script -- the checkout
    the driver names, and the mirror the driver names (t_mirror_dir), which the
    report names so a run says where each workspace read from."""

    def _driven(self, mirror, answer="from=mirror"):
        """sync_workspaces, run for a workspace whose target answers <mirror>
        and whose checkout answers <answer> when the script asks it where it
        read. It lives below the `functions` seam (it drives a target, and
        reads the scope a real run parsed), so it is lifted the way
        tests/test_sync.py lifts sync_target."""
        lifted = _lift("ws_fetch_one", "sync_workspaces")
        with scratch_dir(prefix="wk-sync-wiring-") as d:
            seen = d / "script"
            # t_exec <ws> sh -c <script>: written to a file, since
            # sync_workspaces sends the fetch's own output nowhere.
            cp = bash(SYNC_FUNCS + lifted + f'''
SCOPE=ws
load_target()  {{ :; }}
ws_target()    {{ echo container; }}
ws_state()     {{ echo present; }}
t_src()        {{ echo /src/WebKit; }}
t_mirror_dir() {{ printf '%s' {mirror!r}; }}
t_exec()       {{ shift 3; printf '%s\\n' "$1" > {str(seen)!r}; printf '%s\\n' {answer!r}; }}
sync_workspaces one
''')
            out = cp.stdout + cp.stderr
            self.assertEqual(cp.returncode, 0, out)
            return seen.read_text(), out

    def test_the_script_is_one_fetch_and_the_report_names_the_mirror(self):
        """Every kind of workspace, by the path its own driver answers with
        (tests/test_mirror_path.py holds those): the fetch is `git fetch --all`
        as that checkout is configured, and the line printed for it says which
        mirror that configuration reads."""
        for mirror in ("/mirror/WebKit.git",              # container
                       "/Users/admin/WebKit.git",         # macOS guest
                       "/home/you/wk/mirror"):            # build machine
            with self.subTest(mirror=mirror):
                script, out = self._driven(mirror)
                self.assertIn("cd '/src/WebKit'", script)
                self.assertIn("git fetch --all --prune --quiet", script)
                self.assertIn(f"ok  ({mirror})", out)
                # No upstream is named, by name or by URL: which source each
                # remote reads is the checkout's own configuration.
                for remote in ("origin", "wpe", "fork", "forkwpe", "github.com"):
                    self.assertNotIn(f"get-url '{remote}'", script)
                self.assertNotIn("github.com", script)

    def test_a_target_with_no_mirror_says_the_fetch_went_to_the_upstreams(self):
        """The default is no mirror (lib/target.sh), and a build box cloning
        from its machine's reference keeps it: the fetch works, over the
        network, and the report does not claim a local read."""
        script, out = self._driven("", answer="from=github")
        self.assertIn("git fetch --all --prune --quiet", script)
        self.assertIn("over the network", out)

    def test_the_source_reported_is_the_one_the_checkout_answered_with(self):
        """The rewrite lives in the checkout, so which source a fetch read is
        the checkout's answer and not this machine's belief about it: a
        workspace that is not wired to the mirror this machine keeps says so
        and names what re-asserts the wiring."""
        script, out = self._driven("/mirror/WebKit.git", answer="from=github")
        self.assertIn("url./mirror/WebKit.git.insteadOf", script)
        self.assertIn("over the network", out)
        self.assertIn("wk remotes one --fix", out)
        self.assertNotIn("ok  (/mirror/WebKit.git)", out)

    def test_a_checkout_that_says_nothing_is_not_reported_as_a_local_read(self):
        script, out = self._driven("/mirror/WebKit.git", answer="")
        self.assertIn("did not say which source", out)


class TestWhatSyncWorkspacesReports(unittest.TestCase):
    """One line per workspace, and every one of them derived from what the
    fetch actually did: `wk sync --all` is often the only thing anybody reads
    about a machine's workspaces, and a run that reports success for a
    workspace nothing reached is worse than one that fails."""

    LIFTED = _lift("ws_fetch_one", "sync_workspaces")

    def _run(self, scope="all", state="present", exec_body="echo from=mirror",
             names="one", load=":"):
        cp = bash(SYNC_FUNCS + self.LIFTED + f'''
SCOPE={scope}
ONLY=one
load_target()  {{ {load}; }}
ws_target()    {{ echo container; }}
ws_state()     {{ echo {state}; }}
t_src()        {{ echo /src/WebKit; }}
t_mirror_dir() {{ echo /mirror/WebKit.git; }}
t_exec()       {{ {exec_body}; }}
sync_workspaces {names} && echo "rc=0" || echo "rc=$?"
''')
        return cp, cp.stdout + cp.stderr

    def test_a_fetch_says_which_mirror_it_read(self):
        """One line per workspace, and the mirror named in it is the one that
        workspace's own driver answers with -- not a word about a mirror in
        general."""
        cp, out = self._run()
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("ok  (/mirror/WebKit.git)", out)
        self.assertIn("rc=0", cp.stdout)

    def test_a_workspace_that_is_not_there_is_skipped_by_name(self):
        cp, out = self._run(scope="all", state="stopped")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("stopped -- skipped", out)

    def test_the_one_named_being_absent_is_a_refusal_not_a_skip(self):
        """`wk sync <ws>` asked for exactly that workspace: reporting a clean
        run over nothing is the report this refuses to make."""
        cp, out = self._run(scope="ws", state="absent")
        self.assertNotEqual(cp.returncode, 0, out)
        self.assertIn("workspace 'one' is not there to fetch in", out)

    def test_a_fetch_that_failed_is_counted_and_the_sweep_goes_on(self):
        """One unreachable workspace must not end a sync that has more to do,
        and it must not pass either."""
        cp, out = self._run(exec_body="return 1")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("FAILED (continuing)", out)
        self.assertIn("1 workspace(s) did not fetch", out)
        self.assertIn("rc=0", cp.stdout)

    def test_a_driver_that_will_not_load_is_counted_too(self):
        """A workspace whose target this machine cannot load is not reached
        and not skipped: it is one that did not fetch, and the sweep says so
        at the end rather than passing in silence."""
        cp, out = self._run(load="return 1")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("1 workspace(s) did not fetch", out)


class TestTheWiringCheck(MirrorFixture):
    """wk_wiring_check_script is what `wk remotes` asks of a checkout and what
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
        `wk remotes --fix` is the wiring run again."""
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

        cp = bash(STORE_FUNCS + f'wk_wiring_script {str(tree)!r} {str(self.mirror)!r}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        run = subprocess.run(["sh", "-c", cp.stdout], cwd=str(tree), env=env,
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
    """container/firstrun.sh wires the checkout from the store's own functions,
    reached through `_store_fn`. A lookup that fails and an answer of "no
    mirror on this target" wire the same remotes to github.com, so the log has
    to tell them apart: the first is a fault with a name, the second is what a
    machine keeping no mirror looks like."""

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
_store_fn() {
    case "$1" in
        mirror_in_container) %s ;;
        wk_wiring_script)    printf 'true\\n' ;;
    esac
}
"""

    def _run(self, mirror_body):
        self.assertIn("_store_fn wk_wiring_script", self.BLOCK)
        with scratch_dir(prefix="wk-firstrun-") as d:
            (d / ".git").mkdir()
            cp = bash(self.HARNESS % (repr(str(d)), mirror_body)
                      + self.BLOCK + "\nfi\n")
            self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
            return cp.stdout + cp.stderr

    def test_a_lookup_that_failed_is_a_warning_that_names_it(self):
        out = self._run("return 1")
        self.assertIn("mirror_in_container failed", out)
        self.assertIn("fetches read github.com", out)

    def test_no_mirror_on_the_target_is_stated_and_is_not_a_warning(self):
        out = self._run("printf ''")
        self.assertIn("no mirror on this target", out)
        self.assertNotIn("warning", out)
        self.assertIn("fetches read github.com", out)

    def test_a_mirror_is_named_in_the_line_that_says_what_was_wired(self):
        out = self._run("printf /mirror/WebKit.git")
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
        cp = self.checkout(second)

        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(self.status_line(second), "## main...origin/main")
        self.assertEqual(self.head(second), sha2)


class StoreFixture(MirrorFixture):
    """A $WK_STORE with published snapshots under base/<id>/, the way cmd/sync
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
            cp = self.checkout(tree)
            assert cp.returncode == 0, cp.stdout + cp.stderr
        if branch_file is not None:
            (d / "branch").write_text(branch_file + "\n")
        (d / "sha").write_text(self.head(tree) + "\n")
        return d

    def store_fn(self, call):
        return bash(STORE_FUNCS + call, env={"WK_STORE": str(self.store)})


class TestBaseVerify(StoreFixture):
    """lib/store.sh's base_verify, which `wk new` asks before overlaying a
    workspace on a snapshot and current_base asks before offering one."""

    def test_a_snapshot_on_its_branch_verifies(self):
        self.publish("20260101T000000Z")
        cp = self.store_fn("base_verify 20260101T000000Z")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout, "", "nothing is said about a good one")

    def test_a_detached_snapshot_is_refused_and_names_wk_sync(self):
        self.publish("20260101T000000Z", detached=True)
        cp = self.store_fn("base_verify 20260101T000000Z")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("is not on branch main", cp.stdout)
        self.assertIn("wk sync", cp.stdout)

    def test_a_snapshot_that_records_no_branch_is_refused(self):
        """Published before a snapshot recorded one: whether its HEAD is that
        branch cannot be known, so it is not handed out."""
        self.publish("20260101T000000Z", branch_file=None)
        cp = self.store_fn("base_verify 20260101T000000Z")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("does not record the branch", cp.stdout)

    def test_a_branch_that_tracks_nothing_is_refused(self):
        d = self.publish("20260101T000000Z")
        _git("branch", "--unset-upstream", cwd=d / "WebKit")
        cp = self.store_fn("base_verify 20260101T000000Z")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("tracking origin/main", cp.stdout)

    def test_current_base_skips_one_it_would_refuse(self):
        """`wk new` takes the newest publishable snapshot, and a detached one
        is not publishable: the older good one is what it gets, and a store
        with nothing but bad ones answers with nothing at all."""
        self.publish("20260101T000000Z")
        self.publish("20260102T000000Z", detached=True)
        cp = self.store_fn("current_base")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "20260101T000000Z")

    def test_a_store_of_only_detached_snapshots_offers_none(self):
        self.publish("20260102T000000Z", detached=True)
        cp = self.store_fn("current_base")
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertEqual(cp.stdout.strip(), "")


class TestPublishSkipsOnlyAVerifiedCurrentSnapshot(StoreFixture):
    """cmd/sync's "mirror unchanged" shortcut: a snapshot whose sha is the
    mirror's main is left alone only when base_verify accepts it. A detached
    one with the same sha is republished, or `wk new` refuses forever while
    `wk sync` reports nothing to do."""

    def _shortcut(self):
        text = (REPO / "cmd" / "sync").read_text().splitlines()
        start = next(i for i, l in enumerate(text)
                     if l.strip().startswith("PREV_ID=$(newest_complete_base"))
        end = next(i for i in range(start, len(text)) if text[i].strip() == "fi")
        lifted = "\n".join(l.strip() for l in text[start:end + 1])
        sha = self.head(self.mirror, "refs/heads/main")
        script = (f"MIRROR={str(self.mirror)!r}\n_mirror_main_sha={sha!r}\nBRANCH=origin/main\n"
                  f"probe() {{\n{lifted}\necho continued\n}}\nprobe\n")
        return bash(SYNC_FUNCS + script, env={"WK_STORE": str(self.store)})

    def test_a_verified_current_snapshot_is_left_alone(self):
        self.publish("20260101T000000Z")
        cp = self._shortcut()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("continued", cp.stdout, "the publish went on past the shortcut")

    def test_a_detached_snapshot_with_the_current_sha_is_republished(self):
        self.publish("20260101T000000Z", detached=True)
        cp = self._shortcut()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("continued", cp.stdout, "the shortcut kept a detached snapshot")


class TestNewFetchFrom(unittest.TestCase):
    """cmd/new's decision, on what the workspace itself answered."""

    def _from(self, probe):
        cp = bash(NEW_FUNCS + f'new_fetch_from {probe!r}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_a_mounted_mirror_is_fetched_from(self):
        self.assertEqual(self._from("yes"), "mirror")

    def test_no_mirror_is_not_fetched_over_the_network_at_creation(self):
        self.assertEqual(self._from("no"), "network")

    def test_nothing_answering_is_neither(self):
        """A guest is not started when `wk new` finishes making it, so
        there is nothing to run the probe in -- and no answer is not `no`."""
        self.assertEqual(self._from(""), "unreachable")
        self.assertEqual(self._from("wkdev-enter: no such container"), "unreachable")


class TestNewCheckoutScript(WorkspaceFixture):
    """cmd/new's fast-forward and its report, run against the workspace
    checkout copied off the snapshot."""

    def _run(self):
        cp = bash(NEW_FUNCS + f'new_checkout_script {str(self.ws)!r}')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = subprocess.run(["sh", "-c", cp.stdout], cwd=str(self.tmp),
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


class TestNewFreshen(WorkspaceFixture):
    """cmd/new's whole creation-time step, with `t_exec` standing in for the
    container exec it reaches the checkout through, and the log it writes
    read back. Never fatal is the property under test: a workspace exists by
    the time this runs."""

    def _freshen(self, probe, wk_dir=None):
        """new_freshen for a workspace whose probe answers <probe>. `wk` is a
        recorder in a scratch directory when one is given, so the fetch
        (`wk sync <name>`) is observed rather than run."""
        pre = NEW_FUNCS + f'''
t_src() {{ echo {str(self.ws)!r}; }}
t_mirror_dir() {{ echo /mirror/WebKit.git; }}
t_exec() {{
    shift
    case "$*" in
        *"/mirror/WebKit.git"*) echo {probe!r} ;;
        *) sh -c "$3" ;;
    esac
}}
'''
        if wk_dir:
            pre += f'WK_ROOT={str(wk_dir)!r}\n'
        return bash(pre + 'new_freshen probe-ws')

    def test_a_mirror_in_reach_is_fetched_with_wk_sync(self):
        """The fetch is `wk sync <name>` -- cmd/sync's sync_workspaces is the
        one implementation of a workspace's fetch, and `wk new` does not
        carry a second one."""
        with scratch_dir(prefix="wk-fake-root-") as fake:
            (fake / "wk").write_text(
                f'#!/bin/sh\nprintf "%s\\n" "$*" >> {str(fake / "calls")!r}\n')
            (fake / "wk").chmod(0o755)
            cp = self._freshen("yes", wk_dir=fake)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual((fake / "calls").read_text().strip(), "sync probe-ws")
        self.assertIn("is on main", cp.stdout + cp.stderr)
        self.assertIn("up to date with origin/main", cp.stdout + cp.stderr)

    def test_no_mirror_names_wk_sync_and_still_succeeds(self):
        """A guest has no mirror bind-mounted, and creation does not wait on
        GitHub -- so it says what would bring the checkout up to date, and
        the workspace is still made."""
        cp = self._freshen("no")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("no mirror in reach", out)
        self.assertIn("wk sync probe-ws", out)
        self.assertIn("is on main", out)

    def test_a_workspace_nothing_answers_in_is_reported_not_fetched(self):
        cp = self._freshen("")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("nothing to run in 'probe-ws' yet", out)
        self.assertIn("wk sync probe-ws", out)

    def _freshen_dying(self, on):
        """new_freshen with a `t_exec` that dies the way a real driver's
        does -- targets/vm.sh calls `die` when the guest is not running, and
        a guest is not running when `wk new` has just cloned it. <on> picks
        which call dies: the mirror probe, or the checkout read after it."""
        probe_body = ('die "\'probe-ws\' is not running (wk vm start probe-ws)"'
                      if on == "probe" else "echo no")
        read_body = ('die "\'probe-ws\' is not running (wk vm start probe-ws)"'
                     if on == "read" else 'sh -c "$3"')
        return bash(NEW_FUNCS + f'''
t_src() {{ echo {str(self.ws)!r}; }}
t_mirror_dir() {{ echo /mirror/WebKit.git; }}
t_exec() {{
    shift
    case "$*" in
        *"/mirror/WebKit.git"*) {probe_body} ;;
        *) {read_body} ;;
    esac
}}
new_freshen probe-ws
''')

    def test_a_target_that_cannot_be_reached_at_all_is_not_fatal(self):
        """The defect: `wk new --target vm` died here with nothing printed.
        The probe runs under `set -e` with pipefail, so t_exec's `die`
        status propagated out of the assignment and ended the driver --
        creation reported "failed" at stage `fetch` and the log said why
        nowhere. No answer is the `unreachable` arm, not a failure."""
        cp = self._freshen_dying("probe")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("nothing to run in 'probe-ws' yet", out)

    def test_a_checkout_that_cannot_be_read_is_not_fatal_either(self):
        """The same hazard one line down: the workspace answered the probe
        and then went away (a guest stopped, a container killed). There is
        nothing to say about the checkout, and still a made workspace."""
        cp = self._freshen_dying("read")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("could not read the checkout in 'probe-ws'", out)

    def test_a_detached_checkout_names_what_puts_it_on_a_branch(self):
        _git("checkout", "-q", "--detach", "HEAD", cwd=self.ws)
        cp = self._freshen("no")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("is not on a branch", out)
        self.assertIn("git checkout main", out)

    def test_a_branch_with_no_upstream_names_wk_remotes_fix(self):
        _git("checkout", "-q", "-b", "eng/local", cwd=self.ws)
        cp = self._freshen("no")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("tracks nothing", out)
        self.assertIn("wk remotes probe-ws --fix", out)


REMOTES = (REPO / "cmd" / "remotes").read_text()

FIX_HARNESS = f"""set -euo pipefail
cd "{REPO}"
. lib/common.sh
t_src() {{ printf '/src/WebKit'; }}
wk_gitwebkit_setup_script() {{ printf 'setup script for %s' "$1"; }}
t_exec() {{ ws="$1"; shift; printf 'EXEC %s: %s\\n' "$ws" "$*" >&2
            printf '%s\\r\\n' "${{WK_TEST_OUT:-setup=ok}}"
            return "${{WK_TEST_RC:-0}}"; }}
"""


def _fix_gitwebkit(kind="container", out="setup=ok", rc="0"):
    script = (FIX_HARNESS
              + "fix_gitwebkit() {%s}\n" % func_body(REMOTES, "fix_gitwebkit")
              + 'WK_TARGET_KIND=%s\nrc=0\nfix_gitwebkit demo || rc=$?\n'
                'printf "rc=%%s changes=%%s\\n" "$rc" "$WK_CHANGES"\n' % kind)
    return bash(script, env={"WK_TEST_OUT": out, "WK_TEST_RC": rc})


class TestFixRunsGitWebkitSetup(unittest.TestCase):
    """`git-webkit setup` runs once, at a container's first start or in a
    guest's golden base, and a GitHub request that fails there leaves a
    checkout whose `git-webkit pr` prompts or refuses. `wk remotes <ws> --fix`
    is what re-runs it: the script is lib/store.sh's one generator, it reports
    its own `setup=` line, and a failure names the remedy rather than leaving
    the workspace wired but unusable."""

    def test_the_setup_script_is_run_against_the_checkout_and_ok_is_a_change(self):
        cp = _fix_gitwebkit()
        out = cp.stdout + cp.stderr
        self.assertIn("EXEC demo: sh -c setup script for /src/WebKit", out)
        self.assertIn("git-webkit is set up in 'demo'", out)
        self.assertIn("rc=0 changes=1", cp.stdout)

    def test_a_checkout_already_set_up_is_reported_and_is_not_a_change(self):
        cp = _fix_gitwebkit(out="setup=already")
        self.assertIn("git-webkit: setup=already", cp.stdout + cp.stderr)
        self.assertIn("rc=0 changes=0", cp.stdout)

    def test_a_failure_is_non_zero_and_names_both_remedies(self):
        cp = _fix_gitwebkit(out="setup=failed", rc="1")
        out = cp.stdout + cp.stderr
        self.assertIn("did not finish in 'demo' (setup=failed)", out)
        self.assertIn("wk push on", out)
        self.assertIn("wk remotes demo --fix", out)
        self.assertIn("rc=1", cp.stdout)

    def test_a_machine_of_the_persons_own_is_left_to_its_own_setup(self):
        """The placeholder credential and the injector's CA reach a container
        and a guest; a remote target is a checkout on a shared machine, with
        the person's own credentials."""
        cp = _fix_gitwebkit(kind="remote")
        self.assertNotIn("EXEC", cp.stdout + cp.stderr)
        self.assertIn("rc=0 changes=0", cp.stdout)

    def test_a_containers_exec_already_carries_the_injected_credential(self):
        """Why the setup runs through plain `t_exec` and not a second bridge:
        every command a container's driver execs is wrapped in it already."""
        wrap = (REPO / "targets" / "container.sh").read_text()
        self.assertIn("container/proxy/ensure-bridge.sh",
                      wrap.split("_wrap_cmd() {")[1].split("\n}")[0])


class TestWhatFixConverges(unittest.TestCase):
    """One `--fix` path, and the two halves it asserts are independent: a
    checkout can be wired right and still have no `git-webkit setup` behind it,
    which is the state a first start that lost its GitHub request leaves."""

    def _one(self, wired, fix="1"):
        script = (f'set -euo pipefail\ncd "{REPO}"\n. lib/common.sh\n'
                  'load_target() { :; }\nws_target() { :; }\n'
                  'ws_state() { echo running; }\n'
                  'check_one() { echo CHECK; return %s; }\n'
                  'fix_one() { echo FIXWIRING; }\n'
                  'fix_gitwebkit() { echo GITWEBKIT; }\n'
                  'FIX=%s\n' % (wired, fix)
                  + "one() {%s}\n" % func_body(REMOTES, "one")
                  + 'rc=0\none demo || rc=$?\nprintf "rc=%s\\n" "$rc"\n')
        return bash(script)

    def test_a_wired_checkout_is_rewired_and_gets_the_setup_step(self):
        """The check reads the remotes; the wiring also carries config the
        check does not read (wk_fetch_config), so --fix re-asserts it whatever
        the verdict, and then runs the setup."""
        cp = self._one("0")
        self.assertIn("FIXWIRING", cp.stdout)
        self.assertIn("GITWEBKIT", cp.stdout)
        self.assertIn("rc=0", cp.stdout)

    def test_a_wrongly_wired_checkout_gets_both(self):
        cp = self._one("1")
        self.assertIn("FIXWIRING", cp.stdout)
        self.assertIn("GITWEBKIT", cp.stdout)

    def test_without_fix_nothing_is_run_and_the_verdict_is_the_check(self):
        cp = self._one("1", fix="")
        self.assertNotIn("FIXWIRING", cp.stdout)
        self.assertNotIn("GITWEBKIT", cp.stdout)
        self.assertIn("rc=1", cp.stdout)


class TestTheAliasIsResolvedByTheResolver(MirrorFixture):
    """The other half of the check: whether an ssh alias reaches github.com.
    Which file holds the Host block is ssh's business -- a container's own
    ~/.ssh/config is one `Include /secrets/ssh_config` line
    (container/firstrun.sh) -- so a check that reads the file itself calls
    every fork alias unresolved and `wk remotes --fix` re-wires on every run.
    `ssh -G` is the resolver, and it honours Include.

    ssh reads the per-user config from the passwd entry rather than $HOME, so
    these drive it through the `-F` file `core.sshCommand` names, which is
    how a build box is wired (wk_wiring_script).
    """

    def aliases(self):
        cp = bash(STORE_FUNCS + "wk_push_forks | awk 'NF {print $3}'")
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout.split()

    def ssh_config(self, name, body):
        p = self.tmp / name
        p.write_text(body)
        p.chmod(0o600)
        return p

    def resolved(self, tree, config):
        _git("config", "core.sshCommand", f"ssh -F {config}", cwd=tree)
        cp = bash(STORE_FUNCS
                  + f'wk_wiring_check_script {str(tree)!r} {str(self.mirror)!r}')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return subprocess.run(["sh", "-c", cp.stdout], cwd=str(tree),
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
        cp = bash(STORE_FUNCS
                  + f'wk_wiring_check_script /nowhere {str(self.mirror)!r}')
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertNotIn(".ssh/config", cp.stdout)
        self.assertIn("ssh -G", cp.stdout)


if __name__ == "__main__":
    unittest.main()
