"""`wk help` prints README.md, the one document; `wk help <topic>` prints the
sections whose heading names the topic, and an unknown topic lists the
headings there are. The lifecycle section is the one a newcomer follows from
a bare Pi to an automated A/B, so it has to name every command on that path.

Run: python3 -m unittest tests.test_help_topics -v
"""
import unittest

from tests.support import REPO, run


class TestHelpTopics(unittest.TestCase):
    def test_bare_help_is_the_whole_readme(self):
        cp = run("help")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), (REPO / "README.md").read_text().strip())

    def test_a_topic_prints_that_section_only(self):
        cp = run("help", "lifecycle")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(cp.stdout.startswith("## Lifecycle"), cp.stdout[:200])
        self.assertNotIn("## Architecture", cp.stdout)
        self.assertNotIn("## Where the rest is", cp.stdout)

    def test_the_lifecycle_names_every_step_from_bare_board_to_ab(self):
        out = run("help", "lifecycle").stdout
        for step in ("boot/machines/<name>.conf", "wk sysimage build", "wk sysimage disks",
                     "wk sysimage write", "--rescue", "@second", "wk pi boot-order",
                     "wk boot", "--keep", "wk sysimage webkit", "wk pi deploy",
                     "wk pi bench", "--ab", "wk bench report", "wk ab",
                     "setup --stage quiesce", "admin console", "wpewebkit-dirclean",
                     "S50dropbear", "wk enter", "tailscaled.log"):
            self.assertIn(step, out, f"the lifecycle section does not mention {step!r}")

    def test_an_unknown_topic_lists_the_topics(self):
        cp = run("help", "nosuchtopic")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no help topic matches 'nosuchtopic'", cp.stdout + cp.stderr)
        self.assertIn("Lifecycle", cp.stdout + cp.stderr)

    def test_a_word_in_a_workflow_title_is_a_topic(self):
        cp = run("help", "bridge")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Provision a bridge phone", cp.stdout)


if __name__ == "__main__":
    unittest.main()
