"""A buildroot image whose kernel is declared rather than built
(BR_KERNEL_DEB_URL in image/configs/<profile>.conf, prepared by
image/buildroot/kernel-pin.sh).

The rpi4's kernel is pinned because the one buildroot builds from the
release-pinned tree never reaches userspace on that board, measured by
staging one kernel at a time with everything else identical. What these
tests hold down is the part that silently ruins an image: the four things
that must agree about a version (kernel, modules, device trees, overlays)
and the module compression that leaves a board with no wifi and so no
tailnet.

Run: python3 -m unittest tests.test_kernel_pin -v
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PIN = REPO / "image" / "buildroot" / "kernel-pin.sh"


def run(*args):
    return subprocess.run([str(PIN), *[str(a) for a in args]],
                          capture_output=True, text=True)


class TestPrepare(unittest.TestCase):
    """Against a synthetic .deb, so this needs no network and no kernel."""

    RELEASE = "9.9.9+rpt-rpi-v7l"

    def setUp(self):
        if not shutil.which("dpkg-deb") or not shutil.which("depmod"):
            self.skipTest("needs dpkg-deb and depmod (the machine that prepares a pinned kernel has both)")
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-kpin-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.out = self.tmp / "out"

    def _deb(self, *, zimage=True, modules=True, dtbs=True, xz=True):
        root = self.tmp / "pkg"
        shutil.rmtree(root, ignore_errors=True)
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN" / "control").write_text(
            f"Package: linux-image-test\nVersion: 1\nArchitecture: armhf\n"
            f"Maintainer: t <t@t>\nDescription: test\n")
        if zimage:
            (root / "boot").mkdir(parents=True)
            # A 32-bit ARM zImage is recognised by its magic at offset 36.
            blob = bytearray(b"\0" * 64)
            blob[36:40] = (0x016f2818).to_bytes(4, "little")
            (root / "boot" / f"vmlinuz-{self.RELEASE}").write_bytes(bytes(blob))
        if modules:
            md = root / "lib" / "modules" / self.RELEASE / "kernel" / "drivers" / "net"
            md.mkdir(parents=True)
            ko = md / "brcmfmac.ko"
            ko.write_bytes(b"\x7fELF" + b"\0" * 64)
            if xz:
                subprocess.run(["xz", str(ko)], check=True)
            for f in ("modules.order", "modules.builtin"):
                (root / "lib" / "modules" / self.RELEASE / f).write_text("")
        if dtbs:
            dd = root / "usr" / "lib" / f"linux-image-{self.RELEASE}"
            (dd / "overlays").mkdir(parents=True)
            (dd / "bcm2711-rpi-4-b.dtb").write_bytes(b"\xd0\x0d\xfe\xed")
            (dd / "overlays" / "vc4-kms-v3d-pi4.dtbo").write_bytes(b"\xd0\x0d\xfe\xed")
        deb = self.tmp / "k.deb"
        subprocess.run(["dpkg-deb", "--build", "--nocheck", str(root), str(deb)],
                       check=True, capture_output=True)
        return deb

    def test_the_prepared_tree_carries_all_four_halves(self):
        cp = run(self._deb(), self.RELEASE, self.out)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        tar = Path(cp.stdout.strip())
        self.assertTrue(tar.is_file(), cp.stdout + cp.stderr)
        names = subprocess.run(["tar", "-tf", str(tar)], capture_output=True, text=True).stdout
        for want in ("./boot/zImage",
                     f"./lib/modules/{self.RELEASE}/modules.dep",
                     "./dtb/bcm2711-rpi-4-b.dtb",
                     "./dtb/overlays/vc4-kms-v3d-pi4.dtbo"):
            self.assertIn(want, names, f"the prepared tree is missing {want}")

    def test_modules_arrive_decompressed_and_modules_dep_agrees(self):
        """BusyBox modprobe reads modules.dep and cannot read a .ko.xz; a tree
        where either half still says .xz is a board with no wifi."""
        cp = run(self._deb(xz=True), self.RELEASE, self.out)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        names = subprocess.run(["tar", "-tf", cp.stdout.strip()], capture_output=True, text=True).stdout
        self.assertIn("brcmfmac.ko", names)
        self.assertNotIn("brcmfmac.ko.xz", names, "a module was left compressed")
        dep = subprocess.run(["tar", "-xOf", cp.stdout.strip(),
                              f"./lib/modules/{self.RELEASE}/modules.dep"],
                             capture_output=True, text=True).stdout
        self.assertIn("brcmfmac.ko", dep)
        self.assertNotIn(".ko.xz", dep, "modules.dep still names the compressed paths")

    def test_a_release_the_package_does_not_carry_is_refused(self):
        cp = run(self._deb(), "1.2.3-nope", self.out)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no boot/vmlinuz-1.2.3-nope", cp.stderr)

    def test_a_package_without_modules_is_refused(self):
        cp = run(self._deb(modules=False), self.RELEASE, self.out)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("no modules", cp.stderr)

    def test_a_kernel_that_is_not_a_32_bit_zimage_is_refused(self):
        """the firmware jumps to whatever this is; a mismatch is a board that
        hangs with no console, which is expensive to find out at the board."""
        deb = self._deb()
        root = self.tmp / "pkg"
        (root / "boot" / f"vmlinuz-{self.RELEASE}").write_bytes(b"\0" * 64)
        subprocess.run(["dpkg-deb", "--build", "--nocheck", str(root), str(deb)],
                       check=True, capture_output=True)
        cp = run(deb, self.RELEASE, self.out)
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not a 32-bit ARM zImage", cp.stderr)

    def test_a_second_run_reuses_the_tree_it_made(self):
        deb = self._deb()
        first = run(deb, self.RELEASE, self.out)
        self.assertEqual(first.returncode, 0, first.stderr)
        tar = Path(first.stdout.strip())
        stamp = tar.stat().st_mtime_ns
        second = run(deb, self.RELEASE, self.out)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), first.stdout.strip())
        self.assertEqual(Path(second.stdout.strip()).stat().st_mtime_ns, stamp,
                         "the tree was rebuilt from an unchanged package")


class TestWiring(unittest.TestCase):
    def test_the_profile_pins_url_hash_and_release_together(self):
        """a kernel by URL alone is not pinned."""
        conf = (REPO / "image" / "configs" / "wpewebkit-2.38-buildroot-rpi4-32.conf").read_text()
        self.assertIn("BR_KERNEL_DEB_URL=", conf)
        self.assertIn("BR_KERNEL_DEB_SHA256=", conf)
        self.assertIn("BR_KERNEL_RELEASE=", conf)

    def test_the_driver_refuses_a_half_declared_pin(self):
        text = (REPO / "image" / "buildroot.sh").read_text()
        self.assertIn("BR_KERNEL_DEB_SHA256", text)
        self.assertIn("is not pinned", text)

    def test_the_kernel_is_prepared_where_depmod_is(self):
        """the build image has dpkg-deb and xz but no kmod; preparing in the
        build would mean adding a tool to a container to run a command the
        driving machine already has."""
        self.assertIn("kernel-pin.sh", (REPO / "image" / "buildroot.sh").read_text())
        build = (REPO / "image" / "buildroot-build.sh").read_text()
        self.assertNotIn("depmod", build, "the build image has no depmod")

    def test_the_pinned_boot_files_go_in_before_the_card_is_assembled(self):
        """buildroot's own hook, ahead of the board script that runs genimage
        -- not an edit of output/ behind the build's back."""
        build = (REPO / "image" / "buildroot-build.sh").read_text()
        self.assertIn('BR2_ROOTFS_POST_IMAGE_SCRIPT=\\"$WK_POST_IMAGE $POST_IMAGE_ORIG\\"', build)

    def test_the_modules_go_in_as_an_overlay(self):
        build = (REPO / "image" / "buildroot-build.sh").read_text()
        self.assertIn("KERNEL_OVERLAY", build)
        self.assertIn('OVERLAY="${OVERLAY:+$OVERLAY }$KERNEL_OVERLAY"', build)


if __name__ == "__main__":
    unittest.main()
