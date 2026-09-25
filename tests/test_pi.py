"""`wk pi` is a tombstone (lib/wk/dispatch.py's TOMBSTONES): every verb it had names the command that does it now.
The replacements' own behaviour is theirs: `wk bench run --system`/`--ab`/`--ab-systems`/`--collect` and `wk bench
deploy` are tests/test_bench_board.py's, `wk boot --boot-order` tests/test_boot_cmd.py's, and `wk machine setup
<board>` is below.

Run: python3 tests/run.py -k tests.test_pi
"""
from tests.support import REPO, WkTest

VERBS = {
    ("bench", "rpi3", "speedometer3", "--ab", "a,b"): "wk bench run <ws> <plan> --system <board> --ab A,B | --ab-systems A,B",
    ("bench", "rpi3", "speedometer3", "--slot", "a"): "wk bench run <ws> <plan> --system <board> --slot <name>",
    ("bench", "rpi3", "speedometer3", "--pgo", "p"): "--slot <name>-instr --collect",
    ("deploy", "lane", "rpi3"): "wk bench deploy <ws> <board> --slot <name>",
    ("boot-order", "rpi4", "sd-first"): "wk boot <board> --boot-order",
    ("setup", "rpi3"): "wk machine setup <board>",
    ("helper", "rpi3"): "wk machine setup <board>",
    ("flash", "rpi3", "/dev/sda"): "wk sysimage write --from <path> --disk <board>:<device>",
}


class TestPiIsATombstone(WkTest):
    def test_every_verb_names_its_replacement(self):
        for argv, replacement in VERBS.items():
            with self.subTest(verb=argv[0]):
                cp = self.run_wk("pi", *argv, timeout=15)
                self.assertNotEqual(cp.returncode, 0, cp.stdout)
                self.assertIn(replacement, cp.stdout)
        self.assertFalse((REPO / "cmd" / "pi").exists())

    def test_machine_setup_installs_the_card_helper_on_a_board(self):
        cp = self.run_wk("machine", "setup", "rpi3", "--dry-run", timeout=15)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk-card-priv", cp.stdout)


if __name__ == "__main__":
    import unittest
    unittest.main()
