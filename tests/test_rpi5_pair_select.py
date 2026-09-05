"""The rpi5 picks its pair with autoboot.txt, never with the tryboot flag.

The stick holds two systems on primaries 1-2 and 3-4, and something has to
tell the firmware which one a boot lands on. Two mechanisms exist and only one
works here: this board's tryboot flag belongs to flash-kernel's staging on its
NVMe (boot/machines/rpi5.conf), and a pair selected with it does not boot at
all -- dark, no kernel, no panic -- where the same pair, same kernel, same
card, selected by `[all] boot_partition=3` and a plain reboot runs to
userspace (rpi5, 2026-09-05).

The write-then-read-back matters: a card helper older than the pair argument
ignores it and writes partition 1, which would boot the other system and
measure it under this one's name.

Run: python3 -m unittest tests.test_rpi5_pair_select -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, bash

DRIVER = REPO / "boot" / "rpi5-usb.sh"
CARD_PRIV = REPO / "admin" / "wk-card-priv"


class TestTheDriverSelectsByAutoboot(unittest.TestCase):
    def setUp(self):
        self.text = DRIVER.read_text()
        self.arm = re.search(r"(?ms)^b_arm\(\) \{.*?^\}", self.text).group(0)

    def test_arming_writes_the_selector(self):
        self.assertIn("rpi5_select_pair", self.arm)

    def test_arming_never_uses_the_tryboot_flag(self):
        self.assertNotIn("RPI5_TRYBOOT", self.text,
                         "the driver still carries a tryboot arming path")
        self.assertNotIn("b_reboot_tryboot", self.text,
                         "the driver still reboots into tryboot")

    def test_a_plain_reboot_is_the_only_reboot(self):
        body = re.search(r"(?ms)^b_reboot\(\) \{.*?^\}", self.text).group(0)
        self.assertIn("boot_priv reboot", body)
        self.assertNotIn("tryboot", body)

    def test_both_pairs_are_selectable_and_nothing_else_is(self):
        self.assertIn("pair=1", self.arm)
        self.assertIn("pair=3", self.arm)
        self.assertIn("is not a boot partition this stick selects between", self.arm)

    def test_the_selection_is_read_back(self):
        """An older helper ignores the argument and writes partition 1."""
        body = re.search(r"(?ms)^rpi5_select_pair\(\) \{.*?^\}", self.text).group(0)
        self.assertIn("card_priv autoboot", body)
        self.assertIn("b_medium_read", body)
        self.assertIn("boot_partition=$want", body)
        self.assertIn("./setup --stage quiesce", body,
                      "the refusal does not name the remedy")


class TestTheHelperTakesAPair(unittest.TestCase):
    """The rule lives where the privilege is: the helper decides which pairs
    exist, and refuses anything else."""

    def _run(self, args):
        body = subprocess.run(
            ["sed", "-n", "/^_autoboot_write()/,/^}/p;/^v_autoboot()/,/^}/p", str(CARD_PRIV)],
            capture_output=True, text=True).stdout
        script = (
            'say() { echo "$*"; }\n'
            'deny() { echo "REFUSED: $*" >&2; exit 3; }\n'
            'fail() { echo "$*" >&2; exit 1; }\n'
            'gate() { GATED_DEV="$1"; }\n'
            'part() { echo "$1$2"; }\n'
            'with_mount() { local m=/tmp/wk-ab-$$; mkdir -p "$m"; shift; "$@" "$m"; }\n'
            + body.replace('with_mount "$(part "$dev" 1)" _autoboot_write "$pair"',
                           'mkdir -p /tmp/wk-ab-$$; _autoboot_write /tmp/wk-ab-$$ "$pair"; cat /tmp/wk-ab-$$/autoboot.txt')
            + f"\nv_autoboot {args}\n")
        return bash(script)

    def test_pair_three_is_written(self):
        cp = self._run("/dev/sdX 3")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("boot_partition=3", cp.stdout)

    def test_pair_one_is_the_default(self):
        cp = self._run("/dev/sdX")
        self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
        self.assertIn("boot_partition=1", cp.stdout)

    def test_any_other_pair_is_refused(self):
        for bad in ("2", "4", "0", "1;rm -rf /"):
            with self.subTest(pair=bad):
                cp = self._run(f"/dev/sdX '{bad}'")
                self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("REFUSED", cp.stderr)


if __name__ == "__main__":
    unittest.main()
