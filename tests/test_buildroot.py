"""The buildroot builder's driving half (lib/wk/sysimage/buildroot.py) and the defconfigs this repository derives
(image/buildroot/external/configs); its task is tests/test_sysimage_task.py's."""
import contextlib
import io
import sys
import tempfile
import unittest

from tests.support import REPO, WkTest, run_here

sys.path.insert(0, str(REPO / "lib"))
from wk.act import Refused  # noqa: E402
from wk.sysimage import buildroot, buildroot_ws  # noqa: E402

EXTERNAL_DIR = REPO / "image" / "buildroot" / "external"
PRESETS = ["wpewebkit-2.38-buildroot-rpi3-32", "wpewebkit-2.38-buildroot-rpi4-32"]


def driver(name, p, env=None):
    br = buildroot.Buildroot.__new__(buildroot.Buildroot)
    br.name, br.p, br.env = name, p, env or {"WK_ROOT": str(REPO)}
    return br


class TestDryRun(WkTest):
    def test_names_workspace_defconfig_and_the_container_s_cache_paths(self):
        for preset in PRESETS:
            with self.subTest(preset=preset), tempfile.TemporaryDirectory() as store:
                cp = run_here("sysimage", "build", preset, "--dry-run", env={"WK_STORE": store})
                out = cp.stdout
                self.assertEqual(cp.returncode, 0, out)
                self.assertIn("buildroot-" + preset, out)
                self.assertRegex(out, r"(?m)^\s*defconfig\s+\S+cog_defconfig")
                for words in (store + "/cache/buildroot/dl", "BR2_DL_DIR", store + "/cache/buildroot/ccache",
                              "BR2_CCACHE_DIR"):
                    self.assertIn(words, out)

    def test_the_driver_s_argv_is_one_the_target_half_parses(self):
        br = driver(PRESETS[0], {"BR_TREE_URL": "u", "BR_TREE_BRANCH": "b", "BR_TREE_COMMIT": "c",
                                  "BR_DEFCONFIG": "d_defconfig", "BR_EXTERNAL": "1", "BR_IMAGE": "sdcard.img",
                                  "BR_OVERLAY_TAILSCALE": "arm", "BR_KERNEL_RELEASE": "6.1"})
        argv = br.image_argv("/opt/wk-tools", 8, True, "/cache/buildroot/dl/k.tar", "bcm2711-rpi-4-b")
        a = buildroot_ws.parse(argv[2:])
        self.assertEqual((a.overlay_wifi, a.overlay_arch, a.kernel_tar, a.kernel_dts, a.external, a.jobs),
                         ("1", "arm", "/cache/buildroot/dl/k.tar", "bcm2711-rpi-4-b", "1", "8"))

    def test_kernel_dts_reads_the_board_s_own_dtb_or_refuses(self):
        self.assertEqual(driver("x", {"IMG_MACHINE": "rpi4"}).kernel_dts(), "bcm2711-rpi-4-b")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(Refused):
            driver("x", {"IMG_MACHINE": "no-such-board"}).kernel_dts()
        self.assertIn("no-such-board.conf names no dtb=", err.getvalue())


def settings(path):
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            out[key] = value
    return out


class TestDerivedDefconfigs(unittest.TestCase):
    """What a defconfig must agree on with the board it names and the fleet it joins."""

    CONFIGS = sorted((EXTERNAL_DIR / "configs").glob("*_defconfig"))

    def each(self):
        self.assertTrue(self.CONFIGS)
        for cfg in self.CONFIGS:
            with self.subTest(defconfig=cfg.name):
                yield cfg, settings(cfg)

    def test_the_firmware_variant_matches_the_board(self):
        for cfg, s in self.each():
            board = s.get("BR2_ROOTFS_POST_IMAGE_SCRIPT", "").strip('"')
            self.assertTrue(board)
            pi4 = s.get("BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4") == "y"
            self.assertEqual(pi4, "board/raspberrypi4" in board)
            if pi4:
                self.assertEqual(s.get("BR2_PACKAGE_RPI_FIRMWARE_3BPLUSNEW"), "y")

    def test_no_setting_kconfig_would_discard(self):
        for cfg, s in self.each():
            if s.get("BR2_PACKAGE_COG_PLATFORM_DRM") == "y":
                self.assertEqual(s.get("BR2_PACKAGE_WPEBACKEND_FDO"), "y")

    def test_every_derived_defconfig_can_be_reached(self):
        """wpa_supplicant for WiFi, OpenSSH (dropbear 2019.78 refuses ed25519), iptables and TUN for tailscaled."""
        for cfg, s in self.each():
            for key in ("BR2_PACKAGE_WPA_SUPPLICANT", "BR2_PACKAGE_OPENSSH", "BR2_PACKAGE_IPTABLES"):
                self.assertEqual(s.get(key), "y", key)
            self.assertNotIn("BR2_PACKAGE_DROPBEAR", s)
            frag = s.get("BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES", "").strip('"')
            if s.get("BR2_LINUX_KERNEL") == "y":
                self.assertIn("linux-fleet.fragment", frag)
                self.assertTrue((EXTERNAL_DIR / frag.replace("$(BR2_EXTERNAL_WK_PATH)/", "")).is_file(), frag)
            else:
                self.assertEqual(frag, "")

    def test_a_release_pinned_defconfig_pins_exactly_its_own_release(self):
        for cfg, s in self.each():
            pinned = [k for k, v in s.items() if k.startswith("BR2_PACKAGE_WPEWEBKIT2_") and v == "y"]
            release = cfg.name.split("_wpe_")[1].rsplit("_cog", 1)[0]
            self.assertEqual(pinned, ["BR2_PACKAGE_WPEWEBKIT" + release.upper()])


if __name__ == "__main__":
    unittest.main()
