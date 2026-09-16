"""The pre-push hook's remote classification (lib/store.sh).

WebKit's Tools/Scripts/hooks/pre-push decides whether a commit is safe to push
publically from the URL `git remote -v` reports for the remote carrying it.
That is the rewritten URL, so a wired checkout shows the hook a bare mirror
path (which does not parse as a remote) and a no-push sentinel (which parses,
and as the push line wins over the fetch URL). Either way origin classifies as
uncategorized and the hook refuses every commit on main. wk_hook_levels names
those rewritten forms for `git-webkit install-hooks --level`.

Run: python3 -m unittest tests.test_hook_levels -v
"""
import re
import unittest

from tests.support import REPO, WkTest, bash


def _levels(extra=""):
    """wk_hook_levels' flag list, as {key: level}."""
    cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
{extra}
wk_hook_levels
''')
    assert cp.returncode == 0, f"wk_hook_levels failed: {cp.stdout}\n{cp.stderr}"
    out = {}
    for key, value in re.findall(r"--level (\S+)=(\d+)", cp.stdout):
        out[key] = int(value)
    return out


class TestHookLevels(WkTest):
    def test_the_sentinel_every_upstream_pushes_to_is_named_public(self):
        """origin and the other upstreams carry no-push://use-a-fork-remote (wk_wiring_script), and that is the URL the hook reads for them"""
        self.assertEqual(_levels().get("no-push://use-a-fork-remote"), 0)

    def test_each_fork_is_named_by_the_ssh_alias_it_pushes_through(self):
        """a fork's push URL is rewritten to its alias (wk_push_rewrite_config), so github.com is not what the hook sees"""
        levels = _levels()
        cp = bash(f'. "{REPO}/lib/common.sh"; . "{REPO}/lib/store.sh"; wk_push_forks')
        for line in cp.stdout.split("\n"):
            parts = line.split()
            if not parts:
                continue
            _, repo, alias = parts
            self.assertEqual(levels.get(f"{alias}:{repo}"), 0, f"{alias}:{repo} unnamed")

    def test_the_list_is_derived_from_the_forks_not_written_out(self):
        """a fork added to wk_push_forks is classified without touching wk_hook_levels"""
        levels = _levels(
            'wk_push_forks() { echo "extra  someone/WebKit  github-extra"; }'
        )
        self.assertEqual(levels.get("github-extra:someone/WebKit"), 0)

    def test_every_level_is_public(self):
        """wk wires only public repositories (wk_remotes); a secure remote must never carry the shared sentinel"""
        self.assertTrue(_levels())
        for key, value in _levels().items():
            self.assertEqual(value, 0, f"{key} is not public")


class TestSetupScriptInstallsThem(WkTest):
    def _script(self):
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
wk_gitwebkit_setup_script /src/WebKit
''')
        self.assertEqual(cp.returncode, 0, cp.stderr)
        return cp.stdout

    def test_install_hooks_runs_with_the_levels(self):
        script = self._script()
        self.assertIn("git-webkit install-hooks $WK_HOOK_LEVELS", script)
        self.assertIn("--level no-push://use-a-fork-remote=0", script)

    def test_it_runs_for_a_checkout_already_set_up(self):
        """`setup` bakes the hook without the levels, so the already-set-up branch must not return before install-hooks"""
        script = self._script()
        before, _, after = script.partition("state=already")
        self.assertNotIn("exit 0", before + "state=already")
        self.assertIn("install-hooks", after)

    def test_the_state_it_reports_still_distinguishes_the_two(self):
        """cmd/remotes' fix_gitwebkit reads the last line to say whether it changed anything"""
        script = self._script()
        self.assertIn("state=already", script)
        self.assertIn("state=ok", script)
        self.assertIn('echo "setup=$state"', script)

    def test_a_failed_install_is_reported_not_swallowed(self):
        self.assertIn("setup=hooks-failed", self._script())


if __name__ == "__main__":
    unittest.main()
