"""`wk help` prints README.md, the one document; `wk help <topic>` prints the sections that word names."""
import contextlib
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import dispatch  # noqa: E402

README = """# wk

## Architecture

The design.

## Lifecycle

Make one, remove one.

**An agent in a workspace**

How an agent works.

## Hardware

The boards.
"""


class TestHelpTopics(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="wk-test-help-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "README.md").write_text(README)
        for p in (mock.patch.object(dispatch, "ROOT", root), mock.patch.object(dispatch.os, "isatty", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def help(self, topic=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(dispatch.Exit) as cm:
            dispatch.help_doc(topic)
        return cm.exception.status, out.getvalue(), err.getvalue()

    def test_bare_help_is_this_machines_line_and_the_whole_readme(self):
        rc, out, _ = self.help()
        self.assertEqual(rc, 0)
        self.assertEqual(out.split("\n", 2)[2], README)

    def test_a_topic_prints_that_section_only(self):
        self.assertEqual(self.help("lifecycle"), (0, "## Lifecycle\n\nMake one, remove one.\n\n**An agent in a workspace**\n\nHow an agent works.\n", ""))

    def test_a_word_in_a_workflow_title_is_a_topic(self):
        self.assertEqual(self.help("agent")[1], "**An agent in a workspace**\n\nHow an agent works.\n")

    def test_an_unknown_topic_lists_the_topics(self):
        rc, out, err = self.help("nosuchtopic")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no help topic matches 'nosuchtopic'", err)
        self.assertIn("    Lifecycle\n    An agent in a workspace\n    Hardware\n", err)


if __name__ == "__main__":
    unittest.main()
