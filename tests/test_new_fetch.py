"""What a fresh workspace's checkout is -- on a branch, tracking it, wired to the mirror and as current as it -- typed as
`wk sync` and `wk new` into worlds whose git runs for real in scratch repositories."""
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from tests import test_sync
from tests.fakes import Result
from tests.support import REPO, GitMirror, bash, git_commit, git_run, scratch_dir
from tests.test_wk_workspace import WK, Flow, World, said

# Nothing here may reach github.com: the wiring names the upstreams' real URLs and rewrites them to the mirror, so a
# fetch that ignored the rewrite would go there. Port 1 refuses at once.
OFFLINE = {"http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1", "GIT_TERMINAL_PROMPT": "0"}
SRC = "/src/WebKit"
ALIAS = "git@github-webkit:justinmichaud/WebKit.git"
URLS = ["https://github.com/WebKit/WebKit.git", "https://github.com/WebPlatformForEmbedded/WPEWebKit.git",
        "https://github.com/justinmichaud/WebKit.git", "https://github.com/justinmichaud/WPEWebKit.git"]


def real(argv, env=None):
    cp = subprocess.run(argv, capture_output=True, text=True, env=dict(os.environ, **OFFLINE, **(env or {})))
    return Result(cp.returncode, cp.stdout, cp.stderr)


def problems(err):
    """What `wk sync` names under a workspace or a snapshot, one line per fault."""
    return [l[len("    - "):] for l in err.splitlines() if l.startswith("    - ")]


class Box(test_sync.Host):
    """The podman VM whose store holds a real mirror (support.GitMirror: its upstreams are local repositories): git,
    `cp -al` and the snapshot's wiring and check run for real, and a workspace in `co` has its checkout there -- a copy
    of the snapshot, as its overlay is -- every script and git argv in it run for real; `git_env` is their environment."""

    def __init__(self, tmp, **kw):
        super().__init__(tmp, **kw)
        self.up = GitMirror(self.tmp)
        os.makedirs(os.path.dirname(self.mirror))
        os.rename(str(self.up.mirror), self.mirror)
        self.up.mirror = Path(self.mirror)
        self.dirs.add(self.mirror)
        self.co, self.git_env = {}, {}
        self.effects = []

    def mkdir(self, path):
        super().mkdir(path)
        if path in self.dirs:
            os.makedirs(path, exist_ok=True)

    def remove(self, path):
        super().remove(path)
        if path not in self.dirs:
            shutil.rmtree(path, True)

    def _git(self, argv, f):
        r = real(argv, self.git_env)
        if argv[1] == "clone" and r.ok:
            f.dirs.update({argv[-1], argv[-1] + "/.git"})
        return r

    def _cp(self, argv, f):
        super()._cp(argv, f)
        return real(argv)

    def _sh(self, text):
        if test_sync.kind_of(text) in ("check", "wire"):
            return real(["sh", "-c", text])
        return super()._sh(text)

    def in_ws(self, ws, argv):
        co = self.co.get(ws)
        if co is None or (argv[:2] == ["sh", "-c"] and test_sync.kind_of(argv[2]) == "setup"):
            return super().in_ws(ws, argv)
        return real([a.replace(SRC, str(co)) for a in argv], self.git_env)

    def tree(self, bid=test_sync.STAMP):
        return Path(self.snapshot(bid)) / "WebKit"

    def workspace(self, name="ws"):
        """Workspace `name`, made from the snapshot just published."""
        self.make(name)
        self.co[name] = self.tmp / name
        subprocess.run(["cp", "-a", str(self.tree()), str(self.co[name])], check=True)
        return self.co[name]


def status(tree):
    return git_run("status", "-sb", cwd=tree).stdout.splitlines()[0]


def config(tree, *args):
    return git_run("config", *args, cwd=tree, check=False).stdout.strip()


class BoxFlow(test_sync.Flow):
    world_class = Box

    def publish(self, w=None):
        return self.sync("--tools", "container", w=w)


