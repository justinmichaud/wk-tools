"""The image presets as they stand in image/presets: each is data the fleet accounts for, and rpi5's overclock is one of them."""
import re
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.fleet import Fleet  # noqa: E402
from wk.sysimage import write  # noqa: E402

ENV = {"WK_ROOT": str(REPO)}
CONFIGS = REPO / "image" / "buildroot" / "external" / "configs"
CLOCKS = re.compile(r"(?m)^\s*(arm_freq|over_voltage)\w*=")


def settings(path):
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


class TestImagePresetsAreData(unittest.TestCase):
    wk_tier = "lint"

    def test_each_preset_is_described_names_its_watchdog_only_to_differ_and_is_for_a_machine_the_fleet_declares(self):
        fleet = Fleet(REPO)
        keys = ("BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE")
        for n in images.names(ENV):
            p = images.load(n, ENV)
            with self.subTest(preset=n):
                self.assertTrue(images.blurb(n, ENV), "no '# %s -- <description>' header" % n)
                self.assertNotEqual(images.parse(images.conf_path(n, ENV)).get("IMG_WATCHDOG"), images.FIELDS["IMG_WATCHDOG"])
                kind = {"guest": "", "mac-volume": "mac"}.get(p["IMG_BUILDER"], "board")
                self.assertEqual(fleet.kind(p["IMG_MACHINE"]) if p["IMG_MACHINE"] else "", kind)
                self.assertIn(sum(bool(p[k]) for k in keys), (0, 3))
                if p["IMG_BUILDER"] == "buildroot":
                    self.assertEqual(bool(p["CFG_NEEDS"]), not (CONFIGS / p["BR_DEFCONFIG"]).is_file() if p["BR_DEFCONFIG"] else True)
                    if images.pgo_wanted("yocto", p["CFG_RELEASE"]):
                        self.assertIn("PGO", p["CFG_NEEDS"])

    def test_the_rpi5_overclock_is_its_own_preset_of_the_same_image_and_nowhere_else(self):
        oc, stock = images.load("webkit-2.52-yocto-rpi5-64-oc", ENV), images.load("webkit-2.52-yocto-rpi5-64", ENV)
        same = lambda p: {k: v for k, v in p.items() if k not in ("IMG_PRESET", "IMG_SPEC_DIR")}  # noqa: E731
        self.assertEqual(same(oc), same(stock))
        self.assertEqual(set(CLOCKS.findall((REPO / "image" / oc["IMG_PRESET"] / "config.txt.append").read_text())), {"arm_freq", "over_voltage"})
        for path in [REPO / "image" / stock["IMG_PRESET"] / "config.txt.append", REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh"] \
                + sorted((REPO / "image" / "boards" / "rpi5").iterdir()):
            if path.is_file():
                    self.assertIsNone(CLOCKS.search(settings(path)), path)
        text = write.config_add(REPO, oc)
        self.assertLess(text.index("os_check=0"), text.index("arm_freq=2800"))


if __name__ == "__main__":
    unittest.main()
