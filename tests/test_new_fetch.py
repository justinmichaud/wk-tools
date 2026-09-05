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
             ws_fetch_script     the one fetch a workspace's checkout does --
                                 the mirror when it is mounted in there,
                                 the upstreams themselves when it is not
             net_refspecs        the refs asked of an upstream directly
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
import subprocess
import unittest
from pathlib import Path

from tests.support import REPO, bash, scratch_dir

SYNC_FUNCS = f'set -euo pipefail\ncd "{REPO}"\n. cmd/sync functions\n'
NEW_FUNCS = f'set -euo pipefail\ncd "{REPO}"\n. cmd/new functions\n'

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
        lines = [l for l in (REPO / "cmd" / "sync").read_text().splitlines()
                 if l.startswith("_why=$(snapshot_checkout")
                 or l.startswith('info "checked out on branch')]
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
        cp = self.checkout(self.base)
        assert cp.returncode == 0, cp.stdout + cp.stderr
        self.ws = self.tmp / "ws"
        subprocess.run(["cp", "-a", str(self.base), str(self.ws)], check=True,
                       capture_output=True)

    def fetch_script(self, mirror):
        cp = bash(SYNC_FUNCS
                  + f'ws_fetch_script {str(self.ws)!r} {str(mirror)!r}')
        assert cp.returncode == 0, cp.stdout + cp.stderr
        return cp.stdout

    def run_fetch(self, mirror):
        script = self.fetch_script(mirror)
        return subprocess.run(["sh", "-c", script], cwd=str(self.tmp),
                              capture_output=True, text=True)


class TestWsFetchScript(WorkspaceFixture):
    def test_the_mirror_is_the_only_thing_fetched_from_when_it_is_there(self):
        """The mirror arm, driven for real: the workspace's own remotes are
        pointed at paths that do not exist, so a fetch over any of them
        would fail -- and origin/main still arrives."""
        sha2 = self.advance_upstream()
        for remote in ("origin", "wpe", "fork", "forkwpe"):
            _git("remote", "remove", remote, cwd=self.ws, check=False)
            _git("remote", "add", remote, str(self.tmp / "gone.git"), cwd=self.ws)

        cp = self.run_fetch(self.mirror)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("from=mirror", cp.stdout)
        self.assertNotIn("from=remotes", cp.stdout)
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

        cp = self.run_fetch(self.mirror)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(_git("for-each-ref", "refs/tags", cwd=self.ws).stdout, "")

    def test_without_a_mirror_it_asks_the_upstreams_it_has(self):
        """The other arm: no mirror mounted, so each remote the checkout
        actually has is fetched -- and the ones it does not have are not
        named at all, which is what would fail the whole fetch."""
        sha2 = self.advance_upstream()
        for remote in ("wpe", "fork", "forkwpe"):
            _git("remote", "remove", remote, cwd=self.ws, check=False)

        cp = self.run_fetch(self.tmp / "no-such-mirror.git")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("from=remotes", cp.stdout)
        self.assertEqual(self.head(self.ws, "refs/remotes/origin/main"), sha2)

    def test_the_network_arm_asks_for_the_refs_the_mirror_carries(self):
        """Parity between the two arms: origin is narrowed to the branches
        the mirror carries, so a workspace fetch never writes a
        remote-tracking ref per branch of WebKit/WebKit (~920 of them)."""
        for extra in ("safari-1-branch", "safari-2-branch"):
            _git("branch", "-q", extra, cwd=self.seed)
        _git("push", "-q", "origin", "safari-1-branch", "safari-2-branch", cwd=self.seed)
        for remote in ("wpe", "fork", "forkwpe"):
            _git("remote", "remove", remote, cwd=self.ws, check=False)

        cp = self.run_fetch(self.tmp / "no-such-mirror.git")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        refs = _git("for-each-ref", "--format=%(refname)", "refs/remotes/origin",
                    cwd=self.ws).stdout.split()
        self.assertNotIn("refs/remotes/origin/safari-1-branch", refs)
        self.assertIn("refs/remotes/origin/main", refs)