class TestASnapshotIsPublishedOnItsBranch(BoxFlow):
    def test_the_first_is_on_main_tracking_origin_main_at_the_mirrors_head(self):
        rc, err = self.publish()
        tree = self.w.tree()
        self.assertEqual((rc, status(tree), git_run("rev-parse", "HEAD", cwd=tree).stdout.strip()), (0, "## main...origin/main", self.w.up.sha1))

    def test_the_next_resets_the_branch_forward_even_from_a_detached_one(self):
        self.publish()
        git_run("checkout", "-q", "--detach", "origin/main", cwd=self.w.tree())
        sha2 = self.w.up.advance()
        self.w.clock.t += 60
        rc, err = self.publish()
        second = self.w.tree(time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.w.clock.t)))
        self.assertIn("==> snapshotting %s -> %s" % (test_sync.STAMP, second.parent.name), err.splitlines())
        self.assertEqual((rc, status(second), git_run("rev-parse", "refs/heads/main", cwd=second).stdout.strip()), (0, "## main...origin/main", sha2))

    def test_a_release_branch_keeps_its_own_name_and_tracks_it_whatever_git_would_set_up_itself(self):
        (self.tmp / "gitconfig").write_text("[branch]\n\tautoSetupMerge = false\n")
        self.w.git_env["GIT_CONFIG_GLOBAL"] = str(self.tmp / "gitconfig")
        up = self.w.up
        git_run("branch", "-q", "wpe-2.46", cwd=up.seed)
        git_run("push", "-q", "origin", "wpe-2.46", cwd=up.seed)
        git_run("config", "--add", "remote.origin.fetch", "+refs/heads/wpe-2.46:refs/heads/wpe-2.46", cwd=up.mirror)
        up.fetch()
        self.w.env.update(WK_MIRROR_BRANCHES="main wpe-2.46", WK_BRANCH="origin/wpe-2.46")
        rc, err = self.publish()
        self.assertEqual((rc, status(self.w.tree())), (0, "## wpe-2.46...origin/wpe-2.46"))
        self.assertIn("==> checked out on branch wpe-2.46 (tracking origin/wpe-2.46)", err.splitlines())

    def test_a_branch_that_is_not_remote_tracking_is_refused_by_name_and_nothing_is_left(self):
        for branch in ("main", self.w.up.sha1, "nosuch/branch"):
            self.w.env["WK_BRANCH"] = branch
            rc, err = self.publish()
            self.assertEqual((rc, said(err), self.w.tree().exists()), (1, "error: '%s' is not a branch this mirror carries: a snapshot is published from a" % branch, False), branch)

    def test_the_hard_linked_next_one_leaves_the_last_clean_though_every_ctime_moved(self):
        self.publish()
        first = self.w.tree()
        git_run("update-index", "--refresh", "-q", cwd=first, check=False)
        time.sleep(1.1)   # git compares ctime to the second
        self.w.up.advance()
        self.w.clock.t += 60
        self.publish()
        dirty = [git_run(*c, "diff-files", "--name-only", cwd=first).stdout.split() for c in ([], ["-c", "core.trustctime=true"])]
        self.assertEqual(dirty, [[], ["a"]], "trusting ctime, the same link stat-dirties the file")


class WorkspaceFlow(BoxFlow):
    """A snapshot published and workspace `ws` made from it."""

    def setUp(self):
        super().setUp()
        self.publish()
        self.ws = self.w.workspace()

    def row(self, err):
        return next(l for l in err.splitlines() if l.startswith("  %-24s " % "ws"))


