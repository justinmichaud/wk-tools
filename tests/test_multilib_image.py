"""A multilib image installs one width, and the SDK that cross-builds for it
can be assembled at all.

`MLPREFIX` is set for a multilib image recipe and the recipes gain their
variants from multilib.conf's global BBCLASSEXTEND, but nothing rewrites
`IMAGE_INSTALL` -- unmapped, it assembles a 64-bit rootfs under a 32-bit name
with no error anywhere.

An SDK holds both widths in one sysroot by design, which is fine until two
variants ship one path with different bytes: lib32-libc6-dev and libc6-dev both
carried /usr/include/finclude/math-vector-fortran.h and rpm refused the whole
transaction, so no SDK could be built and no slot with it (rpi5, 2026-09-03).

The bbappend's anonymous python is lifted and run against a fake datastore --
bitbake is not importable here, and the logic is what is under test. The glibc
bbappend beside it is checked as text: what it produces is a package, which
only a build can weigh.

Run: python3 -m unittest tests.test_multilib_image -v
"""
import re
import unittest

from tests.support import REPO

BBAPPEND = (REPO / "image" / "yocto" / "meta-wk-multilib" / "recipes-webkit"
            / "images" / "webkit-dev-ci-tools.bbappend")


class FakeD(dict):
    """Enough of bitbake's datastore for the lifted function."""

    def getVar(self, k):
        return self.get(k)

    def setVar(self, k, v):
        self[k] = v


def run_bbappend(d):
    """The bbappend's `python () { ... }` body, run against `d`."""
    text = BBAPPEND.read_text()
    m = re.search(r"(?ms)^python \(\) \{\n(.*)^\}", text)
    assert m, "the bbappend no longer has an anonymous python function"
    body = "\n".join(l[4:] if l.startswith("    ") else l
                     for l in m.group(1).splitlines())
    exec(compile("def _f(d):\n" + "\n".join("    " + l for l in body.splitlines()),
                 str(BBAPPEND), "exec"), globals())
    return _f(d)  # noqa: F821


def a_lib32_image(**over):
    d = FakeD({
        "MLPREFIX": "lib32-",
        "IMAGE_INSTALL": "wpewebkit cog linux-raspberrypi",
        "WK_MULTILIB_KEEP": "linux-raspberrypi",
        "NON_MULTILIB_RECIPES": "",
    })
    d.update(over)
    return d


class TestTheImageInstallsOneWidth(unittest.TestCase):
    def test_every_package_takes_the_prefix(self):
        d = a_lib32_image()
        run_bbappend(d)
        self.assertIn("lib32-wpewebkit", d["IMAGE_INSTALL"].split())
        self.assertIn("lib32-cog", d["IMAGE_INSTALL"].split())

    def test_what_has_one_build_per_machine_does_not(self):
        """The kernel, firmware and bootloader are per-machine, not per-width."""
        d = a_lib32_image()
        run_bbappend(d)
        self.assertIn("linux-raspberrypi", d["IMAGE_INSTALL"].split())
        self.assertNotIn("lib32-linux-raspberrypi", d["IMAGE_INSTALL"].split())

    def test_a_plain_image_is_left_alone(self):
        """No MLPREFIX, no rewriting -- the 64-bit build shares this recipe."""
        d = a_lib32_image(MLPREFIX="")
        run_bbappend(d)
        self.assertEqual("wpewebkit cog linux-raspberrypi", d["IMAGE_INSTALL"])


class TestTheDuplicateHeaderIsDropped(unittest.TestCase):
    """Shared /usr/include is the point of a multilib sysroot: headers are
    meant to be width-independent, and only ${baselib} differs. This one is
    not, so the non-primary width does not ship it and the primary keeps it.
    Verified against the built packages with `oe-pkgdata-util list-pkg-files`
    (lib32-libc6-dev: 0, libc6-dev: 1)."""

    GLIBC = (REPO / "image" / "yocto" / "meta-wk-multilib" / "recipes-core"
             / "glibc" / "glibc_%.bbappend")

    def test_it_exists_in_the_multilib_layer(self):
        """meta-wk-multilib, not meta-wk: it is only ever right for a build
        that has a second width."""
        self.assertTrue(self.GLIBC.is_file(), f"{self.GLIBC} is missing")

    def test_only_the_non_primary_width_drops_it(self):
        """Guarded on MLPREFIX. Unguarded, the 64-bit build sharing this
        recipe would lose the header too and nothing would provide it."""
        text = self.GLIBC.read_text()
        self.assertIn("${MLPREFIX}", text, "the removal is not guarded by MLPREFIX")
        self.assertIn("math-vector-fortran.h", text)

    def test_it_removes_one_named_file_and_no_glob(self):
        """A glob here would silently take whatever else lands beside it."""
        for line in self.GLIBC.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("rm "):
                with self.subTest(line=stripped):
                    self.assertIn("math-vector-fortran.h", stripped)
                    self.assertNotIn("*", stripped)


if __name__ == "__main__":
    unittest.main()
