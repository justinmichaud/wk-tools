"""The image presets as they stand in image/presets: each is data the fleet
accounts for, and rpi5's overclock is one of them."""
import re
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.fleet import Fleet  # noqa: E402
from wk.sysimage import write  # noqa: E402

ENV = {"WK_ROOT": str(REPO)}
EXTERNAL_CONFIGS = REPO / "image" / "buildroot" / "external" / "configs"
OC = "webkit-2.52-yocto-rpi5-64-oc"
STOCK = "webkit-2.52-yocto-rpi5-64"
CLOCKS = re.compile(r"(?m)^\s*(arm_freq|over_voltage)\w*=")


def presets():
    return {n: images.load(n, ENV) for n in images.names(ENV)}


def setting_lines(path):
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


class TestImagePresetsAreData(unittest.TestCase):
    wk_tier = "lint"

    def test_every_preset_is_a_conf_the_loader_reads_and_declared_in_no_code(self):
        self.assertTrue(presets())
        for n in images.names(ENV):
            self.assertTrue(images.blurb(n, ENV), "%s.conf has no '# %s -- <description>' header" % (n, n))
        self.assertEqual(images.FIELDS["IMG_BUILDER"], "")

    def test_the_watchdog_is_one_value_a_preset_names_only_to_differ(self):
        self.assertEqual(images.FIELDS["IMG_WATCHDOG"], "300")
        for n in images.names(ENV):
            with self.subTest(preset=n):
                self.assertNotEqual(images.parse(images.conf_path(n, ENV)).get("IMG_WATCHDOG"), "300")

    def test_a_board_image_is_for_a_board_the_fleet_declares(self):
        fleet = Fleet(REPO)
        boards, bridges = set(fleet.names(("board",))), set(fleet.names(("bridge",)))
        phones = {p["PMO_DEVICE"] for p in presets().values() if p["IMG_BUILDER"] == "pmos"}
        for n, p in presets().items():
            with self.subTest(preset=n):
                if p["IMG_BUILDER"] in images.WS_BUILDERS:
                    self.assertIn(p["IMG_MACHINE"], boards)
                elif p["IMG_BUILDER"] == "pmos":
                    self.assertIn(p["PMO_BRIDGE"], bridges)
                elif p["IMG_BUILDER"] == "mac-volume":
                    self.assertEqual(fleet.kind(p["IMG_MACHINE"]), "mac")
                elif p["IMG_BUILDER"] == "guest":
                    self.assertEqual("", p["IMG_MACHINE"], "the guest base is no board's")
                else:
                    self.assertEqual(p["IMG_BUILDER"], "fetch")
                    self.assertIn(p["FET_DEVICE"], phones)

    def test_a_buildroot_defconfig_is_the_repos_or_the_preset_says_what_it_needs(self):
        for n, p in presets().items():
            if p["IMG_BUILDER"] != "buildroot":
                continue
            with self.subTest(preset=n):
                if p["CFG_NEEDS"]:
                    self.assertEqual(p["BR_DEFCONFIG"], "")
                else:
                    self.assertTrue((EXTERNAL_CONFIGS / p["BR_DEFCONFIG"]).is_file(), p["BR_DEFCONFIG"])

    def test_a_pinned_kernel_is_pinned_by_all_three_fields_and_builds_none(self):
        keys = ("BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE")
        for n, p in presets().items():
            with self.subTest(preset=n):
                self.assertIn(sum(bool(p[k]) for k in keys), (0, 3))
                if p["BR_KERNEL_DEB_URL"]:
                    self.assertNotIn("BR2_LINUX_KERNEL=y", (EXTERNAL_CONFIGS / p["BR_DEFCONFIG"]).read_text())

    def test_a_buildroot_image_preset_at_a_pgo_release_says_why_it_takes_no_pgo(self):
        for n, p in presets().items():
            if p["IMG_BUILDER"] == "buildroot" and images.pgo_wanted("yocto", p["CFG_RELEASE"]):
                with self.subTest(preset=n):
                    self.assertIn("PGO", p["CFG_NEEDS"])


class TestSysimage(unittest.TestCase):
    def test_oc_preset_in_image(self):
        oc, stock = images.load(OC, ENV), images.load(STOCK, ENV)
        same = lambda p: {k: v for k, v in p.items() if k not in ("IMG_PRESET", "IMG_SPEC_DIR")}
        self.assertEqual(same(oc), same(stock), "the -oc preset is the same image as the stock one")
        spec = REPO / "image" / OC / "config.txt.append"
        self.assertEqual(set(CLOCKS.findall(spec.read_text())), {"arm_freq", "over_voltage"})
        for path in [REPO / "image" / STOCK / "config.txt.append", REPO / "lib" / "wk" / "boot" / "eeprom.py"] \
                + sorted((REPO / "image" / "boards" / "rpi5").iterdir()):
            if path.is_file():
                with self.subTest(file=str(path.relative_to(REPO))):
                    self.assertIsNone(CLOCKS.search(setting_lines(path)))
        text = write.config_add(REPO, oc)
        self.assertLess(text.index("os_check=0"), text.index("arm_freq=2800"))

    def test_setup_carries_no_overclock(self):
        self.assertIsNone(CLOCKS.search(setting_lines(REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh")))


if __name__ == "__main__":
    unittest.main()
