"""The rpi5 picks its stick's pair with autoboot.txt's boot_partition= and a plain reboot: its tryboot flag belongs
to flash-kernel on the NVMe, and a pair selected with it does not boot (rpi5, measured)."""
import contextlib
import io
import subprocess
import sys
import unittest

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))

from wk import act  # noqa: E402
from tests.fake_boot import FakeBoard  # noqa: E402
from wk.boot.pi import Rpi5Usb  # noqa: E402

CARD_PRIV = REPO / "admin" / "wk-card-priv"


class TestTheDriverSelectsByAutoboot(unittest.TestCase):

    def board(self):
        conf = {"name": "rpi5", "driver": "rpi5-usb", "device": "/dev/sda",
                "root": "/dev/nvme0n1p2", "role": "workstation"}
        fake = FakeBoard(conf)
        fake.write_system("/dev/sda1", "sys-a")
        fake.write_system("/dev/sda3", "sys-b")
        fake.channel = "host"
        return fake, Rpi5Usb(REPO, conf, fake)

    def test_arming_never_uses_the_tryboot_flag(self):
        fake, d = self.board()
        d.arm("/dev/sda3", d.order_image)
        d.reboot(armed=True)
        d.probe()
        d.reboot()
        self.assertEqual([("reboot", False)] * 2, [e for e in fake.effects if e[0] == "reboot"])
        self.assertFalse([e for e in fake.effects if "reboot-tryboot" in e])

    def test_both_pairs_are_selectable_and_nothing_else_is(self):
        fake, d = self.board()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertRaises(act.Refused, d.arm, "/dev/sda5", d.order_image)
        self.assertIn("is not a boot partition this stick selects between", err.getvalue())
        self.assertNotIn("autoboot", [e[1] for e in fake.effects if len(e) > 1])

    def test_the_selection_is_read_back(self):
        fake, d = self.board()
        fake.stuck = True
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertRaises(act.Refused, d.arm, "/dev/sda3", d.order_image)
        self.assertIn("does not select pair 3", err.getvalue())
        self.assertIn("./setup --stage quiesce", err.getvalue(), "the refusal does not name the remedy")
        self.assertNotIn(("boot_priv", "order", "0xf64"), fake.effects, "the firmware was told anyway")


class TestTheHelperTakesAPair(unittest.TestCase):

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

    def test_pair_three_is_written_and_pair_one_is_the_default(self):
        for args, pair in (("/dev/sdX 3", "3"), ("/dev/sdX", "1")):
            with self.subTest(args=args):
                cp = self._run(args)
                self.assertEqual(0, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("boot_partition=" + pair, cp.stdout)

    def test_any_other_pair_is_refused(self):
        for bad in ("2", "4", "0", "1;rm -rf /"):
            with self.subTest(pair=bad):
                cp = self._run(f"/dev/sdX '{bad}'")
                self.assertEqual(3, cp.returncode, cp.stdout + cp.stderr)
                self.assertIn("REFUSED", cp.stderr)


if __name__ == "__main__":
    unittest.main()
