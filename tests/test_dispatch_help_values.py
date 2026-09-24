"""docs/PLAN.md, `unit dispatch.help_previews_and_lists_values`, the flag-list
half: completion lists the values a config-taking flag accepts from the one
command that declares them (`wk build`'s values=), on every command that
declares the flag.

Run: python3 -m unittest tests.test_dispatch_help_values -v
"""
import re
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import completion as C   # noqa: E402
from wk import decl as D          # noqa: E402


def _arms():
    script = C.generate(REPO, "bash", {})
    return dict(re.findall(r"^        ([a-z-]+)\) (.*) ;;$", script, re.M))


class TestConfigFlagListsTheValuesItAccepts(unittest.TestCase):
    def test_every_command_declaring_config_lists_build_configs(self):
        """every command declaring `--config=` completes it from build's values="""
        arms = _arms()
        takers = [d.name for d in D.all_commands(REPO) if C.CONFIG_FLAG in C.flags_for(d)]
        self.assertTrue(takers)
        for name in takers:
            with self.subTest(cmd=name):
                self.assertIn("_wk_config='yes'", arms[name])

    def test_a_command_without_config_does_not(self):
        """`wk new` declares no `--config`, so it lists no build configs"""
        self.assertIn("_wk_config=''", _arms()["new"])

    def test_the_values_come_from_builds_declaration(self):
        """the config list is asked of `wk build` with its declared values= flag"""
        build = next(d for d in D.all_commands(REPO) if d.name == "build")
        script = C.generate(REPO, "bash", {})
        self.assertIn("_wk_config_owner=build; _wk_config_values=%s" % build.values, script)


if __name__ == "__main__":
    unittest.main()
