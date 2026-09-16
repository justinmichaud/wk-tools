"""Asking GitHub a question from inside a wired checkout.

`wk_fetch_config` (lib/store.sh) gives every checkout `url.<mirror>.insteadOf`
for each URL in `wk_remotes`, so a `git fetch origin` in there reads this
machine's mirror instead of the network. The rewrite applies to *every* git
transport, `ls-remote` included, and a mirror keeps only origin's branches as
its own heads -- a fork's branch lives under `refs/remotes/fork/*` and a pull
request head is not in it at all. So a question about what the upstream itself
has, asked with a plain `git ls-remote` from inside a checkout, is answered by
the mirror: empty, and with git's own exit status 0.

`upstream_ls_remote` is the one way that question is asked (`git -C /`, outside
every repository, where no rewrite is configured); `pr_branch_repo` is the one
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

    def current_line(self):
        script = (REPO / "container" / "bin" / "git-sync-fork").read_text()
        line = [l for l in script.splitlines() if l.startswith("current=")]
        self.assertEqual(len(line), 1, "git-sync-fork no longer has one `current=` line")
        return line[0]

    def test_it_reads_the_forks_head_not_the_mirrors(self):
        # wk_fetch_config already made a `remote.fork` section (its fetch
        # refspec), so the URL is set rather than the remote added.
        subprocess.run(["git", "config", "remote.fork.url", str(self.fork)],
                       cwd=self.src, check=True, capture_output=True)
        cp = self.in_src(f'remote=fork\nbranch=main\n{self.current_line()}\necho "$current"')
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


if __name__ == "__main__":
    unittest.main()