class TestTheFetch(WorkspaceFlow):
    def test_it_reads_the_mirror_although_the_remotes_name_github(self):
        sha2 = self.w.up.advance()
        rc, err = self.sync("ws")
        self.assertEqual((rc, self.row(err)), (0, "  %-24s ok  (%s)" % ("ws", self.w.mirror)))
        self.assertEqual((git_run("rev-parse", "origin/main", cwd=self.ws).stdout.strip(), config(self.ws, "remote.origin.url")), (sha2, URLS[0]))

    def test_no_tags_are_followed(self):
        up = self.w.up
        git_run("tag", "some-release", up.sha1, cwd=up.seed)
        git_run("push", "-q", "origin", "some-release", cwd=up.seed)
        git_run("fetch", "-q", "--tags", "origin", "refs/heads/main:refs/heads/main", cwd=up.mirror)
        up.advance()
        self.assertEqual((self.sync("ws")[0], git_run("for-each-ref", "refs/tags", cwd=self.ws).stdout), (0, ""))

    def test_origin_is_narrowed_to_the_branches_the_mirror_carries(self):
        up = self.w.up
        git_run("branch", "-q", "safari-1-branch", cwd=up.seed)
        git_run("push", "-q", "origin", "safari-1-branch", cwd=up.seed)
        git_run("fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/heads/*", cwd=up.mirror)
        self.assertEqual(self.sync("ws")[0], 0)
        refs = git_run("for-each-ref", "--format=%(refname)", "refs/remotes/origin", cwd=self.ws).stdout.split()
        self.assertEqual(("refs/remotes/origin/main" in refs, "refs/remotes/origin/safari-1-branch" in refs), (True, False))

    def test_a_ref_the_mirror_cannot_answer_fails_it(self):
        git_run("config", "--add", "remote.origin.fetch", "+refs/heads/webkitglib/9.9:refs/remotes/origin/webkitglib/9.9", cwd=self.ws)
        rc, err = self.sync("ws")
        self.assertEqual((rc, self.row(err)), (1, "  %-24s FAILED (continuing)" % "ws"))


