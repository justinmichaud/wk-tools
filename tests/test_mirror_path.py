"""Where each machine keeps its mirror, what it carries, and how a snapshot, a workspace and a build read it: `wk sync`,
`wk build` and `wk pr` typed into worlds whose git runs for real."""
import os
import sys
import unittest
from pathlib import Path

from tests import test_pr_workflow, test_sync, test_wk_build
from tests.fakes import JobDriver
from tests.support import REPO, GitMirror, git_run
from tests.test_new_fetch import BoxFlow, WorkspaceFlow, real
from tests.test_wk_workspace import said

GUEST = "/Volumes/wk-mirror/mirror/WebKit.git"


class TestEachPlaceNamesItsMirror(test_sync.Flow):
    """A workspace's fetch reads the mirror of the place it is on, and its row names it."""

    def row(self, w, *argv):
        rc, err = self.sync(*argv, w=w)
        self.assertEqual(rc, 0, err)
        return next(l for l in err.splitlines() if l.startswith("  %-24s ok  (" % "one")).split("(", 1)[1][:-1]

    def test_a_container_reads_the_store_a_guest_its_mount_and_a_build_box_its_own(self):
        w = self.make_world()
        w.make("one")
        self.assertEqual(self.row(w, "one"), w.env["WK_STORE"] + "/git/WebKit.git")
        w = self.make_world(kinds={"vm": "vm"}, in_vm=False)
        w.make_guest("one")
        self.assertEqual(self.row(w, "one"), GUEST)
        w = self.make_world(kinds={"buildbox4": "remote"}, in_vm=False)
        w.env.update(WK_REMOTE_LOCAL="1", WK_REMOTE_ROOT="/far")
        w.dirs.add("/far/ws/one")
        w.files["/far/ws/one/.wk-ready"] = ""
        self.assertEqual(self.row(w, "one"), "/far/mirror")

    def inside(self, **env):
        w = self.make_world(kinds={"local": "local"}, in_vm=False)
        Path(w.env["WK_MARKER"]).write_text("name=one\nsrc=/src/WebKit\n")
        w.env.update(env)
        w.answer(["test", "-S"])
        w.files[test_sync.CLIENT] = ""
        return w

    @unittest.skipUnless(sys.platform == "darwin", "a workspace on macOS is a guest")
    def test_inside_a_guest_it_is_the_guests_mount(self):
        self.assertEqual(self.row(self.inside(WK_MIRROR="/elsewhere")), GUEST)

    @unittest.skipUnless(sys.platform.startswith("linux"), "a workspace on Linux is a container")
    def test_inside_a_container_it_is_the_one_its_place_handed_it(self):
        self.assertEqual(self.row(self.inside(WK_MIRROR="/some/store/git/WebKit.git")), "/some/store/git/WebKit.git")


