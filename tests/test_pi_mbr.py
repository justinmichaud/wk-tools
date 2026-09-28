"""The pi-mbr boot driver and the buildroot fleet overlay: a board with two
media, armed by one byte of the bench medium's partition table, whose image
parks that medium and reboots unless claimed -- as systemd units on yocto and
as BusyBox init scripts on buildroot, from one string."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "lib"))

from wk.boot.cli import load_conf  # noqa: E402
from wk.boot.driver import disk_of, part, partno  # noqa: E402
from wk.boot.pi import PiMbr  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.reach import Reach  # noqa: E402


ENV = {"WK_ROOT": str(REPO), "WK_MACHINES_DIR": str(REPO / "machines")}


def rpi4():
    return load_conf(REPO, "rpi4", ENV)


class TestDiskOfPart(unittest.TestCase):
    def test_partition_to_disk_for_every_transport(self):
        """disk_of inverts part for sd, mmc and nvme names"""
        for p, disk in (("/dev/sda2", "/dev/sda"), ("/dev/mmcblk0p2", "/dev/mmcblk0"),
                        ("/dev/nvme0n1p2", "/dev/nvme0n1"), ("/dev/sdb1", "/dev/sdb")):
            with self.subTest(part=p):
                self.assertEqual(disk_of(p), disk)
                self.assertEqual(part(disk, partno(p)), p)


class TestRpi4Arrangement(unittest.TestCase):
    def test_rpi4_bench_medium_is_the_usb_drive_and_the_rescue_is_the_sd(self):
        """rpi4.conf: device is the USB drive, root is on the SD
        card. The driver is pi-tryboot (tests/test_pi_tryboot.py): the
        bootloader will not MSD-boot the drive there, so pi-mbr's arrangement
        is exercised here with the conf's media and the driver loaded directly."""
        c = rpi4()
        self.assertEqual((c["driver"], c["device"], c["root"]), ("pi-tryboot", "/dev/sda", "/dev/mmcblk0p2"))

    def test_media_and_reprovision_name_the_media_from_the_conf(self):
        """media and reprovision name each medium from the conf, whichever way round it is declared"""
        for dev, root, bench, rescue in (("/dev/sda", "/dev/mmcblk0p2", "USB stick", "SD card"),
                                          ("/dev/mmcblk0", "/dev/sda2", "SD card", "USB stick")):
            with self.subTest(dev=dev):
                conf = {"name": "rpi4", "device": dev, "root": root, "profile": "p"}
                d = PiMbr(REPO, conf, None, mode="bench x-1")
                self.assertIn(f"booted from its {bench}", d.media())
                self.assertIn(f"the {rescue} is the rescue", d.media())
                self.assertIn(f"--disk <reader>:{disk_of(root)} --rescue", d.reprovision())
                self.assertIn(f"--disk rpi4:{dev}", d.reprovision())


class TestSelfDisarm(unittest.TestCase):
    def _disarm(self):
        return PiMbr(REPO, rpi4(), None).self_disarm_sh()

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


class TestWithoutTailnet(unittest.TestCase):
    def test_without_tailscale_says_nothing_for_a_board_on_the_tailnet_by_role_name(self):
        """without_tailnet is silent when ssh or bench_ssh is a node"""
        for peers in ([("rpi4-rescue", "100.1.1.1", "up")], [("rpi4-bench", "100.1.1.2", "up")]):
            with self.subTest(peers=peers):
                self.assertEqual(Reach(Fake(), ENV, peers=peers).without_tailnet("rpi4"), "")


class TestBootPartFollowsTheMedium(unittest.TestCase):
    """the boot partition is on the medium the board resolves (the disk model's own_or_declared), not device's
    name: with another USB disk enumerating first, the stick is sdb."""

    def test_boot_part_uses_the_resolved_disk(self):
        class Ch:
            def call(self, fn, *args, **kw):
                lsblk = '{"blockdevices": [{"name": "/dev/sdb", "type": "disk", "rm": true, "tran": "usb"}]}'
                return Result(0, lsblk) if fn == "m_ssh" else Result(1)
        d = PiMbr(REPO, {"name": "rpi4", "device": "/dev/sda"}, Ch())
        self.assertEqual(d.boot_part(), "/dev/sdb1")


if __name__ == "__main__":
    unittest.main()
