"""What `wk pr` and `wk sync --fix` leave in a real checkout wired to a real mirror: the PR head fetched from GitHub past
the mirror's rewrite, and every branch tracking the fork it is pushed to."""
import importlib.util
import os
import unittest

from tests import test_pr_workflow
from tests.support import REPO, GitMirror, git_commit, git_run
from tests.test_new_fetch import URLS, WorkspaceFlow, firstrun, problems, real
from tests.test_wk_workspace import said

DOTFILES = "[fetch]\n\tprune = true\n[push]\n\tdefault = current\n"   # what a workspace's git has of dotfiles/gitconfig that matters here
SSH = '#!/bin/sh\nfor a; do cmd=$a; done\nrepo=${cmd#* }\nrepo=$(eval echo "$repo")\nexec ${cmd%% *} "$GITHUB/$repo"\n'


def github(tmp):
    """GitHub as the directory `tmp/hub`: a global git config with the workspace's dotfiles, the URLs `serve` adds, and
    an ssh that lands a push in `hub/<owner>/<repo>.git`; the environment that git reads it from."""
    (tmp / "gitconfig").write_text(DOTFILES)
    (tmp / "ssh").write_text(SSH)
    (tmp / "ssh").chmod(0o755)
    return {"GIT_CONFIG_GLOBAL": str(tmp / "gitconfig"), "GIT_SSH_COMMAND": str(tmp / "ssh"), "GITHUB": str(tmp / "hub")}


