"""`--ab-systems` (lib/wk/bench/board_ab.py): a leg runs on the system the running system's marker names, or not at all,
and the subtests a narrow arm cannot run are excluded from both."""
import contextlib
import io
import os
import sys
import unittest

from tests.support import REPO
from tests.test_bench_board import BOARD, BoardTest, pipeline_registry

sys.path.insert(0, str(REPO / "lib"))
from wk.bench import board_ab, cli  # noqa: E402
from wk.boot import cli as bootcli  # noqa: E402
from wk.boot.driver import part  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402

class TestTheLegsSystem(BoardTest):
    """Each leg boots its system through Boot.enter (tests/test_boot_cmd.py's TestEnter holds the retry rule)."""

    def test_a_leg_whose_system_will_not_come_up_is_lost_and_nothing_runs(self):
        w = self.world()
        w.board.write_system(part("/dev/sda", 3), "sys-b")
        w.board.stuck = True
        err = io.StringIO()
        with w.patches(), contextlib.redirect_stderr(err):
            ab = board_ab.AB(str(REPO), pipeline_registry(w), "ws", "jetstream3", {"system": BOARD, "ab_systems": "sys-a,sys-b"},
                             w.clock, cli.ab_report, driver=Rpi5Usb(REPO, w.board.conf, w.board))
            self.assertFalse(ab.arm_leg(1, {"round": "1", "arm": "b"}))
        self.assertIn("would not come up as sys-b in %d attempts; the leg is lost" % bootcli.TRIES, err.getvalue())
        self.assertFalse(w.board.kept)


class TestTheWidth(unittest.TestCase):

    def test_a_system_id_or_a_preset_name_reads_its_presets_width(self):
        for name, bits in (("webkit-2.52-yocto-rpi3-32-ebb646f3bf67", 32), ("webkit-2.52-yocto-rpi5-64-cddf63dc0d4b", 64),
                           ("webkit-2.52-yocto-rpi4-32", 32)):
            self.assertEqual(board_ab.width(name, os.environ), bits, name)

    def test_the_narrow_width_selects_the_exclusions_and_the_wide_one_none(self):
        self.assertIn("argon2-wasm", [n for n, _ in board_ab.exclusions(str(REPO), "jetstream3", 32)])
        self.assertEqual(board_ab.exclusions(str(REPO), "jetstream3", 64), [])

    def test_every_exclusion_names_a_width_and_a_reason(self):
        for line in (REPO / board_ab.EXCLUSIONS).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                plan, bits, name, why = line.split(None, 3)
                self.assertIn(bits, ("32", "64"), name)
                self.assertTrue(why.strip(), name)


class TestTheKeptSubtests(unittest.TestCase):
    PLAN = {"subtests": {"": ["a-wasm", "argon2-wasm", "b", "c"]}}

    def kept(self, drop):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return board_ab.kept(self.PLAN, drop)
            except board_ab.Refused:
                return err.getvalue()

    def test_the_kept_set_is_the_plan_minus_the_exclusions(self):
        self.assertEqual(self.kept(["argon2-wasm"]), ["a-wasm", "b", "c"])

    def test_a_name_the_plan_does_not_have_refuses(self):
        self.assertIn("names no subtest", self.kept(["nope"]))

    def test_excluding_everything_refuses(self):
        self.assertIn("every subtest", self.kept(["a-wasm", "argon2-wasm", "b", "c"]))


if __name__ == "__main__":
    unittest.main()
