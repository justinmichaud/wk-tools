"""The rescue's card writer rides in every yocto image (image/yocto/
meta-wk-rescue): the recipe installs the repository's own admin/wk-card-priv
-- no second copy -- and image/yocto-build.sh adds the layer and the package
to every image, so a system written as a rescue can write the board's other
medium (README.md, "rescue").

Run: python3 -m unittest tests.test_rescue_layer -v
"""
import re
import unittest

from tests.support import REPO

LAYER = REPO / "image/yocto/meta-wk-rescue"
RECIPE = LAYER / "recipes-wk/wk-card-priv/wk-card-priv.bb"


class TestRescueLayer(unittest.TestCase):
    def test_recipe_installs_the_repositorys_own_helper(self):
        text = RECIPE.read_text()
        m = re.search(r'FILESEXTRAPATHS:prepend := "\$\{THISDIR\}/([^:"]+):"', text)
        self.assertIsNotNone(m, "the recipe must point FILESEXTRAPATHS at the repository's admin/")
        self.assertTrue((RECIPE.parent / m.group(1) / "wk-card-priv").resolve().samefile(REPO / "admin/wk-card-priv"))
        self.assertIn('SRC_URI = "file://wk-card-priv"', text)
        self.assertFalse((RECIPE.parent / "files").exists(), "no second copy of the helper under files/")

    def test_installed_where_the_card_code_looks(self):
        self.assertIn('CARD_PRIV_DIR = "/usr/local/libexec"', RECIPE.read_text())
        self.assertIn("CARD_PRIV=/usr/local/libexec/wk-card-priv", (REPO / "boot/disk.sh").read_text())

    def test_layer_is_wired_into_every_yocto_image(self):
        build = (REPO / "image/yocto-build.sh").read_text()
        self.assertIn("meta-wk-rescue", build)
        self.assertIn('IMAGE_INSTALL:append = " wk-card-priv"', build)
        conf = (LAYER / "conf/layer.conf").read_text()
        self.assertIn('BBFILE_COLLECTIONS += "meta-wk-rescue"', conf)
        self.assertIn("LAYERSERIES_COMPAT_meta-wk-rescue", conf)

    def test_every_command_the_helper_runs_is_a_runtime_dependency(self):
        helper = (REPO / "admin/wk-card-priv").read_text()
        rdepends = re.search(r'RDEPENDS:\$\{PN\} \+= "([^"]+)"', RECIPE.read_text()).group(1)
        for cmd, pkg in (("findmnt", "util-linux-findmnt"), ("lsblk", "util-linux-lsblk"),
                         ("sfdisk", "util-linux-sfdisk"), ("partx", "util-linux-partx"),
                         ("blkid", "util-linux-blkid"), ("resize2fs", "e2fsprogs-resize2fs"),
                         ("e2fsck", "e2fsprogs-e2fsck"), ("mcopy", "mtools"), ("python3", "python3-core")):
            if re.search(r"\b%s\b" % cmd, helper):
                self.assertIn(pkg, rdepends, f"the helper runs {cmd}; the recipe must RDEPEND on {pkg}")


if __name__ == "__main__":
    unittest.main()