class TestNetRefspecs(unittest.TestCase):
    def _specs(self, remote, env=None):
        cp = bash(SYNC_FUNCS + f'net_refspecs {remote!r}', env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.split()

    def test_origin_is_narrowed_to_the_mirrored_branches(self):
        self.assertEqual(self._specs("origin"),
                         ["+refs/heads/main:refs/remotes/origin/main"])

    def test_wk_mirror_branches_is_the_one_list(self):
        self.assertEqual(
            self._specs("origin", env={"WK_MIRROR_BRANCHES": "main wpe-2.46"}),
            ["+refs/heads/main:refs/remotes/origin/main",
             "+refs/heads/wpe-2.46:refs/remotes/origin/wpe-2.46"])

    def test_another_upstream_is_namespaced_on_this_side(self):
        self.assertEqual(self._specs("wpe"),
                         ["+refs/heads/*:refs/remotes/wpe/*"])


class TestSyncWorkspacesUsesTheScript(unittest.TestCase):
    """cmd/sync's sync_workspaces is what `wk sync <ws>` and `wk new` both
    reach; this is the wiring between it and ws_fetch_script -- the checkout
    the driver names, and the mirror the driver names (t_mirror_dir), which is
    a different path for each kind of workspace."""

    def _driven(self, mirror):
        """sync_workspaces, run for a workspace whose target answers <mirror>.
        It lives below the `functions` seam (it drives a target, and reads the
        scope a real run parsed), so it is lifted the way tests/test_sync.py
        lifts sync_target."""
        lifted = subprocess.run(
            ["sed", "-n", "/^sync_workspaces()/,/^}/p", str(REPO / "cmd" / "sync")],
            capture_output=True, text=True).stdout
        self.assertTrue(lifted.strip(), "sync_workspaces not found in cmd/sync")
        with scratch_dir(prefix="wk-sync-wiring-") as d:
            seen = d / "script"
            # t_exec <ws> sh -c <script>: written to a file, since
            # sync_workspaces reads this function's stdout as the fetch's
            # own report and discards its stderr.
            cp = bash(SYNC_FUNCS + lifted + f'''
SCOPE=ws
load_target()  {{ :; }}
ws_target()    {{ echo container; }}
ws_state()     {{ echo present; }}
t_src()        {{ echo /src/WebKit; }}
t_mirror_dir() {{ printf '%s' {mirror!r}; }}
t_exec()       {{ shift 3; printf '%s\\n' "$1" > {str(seen)!r}; echo from=mirror; }}
sync_workspaces one
''')
            out = cp.stdout + cp.stderr
            self.assertEqual(cp.returncode, 0, out)
            return seen.read_text(), out

    def test_it_hands_the_script_the_mirror_the_driver_names(self):
        """Every kind of workspace, by the path its own driver answers with
        (tests/test_mirror_path.py holds those): the fetch is against that
        path, and no upstream is named at all when it is there -- the four
        network fetches are what a guest was paying before."""
        for mirror in ("/mirror/WebKit.git",              # container
                       "/Users/admin/WebKit.git",         # macOS guest
                       "/home/you/wk/mirror"):            # build machine
            with self.subTest(mirror=mirror):
                script, out = self._driven(mirror)
                self.assertIn("cd '/src/WebKit'", script)
                self.assertIn(f"[ -d '{mirror}' ]", script)
                self.assertIn(f"git fetch --no-tags --prune --quiet '{mirror}'", script)
                self.assertIn("ok  (from the mirror)", out)
                # The mirror carries every upstream, so its arm ends the
                # fetch: nothing reaches github.com, by name or by URL.
                before = script.split("echo from=mirror")[0]
                for remote in ("origin", "wpe", "fork", "forkwpe", "github.com"):
                    self.assertNotIn(f"get-url '{remote}'", before)
                    self.assertNotIn("github.com", script)

    def test_a_target_with_no_mirror_gets_no_mirror_arm(self):
        """The default is no mirror (lib/target.sh), and a `[ -d '' ]` test
        against it would read as a mirror that is merely absent."""
        script, out = self._driven("")
        self.assertNotIn("[ -d", script)
        self.assertIn("get-url 'origin'", script)


class TestWhatSyncWorkspacesReports(unittest.TestCase):
    """One line per workspace, and every one of them derived from what the
    fetch actually did: `wk sync --all` is often the only thing anybody reads
    about a machine's workspaces, and a run that reports success for a
    workspace nothing reached is worse than one that fails."""

    LIFTED = subprocess.run(
        ["sed", "-n", "/^sync_workspaces()/,/^}/p", str(REPO / "cmd" / "sync")],
        capture_output=True, text=True).stdout

    def _run(self, scope="all", state="present", exec_body='echo from=mirror',
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

    def test_a_fetch_that_used_no_mirror_says_so_and_says_what_fixes_it(self):
        """The network arm is four fetches of four upstreams; it works, and a
        person is owed the reason it happened -- a guest cloned from a base
        built before its mirror existed is the case."""
        cp, out = self._run(exec_body="echo from=remotes")
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("ok  (over the network: no mirror in this workspace", out)
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
        self.run_fetch(self.mirror)
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
        self.run_fetch(self.mirror)
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
        self.assertIn("wk sync --tools", out)
        self.assertIn("git checkout main", out)

    def test_a_branch_with_no_upstream_names_wk_remotes_fix(self):
        _git("checkout", "-q", "-b", "eng/local", cwd=self.ws)
        cp = self._freshen("no")
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("tracks nothing", out)
        self.assertIn("wk remotes probe-ws --fix", out)


if __name__ == "__main__":
    unittest.main()
