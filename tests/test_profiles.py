"""The image profiles as they stand in image/configs: each is data the fleet
accounts for, and rpi5's overclock is one of them.

Run: python3 -m unittest tests.test_profiles -v
"""
import re
import sys
import unittest

from tests.support import REPO, owed

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.fleet import Fleet  # noqa: E402
from wk.sysimage import write  # noqa: E402

ENV = {"WK_ROOT": str(REPO)}
EXTERNAL_CONFIGS = REPO / "image" / "buildroot" / "external" / "configs"
OWN_FILES = [REPO / "lib" / "image.sh", REPO / "image" / "profiles.sh", REPO / "lib" / "wk" / "images.py"] \
    + sorted((REPO / "image" / "configs").glob("*.conf"))
SHIMS = {"image_lane_ws", "image_lane_profile", "image_lane_arg", "image_lane_machine", "image_lane_here"}
OC = "webkit-2.52-yocto-rpi5-64-oc"
STOCK = "webkit-2.52-yocto-rpi5-64"
CLOCKS = re.compile(r"(?m)^\s*(arm_freq|over_voltage)\w*=")


def profiles():
    return {n: images.load(n, ENV) for n in images.names(ENV)}


def setting_lines(path):
    return "\n".join(l for l in path.read_text().splitlines() if not l.lstrip().startswith("#"))


class TestProfilesAreData(unittest.TestCase):
    wk_tier = "lint"

    def test_every_profile_is_a_conf_the_loader_reads(self):
        self.assertTrue(profiles())
        for n in images.names(ENV):
            self.assertTrue(images.blurb(n, ENV), "%s.conf has no '# %s -- <description>' header" % (n, n))

    def test_no_profile_is_declared_in_code(self):
        for path in (REPO / "lib" / "image.sh", REPO / "image" / "profiles.sh"):
            self.assertNotRegex(path.read_text(), r"(?m)^\s*(IMG|YOC|BR|PMO|FET|CFG)_[A-Z_]+=", path)
        self.assertEqual({v for k, v in images.FIELDS.items() if k == "IMG_BUILDER"}, {""})

    def test_the_watchdog_is_one_value_a_profile_names_only_to_differ(self):
        self.assertEqual(images.FIELDS["IMG_WATCHDOG"], "300")
        for n in images.names(ENV):
            with self.subTest(profile=n):
                self.assertNotEqual(images.parse(images.conf_path(n, ENV)).get("IMG_WATCHDOG"), "300")

    def test_a_board_image_is_for_a_board_the_fleet_declares(self):
        fleet = Fleet(REPO)
        boards, bridges = set(fleet.names(("board",))), set(fleet.names(("bridge",)))
        phones = {p["PMO_DEVICE"] for p in profiles().values() if p["IMG_BUILDER"] == "pmos"}
        for n, p in profiles().items():
            with self.subTest(profile=n):
                if p["IMG_BUILDER"] in images.WS_BUILDERS:
                    self.assertIn(p["IMG_MACHINE"], boards)
                elif p["IMG_BUILDER"] == "pmos":
                    self.assertIn(p["PMO_BRIDGE"], bridges)
                else:
                    self.assertEqual(p["IMG_BUILDER"], "fetch")
                    self.assertIn(p["FET_DEVICE"], phones)

    @owed("image/pgo.sh's image_pgo_machine finds the collection board by NODE_PROFILE, so "
          "webkit-2.52-yocto-rpi4-32 and the -oc profile have none; 5.26 reads IMG_MACHINE")
    def test_a_profile_guided_profile_collects_on_its_own_board(self):
        body = re.search(r"(?ms)^image_pgo_machine\(\).*?^\}", (REPO / "image" / "pgo.sh").read_text()).group(0)
        self.assertNotIn("NODE_PROFILE", body)

    def test_a_buildroot_defconfig_is_the_repos_or_the_profile_says_what_it_needs(self):
        for n, p in profiles().items():
            if p["IMG_BUILDER"] != "buildroot":
                continue
            with self.subTest(profile=n):
                if p["CFG_NEEDS"]:
                    self.assertEqual(p["BR_DEFCONFIG"], "")
                else:
                    self.assertTrue((EXTERNAL_CONFIGS / p["BR_DEFCONFIG"]).is_file(), p["BR_DEFCONFIG"])

    def test_a_pinned_kernel_is_pinned_by_all_three_fields(self):
        keys = ("BR_KERNEL_DEB_URL", "BR_KERNEL_DEB_SHA256", "BR_KERNEL_RELEASE")
        for n, p in profiles().items():
            with self.subTest(profile=n):
                self.assertIn(sum(bool(p[k]) for k in keys), (0, 3))

    @owed("a pinned-kernel profile's defconfig still sets BR2_LINUX_KERNEL=y, and image/buildroot-build.sh "
          "reads the DTS name from that build; 5.17 owns both")
    def test_a_pinned_kernel_builds_none(self):
        for n, p in profiles().items():
            if p["BR_KERNEL_DEB_URL"]:
                with self.subTest(profile=n):
                    self.assertNotIn("BR2_LINUX_KERNEL=y", (EXTERNAL_CONFIGS / p["BR_DEFCONFIG"]).read_text())

    def test_a_buildroot_profile_at_a_pgo_release_says_why_it_takes_no_pgo(self):
        for n, p in profiles().items():
            if p["IMG_BUILDER"] == "buildroot" and images.pgo_wanted("yocto", p["CFG_RELEASE"]):
                with self.subTest(profile=n):
                    self.assertIn("PGO", p["CFG_NEEDS"])


