"""The rpi5 picks its pair with autoboot.txt, never with the tryboot flag.

The stick holds two systems on primaries 1-2 and 3-4, and something has to
tell the firmware which one a boot lands on. Two mechanisms exist and only one
works here: this board's tryboot flag belongs to flash-kernel's staging on its
NVMe (machines/rpi5.conf), and a pair selected with it does not boot at
all -- dark, no kernel, no panic -- where the same pair, same kernel, same
card, selected by `[all] boot_partition=3` and a plain reboot runs to
userspace (rpi5, 2026-09-05).

The write-then-read-back matters: a card helper older than the pair argument
ignores it and writes partition 1, which would boot the other system and
measure it under this one's name.

Run: python3 -m unittest tests.test_rpi5_pair_select -v
"""
import contextlib
import io
import subprocess
import sys
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from wk.boot.driver import Driver  # noqa: E402
from wk.boot.fake import FakeBoard  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402

CARD_PRIV = REPO / "admin" / "wk-card-priv"


class TestTheDriverSelectsByAutoboot(unittest.TestCase):
    """lib/wk/boot/pi.py's Rpi5Usb against a FakeBoard holding a stick with two systems."""

    def board(self):
        conf = {"NODE_NAME": "rpi5", "NODE_DRIVER": "rpi5-usb", "NODE_DEVICE": "/dev/sda",
                "NODE_ROOT": "/dev/nvme0n1p2", "NODE_ROLE": "workstation"}
        fake = FakeBoard(conf)
        fake.write_system("/dev/sda1", "sys-a")
        fake.write_system("/dev/sda3", "sys-b")
        fake.channel = "host"
        return fake, Rpi5Usb(REPO, conf, fake)

    def test_arming_writes_the_selector_and_boots_that_pair(self):
        for boot, root in (("/dev/sda3", "/dev/sda4"), ("/dev/sda1", "/dev/sda2")):
            with self.subTest(pair=boot):
                fake, d = self.board()
                d.arm(boot, d.order_image)
                self.assertIn(("card_priv", "autoboot", "/dev/sda", boot[-1]), fake.effects)
                d.reboot(armed=True)
                self.assertEqual(fake.running, root)

    def test_arming_never_uses_the_tryboot_flag(self):
        """the arming reboot is a plain one, and a plain one is the only reboot this driver makes."""
        fake, d = self.board()
        d.arm("/dev/sda3", d.order_image)
        d.reboot(armed=True)
        d.probe()
        d.reboot()
        self.assertEqual([e for e in fake.effects if e[0] in ("boot_priv", "reboot")],
                         [("boot_priv", "order", "0xf64"), ("boot_priv", "reboot"), ("reboot", False),
                          ("boot_priv", "reboot"), ("reboot", False)])
        self.assertIs(Rpi5Usb.reboot, Driver.reboot)

    def test_both_pairs_are_selectable_and_nothing_else_is(self):
        fake, d = self.board()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertRaises(act.Refused, d.arm, "/dev/sda5", d.order_image)
        self.assertIn("is not a boot partition this stick selects between", err.getvalue())
        self.assertNotIn("autoboot", [e[1] for e in fake.effects if len(e) > 1])

    def test_the_selection_is_read_back(self):
        """An older helper ignores the argument and writes partition 1."""
        fake, d = self.board()
        fake.stuck = True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertRaises(act.Refused, d.arm, "/dev/sda3", d.order_image)
        self.assertIn("does not select pair 3", err.getvalue())
        self.assertIn("./setup --stage quiesce", err.getvalue(), "the refusal does not name the remedy")
        self.assertNotIn(("boot_priv", "order", "0xf64"), fake.effects, "the firmware was told anyway")


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
