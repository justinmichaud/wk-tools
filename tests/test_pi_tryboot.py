"""The pi-tryboot boot driver (lib/wk/boot/pi.py, boot/onboard/tryboot.sh): a Pi 4 whose bench medium the
bootloader will not boot. The firmware loads the bench kernel from the SD via a tryboot one-shot, staged at arm
time from the medium's own boot partition, and the kernel mounts the bench root on the medium by PARTUUID."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))

from wk.boot.driver import Onboard  # noqa: E402


class TestStagingRuns(unittest.TestCase):
    """boot/onboard/tryboot.sh run against a fixture, with mount, umount, sync and /proc/mounts shimmed."""

    ZIMAGE_MAGIC = (36, bytes((0x18, 0x28, 0x6F, 0x01)))
    ARM64_MAGIC = (56, b"ARM\x64")

    def write_kernel(self, path, magic):
        off, word = magic
        blob = bytearray(1024)
        blob[off:off + 4] = word
        path.write_bytes(bytes(blob))

    def stage(self, config, kernels, cmdline="root=PARTUUID=aa-02 rootwait\n"):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        medium, sd, shim = tmp / "medium", tmp / "sd", tmp / "bin"
        for d in (medium, sd, shim):
            d.mkdir()
        (medium / "config.txt").write_text(config)
        (medium / "cmdline.txt").write_text(cmdline)
        (medium / "bcm2711-rpi-4-b.dtb").write_bytes(b"dtb")
        (medium / "overlays").mkdir()
        (medium / "overlays" / "vc4-kms-v3d.dtbo").write_bytes(b"ovl")
        for name, magic in kernels.items():
            self.write_kernel(medium / name, magic)
        for f in ("start4.elf", "fixup4.dat"):
            (sd / f).write_bytes(b"fw")
        real_grep = shutil.which("grep")
        (shim / "grep").write_text('#!/bin/sh\nfor a; do l=$a; done\n[ "$l" = /proc/mounts ] && { echo "/dev/fake1 %s vfat rw 0 0"; exit 0; }\n'
                                   'exec %s "$@"\n' % (sd, real_grep))
        (shim / "mount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\ncp -a "%s"/. "$t"\n' % medium)
        (shim / "umount").write_text('#!/bin/sh\nfor a in "$@"; do t=$a; done\nfind "$t" -mindepth 1 -delete\n')
        (shim / "sync").write_text("#!/bin/sh\nexit 0\n")
        for f in shim.iterdir():
            f.chmod(0o755)
        text = Onboard(REPO, "tryboot.sh", WK_DO="stage", WK_SD="/dev/fake1", WK_SRC="/dev/sda1", WK_DTB="bcm2711-rpi-4-b.dtb").text()
        cp = subprocess.run(["sh", "-c", text], capture_output=True, text=True,
                            env={"PATH": "%s:/usr/bin:/bin" % shim})
        return cp, sd

    def test_a_kernel_line_names_the_kernel_and_states_32_bit(self):
        cp, sd = self.stage("kernel=zImage\ndtparam=audio=on\n", {"zImage": self.ZIMAGE_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        for f in ("zImage", "bcm2711-rpi-4-b.dtb", "start4.elf", "fixup4.dat"):
            self.assertTrue((sd / "second" / f).exists(), f"{f} was not staged")
        self.assertTrue((sd / "second" / "overlays" / "vc4-kms-v3d.dtbo").exists())
        txt = (sd / "tryboot.txt").read_text()
        self.assertIn("os_prefix=second/", txt)
        self.assertIn("arm_64bit=0", txt)
        self.assertIn("panic=10", (sd / "second" / "cmdline.txt").read_text())

    def test_a_cmdline_that_chose_a_panic_keeps_its_own(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": self.ZIMAGE_MAGIC}, cmdline="root=PARTUUID=aa-02 panic=5\n")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual((sd / "second" / "cmdline.txt").read_text().split(), ["root=PARTUUID=aa-02", "panic=5"])

    def test_the_prefix_leads_the_staged_config(self):
        """the firmware resolves each filename as it reads the directive asking for it."""
        cp, sd = self.stage("dtoverlay=vc4-kms-v3d\n[pi4]\ndtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        lines = [l for l in (sd / "tryboot.txt").read_text().splitlines() if l.strip()]
        self.assertEqual(lines[0], "os_prefix=second/")
        self.assertLess(lines.index("arm_64bit=1"), lines.index("dtoverlay=vc4-kms-v3d"))

    def test_a_kernel_of_neither_bitness_states_none(self):
        cp, sd = self.stage("kernel=zImage\n", {"zImage": (0, b"\x00\x00\x00\x00")})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertNotIn("arm_64bit", (sd / "tryboot.txt").read_text())

    def test_no_kernel_line_resolves_the_one_default_name_there_is(self):
        cp, sd = self.stage('#kernel=""\ndtoverlay=vc4-kms-v3d\n', {"kernel8.img": self.ARM64_MAGIC})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertTrue((sd / "second" / "kernel8.img").exists())

    def test_two_default_names_and_no_kernel_line_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {"kernel8.img": self.ARM64_MAGIC, "kernel7l.img": self.ZIMAGE_MAGIC})
        self.assertNotEqual(cp.returncode, 0)
        for want in ("kernel8.img", "kernel7l.img", "config.txt.append"):
            self.assertIn(want, cp.stderr)
        self.assertFalse((sd / "second").exists(), "nothing is staged on a refusal")

    def test_no_kernel_at_all_refuses(self):
        cp, sd = self.stage("dtparam=audio=on\n", {})
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("holds none of", cp.stderr)
        self.assertFalse((sd / "second").exists())


if __name__ == "__main__":
    unittest.main()
