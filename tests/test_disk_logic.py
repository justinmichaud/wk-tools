"""Pure image/disk logic that needs no card in a reader: `image_boot_offset`'s
partition-table parsing (lib/image.sh), `_image_wants_wifi`'s conf lookup
(boot/disk.sh) and `disk_refuse_unless_safe`'s size check (boot/disk.sh).
Each drives the real function against a stub of the one external tool it
shells out to (`sfdisk`, a faked `machine_load`, `lsblk` via a stubbed
`card_priv`/`m_ssh MACH_LOCAL=1`) rather than reimplementing the logic here.

Run: python3 -m unittest tests.test_disk_logic -v
"""
import unittest

from tests.support import REPO, WkTest, bash, stub_path


_SFDISK_ONE_BOOT_PARTITION = '''#!/bin/sh
case "$*" in
  *-J*) cat <<'JSON'
{"partitiontable": {"label": "gpt", "partitions": [
  {"node": "/dev/loop0p1", "start": 8192, "size": 1048576, "type": "0700"},
  {"node": "/dev/loop0p2", "start": 1056768, "size": 20971520, "type": "8300"}
]}}
JSON
  ;;
esac
'''

_SFDISK_NO_PARTITIONS = '''#!/bin/sh
echo '{"partitiontable": {"partitions": []}}'
'''

_LSBLK_SIZE = '''#!/bin/sh
case "$*" in
  *-bdno*) echo "${LSBLK_BYTES:-0}" ;;
  *-dno*)  echo "${LSBLK_HUMAN:-0}" ;;
esac
'''


class TestImageBootOffset(WkTest):
    def test_parses_the_first_partitions_start_into_a_byte_offset(self):
        with stub_path({"sfdisk": _SFDISK_ONE_BOOT_PARTITION}) as binp:
            script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
image_boot_offset /fake/disk.img
'''
            cp = self.bash(script, env={"PATH": f"{binp}:/usr/bin:/bin"})
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            # start=8192 sectors * 512 bytes/sector.
            self.assertEqual(cp.stdout.strip(), "4194304", cp.stdout + cp.stderr)

    def test_a_partition_table_with_no_start_fails_rather_than_guesses(self):
        with stub_path({"sfdisk": _SFDISK_NO_PARTITIONS}) as binp:
            script = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/image.sh"
image_boot_offset /fake/disk.img
'''
            cp = self.bash(script, env={"PATH": f"{binp}:/usr/bin:/bin"})
            self.assertNotEqual(
                cp.returncode, 0,
                f"an empty partition table should not resolve to a boot offset: {cp.stdout}",
            )


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
    """The size half of `disk_refuse_unless_safe` (boot/disk.sh): the
    privileged safety check itself (`card_priv check`) is stubbed out --
    that is admin/wk-card-priv's own contract, not this function's -- and
    what is exercised for real is the size comparison against a fake
    `lsblk`, reached through `m_ssh` with MACH_LOCAL=1 so it runs the fake
    tool locally instead of over ssh."""

    def _script(self, device_bytes, human, image_bytes):
        return f'''
. "{REPO}/lib/common.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
MACH_LOCAL=1 MACH_NAME=testmach MACH_SSH=testmach
card_priv() {{ return 0; }}
LSBLK_BYTES={device_bytes} LSBLK_HUMAN={human}
export LSBLK_BYTES LSBLK_HUMAN
disk_refuse_unless_safe /dev/sdX {image_bytes}
echo OK
'''

    def test_a_disk_at_least_as_big_as_the_image_is_allowed(self):
        with stub_path({"lsblk": _LSBLK_SIZE}) as binp:
            cp = self.bash(
                self._script(68719476736, "64G", 1_000_000),
                env={"PATH": f"{binp}:/usr/bin:/bin"},
            )
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("OK", cp.stdout, cp.stdout)

    def test_a_disk_smaller_than_the_image_is_refused(self):
        with stub_path({"lsblk": _LSBLK_SIZE}) as binp:
            cp = self.bash(
                self._script(1000, "1000B", 2000),
                env={"PATH": f"{binp}:/usr/bin:/bin"},
            )
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn(
                "smaller than the image", cp.stdout + cp.stderr,
                cp.stdout + cp.stderr,
            )


if __name__ == "__main__":
    unittest.main()
