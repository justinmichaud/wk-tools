"""The buildroot builder: lib/wk/sysimage/buildroot.py (the driving half, over a
container workspace) and lib/wk/sysimage/buildroot_target.py (runs inside it). See
docs/PLAN.md for what this exists to close out.

Four things are checked by the classes down to TestDerivedDefconfigs,
matching the four real defects found while writing this lane:

  dry run    `wk sysimage build <profile> --dry-run` has to name the actual
             workspace, defconfig and cache paths -- not a host path that
             would resolve to nothing once handed to a process running
             inside the container (BR2_DL_DIR/BR2_CCACHE_DIR arrive as
             environment variables the container already sets, the same
             way DL_DIR/SSTATE_DIR do for the Yocto lane; buildroot.py only
             ever *displays* the host-side path these are mounted from).

  freshness  the image stage's completion line has to be evidence, not an
             echo of `make`'s exit code -- `make` exits 0 on a tree that
             decided there was nothing left to do. That check is
             buildroot_target.py's verify_fresh, tested in
             tests/test_buildroot_target.py.

  defconfig  a defconfig this repository derives has to agree with the board
             files it names: board/raspberrypi4's genimage config assembles a
             boot partition out of start4.elf and fixup4.dat, which only
             BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4 installs. Selecting the
             wrong variant costs the whole build -- kconfig takes it, every
             package compiles, and genimage refuses at the last step over a
             file rpi-firmware never installed.

  libffi fix host-python-2.7's bundled 2013-era libffi cannot assemble
             aarch64/sysv.S, so a buildroot build dies at `sharedmods` on an
             arm64 build host. The fix (image/buildroot/external/external.mk)
             is applied from outside the vendor tree as real Makefile
             semantics -- not a patch file and not sed -- because the target
             it changes (package/python/python.mk in
             WebPlatformForEmbedded/buildroot) is not in this repository: it
             is cloned at build time. So what is checked is what can be
             checked without that tree: the fix file exists, it is wired into
             the build through BR2_EXTERNAL, and it carries the two specific
             appends the upstream fix (buildroot 2021.02 -> 2021.08, applied
             to host-python) makes.

The driving half's task -- its record, refusals, --detach, --stop and kill
points -- is tests/test_sysimage_task.py's.

Run: python3 -m unittest tests.test_buildroot -v
"""
import subprocess
import sys
import tempfile
import unittest

from tests.support import REPO, WkTest, run_here

sys.path.insert(0, str(REPO / "lib"))
from wk.sysimage import buildroot, buildroot_target  # noqa: E402

BUILDROOT_PY = REPO / "lib" / "wk" / "sysimage" / "buildroot.py"
EXTERNAL_DIR = REPO / "image" / "buildroot" / "external"
EXTERNAL_MK = EXTERNAL_DIR / "external.mk"
DEAD_PID = "99999999"  # a pid essentially guaranteed not to exist

PROFILES = [
    "wpewebkit-2.38-buildroot-rpi3-32",
    "wpewebkit-2.38-buildroot-rpi4-32",
]


# --------------------------------------------------------------------------- #
# dry run -- names the real workspace, defconfig and cache paths.
# --------------------------------------------------------------------------- #

