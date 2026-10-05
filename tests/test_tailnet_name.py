"""The tailnet name a system `wk sysimage write` seeds onto a card is the board's, by role: a bench system joins
as bench_ssh (`<board>-bench`), a rescue as ssh -- `<board>-rescue` on a bench machine, the workstation's own
name on a workstation (rpi5), whose own install is never written."""
import sys
import unittest

from tests.support import FLEET_ENV, REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet  # noqa: E402
from wk.sysimage import write  # noqa: E402


class TestBenchName(WkTest):
    def _name_for(self, machine, role="bench"):
        return write.tailnet_name(fleet.Fleet(REPO, FLEET_ENV), machine, role)

    def test_every_pi_bench_system_joins_as_board_bench(self):
        for board in ("rpi3", "rpi4", "rpi5"):
            with self.subTest(board=board):
                self.assertEqual(self._name_for(board), f"{board}-bench")
                self.assertEqual(self._name_for(board, "bench"), f"{board}-bench")

    def test_a_bench_devices_rescue_has_its_own_name(self):
        for board in ("rpi3", "rpi4"):
            with self.subTest(board=board):
                self.assertEqual(self._name_for(board, "rescue"), f"{board}-rescue")

    def test_a_machine_with_no_written_system_has_no_name_to_seed(self):
        self.assertEqual(self._name_for("benchvm"), "")


if __name__ == "__main__":
    unittest.main()
