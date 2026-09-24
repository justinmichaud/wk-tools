"""The pi-mbr boot driver and the buildroot fleet overlay: a board with two
media, armed by one byte of the bench medium's partition table, whose image
parks that medium and reboots unless claimed -- as systemd units on yocto and
as BusyBox init scripts on buildroot, from one string."""
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from wk.boot.driver import disk_of  # noqa: E402
from wk.boot.pi import PiMbr  # noqa: E402
from wk.machine import Result  # noqa: E402


def bash(script, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=e)


LOAD = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/image/profiles.sh"
. "{REPO}/boot/machines.sh"
. "{REPO}/boot/disk.sh"
'''


class TestDiskOfPart(unittest.TestCase):
    def test_partition_to_disk_for_every_transport(self):
        """disk_of_part inverts disk_part for sd, mmc and nvme names"""
        cp = bash(LOAD + '''
for pair in "/dev/sda2 /dev/sda" "/dev/mmcblk0p2 /dev/mmcblk0" "/dev/nvme0n1p2 /dev/nvme0n1" "/dev/sdb1 /dev/sdb"; do
    set -- $pair
    got=$(disk_of_part "$1"); [ "$got" = "$2" ] || { echo "disk_of_part $1 = $got, want $2"; exit 1; }
    back=$(disk_part "$got" "${1##*[!0-9]}"); [ "$back" = "$1" ] || { echo "disk_part $got = $back, want $1"; exit 1; }
done
''')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)


class TestRpi4Arrangement(unittest.TestCase):
    def test_rpi4_bench_medium_is_the_usb_drive_and_the_rescue_is_the_sd(self):
        """rpi4.conf: NODE_DEVICE is the USB drive, NODE_ROOT is on the SD
        card. The driver is pi-tryboot (tests/test_pi_tryboot.py): the
        bootloader will not MSD-boot the drive there, so pi-mbr's arrangement
        is exercised here with the conf's media and the driver loaded directly."""
        cp = bash(LOAD + 'machine_load rpi4; echo "$NODE_DRIVER $NODE_DEVICE $NODE_ROOT"')
        self.assertEqual(cp.stdout.strip(), "pi-tryboot /dev/sda /dev/mmcblk0p2", cp.stdout + cp.stderr)

    def test_media_and_reprovision_name_the_media_from_the_conf(self):
        """media and reprovision name each medium from the conf, whichever way round it is declared"""
        for dev, root, bench, rescue in (("/dev/sda", "/dev/mmcblk0p2", "USB stick", "SD card"),
                                          ("/dev/mmcblk0", "/dev/sda2", "SD card", "USB stick")):
            with self.subTest(dev=dev):
                conf = {"NODE_NAME": "rpi4", "NODE_DEVICE": dev, "NODE_ROOT": root, "NODE_PROFILE": "p"}
                d = PiMbr(REPO, conf, None, mode="bench x-1")
                self.assertIn(f"booted from its {bench}", d.media())
                self.assertIn(f"the {rescue} is the rescue", d.media())
                self.assertIn(f"--disk <reader>:{disk_of(root)} --rescue", d.reprovision())
                self.assertIn(f"--disk rpi4:{dev}", d.reprovision())


class TestSelfDisarm(unittest.TestCase):
    def _disarm(self):
        cp = bash(LOAD + 'machine_load rpi4; load_driver pi-mbr; b_self_disarm_sh')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout

    def test_disarm_is_posix_sh_without_util_linux(self):
        """parses under sh -n, reads /proc and /sys only, no findmnt or lsblk, no single quote"""
        s = self._disarm()
        self.assertNotIn("'", s)
        for tool in ("findmnt", "lsblk"):
            self.assertNotIn(tool, s)
        self.assertIn("/proc/self/mountinfo", s)
        self.assertIn("/sys/dev/block/", s)
        self.assertIn("seek=450", s)
        self.assertIn("conv=notrunc", s)
        cp = subprocess.run(["sh", "-n"], input=s, capture_output=True, text=True)
        self.assertEqual(cp.returncode, 0, cp.stderr)

    def test_disarm_finds_the_disk_the_root_is_on(self):
        """run against a fake /proc and /sys: the byte lands on the parent disk of the root partition"""
        if not Path("/proc/self/mountinfo").exists():
            self.skipTest("needs a Linux /proc")
        s = self._disarm()
        with tempfile.TemporaryDirectory() as d:
            img = Path(d, "disk")
            img.write_bytes(b"\0" * 512)
            # The script names /dev/<disk>; re-point /dev via PATH-free text substitution
            # of the two absolute paths it uses, which is what makes it testable at all.
            # Longest first: "/sys/dev/block/" contains "/dev/", so replacing
            # "/dev/" ahead of it rewrites the middle of that path and the
            # later replacement then matches nothing -- the script read the
            # host's real /sys and exited without writing a byte.
            fake = (s.replace("/sys/dev/block/", f"{d}/block/")
                     .replace("/proc/self/mountinfo", f"{d}/mountinfo")
                     .replace("/dev/", f"{d}/dev/"))
            Path(d, "dev").mkdir()
            os.symlink(img, Path(d, "dev", "fakedisk"))
            Path(d, "block").mkdir()
            Path(d, "block", "fakedisk").mkdir()
            Path(d, "block", "fakedisk", "fakedisk2").mkdir()
            os.symlink(Path(d, "block", "fakedisk", "fakedisk2"), Path(d, "block", "8:2"))
            Path(d, "mountinfo").write_text("20 1 8:2 / / rw - ext4 /dev/root rw\n")
            cp = subprocess.run(["sh", "-c", fake], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(img.read_bytes()[450], 0x83)


class TestDisarmWithoutARecord(unittest.TestCase):
    def _disarm(self, arming):
        lift = f"eval \"$(sed -n '/^cmd_disarm()/,/^}}/p' \"{REPO}/cmd/boot\")\""
        return bash(f'''
