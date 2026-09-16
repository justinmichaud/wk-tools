"""Asking GitHub a question from inside a wired checkout.

`wk_fetch_config` (lib/store.sh) gives every checkout `url.<mirror>.insteadOf`
for each URL in `wk_remotes`, so a `git fetch origin` in there reads this
machine's mirror instead of the network. The rewrite applies to *every* git
transport, `ls-remote` included, and a mirror keeps only origin's branches as
its own heads -- a fork's branch lives under `refs/remotes/fork/*` and a pull
request head is not in it at all. So a question about what the upstream itself
has, asked with a plain `git ls-remote` from inside a checkout, is answered by
the mirror: empty, and with git's own exit status 0.

`upstream_direct_url` is the one way past it -- the same repository spelled
without the `.git` the rewrite is keyed on -- and `upstream_ls_remote` and
`wk_pr_checkout`'s fetch both go through it. `pr_branch_repo` is the one
implementation of "which of this fork's repositories carries this branch",
shared by `wk pr <user>:<branch>` and `wk ab <user>:<branch>`.

Everything runs against local bare repositories standing in for the upstreams
and for the mirror -- git takes a path as a URL, so no network is touched, and
the rewrite is written by the real `wk_fetch_config` over those same paths,
which is what puts it in the way of the very URL each question asks for.

Run: python3 -m unittest tests.test_pr_upstream -v
"""
import subprocess
import unittest

from tests.support import REPO, bash, scratch_dir

# Sourced by absolute path: every script below runs in the wired checkout,
# never in this repository.
PRELUDE = f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/lib/store.sh"\n'

GITHUB = "https://github.com/justinmichaud"


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


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
        git("init", "-q", "-b", "main", str(seed))
        self.commit(seed, "one")
        self.origin_main = git("rev-parse", "main", cwd=seed)
        git("branch", "topic", cwd=seed)
        self.topic = self.origin_main

        for bare in (self.mirror, self.fork):
            git("init", "-q", "--bare", "-b", "main", str(bare))
        # The same repository under a second name, which is what GitHub's two
        # spellings of one URL are: the rewrite is keyed on one of them.
        (self.dir / "fork").symlink_to(self.fork)
        git("push", "-q", str(self.mirror), "main", cwd=seed)
        git("push", "-q", str(self.mirror), "topic:refs/remotes/fork/topic", cwd=seed)
        self.commit(seed, "two")
        self.fork_main = git("rev-parse", "main", cwd=seed)
        git("push", "-q", str(self.fork), "main", "topic", cwd=seed)

        git("clone", "-q", str(self.mirror), str(self.src))
        script = bash(PRELUDE + self.stub_remotes + f'wk_fetch_config {self.mirror}').stdout
        self.assertNotEqual(script.strip(), "")
        subprocess.run(["bash", "-c", script], cwd=self.src, check=True,
                       capture_output=True)

    def commit(self, repo, text):
        (repo / text).write_text(text + "\n")
        git("add", text, cwd=repo)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", text, cwd=repo)

    @property
    def stub_remotes(self):
        """wk_remotes over the local bare repositories, so the rewrite the
        checkout gets covers exactly the URLs the questions below ask for."""
        return (f'wk_remotes() {{ cat <<EOF\n'
                f'origin   {self.mirror}\n'
                f'fork     {self.fork}\n'
                f'EOF\n}}\n')

    def in_src(self, script):
        return bash(PRELUDE + f'cd "{self.src}"\n' + script)


class TestTheRewriteIsReallyInTheWay(Wired):
    """The defect everything else is for: in here the question is answered by
    the mirror, and neither git nor the caller is told."""

    def test_a_plain_ls_remote_finds_no_fork_branch_and_does_not_fail(self):
        cp = self.in_src(f'git ls-remote {self.fork} refs/heads/topic; echo "rc=$?"')
        self.assertEqual(cp.stdout.strip(), "rc=0", cp.stderr)

    def test_the_url_git_would_contact_is_the_mirror(self):
        cp = self.in_src(f'git ls-remote --get-url {self.fork}')
        self.assertEqual(cp.stdout.strip(), str(self.mirror), cp.stderr)


