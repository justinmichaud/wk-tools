"""Asking GitHub a question from inside a wired checkout, and what `wk pr`
leaves behind in one.

`git.fetch_config` (lib/wk/git.py) gives every checkout
`url.<mirror>.insteadOf` for each URL in `git.REMOTES`, so a `git fetch
origin` in there reads this machine's mirror instead of the network. The
rewrite applies to *every* git transport, `ls-remote` included, and a mirror
keeps only origin's branches as its own heads -- a fork's branch lives under
`refs/remotes/fork/*` and a pull request head is not in it at all. So a
question about what the upstream itself has, asked with a plain `git
ls-remote` from inside a checkout, is answered by the mirror: empty, and with
git's own exit status 0.

`git.direct_url` is the one way past it -- the same repository spelled
without the `.git` the rewrite is keyed on -- and `pr.ls_remote` and
`pr.checkout`'s fetch both go through it. `pr.branch_repos` is the one
implementation of "which of this fork's repositories carries this branch",
shared by `wk pr <user>:<branch>` and `wk bench ab <user>:<branch>`.

Everything runs against local bare repositories standing in for the upstreams
and for the mirror -- git takes a path as a URL, so no network is touched, and
the rewrite is written by the real fetch_config over those same paths, which
is what puts it in the way of the very URL each question asks for.

Run: python3 tests/run.py -k tests.test_pr_upstream
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import re
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, bash, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, pr  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

GITHUB = "https://github.com/justinmichaud"


def _load_cmd_pr():
    """cmd/pr as a module -- a real file with no extension needs its loader spelled out."""
    path = str(REPO / "cmd" / "pr")
    loader = importlib.machinery.SourceFileLoader("wk_cmd_pr", path)
    spec = importlib.util.spec_from_file_location("wk_cmd_pr", path, loader=loader)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


CMD_PR_MODULE = _load_cmd_pr()


def git_(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def wire_fetches(src, mirror, remotes=git.REMOTES):
    subprocess.run(["sh", "-c", git.render(str(src), git.fetch_config(str(mirror), ["main"], remotes))],
                   check=True, capture_output=True)


class In(Local):
    """This machine, with every command run from inside `cwd` -- the checkout whose config is in the way."""

    def __init__(self, cwd):
        self.cwd = cwd

    def run(self, argv, input=None, timeout=None):
        cp = subprocess.run(argv, cwd=str(self.cwd), capture_output=True, text=True)
        return Result(cp.returncode, cp.stdout, cp.stderr)


class Here:
    """A workspace that is a directory here: exec runs the argv, with the checkout at `src`."""

    kind = "container"

    def __init__(self, src):
        self._src = str(src)
        self.ran = []

    def src(self, ws):
        return self._src

    def exec(self, ws, argv, tty=False, timeout=None):
        self.ran.append(list(argv))
        cp = subprocess.run(argv, capture_output=True, text=True)
        return Result(cp.returncode, cp.stdout, cp.stderr)

    act_exec = exec


class Wired(unittest.TestCase):
    """A checkout wired the way `wk new` wires one. The mirror holds origin's
    `main` as its own head and the fork's branch namespaced, which is the
    layout mirror_refresh_script writes; the fork holds `main` one commit
    ahead, so reading the wrong one is visible in the sha."""

    def setUp(self):
        self.dir = self.enterContext(scratch_dir(prefix="wk-test-upstream-"))
        self.mirror = self.dir / "mirror.git"
        self.fork = self.dir / "fork.git"
        self.src = self.dir / "src"

        seed = self.dir / "seed"
        seed.mkdir()
        git_("init", "-q", "-b", "main", str(seed))
        self.commit(seed, "one")
        self.origin_main = git_("rev-parse", "main", cwd=seed)
        git_("branch", "topic", cwd=seed)
        self.topic = self.origin_main

        for bare in (self.mirror, self.fork):
            git_("init", "-q", "--bare", "-b", "main", str(bare))
        # The same repository under a second name, which is what GitHub's two
        # spellings of one URL are: the rewrite is keyed on one of them.
        (self.dir / "fork").symlink_to(self.fork)
        git_("push", "-q", str(self.mirror), "main", cwd=seed)
        git_("push", "-q", str(self.mirror), "topic:refs/remotes/fork/topic", cwd=seed)
        self.commit(seed, "two")
        self.fork_main = git_("rev-parse", "main", cwd=seed)
        git_("push", "-q", str(self.fork), "main", "topic", cwd=seed)

        git_("clone", "-q", str(self.mirror), str(self.src))
        # The rewrite covers exactly the URLs the questions below ask for.
        self.remotes = (("origin", str(self.mirror)), ("fork", str(self.fork)))
        wire_fetches(self.src, self.mirror, self.remotes)

    def commit(self, repo, text):
        (repo / text).write_text(text + "\n")
        git_("add", text, cwd=repo)
        git_("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", text, cwd=repo)

    def in_src(self, script):
        return bash(f'cd "{self.src}"\n' + script)


class TestTheRewriteIsReallyInTheWay(Wired):
    """The defect everything else is for: in here the question is answered by
    the mirror, and neither git nor the caller is told."""

    def test_a_plain_ls_remote_finds_no_fork_branch_and_does_not_fail(self):
        cp = self.in_src(f'git ls-remote {self.fork} refs/heads/topic; echo "rc=$?"')
        self.assertEqual(cp.stdout.strip(), "rc=0", cp.stderr)

    def test_the_url_git_would_contact_is_the_mirror(self):
        cp = self.in_src(f'git ls-remote --get-url {self.fork}')
        self.assertEqual(cp.stdout.strip(), str(self.mirror), cp.stderr)


class TestLsRemote(Wired):
    def test_it_answers_from_the_fork_not_the_mirror(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/topic"), self.topic)

    def test_a_head_both_carry_is_read_from_the_fork(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/main"), self.fork_main)
        self.assertNotEqual(self.fork_main, self.origin_main)

    def test_a_ref_the_fork_does_not_have_is_empty(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/no-such"), "")


class TestBranchRepos(unittest.TestCase):
    """Which repositories get asked, and what the answers add up to. The
    upstream is a fake here -- what a rewrite does to the question is settled
    above; what matters below is that every repository is asked."""

    def ask(self, **sha_by_repo):
        here = Fake()

        def answer(argv, f):
            repo = argv[2].rsplit("/", 1)[-1]
            if repo in sha_by_repo:
                return Result(0, "%s\trefs/heads/topic\n" % sha_by_repo[repo])
            return Result(128, "", "repository not found")   # a fork with no such repository
        here.react(["git", "ls-remote"], answer)
        return here, pr.branch_repos(here, "justinmichaud", "topic")

    def test_both_projects_are_asked_past_the_rewrite(self):
        here, _ = self.ask()
        self.assertEqual([e[1][2] for e in here.effects], [f"{GITHUB}/WebKit", f"{GITHUB}/WPEWebKit"])

    def test_a_branch_only_the_second_project_has_is_found(self):
        """The reported defect: `wk pr justinmichaud:<branch>` said the branch
        was in neither project."""
        self.assertEqual(self.ask(WPEWebKit="beef")[1], [("WPEWebKit", f"{GITHUB}/WPEWebKit.git", "beef")])

    def test_a_branch_only_the_first_project_has_is_found(self):
        self.assertEqual(self.ask(WebKit="cafe")[1], [("WebKit", f"{GITHUB}/WebKit.git", "cafe")])

    def test_a_branch_in_both_is_reported_as_both(self):
        """So the caller refuses it by name rather than silently taking whichever was asked first."""
        self.assertEqual(len(self.ask(WebKit="cafe", WPEWebKit="beef")[1]), 2)

    def test_nothing_anywhere_is_nothing_and_not_a_failure(self):
        self.assertEqual(self.ask()[1], [])

    def test_the_refusals_name_every_repository_that_was_asked(self):
        self.assertEqual(pr.branch_repo_urls("justinmichaud"), [f"{GITHUB}/WebKit.git", f"{GITHUB}/WPEWebKit.git"])


class TestGitSyncFork(Wired):
    """`container/bin/git-sync-fork` asks the same question by remote name.
    Through the rewrite the fork reads as whatever the mirror's `main` is --
    which is origin's -- so it would report nothing to push, for ever."""

    def current_lines(self):
        """The two lines that decide what the fork is at, lifted and run as they ship."""
        script = (REPO / "container" / "bin" / "git-sync-fork").read_text().splitlines()
        first = next(i for i, l in enumerate(script) if l.startswith("url="))
        last = next(i for i, l in enumerate(script) if l.startswith("current="))
        self.assertLess(first, last, "git-sync-fork reads the URL before it asks")
        return "\n".join(script[first:last + 1])

    def test_it_reads_the_forks_head_not_the_mirrors(self):
        # fetch_config already made a `remote.fork` section (its fetch refspec), so the URL is set rather than the remote added.
        subprocess.run(["git", "config", "remote.fork.url", str(self.fork)], cwd=self.src, check=True, capture_output=True)
        self.assertTrue((self.dir / "fork").exists(), "the direct spelling must resolve")
        cp = self.in_src(f'remote=fork\nbranch=main\n{self.current_lines()}\necho "$current"')
        self.assertEqual(cp.stdout.strip(), self.fork_main, cp.stderr)


