"""A buildroot image whose kernel is declared rather than built (BR_KERNEL_DEB_URL, prepared by
lib/wk/sysimage/buildroot.py's kernel_pin): kernel, modules, device trees and overlays agree on one version, and
modules arrive decompressed (BusyBox modprobe cannot read a .ko.xz, so a board would have no wifi)."""
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


def run(deb, release, out, machine=None):
    """kernel_pin as a process would report it: the tarball on stdout, the refusal on stderr."""
    with contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            got = buildroot.kernel_pin(machine or here(), str(deb), release, str(out))
        except Refused as e:
            return subprocess.CompletedProcess(None, e.status, "", err.getvalue())
    return subprocess.CompletedProcess(None, 0, got, err.getvalue())


class TestPrepare(unittest.TestCase):
    """Against a synthetic .deb, so this needs no network and no kernel."""

    RELEASE = "9.9.9+rpt-rpi-v7l"

    def setUp(self):
        if not shutil.which("dpkg-deb") or not shutil.which("depmod"):
            self.skipTest("needs dpkg-deb and depmod (the machine that prepares a pinned kernel has both)")
        self.tmp = Path(tempfile.mkdtemp(prefix="wk-test-kpin-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.out = self.tmp / "out"

    def _deb(self):
        root = self.tmp / "pkg"
        shutil.rmtree(root, ignore_errors=True)
        (root / "DEBIAN").mkdir(parents=True)
        (root / "DEBIAN" / "control").write_text(
            f"Package: linux-image-test\nVersion: 1\nArchitecture: armhf\n"
            f"Maintainer: t <t@t>\nDescription: test\n")
        (root / "boot").mkdir(parents=True)
        # A 32-bit ARM zImage is recognised by its magic at offset 36.
        blob = bytearray(b"\0" * 64)
        blob[36:40] = (0x016f2818).to_bytes(4, "little")
        (root / "boot" / f"vmlinuz-{self.RELEASE}").write_bytes(bytes(blob))
        md = root / "lib" / "modules" / self.RELEASE / "kernel" / "drivers" / "net"
        md.mkdir(parents=True)
        ko = md / "brcmfmac.ko"
        ko.write_bytes(b"\x7fELF" + b"\0" * 64)
        subprocess.run(["xz", str(ko)], check=True)
        for f in ("modules.order", "modules.builtin"):
            (root / "lib" / "modules" / self.RELEASE / f).write_text("")
        dd = root / "usr" / "lib" / f"linux-image-{self.RELEASE}"
        (dd / "overlays").mkdir(parents=True)
        (dd / "bcm2711-rpi-4-b.dtb").write_bytes(b"\xd0\x0d\xfe\xed")
        (dd / "overlays" / "vc4-kms-v3d-pi4.dtbo").write_bytes(b"\xd0\x0d\xfe\xed")
        deb = self.tmp / "k.deb"
        subprocess.run(["dpkg-deb", "--build", "--nocheck", str(root), str(deb)],
                       check=True, capture_output=True)
        return deb

    def test_the_prepared_tree_carries_all_four_halves_with_modules_decompressed(self):
        cp = run(self._deb(), self.RELEASE, self.out)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        names = subprocess.run(["tar", "-tf", cp.stdout.strip()], capture_output=True, text=True).stdout
        for want in ("./boot/zImage",
                     f"./lib/modules/{self.RELEASE}/modules.dep",
                     "./dtb/bcm2711-rpi-4-b.dtb",
                     "./dtb/overlays/vc4-kms-v3d-pi4.dtbo"):
            self.assertIn(want, names, f"the prepared tree is missing {want}")
        self.assertIn("brcmfmac.ko", names)
        self.assertNotIn("brcmfmac.ko.xz", names, "a module was left compressed")
        dep = subprocess.run(["tar", "-xOf", cp.stdout.strip(),
                              f"./lib/modules/{self.RELEASE}/modules.dep"],
                             capture_output=True, text=True).stdout
        self.assertIn("brcmfmac.ko", dep)
        self.assertNotIn(".ko.xz", dep, "modules.dep still names the compressed paths")


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

    def test_each_refusal_names_what_is_wrong(self):
        tool_missing = self.world()
        tool_missing.answer(["sh", "-c"], out="depmod\n")
        for world, why in ((self.world(magic="00000000"), "not a 32-bit ARM zImage"),
                           (self.world(modules=False), "no modules"),
                           (self.world(dep="kernel/brcmfmac.ko.xz:\n"), "still names .xz"),
                           (tool_missing, "depmod")):
            with self.subTest(why=why):
                cp = run(self.DEB, self.R, self.OUT, world)
                self.assertNotEqual(cp.returncode, 0)
                self.assertIn(why, cp.stderr)


if __name__ == "__main__":
    unittest.main()