class Refreshing(test_sync.Host):
    """A host whose mirror refresh and git run for real, origin and the fork answered by local repositories
    (support.GitMirror's) through a global `insteadOf`, and a new repository's first branch is not main; WPEWebKit and
    its fork are not reached."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, in_vm=False, **kw)
        self.up = GitMirror(self.tmp)
        (self.tmp / "gitconfig").write_text('[init]\n\tdefaultBranch = master\n[url "%s"]\n\tinsteadOf = https://github.com/WebKit/WebKit.git\n'
                                            '[url "%s"]\n\tinsteadOf = https://github.com/justinmichaud/WebKit.git\n' % (self.up.upstream, self.up.fork))
        self.git_env = {"GIT_CONFIG_GLOBAL": str(self.tmp / "gitconfig")}
        self.effects = []

    def _git(self, argv, f):
        return real(argv, self.git_env)

    def _sh(self, text):
        if test_sync.kind_of(text) != "refresh":
            return super()._sh(text)
        r = real(["sh", "-c", text], self.git_env)
        self.dirs.add(self.mirror)
        return r


class TestTheRefresh(test_sync.Flow):
    world_class = Refreshing

    def test_it_carries_origins_branches_as_its_heads_and_each_fork_under_its_name_with_no_tags_and_no_repack(self):
        rc, err = self.sync("--mirror")
        m = self.w.mirror
        self.assertEqual(err.splitlines()[2:], ["  origin   ok", "  wpe      FAILED (continuing)", "  fork     ok", "  forkwpe  FAILED (continuing)"])
        refs = git_run("for-each-ref", "--format=%(refname)", cwd=m).stdout.split()
        self.assertEqual((rc, "refs/heads/main" in refs, "refs/remotes/fork/side" in refs), (0, True, True))
        self.assertEqual([git_run(*a, cwd=m).stdout.strip() for a in (("symbolic-ref", "HEAD"), ("config", "remote.origin.tagOpt"), ("config", "gc.auto"))],
                         ["refs/heads/main", "--no-tags", "0"])

    def test_it_carries_main_and_every_image_configurations_branch_unless_told_otherwise(self):
        for branches, line in ((None, "main webkitglib/2.52"), ("main only/this", "main only/this")):
            if branches is None:
                del self.w.env["WK_MIRROR_BRANCHES"]
            else:
                self.w.env["WK_MIRROR_BRANCHES"] = branches
            self.assertIn("==> fetching origin wpe fork forkwpe (origin: %s)" % line, self.sync("--mirror")[1].splitlines())


class TestOneMirrorPerMachine(test_sync.Flow):
    """A machine keeps one mirror, where `wk sync` runs: a macOS host in its own state, the podman VM and a Linux host
    in the store."""

    def test_a_macos_host_keeps_it_in_its_state(self):
        w = self.make_world(kinds={"vm": "vm"}, in_vm=False)
        self.sync("--mirror", w=w)
        self.assertIn(("mkdir", w.env["XDG_STATE_HOME"] + "/wk/git"), w.effects)

    @unittest.skipUnless(sys.platform.startswith("linux"), "this host is a Linux one")
    def test_a_linux_host_keeps_it_in_the_store(self):
        w = self.make_world(in_vm=False)
        self.sync("--mirror", w=w)
        self.assertIn(("mkdir", w.env["WK_STORE"] + "/git"), w.effects)

    def test_the_podman_vm_reads_it_in_the_store(self):
        rc, err = self.sync("--tools", "container")
        self.assertEqual((rc, said(err)), (1, "error: no mirror at %s/git/WebKit.git to publish a snapshot from -- 'wk sync' on the host makes it" % self.w.env["WK_STORE"]))

    def test_nothing_fetches_into_it_from_the_podman_vm(self):
        w = test_pr_workflow.on_branch(self.tmp, "remote")
        w.env["WK_IN_VM"] = "1"
        rc, _, err = self.wk(w, "pr", "open", "ws")
        self.assertEqual((rc, said(err), w.here_ran("git")), (1, "error: the mirror in here is the host's, mounted read-only; run this on the host", []))


class TestASnapshotBorrowsItsObjects(BoxFlow):
    def test_the_published_tree_shares_the_mirror_and_records_its_main_and_branch(self):
        self.assertEqual(self.publish()[0], 0)
        tree, snap = self.w.tree(), self.w.snapshot()
        alternates = tree / ".git" / "objects" / "info" / "alternates"
        self.assertEqual(os.path.realpath(alternates.read_text().strip()), os.path.realpath(self.w.mirror + "/objects"), "the snapshot copied the history")
        self.assertEqual((self.w.files[snap + "/sha"].strip(), self.w.files[snap + "/branch"]),
                         (git_run("rev-parse", "refs/heads/main", cwd=self.w.mirror).stdout.strip(), "origin/main\n"))


class TestAWorkspaceReadsIt(WorkspaceFlow):
    def test_its_fetch_takes_the_forks_too_and_writes_no_commit_graph(self):
        self.assertEqual(self.sync("ws")[0], 0)
        self.assertIn("refs/remotes/fork/side", git_run("for-each-ref", "--format=%(refname)", cwd=self.ws).stdout.split())
        self.assertEqual([git_run("config", k, cwd=self.ws).stdout.strip() for k in ("fetch.writeCommitGraph", "gc.writeCommitGraph")], ["false", "false"])


class Branches(test_wk_build.World):
    """`ws`'s checkout `co` is a clone of a real mirror, origin pointed at its upstream; the checkout `wk build --branch`
    runs there runs for real, reading the mirror or, with `mirrored` off, a place that keeps none."""

    def __init__(self, tmp, mirrored=True):
        super().__init__(tmp)
        self.up = GitMirror(self.tmp)
        self.co = self.tmp / "co"
        git_run("clone", "-q", "--shared", "--branch", "main", str(self.up.mirror), str(self.co), cwd=self.tmp)
        git_run("remote", "set-url", "origin", str(self.up.upstream), cwd=self.co)
        mirror = str(self.up.mirror) if mirrored else ""
        self.reg.make = lambda n, e: JobDriver("box", str(REPO), dict(e, **self.conf), self, self.kind, mirror=mirror)
        self.react(["exec", "ws", "bash", "-c"], lambda a, f: real(["bash", "-c", a[-1].replace("/src/WebKit", str(f.co), 1)]))

    def push(self, branch, to):
        """`branch` on the upstream's main, in the mirror too when `to` says so."""
        git_run("push", "-q", "origin", "main:refs/heads/" + branch, cwd=self.up.seed)
        if to == "mirror":
            git_run("fetch", "-q", "origin", "refs/heads/%s:refs/heads/%s" % (branch, branch), cwd=self.up.mirror)


class TestABranchIsTakenFromTheMirrorFirst(test_wk_build.BuildTest):
    """`wk build <ws> --branch <b>` checks the branch out before it builds, from the mirror when it carries it."""

    def built(self, w, branch):
        rc, _, err = self.wk(w, "build", "ws", "jsc-release", "--branch", branch)
        return rc, git_run("symbolic-ref", "--short", "HEAD", cwd=w.co).stdout.strip()

    def test_a_branch_the_mirror_carries_costs_no_network(self):
        w = Branches(self.tmp)
        w.push("topic", "mirror")
        git_run("remote", "set-url", "origin", str(self.tmp / "gone.git"), cwd=w.co)
        self.assertEqual(self.built(w, "topic"), (0, "topic"))

    def test_a_branch_it_does_not_carry_or_a_place_with_no_mirror_asks_origin(self):
        for mirrored in (True, False):
            w = Branches(self.tmp, mirrored)
            w.push("other", "upstream")
            self.assertEqual(self.built(w, "other"), (0, "other"), mirrored)

    def test_one_nowhere_is_refused_by_name(self):
        rc, _, err = self.wk(Branches(self.tmp), "build", "ws", "jsc-release", "--branch", "nosuch")
        self.assertEqual((rc, test_wk_build.said(err)), (1, "error: could not check out 'nosuch' in 'ws'"))


if __name__ == "__main__":
    unittest.main()