class TestVocabulary(unittest.TestCase):
    """An image's workspace is `image_ws`; the word "lane" survives only as a
    shim name an unported caller still uses."""
    wk_tier = "lint"

    def test_no_file_here_says_lane(self):
        for path in OWN_FILES:
            with self.subTest(file=path.name):
                self.assertNotRegex(path.read_text(), r"(?i)\blanes?\b")
                self.assertLessEqual(set(re.findall(r"\w*lane\w*", path.read_text())), SHIMS)

    def test_each_shim_still_has_a_caller(self):
        for shim in SHIMS:
            with self.subTest(shim=shim):
                callers = [p for d in ("cmd", "image", "lib", "boot", "bench") for p in (REPO / d).rglob("*")
                           if p.is_file() and p.name != "image.sh" and "__pycache__" not in p.parts
                           and shim in p.read_text(errors="replace")]
                self.assertTrue(callers, "%s has no caller left: delete it" % shim)


class TestSysimage(unittest.TestCase):
    def test_oc_profile_in_image(self):
        """rpi5's overclock is a profile of its own, carried onto the card by the write."""
        oc, stock = images.load(OC, ENV), images.load(STOCK, ENV)
        same = lambda p: {k: v for k, v in p.items() if k not in ("IMG_PROFILE", "IMG_SPEC_DIR")}
        self.assertEqual(same(oc), same(stock), "the -oc profile is the same image as the stock one")
        spec = REPO / "image" / OC / "config.txt.append"
        self.assertEqual(set(CLOCKS.findall(spec.read_text())), {"arm_freq", "over_voltage"})
        for path in [REPO / "image" / STOCK / "config.txt.append", REPO / "boot" / "rpi-eeprom.sh"] \
                + sorted((REPO / "image" / "boards" / "rpi5").iterdir()):
            if path.is_file():
                with self.subTest(file=str(path.relative_to(REPO))):
                    self.assertIsNone(CLOCKS.search(setting_lines(path)))
        text = write.config_add(REPO, oc)
        self.assertLess(text.index("os_check=0"), text.index("arm_freq=2800"))

    @owed("host/linux/rpi5/rpi5-setup.sh still writes arm_freq and over_voltage_delta into the "
          "workstation's config.txt; the stability half stays in ./setup and the overclock leaves it")
    def test_setup_carries_no_overclock(self):
        self.assertIsNone(CLOCKS.search(setting_lines(REPO / "host" / "linux" / "rpi5" / "rpi5-setup.sh")))


if __name__ == "__main__":
    unittest.main()