. "{REPO}/lib/common.sh"
{lift}
MACHINE=rpi4 DRY="" BOOT_ARMING={arming}
read_state() {{ ARMED_IMG=""; SPENT=""; }}
b_disarm() {{ echo "b_disarm ran"; }}
b_disarm_note() {{ :; }}
record_clear() {{ echo "record cleared"; }}
cmd_disarm 2>&1
''')

    def test_a_medium_armed_machine_is_disarmed_whoever_armed_it(self):
        """wk boot <m> --disarm parks the medium even with no arming record: the byte is the arming"""
        cp = self._disarm("medium")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("b_disarm ran", cp.stdout)
        self.assertIn("record cleared", cp.stdout)

    def test_a_one_shot_machine_with_no_record_has_nothing_to_disarm(self):
        cp = self._disarm("one-shot")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertIn("no arming record", cp.stdout)
        self.assertNotIn("b_disarm ran", cp.stdout)


class TestFleetTailnetLine(unittest.TestCase):
    def test_reached_line_names_each_role_node(self):
        """wk status: a bench device is reached under its rescue and bench names, not its machine name"""
        peers = "rpi4-rescue\t100.1.1.1\tup\n"
        cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"; . "{REPO}/boot/machines.sh"
wk_tailscale_peers() {{ printf '%s' "$PEERS"; }}
fleet_tailnet rpi4; echo
fleet_tailnet rpi5
''', env={"PEERS": peers, "WK_ROOT": str(REPO)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = cp.stdout.splitlines()
        self.assertEqual(lines[0], "rpi4-rescue 100.1.1.1 (up); rpi4-bench not a node")
        self.assertEqual(lines[1], "rpi5 not a node; rpi5-bench not a node")

    def test_without_tailscale_says_nothing_for_a_board_on_the_tailnet_by_role_name(self):
        """reach_without_tailnet is silent when NODE_SSH or NODE_BENCH_SSH is a node"""
        for peers in ("rpi4-rescue\t100.1.1.1\tup\n", "rpi4-bench\t100.1.1.2\tup\n"):
            cp = bash(f'''
. "{REPO}/lib/common.sh"; . "{REPO}/lib/reach.sh"
wk_tailscale_peers() {{ printf '%s' "$PEERS"; }}
reach_without_tailnet rpi4
''', env={"PEERS": peers, "WK_ROOT": str(REPO)})
            self.assertEqual(cp.returncode, 0, cp.stderr)
            self.assertEqual(cp.stdout, "", f"said {cp.stdout!r} with peers {peers!r}")


if __name__ == "__main__":
    unittest.main()


class TestBootPartFollowsTheMedium(unittest.TestCase):
    """the boot partition is on the medium the board resolves (disk_own_or_declared), not NODE_DEVICE's name:
    with another USB disk enumerating first, the stick is sdb."""

    def test_boot_part_uses_the_resolved_disk(self):
        class Ch:
            def call(self, fn, *args, **kw):
                return Result(0, "/dev/sdb\n") if fn == "disk_own_or_declared" else Result(1)
        d = PiMbr(REPO, {"NODE_NAME": "rpi4", "NODE_DEVICE": "/dev/sda"}, Ch())
        self.assertEqual(d.boot_part(), "/dev/sdb1")
