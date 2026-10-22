"""The rpi5 picks its stick's pair with autoboot.txt's boot_partition= and a plain reboot: its tryboot flag belongs
to flash-kernel on the NVMe, and a pair selected with it does not boot (rpi5, measured)."""
import subprocess
import unittest

from tests.support import REPO, bash

CARD_PRIV = REPO / "admin" / "wk-card-priv"


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
