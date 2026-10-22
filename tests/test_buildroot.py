"""What each defconfig this repository derives (image/buildroot/external/configs) must agree on with the board it names
and the fleet it joins; the builder itself is tests/test_sysimage_task.py's and test_buildroot_ws.py's."""
import unittest

from tests.support import REPO

CONFIGS = sorted((REPO / "image" / "buildroot" / "external" / "configs").glob("*_defconfig"))


def settings(path):
    return dict(l.strip().partition("=")[::2] for l in path.read_text().splitlines() if l.strip() and not l.strip().startswith("#"))


class TestDerivedDefconfigs(unittest.TestCase):
    def test_each_matches_its_board_and_can_be_reached(self):
        """wpa_supplicant for WiFi, OpenSSH (dropbear 2019.78 refuses ed25519), iptables and TUN for tailscaled."""
        self.assertTrue(CONFIGS)
        for cfg in CONFIGS:
            with self.subTest(defconfig=cfg.name):
                s = settings(cfg)
                pi4 = s.get("BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4") == "y"
                self.assertEqual(pi4, "board/raspberrypi4" in s.get("BR2_ROOTFS_POST_IMAGE_SCRIPT", ""))
                if pi4:
                    self.assertEqual(s.get("BR2_PACKAGE_RPI_FIRMWARE_3BPLUSNEW"), "y")
                if s.get("BR2_PACKAGE_COG_PLATFORM_DRM") == "y":
                    self.assertEqual(s.get("BR2_PACKAGE_WPEBACKEND_FDO"), "y")
                self.assertEqual([s.get(k) for k in ("BR2_PACKAGE_WPA_SUPPLICANT", "BR2_PACKAGE_OPENSSH", "BR2_PACKAGE_IPTABLES")], ["y"] * 3)
                self.assertNotIn("BR2_PACKAGE_DROPBEAR", s)
                frag = s.get("BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES", "").strip('"')
                self.assertEqual("linux-fleet.fragment" in frag, s.get("BR2_LINUX_KERNEL") == "y")
                release = cfg.name.split("_wpe_")[1].rsplit("_cog", 1)[0]
                self.assertEqual([k for k, v in s.items() if k.startswith("BR2_PACKAGE_WPEWEBKIT2_") and v == "y"],
                                 ["BR2_PACKAGE_WPEWEBKIT" + release.upper()])


if __name__ == "__main__":
    unittest.main()