def repo(tmp, name, url=None):
    """A repository `name` on GitHub, read at its direct (no `.git`) URL, or `url`, when served."""
    bare = tmp / "hub" / (name + ".git")
    git_run("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp)
    if url is not None:
        with open(tmp / "gitconfig", "a") as f:
            f.write('[url "%s"]\n\tinsteadOf = %s\n' % (bare, url or "https://github.com/" + name))
    return bare


def rev(tree, *args):
    return git_run("rev-parse", *args, cwd=tree, check=False).stdout.strip()


def upstream(tree, branch):
    return tuple(git_run("config", "--get", "branch.%s.%s" % (branch, k), cwd=tree, check=False).stdout.strip() for k in ("remote", "merge"))


def pushed(tree):
    """Where a bare `git push` sends the branch checked out, by a dry run of it."""
    out = git_run("push", "--dry-run", "--porcelain", cwd=tree, check=False).stdout
    return [l.split("\t")[1] for l in out.splitlines() if l[:1] in "* +!=" and "\t" in l]


class GitHub(test_pr_workflow.World):
    """`ws`'s checkout `co` is a real clone of a real mirror, wired by firstrun.sh: its upstream URLs read the mirror,
    and their direct spellings GitHub (`github`), where justinmichaud's WebKit fork carries `topic` one commit past
    the copy of it the mirror holds and WebKit/WebKit is the mirror's upstream."""

    def __init__(self, tmp, kind="container"):
        super().__init__(tmp, kind)
        self.up, self.co, self.git_env = GitMirror(self.tmp), self.tmp / "co", github(self.tmp)
        seed = self.up.seed
        git_run("push", "-q", str(self.up.mirror), "main:refs/remotes/fork/topic", cwd=seed)
        git_run("checkout", "-q", "-b", "topic", cwd=seed)
        self.topic = git_commit(seed, "fork-only")
        git_run("checkout", "-q", "main", cwd=seed)
        self.fork = repo(self.tmp, "justinmichaud/WebKit", "")
        git_run("push", "-q", str(self.fork), "topic", "main", cwd=seed)
        repo(self.tmp, "WebKit/WebKit", "")
        git_run("push", "-q", str(self.tmp / "hub" / "WebKit" / "WebKit.git"), "main", cwd=seed)
        git_run("clone", "-q", str(self.up.mirror), str(self.co), cwd=self.tmp)
        firstrun(self.co, str(self.up.mirror))
        self.react(["git", "ls-remote"], lambda a, f: real(["sh", "-c", 'cd "$0" && exec "$@"', str(f.co)] + a, f.git_env))

    def account(self, name, repos):
        """`name`'s forks, each carrying `topic` at the fork's head."""
        for r in repos:
            git_run("push", "-q", str(repo(self.tmp, "%s/%s" % (name, r), "")), "%s:refs/heads/topic" % self.topic, cwd=self.up.seed)

    def in_checkout(self, argv):
        if argv[:1] != ["git"]:
            return super().in_checkout(argv)
        return real([a.replace(self.src, str(self.co)) for a in argv], self.git_env)


class PrFlow(test_pr_workflow.PrTest):
    world_class = GitHub

    def setUp(self):
        super().setUp()
        os.environ.update(self.w.git_env)

    def head(self):
        return rev(self.w.co, "HEAD")


class TestAPrHeadCheckedOut(PrFlow):
    def test_a_forks_branch_comes_from_the_fork_past_the_mirror_and_tracks_it_so_a_bare_push_goes_to_its_own_name(self):
        rc, err = self.pr("ws", "justinmichaud:topic")
        co = self.w.co
        self.assertEqual((rc, said(err), self.head(), upstream(co, "topic")), (0, "  %s fork-only" % self.w.topic[:7], self.w.topic, ("fork", "refs/remotes/fork/topic")))
        self.assertEqual((rev(co, "--abbrev-ref", "@{u}"), pushed(co), rev(co, "origin/main")), ("fork/topic", ["refs/heads/topic:refs/heads/topic"], self.w.up.sha1))

    def test_it_converges_over_an_upstream_already_recorded_wrong(self):
        self.pr("ws", "justinmichaud:topic")
        git_run("branch", "--set-upstream-to=refs/remotes/fork/topic", "topic", cwd=self.w.co)
        self.assertEqual((self.pr("ws", "justinmichaud:topic")[0], upstream(self.w.co, "topic")), (0, ("fork", "refs/remotes/fork/topic")))

    def test_an_account_with_no_remote_gets_one_named_for_it(self):
        self.w.account("alice", ["WPEWebKit"])
        rc, err = self.pr("ws", "alice:topic")
        co = self.w.co
        self.assertEqual((rc, git_run("config", "remote.alice-wpewebkit.url", cwd=co).stdout.strip(), upstream(co, "topic"), rev(co, "--abbrev-ref", "@{u}")),
                         (0, "https://github.com/alice/WPEWebKit.git", ("alice-wpewebkit", "refs/heads/topic"), "alice-wpewebkit/topic"))

    def test_a_branch_in_both_of_an_accounts_projects_is_refused_by_name_before_anything_moves(self):
        self.w.account("alice", ["WebKit", "WPEWebKit"])
        rc, err = self.pr("ws", "alice:topic")
        self.assertEqual((rc, said(err), self.head()), (1, "error: 'topic' exists in more than one of alice's repositories:", self.w.up.sha1))

    def test_local_commits_the_head_lacks_are_kept_unless_forced(self):
        self.pr("ws", "justinmichaud:topic")
        mine = git_commit(self.w.co, "mine")
        rc, err = self.pr("ws", "justinmichaud:topic")
        self.assertEqual((rc, "warning: local 'topic' has 1 commit(s) the PR head does not have" in err.splitlines(), self.head()), (0, True, mine))
        self.assertIn("    wk pr ws justinmichaud:topic --force", err.splitlines())
        rc, err = self.pr("ws", "justinmichaud:topic", "--force")
        self.assertEqual((rc, self.head()), (0, self.w.topic))

    def test_a_pull_request_head_is_checked_out_tracking_nothing_even_where_git_would_track_it(self):
        git_run("config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=self.w.co)   # a checkout wired before the narrowing
        git_run("update-ref", "refs/pull/1234/head", self.w.up.sha1, cwd=self.w.tmp / "hub" / "WebKit" / "WebKit.git")
        self.assertEqual(self.pr("ws", "1234")[0], 0)
        self.assertEqual((rev(self.w.co, "--abbrev-ref", "HEAD"), upstream(self.w.co, "pr/1234")), ("pr/1234", ("", "")))

    def test_no_such_pull_request_is_refused_before_anything_moves(self):
        rc, err = self.pr("ws", "999")
        self.assertEqual((rc, said(err), self.head()), (1, "error: no pull request #999 on WebKit (checked https://github.com/WebKit/WebKit.git)", self.w.up.sha1))


class TestABranchOpened(PrFlow):
    def test_a_branch_opened_twice_is_pushed_each_time_and_left_tracking_its_fork(self):
        co = self.w.co
        git_run("checkout", "-q", "--track", "-b", "mine", "origin/main", cwd=co)
        for draft in ((), ("--draft",)):
            git_commit(co, "work%s" % len(draft))
            rc, err = self.pr("open", "ws", *draft)
            self.assertEqual((rc, rev(self.w.fork, "mine"), self.w.gh()[-1][-1]), (0, self.head(), "--draft" if draft else "--fill"), draft)
        self.assertEqual(upstream(co, "mine"), ("fork", "refs/remotes/fork/mine"))


class TestTheWiringOfABranch(WorkspaceFlow):
    """`wk sync <ws>` names a branch whose upstream does not resolve or is no fork, and `--fix` points it at its fork."""

    def setUp(self):
        super().setUp()
        env = github(self.tmp)
        repo(self.tmp, "justinmichaud/WebKit")
        os.environ.update(env)
        self.w.git_env.update(env)

    def notes(self, err):
        return [l.strip() for l in err.splitlines() if l.startswith("    ") and not l.startswith("    - ")]

    def test_a_push_u_is_named_and_fix_records_it_so_it_resolves_once_the_mirror_carries_it(self):
        git_run("checkout", "-q", "-b", "mine", cwd=self.ws)
        for k, v in (("remote", "fork"), ("merge", "refs/heads/mine")):   # what `git push -u fork mine` records
            git_run("config", "branch.mine." + k, v, cwd=self.ws)
        rc, err = self.sync("ws")
        self.assertEqual(rc, 1)
        self.assertTrue(any(p.startswith("branch mine was set up by git push -u") and "wk sync --fix" in p for p in problems(err)))
        self.assertIn("converged: mine tracks fork/mine", self.notes(self.sync("ws", "--fix")[1]))
        git_run("push", "-q", self.w.mirror, "mine:refs/remotes/fork/mine", cwd=self.ws)
        rc, err = self.sync("ws")
        self.assertEqual((rc, rev(self.ws, "--abbrev-ref", "@{u}"), pushed(self.ws)), (0, "fork/mine", ["refs/heads/mine:refs/heads/mine"]))
        self.assertEqual(self.notes(self.sync("ws", "--fix")[1]), ["re-wired", "git-webkit: setup=ok"])

    def test_the_second_fork_converges_the_same_way(self):
        git_run("push", "-q", self.w.mirror, "main:refs/remotes/forkwpe/topic", cwd=self.w.up.seed)
        self.sync("ws")
        git_run("checkout", "-q", "-b", "topic", "forkwpe/topic", cwd=self.ws)
        git_run("config", "branch.topic.merge", "refs/heads/topic", cwd=self.ws)
        self.assertIn("converged: topic tracks forkwpe/topic", self.notes(self.sync("ws", "--fix")[1]))
        self.assertEqual(rev(self.ws, "--abbrev-ref", "@{u}"), "forkwpe/topic")

    def test_a_branch_tracking_an_upstream_is_retargeted_to_its_fork_once(self):
        for ref in ("wpe", "forkwpe"):
            git_run("push", "-q", self.w.mirror, "main:refs/remotes/%s/topic" % ref, cwd=self.w.up.seed)
        self.sync("ws")
        git_run("checkout", "-q", "--track", "-b", "topic", "wpe/topic", cwd=self.ws)
        self.assertIn("retargeted: topic now tracks forkwpe/topic", self.notes(self.sync("ws", "--fix")[1]))
        self.assertEqual(upstream(self.ws, "topic"), ("forkwpe", "refs/remotes/forkwpe/topic"))
        self.assertEqual(self.notes(self.sync("ws", "--fix")[1]), ["re-wired", "git-webkit: setup=ok"], "a branch on its fork is left alone")

    def test_a_branch_tracking_its_fork_by_hand_is_not_named_and_pushes_to_its_own_name(self):
        self.sync("ws")
        for name, how in (("side", [("checkout", "-q", "--track", "fork/side")]), ("other", [("checkout", "-q", "-b", "other"), ("branch", "-u", "fork/side")])):
            for argv in how:
                git_run(*argv, cwd=self.ws)
            self.assertEqual((self.sync("ws")[0], rev(self.ws, "--abbrev-ref", "@{u}"), pushed(self.ws)), (0, "fork/side", ["refs/heads/%s:refs/heads/%s" % (name, name)]))

    def test_origin_and_wpe_refuse_a_bare_push_and_every_upstream_url_reads_the_mirror_unless_spelled_directly(self):
        for remote in ("origin", "wpe"):
            git_run("checkout", "-q", "-B", "t-" + remote, "origin/main", cwd=self.ws)
            for k, v in (("remote", remote), ("merge", "refs/heads/main")):
                git_run("config", "branch.t-%s.%s" % (remote, k), v, cwd=self.ws)
            cp = real(["git", "-C", str(self.ws), "push"])
            self.assertEqual((cp.rc != 0, "no-push" in cp.err), (True, True), remote)
        get_url = lambda u: git_run("ls-remote", "--get-url", u, cwd=self.ws).stdout.strip()   # noqa: E731
        self.assertEqual([get_url(u) for u in URLS], [self.w.mirror] * 4)
        for u in [u[:-4] for u in URLS] + ["https://github.com/alice/WebKit.git"]:
            self.assertEqual(get_url(u), u)


class TestTheBrokerServesTheMirrorRefresh(unittest.TestCase):
    """A workspace may ask for this machine's mirror to be refreshed, never for a URL of its choosing."""

    def test_sync_is_wk_sync_mirror_here_serialised_and_takes_no_arguments(self):
        spec = importlib.util.spec_from_file_location("wkbroker", REPO / "container" / "broker" / "wk-broker.py")
        b = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(b)
        resolve, serialised = b.VERBS["sync"][:2]
        subject, argv, _ = resolve({})
        self.assertEqual((argv[1:], subject["name"], bool(subject.get("reach")), bool(serialised)),
                         (["sync", "--mirror"], "store", True, True))
        with self.assertRaises(b.Refused):
            resolve({"machine": "rpi4"})


if __name__ == "__main__":
    unittest.main()
