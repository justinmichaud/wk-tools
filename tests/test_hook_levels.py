"""The pre-push hook's remote classification (git.hook_levels, lib/wk/git.py)."""
import re
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import git, secrets  # noqa: E402


def _levels(forks):
    return {key: int(value) for key, value in re.findall(r"--level (\S+)=(\d+)", git.hook_levels(forks))}


class TestHookLevels(unittest.TestCase):
    def test_every_fork_is_named_by_its_ssh_alias_and_every_level_is_public(self):
        forks = list(secrets.forks()) + [("extra", "someone/WebKit", "github-extra")]
        levels = _levels(forks)
        self.assertEqual({"%s:%s" % (alias, repo) for _, repo, alias in forks} | {git.NO_PUSH}, set(levels))
        self.assertEqual(set(levels.values()), {0})

    def test_the_hooks_are_installed_even_in_a_checkout_already_set_up(self):
        before, _, after = git.pr_tool_setup_script("/src/WebKit", list(secrets.forks())).partition("state=already")
        self.assertNotIn("exit 0", before)
        self.assertIn("install-hooks $WK_HOOK_LEVELS", after)


if __name__ == "__main__":
    unittest.main()
