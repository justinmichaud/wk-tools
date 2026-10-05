"""What replaced `wk pi`: the card helper on a board."""
from tests.support import WkTest


class TestMachineSetup(WkTest):

    def test_machine_setup_installs_the_card_helper_on_a_board(self):
        cp = self.run_wk("machine", "setup", "rpi3", "--dry-run", timeout=15)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk-card-priv", cp.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
