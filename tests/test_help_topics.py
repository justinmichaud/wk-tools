"""`wk help` prints README.md, the one document; `wk help <topic>` prints the section that topic names."""
import re
import unittest

from tests.support import REPO, run

TIER = "lint"


class TestHelpTopics(unittest.TestCase):
    def test_bare_help_is_the_whole_readme(self):
        cp = run("help")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.split("\n", 2)[2].strip(), (REPO / "README.md").read_text().strip())

    def test_a_topic_prints_that_section_only(self):
        cp = run("help", "lifecycle")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue(cp.stdout.startswith("## Lifecycle"), cp.stdout[:200])
        self.assertNotIn("## Architecture", cp.stdout)
        self.assertNotIn("## Where the rest is", cp.stdout)

    def test_an_unknown_topic_lists_the_topics(self):
        cp = run("help", "nosuchtopic")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no help topic matches 'nosuchtopic'", cp.stdout + cp.stderr)
        self.assertIn("Lifecycle", cp.stdout + cp.stderr)

    def test_a_word_in_a_workflow_title_is_a_topic(self):
        cp = run("help", "bridge")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("Provision a bridge phone", cp.stdout)


class TestHardwareSection(unittest.TestCase):
    def test_names_every_fleet_device_and_its_driver(self):
        out = run("help", "hardware").stdout
        self.assertTrue(out.startswith("## Hardware"), out[:200])
        confs = [c for c in sorted((REPO / "machines").glob("*.conf")) if re.search(r"^kind=(board|mac|guest)$", c.read_text(), re.M)]
        self.assertTrue(confs)
        for conf in confs:
            name = conf.stem
            driver = re.search(r"^driver=(\S+)", conf.read_text(), re.M).group(1)
            with self.subTest(machine=name):
                self.assertIn(f"**{name}", out, f"{name} has no paragraph in the hardware section")
                self.assertIn(f"`{driver}`", out, f"{name}'s driver {driver} is not named")


if __name__ == "__main__":
    unittest.main()
