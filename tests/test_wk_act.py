"""lib/wk/act.py in process: --dry-run prints and runs nothing, a destructive
command cannot act before confirm was answered or nothing_to_ask said there is
no question this run, --yes and no terminal answer the question, and --force is the only way past a barrier.

Run: python3 tests/run.py -k tests.test_wk_act
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402

FLAGS = ("WK_DRY_RUN", "WK_YES", "WK_FORCE", "WK_DESTRUCTIVE", "WK_CONFIRMED", "WK_QUIET")


class ActTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-act-"))
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        for f in FLAGS:
            os.environ.pop(f, None)
        act._forced.clear()

    def tearDown(self):
        self.env.stop()
        for p in self.tmp.iterdir():
            p.unlink()
        self.tmp.rmdir()

    def stderr(self, fn):
        buf = io.StringIO()
        with redirect_stderr(buf):
            result = fn()
        return result, buf.getvalue()


class TestAct(ActTest):
    def test_a_dry_run_prints_the_command_and_runs_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        marker = self.tmp / "ran"
        cp, err = self.stderr(lambda: act.act(["touch", str(marker)]))
        self.assertIsNone(cp)
        self.assertFalse(marker.exists())
        self.assertIn("would run: touch", err)

    def test_it_runs_otherwise(self):
        marker = self.tmp / "ran"
        cp, _ = self.stderr(lambda: act.act(["touch", str(marker)]))
        self.assertEqual(cp.returncode, 0)
        self.assertTrue(marker.exists())

    def test_a_destructive_command_cannot_act_before_asking(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        with self.assertRaises(act.Refused):
            self.stderr(lambda: act.act(["true"]))
        os.environ["WK_CONFIRMED"] = "1"
        cp, _ = self.stderr(lambda: act.act(["true"]))
        self.assertEqual(cp.returncode, 0)

    def test_acting_without_confirm_or_nothing_to_ask_is_a_bug(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        marker = self.tmp / "ran"
        with self.assertRaises(act.Refused):
            self.stderr(lambda: act.act(["touch", str(marker)]))
        self.assertFalse(marker.exists())

    def test_nothing_to_ask_acts_without_prompting(self):
        """The destructive part does not apply this run: no question, and what follows acts."""
        os.environ["WK_DESTRUCTIVE"] = "1"
        marker = self.tmp / "ran"
        _, err = self.stderr(act.nothing_to_ask)
        self.assertEqual(err, "")
        self.assertTrue(act.asked())
        cp, _ = self.stderr(lambda: act.act(["touch", str(marker)]))
        self.assertEqual(cp.returncode, 0)
        self.assertTrue(marker.exists())

    def test_an_answered_confirm_lets_a_destructive_command_act(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        os.environ["WK_YES"] = "1"
        ok, _ = self.stderr(lambda: act.confirm("remove it?"))
        self.assertTrue(ok and act.asked())
        cp, _ = self.stderr(lambda: act.act(["true"]))
        self.assertEqual(cp.returncode, 0)

    def test_a_declined_confirm_still_cannot_act(self):
        os.environ["WK_DESTRUCTIVE"] = "1"
        ok, _ = self.stderr(lambda: act.confirm("remove it?", stdin=io.StringIO("y\n")))
        self.assertFalse(ok or act.asked())
        with self.assertRaises(act.Refused):
            self.stderr(lambda: act.act(["true"]))


class TestConfirm(ActTest):
    def test_yes_answers_and_records_the_answer(self):
        os.environ["WK_YES"] = "1"
        ok, err = self.stderr(lambda: act.confirm("remove it?"))
        self.assertTrue(ok)
        self.assertEqual(os.environ.get("WK_CONFIRMED"), "1")
        self.assertEqual(err, "")

    def test_a_dry_run_says_what_it_would_ask(self):
        os.environ["WK_DRY_RUN"] = "1"
        ok, err = self.stderr(lambda: act.confirm("remove it?"))
        self.assertTrue(ok)
        self.assertIn("would ask: remove it? [y/N]", err)

    def test_no_terminal_declines(self):
        ok, err = self.stderr(lambda: act.confirm("remove it?", stdin=io.StringIO("y\n")))
        self.assertFalse(ok)
        self.assertIn("declining (no terminal", err)
        self.assertNotIn("WK_CONFIRMED", os.environ)

    def test_a_terminal_answer_decides(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        ok, _ = self.stderr(lambda: act.confirm("remove it?", stdin=Tty("y\n")))
        self.assertTrue(ok)
        os.environ.pop("WK_CONFIRMED", None)
        ok, _ = self.stderr(lambda: act.confirm("remove it?", stdin=Tty("n\n")))
        self.assertFalse(ok)
        self.assertNotIn("WK_CONFIRMED", os.environ)


class TestBarrier(ActTest):
    def test_a_barrier_refuses_and_names_force(self):
        with self.assertRaises(act.Refused) as cm:
            self.stderr(lambda: act.barrier("the board is held"))
        self.assertEqual(cm.exception.status, 1)

    def test_retry_is_its_own_status(self):
        with self.assertRaises(act.Refused) as cm:
            self.stderr(lambda: act.barrier("another step is ending", retry=True))
        self.assertEqual(cm.exception.status, act.RETRY_EXIT)

    def test_force_crosses_it_and_says_so_again_at_the_end(self):
        os.environ["WK_FORCE"] = "1"
        _, err = self.stderr(lambda: act.barrier("the board is held\nsecond line"))
        self.assertIn("FORCED past a barrier: the board is held", err)
        _, summary = self.stderr(act.forced_summary)
        self.assertIn("forced past 1 barrier(s)", summary)
        self.assertIn("- the board is held", summary)
        self.assertNotIn("second line", summary)


if __name__ == "__main__":
    unittest.main()