class TestTheWiringReadBack(WorkspaceFlow):
    """Each fetch reads the checkout's wiring back, and `--fix` re-asserts it."""

    def test_a_published_checkout_is_wired_to_the_mirror_with_the_forks_recorded_on_github(self):
        rc, err = self.sync("ws")
        self.assertEqual((rc, problems(err)), (0, []))
        self.assertEqual((config(self.ws, "--get-all", "url.%s.insteadOf" % self.w.mirror).split(), config(self.ws, "remote.fork.url"),
                          config(self.ws, "remote.fork.pushurl"), git_run("remote", "get-url", "--push", "fork", cwd=self.ws).stdout.strip()),
                         (URLS, URLS[2], "", ALIAS))
        for row in git_run("config", "--get-regexp", "remote.+url", cwd=self.ws).stdout.splitlines():
            self.assertRegex(row.split(" ", 1)[1], r"^(https://github\.com/|no-push://)", "git-webkit reads any other host as a GitHub of its own")

    def test_a_checkout_wired_before_this_is_named_fault_by_fault_and_fix_converges_it(self):
        git_run("config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=self.ws)
        git_run("config", "--unset", "remote.wpe.tagOpt", cwd=self.ws)
        git_run("config", "--remove-section", "url.%s" % self.w.mirror, cwd=self.ws)
        git_run("config", "--unset", "core.trustctime", cwd=self.ws)
        git_run("config", "remote.fork.pushurl", ALIAS, cwd=self.ws)
        rc, err = self.sync("ws")
        self.assertEqual((rc, self.row(err)), (1, "  %-24s FAILED (continuing)" % "ws"), "its fetch went to github.com")
        for fault in ["origin asks for +refs/heads/*:refs/remotes/origin/*", "wpe follows tags", "git trusts ctime here", "fork records %s as a push URL" % ALIAS] + [
                "%s is not rewritten to %s" % (r, self.w.mirror) for r in ("origin", "wpe", "fork", "forkwpe")]:
            self.assertTrue([p for p in problems(err) if p.startswith(fault)], fault)
        rc, err = self.sync("ws", "--fix")
        self.assertEqual((rc, problems(err)), (0, []))
        self.assertEqual(self.sync("ws")[0], 0)

    def test_a_mirror_without_a_branch_the_checkout_asks_for_is_its_one_fault(self):
        self.w.env["WK_MIRROR_BRANCHES"] = "main webkitglib/9.9"
        rc, err = self.sync("ws", "--fix")
        self.assertEqual((rc, self.row(err), problems(err)[-1:]), (1, "  %-24s FAILED (continuing)" % "ws", [
            "the mirror %s carries no refs/heads/webkitglib/9.9, which origin asks it for -- every fetch in here fails on it; 'wk sync --mirror' on the machine that keeps it" % self.w.mirror]))
        self.assertEqual(len(problems(err)), 1, "the branch the mirror does carry is not a fault")

    def test_fix_clears_stale_local_rewrites_and_keeps_a_global_one(self):
        gitconfig = self.tmp / "global-gitconfig"
        gitconfig.write_text('[url "/somewhere/else.git"]\n\tinsteadOf = https://example.invalid/x.git\n')
        self.w.git_env["GIT_CONFIG_GLOBAL"] = str(gitconfig)
        for stale in ("/gone/one.git", "/gone/two.git"):
            git_run("config", "--local", "--add", "url.%s.insteadOf" % stale, URLS[0], cwd=self.ws)
        git_run("config", "--local", "url.git@old-alias:x/y.git.pushInsteadOf", "git@github.com:x/y.git", cwd=self.ws)
        self.assertEqual(self.sync("ws", "--fix")[0], 0)
        local = git_run("config", "--local", "--name-only", "--get-regexp", r"^url\.", cwd=self.ws).stdout
        self.assertEqual([g for g in ("/gone/", "old-alias", self.w.mirror) if g in local], [self.w.mirror])
        self.assertIn("/somewhere/else.git", gitconfig.read_text())


_FIRSTRUN = (REPO / "container" / "firstrun.sh").read_text()
FIRSTRUN = _FIRSTRUN[_FIRSTRUN.index("_git_py() {"):_FIRSTRUN.index("    _setup=$(_git_py pr-tool-setup-script")] + "fi\n"
HARNESS = "set -u\nSRC=%s\nWK_TOOLS=%s\nlog()  { printf '[firstrun] %%s\\n' \"$*\"; }\nwarn() { printf '[firstrun] warning: %%s\\n' \"$*\"; }\n"


def firstrun(src, mirror="", git_py=None):
    """container/firstrun.sh's wiring block, lifted from the file and run (the half below it is `git-webkit setup`): what it said."""
    block = FIRSTRUN if git_py is None else FIRSTRUN.replace(FIRSTRUN.split("\n", 1)[0], "_git_py() { %s; }" % git_py)
    cp = bash(HARNESS % (src, REPO) + block, env=dict(OFFLINE, WK_MIRROR=mirror, WK_MIRROR_BRANCHES="main"))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    return cp.stdout + cp.stderr


class TestWhatFirstRunWires(unittest.TestCase):
    """container/firstrun.sh wires the checkout through `python3 -m wk.git wiring-script` with the mirror the place
    hands it in WK_MIRROR."""

    def stub(self, git_py, mirror=""):
        with scratch_dir(prefix="wk-firstrun-") as d:
            (d / ".git").mkdir()
            return firstrun(d, mirror, git_py)

    def test_a_wiring_that_failed_is_a_warning_that_names_it(self):
        out = self.stub("return 1")
        self.assertIn("wk.git wiring-script failed", out)
        self.assertIn("could not wire the checkout's remotes", out)

    def test_no_mirror_on_the_target_is_stated_and_is_not_a_warning(self):
        out = self.stub("printf 'true\\n'")
        self.assertEqual(("no mirror on this place" in out, "warning" in out, "fetches read github.com" in out), (True, False, True))

    def test_a_mirror_is_named_in_the_line_that_says_what_was_wired(self):
        out = self.stub("printf 'true\\n'", mirror="/mirror/WebKit.git")
        self.assertEqual(("fetches read /mirror/WebKit.git" in out, "warning" in out), (True, False))

    def test_with_no_mirror_nothing_is_rewritten_and_origin_is_still_narrowed(self):
        with scratch_dir(prefix="wk-firstrun-") as d:
            m = GitMirror(d)
            git_run("clone", "-q", str(m.mirror), "co", cwd=d)
            firstrun(d / "co")
            co = d / "co"
            self.assertEqual((config(co, "--get-regexp", r"^url\..*\.insteadof$"), git_run("remote", "get-url", "--push", "fork", cwd=co).stdout.strip(),
                              config(co, "--get-all", "remote.origin.fetch"), config(co, "--get-all", "remote.wpe.fetch"), config(co, "remote.wpe.tagOpt")),
                             ("", ALIAS, "+refs/heads/main:refs/remotes/origin/main", "+refs/heads/*:refs/remotes/wpe/*", "--no-tags"))

    def test_re_wiring_it_with_a_mirror_leaves_one_rewrite_per_remote(self):
        with scratch_dir(prefix="wk-firstrun-") as d:
            m = GitMirror(d)
            git_run("clone", "-q", str(m.mirror), "co", cwd=d)
            co = d / "co"
            firstrun(co, "/gone/WebKit.git")
            firstrun(co, str(m.mirror))
            self.assertEqual((config(co, "--get-all", "url.%s.insteadOf" % m.mirror).split(), config(co, "--get-regexp", r"url\./gone/"),
                              config(co, "--get-all", "remote.origin.fetch")), (URLS, "", "+refs/heads/main:refs/remotes/origin/main"))


class Made(World):
    """A host making container workspace `ws` from snapshots that are real clones of a real mirror: git in a snapshot,
    and the probe and the fast-forward in `ws`'s checkout `co` (a copy of the snapshot it is made from, as its overlay
    is), run for real; the `wk sync ws` the creation runs fetches `co` from the mirror, then does `meanwhile(co)`."""

    def __init__(self, tmp, **kw):
        self.up = None
        super().__init__(tmp, **kw)
        self.up = GitMirror(self.tmp)
        self.co = self.tmp / "co"
        self.publish("main-1")
        self.meanwhile = lambda co: None
        self.react([WK, "sync", "ws"], self.fetched)
        self.effects = []

    def fetched(self, argv, f):
        r = real(["git", "-C", str(self.co), "fetch", "-q", "origin"])
        self.meanwhile(self.co)
        return r

    def publish(self, bid, on_branch=True, branch="origin/main", tracks=True):
        super().publish(bid)
        if self.up is None:
            return
        tree = self.driver.store.snapshot_tree(bid)
        shutil.rmtree(tree, True)
        git_run("clone", "-q", str(self.up.mirror), tree, cwd=self.tmp)
        if not on_branch:
            git_run("checkout", "-q", "--detach", "HEAD", cwd=tree)
        if not tracks:
            git_run("branch", "--unset-upstream", cwd=tree)
        d = os.path.dirname(tree)
        self.files[os.path.join(d, "sha")] = git_run("rev-parse", "HEAD", cwd=tree).stdout
        if branch:
            self.files[os.path.join(d, "branch")] = branch + "\n"
        else:
            del self.files[os.path.join(d, "branch")]

    def _git(self, argv, f):
        return real(argv)

    def wkdev_create(self, argv, f):
        lower = next(f for f in argv[argv.index("--additional-flags") + 1].split() if ":O,upperdir=" in f)
        subprocess.run(["cp", "-a", lower.split(":")[0], str(self.co)], check=True)
        return super().wkdev_create(argv, f)

    def _in_ws(self, argv, f):
        if SRC in argv[2:] and (argv[2] == "git" or "wsbase=" in argv[4]):
            return real([a.replace(SRC, str(self.co)) for a in argv[2:]])
        return super()._in_ws(argv, f)


class MadeFlow(Flow):
    world_class = Made

    def made(self, *argv):
        rc, err = self.detach(*argv)
        return rc, said(err) if rc else err.splitlines()[-2]

    def head(self):
        return git_run("rev-parse", "HEAD", cwd=self.w.co).stdout.strip()


class TestTheSnapshotItIsMadeFrom(MadeFlow):
    """A snapshot is verified before a workspace is overlaid on it, and the newest one that verifies is the one taken."""

    def test_a_snapshot_off_its_branch_or_not_tracking_it_or_recording_none_is_refused_naming_wk_sync(self):
        for bid, kw, line in (("main-2", dict(on_branch=False), "error: snapshot main-2 is not on branch main tracking origin/main (HEAD is"),
                              ("main-3", dict(tracks=False), "error: snapshot main-3 is not on branch main tracking origin/main (HEAD is"),
                              ("main-4", dict(branch=""), "error: snapshot main-4 does not record the branch it was published from,")):
            self.w.publish(bid, **kw)
            rc, err = self.detach("--base", bid)
            self.assertEqual((rc, said(err), self.w.co.exists()), (1, line, False), bid)
            self.assertIn("wk sync", err)

    def test_the_newest_it_would_not_refuse_is_taken_and_none_is_offered_when_every_one_is_refused(self):
        self.w.publish("main-2", on_branch=False)
        self.assertEqual((self.detach()[0], self.w.files[os.path.join(self.w.ws_dir(), "base-id")]), (0, "main-1\n"))
        w = self.make_world()
        w.publish("main-1", on_branch=False)
        self.assertEqual(said(self.detach(w=w)[1]), "error: no snapshot this machine can build a workspace from:  wk sync")


class TestTheCheckoutItReports(MadeFlow):
    """`wk new`'s last word: the checkout fast-forwarded onto what its branch tracks, and where it is."""

    def test_a_snapshot_behind_the_mirror_is_fast_forwarded_onto_it_and_left_clean(self):
        sha2 = self.w.up.advance()
        rc, line = self.made()
        self.assertEqual((rc, line, self.head()), (0, "==> 'ws' is on main at %s, 1 commit(s) on from its snapshot (origin/main)" % sha2[:7], sha2))
        self.assertEqual(git_run("status", "--porcelain", cwd=self.w.co).stdout, "")

    def test_a_current_one_moves_nothing_and_its_index_is_refreshed(self):
        rc, line = self.made()
        self.assertEqual((rc, line, self.head()), (0, "==> 'ws' is on main at %s, up to date with origin/main" % self.w.up.sha1[:7], self.w.up.sha1))
        self.assertEqual(git_run("diff-files", "--name-only", cwd=self.w.co).stdout, "", "git status in there would re-read every file")

    def test_one_detached_or_on_a_branch_tracking_nothing_is_named(self):
        for change, line in ((("checkout", "-q", "--detach"), "warning: 'ws' is not on a branch (detached at %s), so 'git status',"),
                             (("checkout", "-q", "-b", "eng/local"), "warning: 'ws' is on 'eng/local', which tracks nothing -- 'git pull' and")):
            w = self.make_world()
            w.meanwhile = lambda co: git_run(*change, cwd=co)
            rc, err = self.detach(w=w)
            self.assertEqual((rc, line.replace("%s", w.up.sha1[:7]) in err.splitlines()), (0, True), change)

    def test_a_diverged_branch_is_left_where_it_is(self):
        self.w.up.advance()
        self.w.meanwhile = lambda co: git_commit(co, "mine")
        rc, err = self.detach()
        self.assertIn("warning: 'main' in 'ws' and origin/main have diverged, so the checkout is left where", err.splitlines())
        self.assertEqual(git_run("log", "-1", "--format=%s", cwd=self.w.co).stdout.strip(), "mine")


class TestNoMirror(unittest.TestCase):
    """Not reachable from the CLI: every place's driver names a mirror (only the abstract Driver names none), so no
    command line checks a checkout against none; a checkout firstrun.sh wired with no mirror passes the check made for it."""

    def test_a_checkout_with_no_mirror_is_checked_against_the_upstreams(self):
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from tests.fakes import Fake
        from wk import git, places, secrets
        self.assertEqual(places.Driver("demo", str(REPO), {}, Fake("here")).mirror_dir(), "")
        with scratch_dir(prefix="wk-no-mirror-") as d:
            m = GitMirror(d)
            git_run("clone", "-q", str(m.mirror), "co", cwd=d)
            firstrun(d / "co")
            cp = real(["sh", "-c", git.wiring_check_script(str(d / "co"), "", list(secrets.forks()), ["main"])])
            self.assertEqual(cp.rc, 0, cp.out + cp.err)


if __name__ == "__main__":
    unittest.main()