class TestUpstreamLsRemote(Wired):
    def test_it_answers_from_the_fork_not_the_mirror(self):
        cp = self.in_src(f'upstream_ls_remote {self.fork} refs/heads/topic')
        self.assertIn(self.topic, cp.stdout, cp.stderr)

    def test_a_head_both_carry_is_read_from_the_fork(self):
        cp = self.in_src(f'upstream_ls_remote {self.fork} refs/heads/main')
        self.assertIn(self.fork_main, cp.stdout, cp.stderr)
        self.assertNotIn(self.origin_main, cp.stdout)

    def test_a_ref_the_fork_does_not_have_is_empty(self):
        cp = self.in_src(f'upstream_ls_remote {self.fork} refs/heads/no-such')
        self.assertEqual(cp.stdout.strip(), "", cp.stderr)
        self.assertEqual(cp.returncode, 0, cp.stderr)


class TestPrBranchRepo(Wired):
    """Which repositories get asked, and what the answers add up to. The
    upstream call is stubbed here -- what it does to a rewrite is settled
    above; what matters below is that every repository is asked."""

    def with_answers(self, **sha_by_repo):
        answers = "\n".join(
            f'        {GITHUB}/{repo}.git) echo "{sha}\trefs/heads/x" ;;'
            for repo, sha in sha_by_repo.items())
        # Recorded to a file: pr_branch_repo sends the call's own stderr to
        # /dev/null, which is where git's "repository not found" belongs.
        self.asked = self.dir / "asked"
        return ('upstream_ls_remote() {\n'
                f'    echo "$1" >>{self.asked}\n'
                '    case "$1" in\n'
                f'{answers}\n'
                '        *) return 128 ;;\n'   # a fork with no such repository
                '    esac\n'
                '}\n')

    def test_both_projects_are_asked(self):
        self.in_src(self.with_answers() + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(self.asked.read_text().split(),
                         [f"{GITHUB}/WebKit.git", f"{GITHUB}/WPEWebKit.git"])

    def test_a_branch_only_the_second_project_has_is_found(self):
        """The reported defect: `wk pr justinmichaud:<branch>` said the branch
        was in neither project."""
        cp = self.in_src(self.with_answers(WPEWebKit="beef") + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual(cp.stdout.split(),
                         ["WPEWebKit", f"{GITHUB}/WPEWebKit.git", "beef"], cp.stdout)

    def test_a_branch_only_the_first_project_has_is_found(self):
        cp = self.in_src(self.with_answers(WebKit="cafe") + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(cp.stdout.split(),
                         ["WebKit", f"{GITHUB}/WebKit.git", "cafe"], cp.stdout)

    def test_a_branch_in_both_is_reported_as_both(self):
        """Two lines, so the caller refuses it by name rather than silently
        taking whichever repository was asked first."""
        cp = self.in_src(self.with_answers(WebKit="cafe", WPEWebKit="beef")
                         + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(len(cp.stdout.strip().splitlines()), 2, cp.stdout)

    def test_a_fork_with_no_such_repository_is_not_a_failure(self):
        """`git ls-remote` answers 128 for a repository that is not there, and
        under `pipefail` that would otherwise become this function's status."""
        cp = self.in_src(self.with_answers(WebKit="cafe") + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_nothing_anywhere_is_nothing_and_not_a_failure(self):
        cp = self.in_src(self.with_answers() + 'pr_branch_repo justinmichaud topic')
        self.assertEqual(cp.stdout.strip(), "", cp.stderr)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_the_refusals_name_every_repository_that_was_asked(self):
        cp = bash(PRELUDE + 'pr_branch_repo_urls justinmichaud')
        self.assertEqual(cp.stdout.split(),
                         [f"{GITHUB}/WebKit.git", f"{GITHUB}/WPEWebKit.git"], cp.stdout)


class TestGitSyncFork(Wired):
    """`container/bin/git-sync-fork` asks the same question by remote name.
    Through the rewrite the fork reads as whatever the mirror's `main` is --
    which is origin's -- so it would report nothing to push, for ever."""

    def current_lines(self):
        """The two lines that decide what the fork is at, lifted and run as
        they ship -- not a retyped copy of them."""
        script = (REPO / "container" / "bin" / "git-sync-fork").read_text().splitlines()
        first = next(i for i, l in enumerate(script) if l.startswith("url="))
        last = next(i for i, l in enumerate(script) if l.startswith("current="))
        self.assertLess(first, last, "git-sync-fork reads the URL before it asks")
        return "\n".join(script[first:last + 1])

    def test_it_reads_the_forks_head_not_the_mirrors(self):
        # wk_fetch_config already made a `remote.fork` section (its fetch
        # refspec), so the URL is set rather than the remote added.
        subprocess.run(["git", "config", "remote.fork.url", str(self.fork)],
                       cwd=self.src, check=True, capture_output=True)
        self.assertTrue((self.dir / "fork").exists(), "the direct spelling must resolve")
        cp = self.in_src(f'remote=fork\nbranch=main\n{self.current_lines()}\necho "$current"')
        self.assertEqual(cp.stdout.strip(), self.fork_main, cp.stderr)
        self.assertNotEqual(self.fork_main, self.origin_main)


class TestPrOpenTarget(unittest.TestCase):
    """`wk pr open` reads the branch's upstream and its fork off the checkout
    it runs in, and a wired one rewrites every one of those URLs to the same
    mirror path -- through which both projects and both forks read alike, so
    the project a branch belongs to cannot be told from them."""

    def setUp(self):
        self.dir = self.enterContext(scratch_dir(prefix="wk-test-open-"))
        # Named as wk_mirror names it: through the rewrite every remote reads
        # as this path, whose basename then parses as the project "WebKit".
        self.mirror = self.dir / "WebKit.git"
        self.src = self.dir / "src"
        seed = self.dir / "seed"
        seed.mkdir()
        git("init", "-q", "-b", "main", str(seed))
        (seed / "f").write_text("x\n")
        git("add", "f", cwd=seed)
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c", cwd=seed)
        git("init", "-q", "--bare", "-b", "main", str(self.mirror))
        git("push", "-q", str(self.mirror), "main", cwd=seed)
        git("clone", "-q", str(self.mirror), str(self.src))
        script = bash(PRELUDE + f'wk_fetch_config {self.mirror}').stdout
        subprocess.run(["bash", "-c", script], cwd=self.src, check=True, capture_output=True)
        # The real URLs, which is what the rewrite just written is keyed on.
        rows = [l.split() for l in bash(PRELUDE + 'wk_remotes').stdout.splitlines() if l.strip()]
        for remote, url in rows:
            git("config", f"remote.{remote}.url", url, cwd=self.src)
        head = git("rev-parse", "HEAD", cwd=self.src)
        for remote, _ in rows:
            git("update-ref", f"refs/remotes/{remote}/main", head, cwd=self.src)

    def target(self, upstream_remote, branch):
        git("checkout", "-q", "-b", branch, cwd=self.src)
        git("branch", "-q", f"--set-upstream-to={upstream_remote}/main", branch, cwd=self.src)
        prelude = (PRELUDE
                   + subprocess.run(["sed", "-n", "/^pr_open_target()/,/^}/p",
                                     str(REPO / "cmd" / "pr")],
                                    capture_output=True, text=True).stdout)
        cp = bash(prelude + f'pr_open_target "{self.src}"')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip().split("\t")

    def test_a_webkit_branch_opens_against_webkit_from_the_webkit_fork(self):
        self.assertEqual(self.target("origin", "eng/x"),
                         ["WebKit/WebKit", "justinmichaud:eng/x", "fork", "eng/x"])

    def test_a_wpe_branch_opens_against_wpewebkit_from_the_wpe_fork(self):
        """Through the rewrite both upstreams read as the same repository, so
        a WPEWebKit branch was pushed to the WebKit fork."""
        self.assertEqual(self.target("wpe", "eng/y"),
                         ["WebPlatformForEmbedded/WPEWebKit", "justinmichaud:eng/y",
                          "forkwpe", "eng/y"])


class TestTheDirectSpellingEscapesEveryWiredRewrite(unittest.TestCase):
    """The invariant the whole mechanism rests on, checked against the real
    wk_remotes and a real wk_fetch_config rather than assumed: for every URL
    a checkout is wired to rewrite, the `.git`-less spelling of it is left
    alone. Change wk_remotes to the other spelling and this is what says so,
    rather than `wk pr` quietly fetching from the mirror again."""

    def setUp(self):
        self.dir = self.enterContext(scratch_dir(prefix="wk-test-direct-"))
        self.mirror = self.dir / "WebKit.git"
        self.src = self.dir / "src"
        git("init", "-q", "--bare", "-b", "main", str(self.mirror))
        git("init", "-q", "-b", "main", str(self.src))
        script = bash(PRELUDE + f'wk_fetch_config {self.mirror}').stdout
        subprocess.run(["bash", "-c", script], cwd=self.src, check=True, capture_output=True)

    def wired_urls(self):
        rows = [l.split() for l in bash(PRELUDE + 'wk_remotes').stdout.splitlines() if l.strip()]
        self.assertTrue(rows)
        return [url for _, url in rows]

    def test_every_wired_url_is_rewritten_to_the_mirror(self):
        for url in self.wired_urls():
            got = bash(PRELUDE + f'cd "{self.src}"\ngit ls-remote --get-url {url}')
            self.assertEqual(got.stdout.strip(), str(self.mirror), url)

    def test_and_its_direct_spelling_is_not(self):
        for url in self.wired_urls():
            direct = bash(PRELUDE + f'upstream_direct_url {url}').stdout.strip()
            self.assertNotEqual(direct, url, f"{url} is not spelled with .git")
            got = bash(PRELUDE + f'cd "{self.src}"\ngit ls-remote --get-url {direct}')
            self.assertEqual(got.stdout.strip(), direct, direct)

    def test_an_account_that_is_not_wired_is_not_rewritten_either(self):
        """Which is why another account's fork needs no special handling: the
        rewrite only ever names the four repositories wk wires."""
        url = "https://github.com/alice/WebKit.git"
        got = bash(PRELUDE + f'cd "{self.src}"\ngit ls-remote --get-url {url}')
        self.assertEqual(got.stdout.strip(), url, got.stdout)


class TestTheBrokerServesTheMirrorRefresh(unittest.TestCase):
    """The workspace half of `wk sync` and of `wk pr <wired fork>:<branch>`
    is one request, and the broker's answer to it is one `wk` command with
    no argument of its own -- a workspace may ask for this machine's mirror
    to be brought up to date, never for a URL of its choosing to be fetched."""

    def broker(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wkbroker", REPO / "container" / "broker" / "wk-broker.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_sync_resolves_to_wk_sync_mirror(self):
        b = self.broker()
        subject, argv, what = b.VERBS["sync"][0]({})
        self.assertEqual(argv[1:], ["sync", "--mirror"])
        self.assertEqual(subject["name"], "store")
        self.assertIn("mirror", what)

    def test_it_is_reached_by_being_here_so_no_board_is_probed(self):
        """run_request takes the subject's own reach when it has one; a
        store verb must not send `reach` at a machine that does not exist."""
        b = self.broker()
        subject, _, _ = b.VERBS["sync"][0]({})
        self.assertTrue(subject.get("reach"))

    def test_it_takes_no_arguments(self):
        b = self.broker()
        with self.assertRaises(b.Refused):
            b.VERBS["sync"][0]({"machine": "rpi4"})

    def test_it_is_serialised_like_every_other_mutating_verb(self):
        b = self.broker()
        self.assertTrue(b.VERBS["sync"][1], "a mirror refresh must hold off a second one")


if __name__ == "__main__":
    unittest.main()