class TestDryRun(WkTest):
    def test_names_workspace_defconfig_and_dl_dir(self):
        for profile in PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as store:
                cp = run_here("sysimage", "build", profile, "--dry-run",
                              env={"WK_STORE": store})
                out = cp.stdout
                self.assertEqual(cp.returncode, 0, out)
                self.assertIn(f"buildroot-{profile}", out, out)
                self.assertRegex(out, r"(?m)^\s*defconfig\s+\S+cog_defconfig", out)
                # BR2_DL_DIR/BR2_CCACHE_DIR: the store-reserved host path
                # (lib/store.sh's store_init), not the "downloads" spelling
                # that never matched what store_init actually creates, and
                # not a bare directory with nothing saying it is the
                # container's own BR2_DL_DIR.
                self.assertIn(f"{store}/cache/buildroot/dl", out, out)
                self.assertIn("BR2_DL_DIR", out, out)
                self.assertIn(f"{store}/cache/buildroot/ccache", out, out)
                self.assertIn("BR2_CCACHE_DIR", out, out)
                self.assertNotIn("cache/buildroot/downloads", out, out)

    def test_names_the_containerfile_and_base_image(self):
        with tempfile.TemporaryDirectory() as store:
            cp = run_here("sysimage", "build", PROFILES[0], "--dry-run", env={"WK_STORE": store})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("container/buildroot/Containerfile", cp.stdout)

    def test_rpi5_64_still_refuses_by_name_not_by_crash(self):
        """no defconfig exists for this board/width yet (image/buildroot/
        external/configs/README): CFG_NEEDS refuses it in cmd_build before
        buildroot_build ever runs, and that refusal must keep naming the
        remedy rather than becoming a bare 'unknown profile' or a traceback."""
        with tempfile.TemporaryDirectory() as store:
            cp = run_here("sysimage", "build", "wpewebkit-2.38-buildroot-rpi5-64",
                           "--dry-run", env={"WK_STORE": store})
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("no defconfig for rpi5", cp.stdout)

    def test_the_driver_s_argv_is_one_the_target_half_parses(self):
        """Every flag buildroot.py hands the workspace, the wifi overlay's and the pinned kernel's included."""
        br = buildroot.Buildroot.__new__(buildroot.Buildroot)
        br.name = PROFILES[0]
        br.p = {"BR_TREE_URL": "u", "BR_TREE_BRANCH": "b", "BR_TREE_COMMIT": "c", "BR_DEFCONFIG": "d_defconfig",
                "BR_EXTERNAL": "1", "BR_IMAGE": "sdcard.img", "BR_OVERLAY_TAILSCALE": "arm", "BR_KERNEL_RELEASE": "6.1"}
        argv = br.image_argv("/opt/wk-tools", 8, True, "/cache/buildroot/dl/k.tar")
        self.assertEqual(argv[:3], ["python3", "/opt/wk-tools/lib/wk/sysimage/buildroot_target.py", "image"])
        a = buildroot_target.parse(argv[2:])
        self.assertEqual((a.overlay_wifi, a.overlay_arch, a.kernel_tar, a.external, a.jobs), ("1", "arm", "/cache/buildroot/dl/k.tar", "1", "8"))


# --------------------------------------------------------------------------- #
# the libffi fix: applied from outside the vendor tree, not sed.
# --------------------------------------------------------------------------- #

class TestLibffiFix(unittest.TestCase):
    """host-python-2.7's bundled libffi cannot assemble aarch64/sysv.S. The
    fix is image/buildroot/external/external.mk -- a BR2_EXTERNAL make
    fragment included after every package/*/*.mk, not a patch file and not
    sed against the vendor tree. WebPlatformForEmbedded/buildroot (the file
    it changes the behaviour of, package/python/python.mk) is not vendored
    into this repository -- it is cloned at build time by
    buildroot_target.py -- so there is no copy of that file here for a
    patch to apply to. What is checked instead is everything that can be
    checked without it: the fix file exists, carries the exact two upstream
    appends, and is actually wired into the build."""

    def test_external_mk_exists_and_is_not_a_sed_invocation(self):
        self.assertTrue(EXTERNAL_MK.is_file(), EXTERNAL_MK)
        text = EXTERNAL_MK.read_text()
        self.assertNotIn("sed -i", text)
        self.assertNotIn("sed 's", text)

    def test_external_mk_carries_the_upstream_fix(self):
        text = EXTERNAL_MK.read_text()
        self.assertIn("HOST_PYTHON_CONF_OPTS += --with-system-ffi", text)
        self.assertIn("HOST_PYTHON_DEPENDENCIES += host-libffi", text)
        # The order-only prerequisite: HOST_PYTHON_TARGET_CONFIGURE's own
        # prerequisite list is expanded by pkg-generic.mk before external.mk
        # is ever read, so nothing appended to a variable lands in it -- a
        # second rule for the same target is the only way to add one, and it
        # has to be order-only (external.mk's own comment says why: a phony
        # package target is always newer than a stamp file).
        self.assertRegex(
            text, r"\$\(HOST_PYTHON_TARGET_CONFIGURE\):\s*\|\s*host-libffi",
        )

    def test_external_tree_is_a_valid_br2_external(self):
        """a BR2_EXTERNAL tree must carry Config.in and external.desc, or
        buildroot refuses it before external.mk is ever read."""
        self.assertTrue((EXTERNAL_DIR / "Config.in").is_file())
        desc = EXTERNAL_DIR / "external.desc"
        self.assertTrue(desc.is_file())
        self.assertIn("name:", desc.read_text())

    def test_external_mk_is_wired_into_every_make(self):
        """buildroot records BR2_EXTERNAL in output/.br-external.mk and fails
        outright if a later make omits it, so it goes on every make, naming
        this tree's external directory."""
        self.assertEqual(buildroot_target.Build.br_ext(type("B", (), {"tools": "/opt/wk-tools"})(), True),
                         ["BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external"])
        self.assertTrue((REPO / "image" / "buildroot" / "external" / "external.desc").is_file())


