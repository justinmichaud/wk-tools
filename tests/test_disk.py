"""lib/wk/disk.py: `wk doctor`'s disk rows measure with du, and a tree du cannot read whole shows "??"."""
import contextlib
import io
import os
import sys
import unittest

from tests.support import REPO, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import disk  # noqa: E402


class TestKb(unittest.TestCase):
    def test_an_absent_path_is_zero_and_a_readable_tree_its_size(self):
        with scratch_dir() as d:
            self.assertEqual(0, disk.kb(str(d / "absent")))
            (d / "f").write_bytes(b"x" * 65536)
            self.assertGreaterEqual(disk.kb(str(d)), 64)

    @unittest.skipIf(os.geteuid() == 0, "root reads every tree")
    def test_a_tree_du_cannot_read_whole_is_unknown_and_shows_so(self):
        with scratch_dir() as d:
            (d / "locked").mkdir()
            (d / "locked" / "f").write_text("x")
            (d / "locked").chmod(0)
            try:
                self.assertIsNone(disk.kb(str(d)))
            finally:
                (d / "locked").chmod(0o700)


class TestReport(unittest.TestCase):
    def test_an_unknown_row_reads_question_marks_and_adds_nothing_to_the_total(self):
        rep, err = disk.Report(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rep.render("??\tworkspace a\twk rm a\n2048\tworkspace b\twk rm b\n", add=True)
        self.assertEqual(2048, rep.total)
        self.assertRegex(err.getvalue(), r"\?\?\s+workspace a")
        self.assertRegex(err.getvalue(), r"2(\.0)? ?M\S*\s+workspace b")
