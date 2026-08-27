"""Pure image/disk logic that needs no card in a reader: `_image_wants_wifi`'s
conf lookup (boot/disk.sh) and `disk_refuse_unless_safe`'s refusal (boot/disk.sh),
which asks the card helper -- the only implementation of the rule -- rather
than deciding a second time on this side. Each drives the real function
against a stub of the one thing it shells out to (a faked `machine_load`, a
stubbed `card_priv`) rather than reimplementing the logic here.

Run: python3 -m unittest tests.test_disk_logic -v
"""
import unittest

from tests.support import REPO, WkTest, bash


class TestImageWantsWifi(WkTest):
    """`_image_wants_wifi` asks the machine registry (MACH_NET=wifi|ethernet,
    boot/machines/<name>.conf) rather than guessing from the name -- stub
    `machine_load` itself, the seam the function already calls through, the
    same technique test_state.py's TestWsStateWords uses for `t_info`."""

    def _script(self, machine):
        return f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
machine_load() {{
    case "$1" in
        wifimach) MACH_NET=wifi; return 0 ;;
        ethmach)  MACH_NET=ethernet; return 0 ;;
        *) return 1 ;;
    esac
}}
_image_wants_wifi {machine} && echo YES || echo NO
'''

    def test_a_wifi_board_wants_wifi_seeded(self):
        cp = self.bash(self._script("wifimach"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_an_ethernet_board_does_not(self):
        cp = self.bash(self._script("ethmach"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NO", cp.stdout + cp.stderr)

    def test_an_unknown_machine_does_not(self):
        cp = self.bash(self._script("nosuchmach"))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "NO", cp.stdout + cp.stderr)


class TestDiskRefuseUnlessSafe(WkTest):
    """`disk_refuse_unless_safe` (boot/disk.sh) is the one pre-erase question
    this end asks about a device, and it asks the card helper: the rule lives
    where the privilege is, and a second copy here could drift into permitting
    what the helper refuses. Whether the *image* fits is not asked -- the
    source is a stream, read once as it is written -- so what is exercised
    here is the refusal and what it says, with `card_priv` stubbed for the
    helper's answer and `disk_list` for the listing it appends."""

    def _script(self, check_output, check_rc):
        return f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
MACH_LOCAL=1 MACH_NAME=testmach MACH_SSH=testmach
card_priv() {{
    [ "$1" = status ] && return 0
    printf '%s\\n' {check_output!r}
    return {check_rc}
}}
disk_list() {{ printf '    (none)\\n'; }}
disk_refuse_unless_safe /dev/sdX
echo OK
'''

    def test_a_device_the_helper_allows_is_allowed(self):
        cp = self.bash(self._script("/dev/sdX may be written: usb 64G", 0))
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("OK", cp.stdout, cp.stdout)

    def test_a_device_the_helper_refuses_is_refused_here_too(self):
        cp = self.bash(self._script(
            "wk-card-priv: REFUSED: '/dev/sdX' is on transport 'nvme'", 3))
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        out = cp.stdout + cp.stderr
        self.assertIn("will not write /dev/sdX", out, out)
        # The helper's own words, not a second explanation invented here.
        self.assertIn("transport 'nvme'", out, out)


if __name__ == "__main__":
    unittest.main()
