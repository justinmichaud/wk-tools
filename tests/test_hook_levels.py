"""The pre-push hook's remote classification (git.hook_levels, lib/wk/git.py)."""
import re
import sys
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import git, secrets  # noqa: E402


def _forks(extra=()):
    return list(extra) or list(secrets.FORKS)


def _levels(extra=()):
    out = {}
    for key, value in re.findall(r"--level (\S+)=(\d+)", git.hook_levels(_forks(extra))):
        out[key] = int(value)
    return out


class TestHookLevels(WkTest):

    def test_each_fork_is_named_by_the_ssh_alias_it_pushes_through(self):
        levels = _levels()
        for _, repo, alias in _forks():
            self.assertEqual(levels.get(f"{alias}:{repo}"), 0, f"{alias}:{repo} unnamed")

    def test_the_list_is_derived_from_the_forks_not_written_out(self):
        levels = _levels([("extra", "someone/WebKit", "github-extra")])
        self.assertEqual(levels.get("github-extra:someone/WebKit"), 0)

    def test_every_level_is_public(self):
        self.assertEqual(_levels().get("no-push://use-a-fork-remote"), 0)
        for key, value in _levels().items():
            self.assertEqual(value, 0, f"{key} is not public")


class TestSetupScriptInstallsThem(WkTest):
    def _script(self):
        return git.gitwebkit_setup_script("/src/WebKit", _forks())

    def test_install_hooks_runs_with_the_levels(self):
        script = self._script()
        self.assertIn("git-webkit install-hooks $WK_HOOK_LEVELS", script)
        self.assertIn("--level no-push://use-a-fork-remote=0", script)

    def test_it_runs_for_a_checkout_already_set_up(self):
        script = self._script()
        before, _, after = script.partition("state=already")
        self.assertNotIn("exit 0", before + "state=already")
        self.assertIn("install-hooks", after)


if __name__ == "__main__":
    unittest.main()
