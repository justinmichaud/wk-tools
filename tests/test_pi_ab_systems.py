"""`--ab-systems` (lib/wk/bench/ab.py over a board): a leg runs on the system the running system's marker names, or not
at all, and the subtests a narrow arm cannot run are excluded from both."""
import contextlib
import io
import os
import sys
import unittest

from tests.support import REPO
from tests.test_bench_board import BOARD, BoardTest, pipeline_registry

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.bench import ab, board, cli  # noqa: E402
from wk.boot import cli as bootcli  # noqa: E402
from wk.boot.driver import part  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402
from tests.fakes import FakeClock  # noqa: E402
from tests.fakes import Fake  # noqa: E402


class TestTheLegsSystem(BoardTest):
    """Each leg boots its system through Boot.enter (tests/test_boot_cmd.py's TestEnter holds the retry rule)."""

    def test_a_leg_whose_system_will_not_come_up_is_lost_and_nothing_runs(self):
        w = self.world()
        w.board.write_system(part("/dev/sda", 3), "sys-b")
        w.board.stuck = True
        err = io.StringIO()
        with w.patches(), contextlib.redirect_stderr(err):
            reg = pipeline_registry(w)
            s = board.for_board(str(REPO), reg, "ws", w.clock, BOARD, driver=Rpi5Usb(REPO, w.board.conf, w.board))
            a = ab.AB(str(REPO), reg, w.clock, "ws", ["jetstream3"], {}, "system", [ab.Arm("sys-a", s, "a", ident="sys-a"),
                                                                                   ab.Arm("sys-b", s, "a", ident="sys-b")], cli.ab_report)
            self.assertFalse(a.arm_leg(1, "jetstream3", {"round": "1"}))
        self.assertIn("would not come up as sys-b in %d attempts; the leg is lost" % bootcli.TRIES, err.getvalue())
        self.assertFalse(w.board.kept)


class TestTheWidth(unittest.TestCase):

    def test_a_system_id_or_a_preset_name_reads_its_presets_width(self):
        s = board.Board(str(REPO), type("R", (), {"env": os.environ, "machine": Fake()})(), "", FakeClock(), BOARD, None, Fake())
        for name, bits in (("webkit-2.52-yocto-rpi3-32-ebb646f3bf67", 32), ("webkit-2.52-yocto-rpi5-64-cddf63dc0d4b", 64),
                           ("webkit-2.52-yocto-rpi4-32", 32)):
            self.assertEqual(s.width([name]), bits, name)

    def test_every_exclusion_names_a_width_and_a_reason(self):
        for line in (REPO / ab.EXCLUSIONS).read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                plan, bits, name, why = line.split(None, 3)
                self.assertIn(bits, ("32", "64"), name)
                self.assertTrue(why.strip(), name)


class TestTheKeptSubtests(unittest.TestCase):
    NARROW = ["argon2-wasm", "dotnet-aot-wasm", "dotnet-interp-wasm", "transformersjs-bert-wasm"]
    PLAN = {"subtests": {"": ["a-wasm"] + NARROW + ["b", "c"]}}

    def kept(self, bits, drop=""):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                return ab.subtests(str(REPO), "jetstream3", self.PLAN, bits, "", drop)
            except Refused:
                return err.getvalue()

    def test_the_narrow_width_drops_its_exclusions_from_the_plan_and_the_wide_one_nothing(self):
        sub, dropped = self.kept(32)
        self.assertEqual(sub.split(), ["a-wasm", "b", "c"])
        self.assertEqual(dropped.split(","), self.NARROW)
        self.assertEqual(self.kept(64), ("", ""))

    def test_a_name_the_plan_does_not_have_refuses(self):
        self.assertIn("names no subtest", self.kept(64, "nope"))

    def test_excluding_everything_refuses(self):
        self.assertIn("every subtest", self.kept(64, ",".join(self.PLAN["subtests"][""])))


if __name__ == "__main__":
    unittest.main()