class TestDerivedDefconfigs(unittest.TestCase):
    """The defconfigs this repository derives (image/buildroot/external/
    configs, whose README carries the derivation line by line). The fork's
    board/<board> files are not vendored here, so what is checked is the
    agreement between a defconfig and the board it names -- the part that is
    decidable from this tree alone."""

    CONFIGS = sorted((EXTERNAL_DIR / "configs").glob("*_defconfig"))

    def _settings(self, path):
        out = {}
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            out[key] = value
        return out

    def test_there_is_at_least_one(self):
        """This file's other tests are a loop over them; an empty directory
        would pass every one of them without checking anything."""
        self.assertTrue(self.CONFIGS, EXTERNAL_DIR / "configs")

    def test_the_firmware_variant_matches_the_board(self):
        """The rpi4 boots start4.elf and fixup4.dat and has no bootcode.bin;
        every earlier Pi boots the other set. board/<board>'s genimage config
        names one set or the other, and rpi-firmware installs whichever the
        variant selects, so a defconfig naming board/raspberrypi4 selects
        VARIANT_PI4 and one naming any other board does not."""
        for cfg in self.CONFIGS:
            with self.subTest(defconfig=cfg.name):
                settings = self._settings(cfg)
                board = settings.get("BR2_ROOTFS_POST_IMAGE_SCRIPT", "").strip('"')
                self.assertTrue(board, "names no post-image script")
                pi4 = settings.get("BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4") == "y"
                if "board/raspberrypi4" in board:
                    self.assertTrue(
                        pi4,
                        f"{cfg.name} builds for {board} but does not select "
                        "BR2_PACKAGE_RPI_FIRMWARE_VARIANT_PI4, so rpi-firmware "
                        "installs the pre-4 boot files and genimage dies on a "
                        "missing rpi-firmware/fixup4.dat",
                    )
                else:
                    self.assertFalse(
                        pi4,
                        f"{cfg.name} builds for {board} but selects the rpi4 "
                        "firmware variant, which installs start4.elf/fixup4.dat "
                        "and no bootcode.bin",
                    )

    def test_no_setting_kconfig_would_discard(self):
        """A line kconfig drops claims something the image does not have.
        BR2_PACKAGE_COG_PLATFORM_DRM is the one this bit on: it depends on
        BR2_PACKAGE_WPEBACKEND_FDO, which nothing here selects and which
        itself needs an EGL-on-wayland stack no 2.38 configuration builds, so
        the line resolves away and the image keeps wpebackend-rdk's bcm-rpi
        backend. The README says so; this keeps the line from coming back."""
        for cfg in self.CONFIGS:
            with self.subTest(defconfig=cfg.name):
                settings = self._settings(cfg)
                if settings.get("BR2_PACKAGE_COG_PLATFORM_DRM") == "y":
                    self.assertEqual(
                        settings.get("BR2_PACKAGE_WPEBACKEND_FDO"), "y",
                        f"{cfg.name} selects COG_PLATFORM_DRM without "
                        "BR2_PACKAGE_WPEBACKEND_FDO, which kconfig needs before "
                        "it will keep the platform",
                    )


    def test_every_derived_defconfig_can_be_reached(self):
        """The four things a fleet bench system needs and the fork's cog
        defconfigs have none of. An image without them builds, boots, and is
        unreachable -- no tailscale0 without TUN, no WiFi without
        wpa_supplicant on a board with no cable, and dropbear 2019.78 refuses
        the ed25519 driving key -- which is a board trip to find out. The
        README derives them; this is what keeps a new release's defconfig from
        being the fork's copied straight in."""
        for cfg in self.CONFIGS:
            with self.subTest(defconfig=cfg.name):
                settings = self._settings(cfg)
                self.assertEqual(
                    settings.get("BR2_PACKAGE_WPA_SUPPLICANT"), "y",
                    f"{cfg.name} has no wpa_supplicant: a board with no cable "
                    "at the bench cannot bring up the WiFi its card is seeded for")
                self.assertEqual(
                    settings.get("BR2_PACKAGE_OPENSSH"), "y",
                    f"{cfg.name} does not select OpenSSH; the fork's dropbear "
                    "2019.78 predates ed25519 and refuses the driving key")
                self.assertNotIn(
                    "BR2_PACKAGE_DROPBEAR", settings,
                    f"{cfg.name} keeps dropbear beside OpenSSH: one ssh server")
                self.assertEqual(
                    settings.get("BR2_PACKAGE_IPTABLES"), "y",
                    f"{cfg.name} has no iptables, which tailscaled drives when "
                    "nftables is not usable")
                frag = settings.get("BR2_LINUX_KERNEL_CONFIG_FRAGMENT_FILES", "").strip('"')
                self.assertIn(
                    "linux-fleet.fragment", frag,
                    f"{cfg.name} builds a kernel without the fleet fragment, so "
                    "it has no TUN device and tailscaled cannot create tailscale0")
                name = frag.replace("$(BR2_EXTERNAL_WK_PATH)/", "")
                self.assertTrue((EXTERNAL_DIR / name).is_file(),
                                f"{cfg.name} names a fragment that is not here: {frag}")

    def test_a_release_pinned_defconfig_pins_exactly_one_release(self):
        """The WPE package version is the whole point of a release-pinned
        defconfig, and two of them would let kconfig pick."""
        for cfg in self.CONFIGS:
            with self.subTest(defconfig=cfg.name):
                pinned = [k for k, v in self._settings(cfg).items()
                          if k.startswith("BR2_PACKAGE_WPEWEBKIT2_") and v == "y"]
                self.assertEqual(len(pinned), 1, f"{cfg.name} pins {pinned}")
                # ...and it is the release the file's own name claims.
                release = cfg.name.split("_wpe_")[1].rsplit("_cog", 1)[0]
                self.assertEqual(pinned[0], f"BR2_PACKAGE_WPEWEBKIT{release.upper()}",
                                 f"{cfg.name} is named for {release} and pins {pinned[0]}")



class TestADryRunAfterAKill(WkTest):
    def test_a_pid_file_a_killed_build_left_does_not_claim_a_build_is_running(self):
        """`wk sysimage build <buildroot profile> --dry-run` against a workspace a
        killed build left behind prints its plan, not a claim that one is running."""
        store = self.tmp / "store"
        home = store / "ws" / "buildrootws" / "home"
        home.mkdir(parents=True)
        (home / "buildroot-image.pid").write_text(f"{DEAD_PID}\n")
        cp = run_here("sysimage", "build", "wpewebkit-2.46-buildroot-rpi3-32", "--workspace", "buildrootws", "--dry-run",
                      env={"WK_STORE": str(store), "WK_TARGET": "container"}, timeout=60)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("dry run", cp.stdout)
        self.assertNotIn("still running", cp.stdout)


if __name__ == "__main__":
    unittest.main()
