"""A buildroot image whose kernel is declared rather than built
(BR_KERNEL_DEB_URL in image/configs/<profile>.conf, prepared by
lib/wk/sysimage/buildroot.py's kernel_pin on the driving machine).

The rpi4's kernel is pinned because the one buildroot builds from the
release-pinned tree never reaches userspace on that board, measured by
staging one kernel at a time with everything else identical. What these
tests hold down is the part that silently ruins an image: the four things
that must agree about a version (kernel, modules, device trees, overlays)
and the module compression that leaves a board with no wifi and so no
tailnet.

Run: python3 -m unittest tests.test_kernel_pin -v
"""
import contextlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.machine import Fake, Result, here  # noqa: E402
from wk.sysimage import buildroot  # noqa: E402


class Ran:
    def __init__(self, returncode, stdout, stderr):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def run(deb, release, out, machine=None):
    """kernel_pin as a process would report it: the tarball on stdout, the refusal on stderr."""
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            got = buildroot.kernel_pin(machine or here(), str(deb), release, str(out))
        except Refused as e:
            return Ran(e.status, "", err.getvalue())
    return Ran(0, got, err.getvalue())


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
        text = (REPO / "lib" / "wk" / "sysimage" / "buildroot.py").read_text()
        self.assertIn("BR_KERNEL_DEB_SHA256", text)
        self.assertIn("is not pinned", text)

    def test_the_kernel_is_prepared_where_depmod_is(self):
        """the build image has dpkg-deb and xz but no kmod, so the in-workspace
        half only unpacks what the driving machine prepared."""
        self.assertNotIn("depmod", (REPO / "lib" / "wk" / "sysimage" / "buildroot_target.py").read_text())


class TestPrepareOnTheFake(unittest.TestCase):
    """The refusals and the reuse, on any host: the Fake answers as dpkg-deb, depmod and od would."""

    R = "9.9.9"
    DEB, OUT = "/cache/k.deb", "/cache/dl"
    TAR = OUT + "/wk-kernel-9.9.9.tar"

    def world(self, magic="016f2818", modules=True, dep="kernel/brcmfmac.ko:\n"):
        w = Fake("here")
        x = self.TAR + ".work/x"
        tree = self.TAR + ".work/tree"
        w.answer(["sh", "-c"], out="")
        w.answer(["sha256sum", self.DEB], out="ab  k.deb\n")
        w.answer(["od"], out=" %s\n" % magic)
        for p in (["cp"], ["find"], ["tar"], ["mv"]):
            w.answer(p)

        def unpack(argv, f):
            f._set_file(x + "/boot/vmlinuz-" + self.R, "")
            f._set_file(x + "/usr/lib/linux-image-%s/bcm2711-rpi-4-b.dtb" % self.R, "")
            if modules:
                f.dirs.add(x + "/lib/modules/" + self.R)
            return Result(0)
        w.react(["dpkg-deb", "-x"], unpack)
        w.react(["depmod"], lambda a, f: (f._set_file(tree + "/lib/modules/%s/modules.dep" % self.R, dep), Result(0))[-1])
        return w

    def test_a_prepared_tree_is_packed_and_stamped(self):
        w = self.world()
        cp = run(self.DEB, self.R, self.OUT, w)
        self.assertEqual((cp.returncode, cp.stdout), (0, self.TAR), cp.stderr)
        self.assertEqual(w.files[self.TAR + ".from"], "ab")
        self.assertNotIn(self.TAR + ".work", w.dirs)

    def test_an_unchanged_package_is_not_prepared_again(self):
        w = self.world()
        w.files.update({self.TAR: "", self.TAR + ".from": "ab"})
        cp = run(self.DEB, self.R, self.OUT, w)
        self.assertEqual(cp.stdout, self.TAR)
        self.assertFalse([e for e in w.effects if e[0] == "run" and e[1][0] == "dpkg-deb"])

    def test_a_kernel_that_is_not_a_32_bit_zimage_is_refused(self):
        cp = run(self.DEB, self.R, self.OUT, self.world(magic="00000000"))
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("not a 32-bit ARM zImage", cp.stderr)

    def test_a_package_without_modules_is_refused(self):
        cp = run(self.DEB, self.R, self.OUT, self.world(modules=False))
        self.assertIn("no modules", cp.stderr)

    def test_a_modules_dep_that_still_names_xz_is_refused(self):
        cp = run(self.DEB, self.R, self.OUT, self.world(dep="kernel/brcmfmac.ko.xz:\n"))
        self.assertIn("still names .xz", cp.stderr)

    def test_a_missing_tool_is_named(self):
        w = self.world()
        w.answer(["sh", "-c"], out="depmod\n")
        cp = run(self.DEB, self.R, self.OUT, w)
        self.assertIn("depmod", cp.stderr)


if __name__ == "__main__":
    unittest.main()