class TestPrOpenTarget(unittest.TestCase):
    """`wk pr open` reads the branch's upstream and its fork off the checkout
    it runs in, and a wired one rewrites every one of those URLs to the same
    mirror path -- through which both projects and both forks read alike, so
    the project a branch belongs to cannot be told from them."""

    def setUp(self):
        self.dir = self.enterContext(scratch_dir(prefix="wk-test-open-"))
        # Named as the store names it: through the rewrite every remote reads
        # as this path, whose basename then parses as the project "WebKit".
        self.mirror = self.dir / "WebKit.git"
        self.src = self.dir / "src"
        seed = self.dir / "seed"
        seed.mkdir()
        git_("init", "-q", "-b", "main", str(seed))
        (seed / "f").write_text("x\n")
        git_("add", "f", cwd=seed)
        git_("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=seed)
        git_("init", "-q", "--bare", "-b", "main", str(self.mirror))
        git_("push", "-q", str(self.mirror), "main", cwd=seed)
        git_("clone", "-q", str(self.mirror), str(self.src))
        wire_fetches(self.src, self.mirror)
        # The real URLs, which is what the rewrite just written is keyed on.
        for remote, url in git.REMOTES:
            git_("config", f"remote.{remote}.url", url, cwd=self.src)
        head = git_("rev-parse", "HEAD", cwd=self.src)
        for remote, _ in git.REMOTES:
            git_("update-ref", f"refs/remotes/{remote}/main", head, cwd=self.src)

    def target(self, upstream_remote, branch):
        git_("checkout", "-q", "-b", branch, cwd=self.src)
        git_("branch", "-q", f"--set-upstream-to={upstream_remote}/main", branch, cwd=self.src)
        return list(CMD_PR_MODULE.pr_open_target(self.src))

    def test_a_webkit_branch_opens_against_webkit_from_the_webkit_fork(self):
        self.assertEqual(self.target("origin", "eng/x"),
                         ["WebKit/WebKit", "justinmichaud:eng/x", "fork", "eng/x"])

    def test_a_wpe_branch_opens_against_wpewebkit_from_the_wpe_fork(self):
        """Through the rewrite both upstreams read as the same repository, so
        a WPEWebKit branch was pushed to the WebKit fork."""
        self.assertEqual(self.target("wpe", "eng/y"),
                         ["WebPlatformForEmbedded/WPEWebKit", "justinmichaud:eng/y", "forkwpe", "eng/y"])


class TestTheDirectSpellingEscapesEveryWiredRewrite(unittest.TestCase):
    """The invariant the whole mechanism rests on, checked against the real
    REMOTES and a real fetch_config rather than assumed: for every URL a
    checkout is wired to rewrite, the `.git`-less spelling of it is left
    alone."""

    def setUp(self):
        self.dir = self.enterContext(scratch_dir(prefix="wk-test-direct-"))
        self.mirror = self.dir / "WebKit.git"
        self.src = self.dir / "src"
        git_("init", "-q", "--bare", "-b", "main", str(self.mirror))
        git_("init", "-q", "-b", "main", str(self.src))
        wire_fetches(self.src, self.mirror)

    def get_url(self, url):
        return git_("ls-remote", "--get-url", url, cwd=self.src)

    def test_every_wired_url_is_rewritten_to_the_mirror(self):
        for _, url in git.REMOTES:
            self.assertEqual(self.get_url(url), str(self.mirror), url)

    def test_and_its_direct_spelling_is_not(self):
        for _, url in git.REMOTES:
            direct = git.direct_url(url)
            self.assertNotEqual(direct, url, f"{url} is not spelled with .git")
            self.assertEqual(self.get_url(direct), direct, direct)

    def test_an_account_that_is_not_wired_is_not_rewritten_either(self):
        """Which is why another account's fork needs no special handling."""
        url = "https://github.com/alice/WebKit.git"
        self.assertEqual(self.get_url(url), url)


class Checkout(Wired):
    """pr.checkout against the wired checkout, with the fork's repository standing in for GitHub's: its
    `.git` spelling is the one the checkout rewrites, and the fetch must take the other."""

    def run_checkout(self, spec, found=None, remotes=None, force=False):
        target = Here(self.src)
        rows = found if found is not None else [("WebKit", str(self.fork), git_("rev-parse", "topic", cwd=self.fork))]
        with mock.patch.object(pr, "branch_repos", lambda *a: rows), mock.patch.dict("os.environ", {"WK_FORCE": "1" if force else ""}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            pr.checkout(target, Local(), "ws", spec, remotes or self.remotes)
        return target, err.getvalue()

    def upstream(self, branch="topic"):
        return (git_("config", "--get", f"branch.{branch}.remote", cwd=self.src),
                git_("config", "--get", f"branch.{branch}.merge", cwd=self.src))


class TestThePrFetchRetiresNothing(Checkout):
    """`fetch.prune = true` ships in dotfiles/gitconfig, so every workspace
    has it. A `wk pr` fetch writes one ref inside refs/remotes/<remote>/ --
    the namespace that also holds origin/main, which is what git-webkit
    resolves a pull request's base against -- so it says so rather than
    leaving that to how the source happens to be spelled."""

    def test_the_gitconfig_every_workspace_gets_turns_prune_on(self):
        self.assertIn("prune", (REPO / "dotfiles" / "gitconfig").read_text(), "the hazard below is only real while this holds")

    def test_the_one_fetch_is_by_the_direct_url_with_no_prune(self):
        target, _ = self.run_checkout("justinmichaud:topic")
        fetches = [a for a in target.ran if "fetch" in a]
        self.assertEqual(len(fetches), 1, fetches)
        self.assertIn("--no-prune", fetches[0])
        self.assertIn(str(self.dir / "fork"), fetches[0])
        self.assertNotIn(str(self.fork), fetches[0])


class TestThePrBranchIsLeftPushable(Checkout):
    """What `wk pr <user>:<branch>` leaves behind: a branch whose upstream is
    the fork's branch *by name*, so a bare `git push` in the workspace works.

    The wiring is what makes this need saying. A non-origin remote is fetched
    as `+refs/remotes/<r>/*:refs/remotes/<r>/*` (fetch_refspecs), because
    the mirror keeps those namespaced -- and git derives an upstream by
    mapping the tracking ref back through that refspec, which here answers
    with the tracking ref itself."""

    def setUp(self):
        super().setUp()
        # `wk new` wires the URL beside the refspec the fixture already wrote.
        git_("remote", "set-url", "fork", str(self.fork), cwd=self.src)

    def push(self):
        return subprocess.run(["git", "push", "--dry-run"], cwd=str(self.src), capture_output=True, text=True)

    def test_git_derives_the_tracking_ref_itself_as_the_upstream(self):
        """The defect `track` exists for: `git branch --set-upstream-to` over
        this refspec records `refs/remotes/fork/topic` as the branch's
        upstream, and `git push` then refuses to guess what that means."""
        git_("fetch", "-q", "--no-prune", str(self.dir / "fork"), "refs/heads/topic:refs/remotes/fork/topic", cwd=self.src)
        git_("checkout", "-q", "-b", "topic", "refs/remotes/fork/topic", cwd=self.src)
        git_("branch", "--set-upstream-to=refs/remotes/fork/topic", "topic", cwd=self.src)
        self.assertEqual(self.upstream(), ("fork", "refs/remotes/fork/topic"))
        cp = self.push()
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("does not match", cp.stderr)

    def test_the_branch_tracks_the_forks_branch_by_name_and_a_bare_push_resolves(self):
        _, err = self.run_checkout("justinmichaud:topic")
        self.assertIn("'ws' is on topic (WebKit, from fork)", err)
        self.assertEqual(self.upstream(), ("fork", "refs/heads/topic"))
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("topic -> topic", cp.stderr + cp.stdout)

    def test_it_converges_over_an_upstream_already_recorded_wrong(self):
        self.run_checkout("justinmichaud:topic")
        git_("branch", "--set-upstream-to=refs/remotes/fork/topic", "topic", cwd=self.src)
        self.run_checkout("justinmichaud:topic")
        self.assertEqual(self.upstream(), ("fork", "refs/heads/topic"))

    def test_an_account_with_no_remote_gets_one_named_for_it(self):
        other = self.dir / "alice.git"
        git_("clone", "-q", "--bare", str(self.fork), str(other))
        (self.dir / "alice").symlink_to(other)
        target, err = self.run_checkout("alice:topic", found=[("WPEWebKit", str(other), git_("rev-parse", "topic", cwd=other))])
        self.assertEqual(git_("config", "--get", "remote.alice-wpewebkit.url", cwd=self.src), str(other))
        self.assertEqual(self.upstream(), ("alice-wpewebkit", "refs/heads/topic"))

    def test_local_commits_the_head_lacks_are_kept_unless_forced(self):
        self.run_checkout("justinmichaud:topic")
        self.commit(self.src, "mine")
        mine = git_("rev-parse", "HEAD", cwd=self.src)
        _, err = self.run_checkout("justinmichaud:topic")
        self.assertIn("local 'topic' has 1 commit(s) the PR head does not have", err)
        self.assertIn("wk pr ws justinmichaud:topic --force", err)
        self.assertEqual(git_("rev-parse", "HEAD", cwd=self.src), mine)
        _, err = self.run_checkout("justinmichaud:topic", force=True)
        self.assertIn("discarding 1 local commit(s)", err)
        self.assertEqual(git_("rev-parse", "HEAD", cwd=self.src), git_("rev-parse", "topic", cwd=self.fork))

    def test_a_pull_head_is_left_tracking_nothing(self):
        """`wk pr 1234` fetches `refs/pull/1234/head`, which is no branch on
        the remote: there is nothing to push back to, and an upstream naming
        one would send a bare push at the upstream project."""
        git_("update-ref", "refs/pull/1234/head", "topic", cwd=self.fork)
        self.run_checkout("1234", remotes=(("origin", str(self.fork)),))
        self.assertEqual(git_("rev-parse", "--abbrev-ref", "HEAD", cwd=self.src), "pr/1234")
        cp = subprocess.run(["git", "config", "--get", "branch.pr/1234.remote"], cwd=str(self.src), capture_output=True, text=True)
        self.assertEqual(cp.returncode, 1, cp.stdout)

    def test_no_such_pull_request_is_refused_before_anything_moves(self):
        target = Here(self.src)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.checkout(target, Local(), "ws", "999", (("origin", str(self.fork)),))
        self.assertIn("no pull request #999", err.getvalue())
        self.assertEqual(target.ran, [])

    def test_a_branch_in_both_projects_is_refused_by_name(self):
        rows = [("WebKit", "u1", "a"), ("WPEWebKit", "u2", "b")]
        target = Here(self.src)
        with mock.patch.object(pr, "branch_repos", lambda *a: rows), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(act.Refused):
            pr.checkout(target, Local(), "ws", "justinmichaud:topic")
        self.assertIn("exists in more than one of justinmichaud's repositories", err.getvalue())
        self.assertEqual(target.ran, [])

    def test_the_retarget_points_a_branch_the_same_way(self):
        """`wk sync --fix` moves a branch that tracks an upstream onto the fork
        it can be pushed to, through the same `track`. Its upstream arms are
        live where origin carries git's own refspec, since a branch tracking
        `origin/<b>` has no tracking ref under the narrowed one."""
        git_("fetch", "-q", "--no-prune", str(self.dir / "fork"), "refs/heads/topic:refs/remotes/fork/topic", cwd=self.src)
        git_("checkout", "-q", "-b", "topic", "refs/remotes/fork/topic", cwd=self.src)
        git_("config", "--replace-all", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*", cwd=self.src)
        git_("update-ref", "refs/remotes/origin/topic", "refs/remotes/fork/topic", cwd=self.src)
        git_("config", "branch.topic.remote", "origin", cwd=self.src)
        git_("config", "branch.topic.merge", "refs/heads/topic", cwd=self.src)
        git_("config", "remote.origin.url", git.REMOTES[0][1], cwd=self.src)
        forks = [("fork", "justinmichaud/WebKit", "github-webkit")]
        lines = pr.retarget(Here(self.src), "ws", str(self.src), forks, ["main"], (("origin", git.REMOTES[0][1]), ("fork", str(self.fork))))
        self.assertEqual(lines, ["retargeted: topic now tracks fork/topic"])
        self.assertEqual(self.upstream(), ("fork", "refs/heads/topic"))
        self.assertEqual(pr.retarget(Here(self.src), "ws", str(self.src), forks, ["main"]), [], "a branch on its fork is left alone")

    def test_neither_caller_points_a_branch_but_through_track(self):
        text = (REPO / "lib" / "wk" / "pr.py").read_text()
        self.assertNotIn("--set-upstream-to", text)
        self.assertNotIn('"branch", "-u"', text)
        self.assertEqual(len(re.findall(r"(?<!def )track\(src, ", text)), 2)


class TestTheBrokerServesTheMirrorRefresh(unittest.TestCase):
    """The workspace half of `wk sync` is one request, and the broker's answer
    to it is one `wk` command with no argument of its own -- a workspace may
    ask for this machine's mirror to be brought up to date, never for a URL of
    its choosing to be fetched."""

    def broker(self):
        spec = importlib.util.spec_from_file_location("wkbroker", REPO / "container" / "broker" / "wk-broker.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_sync_resolves_to_wk_sync_mirror(self):
        subject, argv, what = self.broker().VERBS["sync"][0]({})
        self.assertEqual(argv[1:], ["sync", "--mirror"])
        self.assertEqual(subject["name"], "store")
        self.assertIn("mirror", what)

    def test_it_is_reached_by_being_here_so_no_board_is_probed(self):
        subject, _, _ = self.broker().VERBS["sync"][0]({})
        self.assertTrue(subject.get("reach"))

    def test_it_takes_no_arguments(self):
        b = self.broker()
        with self.assertRaises(b.Refused):
            b.VERBS["sync"][0]({"machine": "rpi4"})

    def test_it_is_serialised_like_every_other_mutating_verb(self):
        self.assertTrue(self.broker().VERBS["sync"][1], "a mirror refresh must hold off a second one")


if __name__ == "__main__":
    unittest.main()
