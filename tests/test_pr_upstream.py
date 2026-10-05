"""Asking GitHub a question from inside a wired checkout, and what `wk pr`
leaves behind in one."""
import contextlib
import importlib.util
import io
import subprocess
import sys
import unittest
from unittest import mock

from tests.support import REPO, bash, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import act, git, pr  # noqa: E402
from wk.machine import Fake, Local, Result  # noqa: E402

GITHUB = "https://github.com/justinmichaud"


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


class TestLsRemote(Wired):
    def test_it_answers_from_the_fork_not_the_mirror(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/topic"), self.topic)

    def test_a_head_both_carry_is_read_from_the_fork(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/main"), self.fork_main)
        self.assertNotEqual(self.fork_main, self.origin_main)

    def test_a_ref_the_fork_does_not_have_is_empty(self):
        self.assertEqual(pr.ls_remote(In(self.src), str(self.fork), "refs/heads/no-such"), "")


class TestBranchRepos(unittest.TestCase):

    def ask(self, **sha_by_repo):
        here = Fake()

        def answer(argv, f):
            repo = argv[2].rsplit("/", 1)[-1]
            if repo in sha_by_repo:
                return Result(0, "%s\trefs/heads/topic\n" % sha_by_repo[repo])
            return Result(128, "", "repository not found")   # a fork with no such repository
        here.react(["git", "ls-remote"], answer)
        return here, pr.branch_repos(here, "justinmichaud", "topic")

    def test_both_projects_are_asked_and_each_one_carrying_the_branch_is_found(self):
        here, _ = self.ask()
        self.assertEqual([e[1][2] for e in here.effects], [f"{GITHUB}/WebKit", f"{GITHUB}/WPEWebKit"])
        self.assertEqual(self.ask()[1], [])
        self.assertEqual(self.ask(WPEWebKit="beef")[1], [("WPEWebKit", f"{GITHUB}/WPEWebKit.git", "beef")])
        self.assertEqual(self.ask(WebKit="cafe")[1], [("WebKit", f"{GITHUB}/WebKit.git", "cafe")])
        self.assertEqual(len(self.ask(WebKit="cafe", WPEWebKit="beef")[1]), 2)


class TestGitSyncFork(Wired):

    def current_lines(self):
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


class TestTheDirectSpellingEscapesEveryWiredRewrite(unittest.TestCase):

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
        url = "https://github.com/alice/WebKit.git"
        self.assertEqual(self.get_url(url), url)


class Checkout(Wired):

    def run_checkout(self, spec, found=None, remotes=None, force=False):
        driver = Here(self.src)
        rows = found if found is not None else [("WebKit", str(self.fork), git_("rev-parse", "topic", cwd=self.fork))]
        with mock.patch.object(pr, "branch_repos", lambda *a: rows), mock.patch.dict("os.environ", {"WK_FORCE": "1" if force else ""}), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            pr.checkout(driver, Local(), "ws", spec, remotes or self.remotes)
        return driver, err.getvalue()

    def upstream(self, branch="topic"):
        return (git_("config", "--get", f"branch.{branch}.remote", cwd=self.src),
                git_("config", "--get", f"branch.{branch}.merge", cwd=self.src))


class TestThePrFetchRetiresNothing(Checkout):


    def test_the_one_fetch_is_by_the_direct_url_with_no_prune(self):
        driver, _ = self.run_checkout("justinmichaud:topic")
        fetches = [a for a in driver.ran if "fetch" in a]
        self.assertEqual(len(fetches), 1, fetches)
        self.assertIn("--no-prune", fetches[0])
        self.assertIn(str(self.dir / "fork"), fetches[0])
        self.assertNotIn(str(self.fork), fetches[0])


class Pushable(Checkout):
    """A wired checkout whose fork remote has a real URL and pushes land in the fork."""

    def setUp(self):
        super().setUp()
        # `wk new` wires the URL beside the refspec the fixture already wrote.
        git_("remote", "set-url", "fork", str(self.fork), cwd=self.src)
        git_("config", "push.default", "current", cwd=self.src)
        # A push is not rewritten to the mirror, so the user's own pushes land in the fork.
        git_("config", f"url.{self.dir / 'fork'}.pushInsteadOf", str(self.fork), cwd=self.src)
        self.forks = [("fork", "justinmichaud/WebKit", "github-webkit")]

    def check(self):
        script = git.wiring_check_script(str(self.src), "", self.forks, ["main"], skip_env=True)
        return subprocess.run(["sh", "-c", script], cwd=str(self.src), capture_output=True, text=True).stdout

    def resolves(self):
        return git_("rev-parse", "--abbrev-ref", "@{u}", cwd=self.src)


class TestThePrBranchIsLeftPushable(Pushable):

    def assert_bare_push_goes_to_own_name(self, name):
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("%s -> %s" % (name, name), cp.stderr + cp.stdout)

    def test_a_real_push_u_is_reported_and_converged(self):
        git_("checkout", "-q", "-b", "mine", cwd=self.src)
        self.commit(self.src, "mine")
        git_("push", "-q", "-u", "fork", "mine", cwd=self.src)
        self.assertEqual(self.upstream("mine"), ("fork", "refs/heads/mine"))
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "@{u}"], cwd=str(self.src), capture_output=True).returncode, 0)
        self.assertIn("branch mine was set up by git push -u", self.check())
        self.assertIn("wk sync --fix", self.check())
        self.assertEqual(pr.converge(Here(self.src), "ws", str(self.src), self.forks), ["converged: mine tracks fork/mine"])
        # The next `wk sync` brings the fork's branch into the mirror, and the checkout's fetch brings it here.
        git_("push", "-q", str(self.mirror), "mine:refs/remotes/fork/mine", cwd=self.src)
        git_("fetch", "-q", "fork", cwd=self.src)
        self.assertEqual(self.resolves(), "fork/mine")
        self.assertNotIn("push -u", self.check())
        self.assert_bare_push_goes_to_own_name("mine")

    def test_a_real_checkout_track_resolves_and_pushes(self):
        git_("fetch", "-q", "fork", cwd=self.src)
        git_("checkout", "-q", "--track", "fork/topic", cwd=self.src)
        self.assertEqual(self.resolves(), "fork/topic")
        self.assertNotIn("push -u", self.check())
        self.assert_bare_push_goes_to_own_name("topic")

    def test_a_real_branch_u_resolves_and_pushes(self):
        git_("fetch", "-q", "fork", cwd=self.src)
        git_("checkout", "-q", "-b", "other", cwd=self.src)
        git_("branch", "-u", "fork/topic", cwd=self.src)
        self.assertEqual(self.resolves(), "fork/topic")
        self.assertNotIn("push -u", self.check())
        self.assert_bare_push_goes_to_own_name("other")

    def push(self):
        self.commit(self.src, "work-%s" % git_("rev-parse", "--short", "HEAD", cwd=self.src))
        return subprocess.run(["git", "push", "--dry-run"], cwd=str(self.src), capture_output=True, text=True)

    def test_git_derives_the_tracking_ref_itself_as_the_upstream(self):
        git_("fetch", "-q", "--no-prune", str(self.dir / "fork"), "refs/heads/topic:refs/remotes/fork/topic", cwd=self.src)
        git_("checkout", "-q", "-b", "topic", "refs/remotes/fork/topic", cwd=self.src)
        git_("branch", "--set-upstream-to=refs/remotes/fork/topic", "topic", cwd=self.src)
        self.assertEqual(self.upstream(), ("fork", "refs/remotes/fork/topic"))
        git_("config", "push.default", "simple", cwd=self.src)
        cp = self.push()
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("does not match", cp.stderr)

    def test_the_branch_tracks_the_forks_branch_by_name_and_a_bare_push_resolves(self):
        _, err = self.run_checkout("justinmichaud:topic")
        self.assertIn("'ws' is on topic (WebKit, from fork)", err)
        self.assertEqual(self.upstream(), ("fork", "refs/remotes/fork/topic"))
        self.assertEqual(git_("rev-parse", "--abbrev-ref", "@{u}", cwd=self.src), "fork/topic")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("topic -> topic", cp.stderr + cp.stdout)

    def test_a_push_u_is_converged_by_sync_fix(self):
        self.run_checkout("justinmichaud:topic")
        git_("config", "branch.topic.merge", "refs/heads/topic", cwd=self.src)
        self.assertNotEqual(subprocess.run(["git", "rev-parse", "@{u}"], cwd=str(self.src), capture_output=True).returncode, 0)
        self.assertEqual(pr.converge(Here(self.src), "ws", str(self.src), [("fork", "justinmichaud/WebKit", "github-webkit")]),
                         ["converged: topic tracks fork/topic"])
        self.assertEqual(git_("rev-parse", "--abbrev-ref", "@{u}", cwd=self.src), "fork/topic")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("topic -> topic", cp.stderr + cp.stdout)
        self.assertEqual(pr.converge(Here(self.src), "ws", str(self.src), [("fork", "justinmichaud/WebKit", "github-webkit")]), [])

    def test_the_second_fork_converges_the_same_way(self):
        git_("remote", "add", "forkwpe", str(self.fork), cwd=self.src)
        git_("config", "remote.forkwpe.fetch", "+refs/remotes/forkwpe/*:refs/remotes/forkwpe/*", cwd=self.src)
        git_("push", "-q", str(self.mirror), "topic:refs/remotes/forkwpe/topic", cwd=self.fork)
        git_("fetch", "-q", "forkwpe", cwd=self.src)
        git_("checkout", "-q", "-b", "topic", "refs/remotes/forkwpe/topic", cwd=self.src)
        git_("config", "branch.topic.remote", "forkwpe", cwd=self.src)
        git_("config", "branch.topic.merge", "refs/heads/topic", cwd=self.src)
        forks = [("fork", "justinmichaud/WebKit", "github-webkit"), ("forkwpe", "justinmichaud/WPEWebKit", "github-wpewebkit")]
        self.assertEqual(pr.converge(Here(self.src), "ws", str(self.src), forks), ["converged: topic tracks forkwpe/topic"])
        self.assertEqual(git_("rev-parse", "--abbrev-ref", "@{u}", cwd=self.src), "forkwpe/topic")
        cp = self.push()
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertIn("topic -> topic", cp.stderr + cp.stdout)

    def test_origin_and_wpe_refuse_a_bare_push(self):
        for remote in ("origin", "wpe"):
            if remote == "wpe":
                git_("remote", "add", "wpe", str(self.fork), cwd=self.src)
            git_("remote", "set-url", "--push", remote, git.NO_PUSH, cwd=self.src)
            git_("branch", "-f", "t-" + remote, "main", cwd=self.src)
            git_("checkout", "-q", "t-" + remote, cwd=self.src)
            git_("config", "branch.t-%s.remote" % remote, remote, cwd=self.src)
            git_("config", "branch.t-%s.merge" % remote, "refs/heads/main", cwd=self.src)
            cp = self.push()
            self.assertNotEqual(cp.returncode, 0, remote + cp.stdout)
            self.assertIn("no-push", cp.stderr)

    def test_it_converges_over_an_upstream_already_recorded_wrong(self):
        self.run_checkout("justinmichaud:topic")
        git_("branch", "--set-upstream-to=refs/remotes/fork/topic", "topic", cwd=self.src)
        self.run_checkout("justinmichaud:topic")
        self.assertEqual(self.upstream(), ("fork", "refs/remotes/fork/topic"))

    def test_an_account_with_no_remote_gets_one_named_for_it(self):
        other = self.dir / "alice.git"
        git_("clone", "-q", "--bare", str(self.fork), str(other))
        (self.dir / "alice").symlink_to(other)
        driver, err = self.run_checkout("alice:topic", found=[("WPEWebKit", str(other), git_("rev-parse", "topic", cwd=other))])
        self.assertEqual(git_("config", "--get", "remote.alice-wpewebkit.url", cwd=self.src), str(other))
        self.assertEqual(self.upstream(), ("alice-wpewebkit", "refs/heads/topic"))
        self.assertEqual(self.resolves(), "alice-wpewebkit/topic")
        git_("pull", "--ff-only", "-q", cwd=self.src)

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
        git_("update-ref", "refs/pull/1234/head", "topic", cwd=self.fork)
        self.run_checkout("1234", remotes=(("origin", str(self.fork)),))
        self.assertEqual(git_("rev-parse", "--abbrev-ref", "HEAD", cwd=self.src), "pr/1234")
        cp = subprocess.run(["git", "config", "--get", "branch.pr/1234.remote"], cwd=str(self.src), capture_output=True, text=True)
        self.assertEqual(cp.returncode, 1, cp.stdout)

    def test_no_such_pull_request_is_refused_before_anything_moves(self):
        driver = Here(self.src)
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            pr.checkout(driver, Local(), "ws", "999", (("origin", str(self.fork)),))
        self.assertIn("no pull request #999", err.getvalue())
        self.assertEqual(driver.ran, [])

    def test_a_branch_in_both_projects_is_refused_by_name(self):
        rows = [("WebKit", "u1", "a"), ("WPEWebKit", "u2", "b")]
        driver = Here(self.src)
        with mock.patch.object(pr, "branch_repos", lambda *a: rows), contextlib.redirect_stderr(io.StringIO()) as err, \
                self.assertRaises(act.Refused):
            pr.checkout(driver, Local(), "ws", "justinmichaud:topic")
        self.assertIn("exists in more than one of justinmichaud's repositories", err.getvalue())
        self.assertEqual(driver.ran, [])

    def test_the_retarget_points_a_branch_the_same_way(self):
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
        self.assertEqual(self.upstream(), ("fork", "refs/remotes/fork/topic"))
        self.assertEqual(pr.retarget(Here(self.src), "ws", str(self.src), forks, ["main"]), [], "a branch on its fork is left alone")


class TestTheMergeRefFollowsTheRemotesRefspec(Pushable):

    def test_a_heads_refspec_fork_is_not_flagged_and_not_rewritten(self):
        git_("config", "--replace-all", "remote.fork.fetch", "+refs/heads/*:refs/remotes/fork/*", cwd=self.src)
        git_("fetch", "-q", "--no-prune", str(self.dir / "fork"), "refs/heads/topic:refs/remotes/fork/topic", cwd=self.src)
        git_("checkout", "-q", "-b", "mine", "fork/topic", cwd=self.src)
        git_("config", "branch.mine.remote", "fork", cwd=self.src)
        git_("config", "branch.mine.merge", "refs/heads/topic", cwd=self.src)
        self.assertEqual(self.resolves(), "fork/topic")
        self.assertNotIn("push -u", self.check())
        self.assertEqual(pr.converge(Here(self.src), "ws", str(self.src), self.forks), [])
        self.assertEqual(self.upstream("mine"), ("fork", "refs/heads/topic"))


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
