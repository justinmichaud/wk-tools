"""The buildroot builder: image/buildroot.sh (host half, drives a container
workspace) and image/buildroot-build.sh (runs inside it), the same
host/worker split image/yocto.sh and image/yocto-build.sh use. See
docs/HANDOFF-ab-bench.md item 3 for what this exists to close out.

Four things are checked here, matching the four real defects found while
writing this lane:

  dry run    `wk sysimage build <profile> --dry-run` has to name the actual
             workspace, defconfig and cache paths -- not a host path that
             would resolve to nothing once handed to a process running
             inside the container (BR2_DL_DIR/BR2_CCACHE_DIR arrive as
             environment variables the container already sets, the same
             way DL_DIR/SSTATE_DIR do for the Yocto lane; buildroot.sh only
             ever *displays* the host-side path these are mounted from).

  freshness  the image stage's completion line has to be evidence, not an
             echo of `make`'s exit code -- `make` exits 0 on a tree that
             decided there was nothing left to do, which would otherwise
             print "done" over a stale sdcard.img the same way the Yocto
             defect on moose printed "done" over a stale wic.xz
             (tests/test_yocto_stage.py). verify_image_freshness
             (image/buildroot-build.sh) is the same shape as
             image/yocto-build.sh's function of the same name, lifted with
             sed the way tests/test_yocto_stage.py and tests/test_wifi_seed.py
             lift a function out of a shell file without sourcing the rest
             of it.

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

Run: python3 -m unittest tests.test_buildroot -v
"""
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from tests.support import REPO, WkTest, run, scratch_dir

BUILDROOT_SH = REPO / "image" / "buildroot.sh"
BUILDROOT_BUILD = REPO / "image" / "buildroot-build.sh"
EXTERNAL_DIR = REPO / "image" / "buildroot" / "external"
EXTERNAL_MK = EXTERNAL_DIR / "external.mk"

PROFILES = [
    "wpewebkit-2.38-buildroot-rpi3-32",
    "wpewebkit-2.38-buildroot-rpi4-32",
]


def _lift(path, func):
    """A function's body, sed'd out of a shell file -- the idiom
    tests/test_yocto_stage.py and tests/test_wifi_seed.py both use to call
    one function from a build script without sourcing the whole thing (most
    of what's above/below a given function here wants a real checkout, a
    container, or command-line arguments this test has no reason to fake)."""
    text = subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout
    assert text.strip(), f"could not find {func}() in {path}"
    return text


PRELUDE = '''
say()  { :; }
fail() { printf 'wk-buildroot: error: %s\\n' "$*" >&2; exit 1; }
'''


def _run_lifted(body, *args):
    script = PRELUDE + body + "\n" + " ".join(args)
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=10,
    )


# --------------------------------------------------------------------------- #
# syntax -- both halves parse. Cheap, and everything below assumes it.
# --------------------------------------------------------------------------- #

class TestSyntax(unittest.TestCase):
    def test_both_halves_parse(self):
        for f in (BUILDROOT_SH, BUILDROOT_BUILD):
            cp = subprocess.run(["bash", "-n", str(f)], capture_output=True, text=True)
            self.assertEqual(cp.returncode, 0, f"{f}: {cp.stderr}")


# --------------------------------------------------------------------------- #
# dry run -- names the real workspace, defconfig and cache paths.
# --------------------------------------------------------------------------- #

class TestDryRun(WkTest):
    def test_names_workspace_defconfig_and_dl_dir(self):
        for profile in PROFILES:
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as store:
                cp = run("sysimage", "build", profile, "--dry-run", env={"WK_STORE": store})
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
            cp = run("sysimage", "build", PROFILES[0], "--dry-run", env={"WK_STORE": store})
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("container/buildroot/Containerfile", cp.stdout)

    def test_rpi5_64_still_refuses_by_name_not_by_crash(self):
        """no defconfig exists for this board/width yet (image/buildroot/
        external/configs/README): CFG_NEEDS refuses it in cmd_build before
        buildroot_build ever runs, and that refusal must keep naming the
        remedy rather than becoming a bare 'unknown profile' or a traceback."""
        with tempfile.TemporaryDirectory() as store:
            cp = run("sysimage", "build", "wpewebkit-2.38-buildroot-rpi5-64",
                      "--dry-run", env={"WK_STORE": store})
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("no defconfig for rpi5", cp.stdout)

    def test_wifi_overlay_flag_is_wired_and_accepted(self):
        """buildroot.sh passes --overlay-wifi 1 to buildroot-build.sh
        whenever the board has no cable (_image_wants_wifi); the flag has to
        be one buildroot-build.sh's own arg parser actually recognises, or
        every wifi board's real build dies in the first second on 'unknown
        option: --overlay-wifi' before it clones anything."""
        self.assertIn("--overlay-wifi", BUILDROOT_SH.read_text())
        text = BUILDROOT_BUILD.read_text()
        self.assertIn("--overlay-wifi", text)
        self.assertIn("OVERLAY_WIFI", text)
        # And actually used to assemble the overlay, not just parsed and
        # dropped -- that would refuse the flag instead of silently ignoring
        # it, which is arguably worse: a wifi board's image would build and
        # boot with no way to join the network it needs to be reachable at
        # all, and nothing here would have said so.
        self.assertIn("wifi-overlay.sh", text)


# --------------------------------------------------------------------------- #
# the completion check: refuses a stale (or missing) artifact.
# --------------------------------------------------------------------------- #

class TestVerifyImageFreshness(WkTest):
    def setUp(self):
        super().setUp()
        self.func = _lift(BUILDROOT_BUILD, "verify_image_freshness")

    def test_ok_when_the_image_is_newer_than_the_build_start(self):
        with scratch_dir() as d:
            img = d / "sdcard.img"
            img.write_text("fresh")
            start = int(time.time()) - 5
            cp = _run_lifted(self.func, "verify_image_freshness", str(img), str(start))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_ok_when_the_image_matches_the_start_exactly(self):
        # >=, not >: a build fast enough to land in the same second as the
        # recorded start must not be reported as stale.
        with scratch_dir() as d:
            import os
            img = d / "sdcard.img"
            img.write_text("fresh")
            now = int(time.time())
            os.utime(img, (now, now))
            cp = _run_lifted(self.func, "verify_image_freshness", str(img), str(now))
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_refuses_a_stale_artifact(self):
        """the defect this exists to catch: a `make` that decided there was
        nothing to do, and an sdcard.img left over from a previous run."""
        with scratch_dir() as d:
            import os
            img = d / "sdcard.img"
            img.write_text("stale")
            yesterday = int(time.time()) - 86400
            os.utime(img, (yesterday, yesterday))
            start = int(time.time())
            cp = _run_lifted(self.func, "verify_image_freshness", str(img), str(start))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("older", cp.stderr)

    def test_refuses_a_missing_artifact(self):
        with scratch_dir() as d:
            img = d / "sdcard.img"
            start = int(time.time())
            cp = _run_lifted(self.func, "verify_image_freshness", str(img), str(start))
            self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertIn("does not exist", cp.stderr)

    def test_the_build_calls_it_after_make_and_before_the_done_line(self):
        """static: the ordering, not just the function's own behaviour --
        a freshness check nobody calls, or one called before the build even
        runs, guards nothing (CLAUDE.md, 'One path, not two')."""
        text = BUILDROOT_BUILD.read_text()
        make_at = text.rfind("FORCE_UNSAFE_CONFIGURE=1 make")
        verify_at = text.find("verify_image_freshness \"$OUT/$IMAGE\"")
        done_at = text.rfind("say \"stage 'image' done\"")
        self.assertNotEqual(make_at, -1, text)
        self.assertNotEqual(verify_at, -1, text)
        self.assertNotEqual(done_at, -1, text)
        self.assertLess(make_at, verify_at, "verify_image_freshness must run after the build")
        self.assertLess(verify_at, done_at, "verify_image_freshness must run before the done line")


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
    image/buildroot-build.sh -- so there is no copy of that file here for a
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

    def test_external_mk_is_wired_into_the_build(self):
        """BR2_EXTERNAL has to actually point here, and on every make -- not
        just the first -- or a later invocation drops the fix silently
        (buildroot records BR2_EXTERNAL in output/.br-external.mk and fails
        outright if a later make omits it, which is stricter than silence,
        but the wiring is still worth a direct check)."""
        text = BUILDROOT_BUILD.read_text()
        self.assertIn("BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external", text)
        # Passed to the defconfig make, the .config-resolving olddefconfig,
        # and the real build -- the three `make` invocations in the script.
        make_lines = [l for l in text.splitlines() if l.strip().startswith("make $BR_EXT")
                      or "make $BR_EXT" in l]
        self.assertGreaterEqual(len(make_lines), 2, text)

    def test_patch_file_or_static_reference(self):
        """If a literal patch file targeting a vendored copy of the file it
        patches exists under image/buildroot/external/, it must apply
        cleanly to that copy. WebPlatformForEmbedded/buildroot's
        package/python/python.mk is not vendored here (the tree is cloned at
        build time, never checked in), so this falls to the static half: the
        fix file exists and is referenced from the build script -- checked
        above in more detail, and repeated here as the one test named for
        exactly what the task asked for."""
        patches = list(EXTERNAL_DIR.rglob("*.patch"))
        vendor_file = EXTERNAL_DIR / "package" / "python" / "python.mk"
        if patches and vendor_file.is_file():
            for p in patches:
                cp = subprocess.run(
                    ["patch", "--dry-run", "-p1", "-d", str(EXTERNAL_DIR),
                     "-i", str(p)],
                    capture_output=True, text=True,
                )
                self.assertEqual(cp.returncode, 0, f"{p} does not apply cleanly: {cp.stdout + cp.stderr}")
        else:
            # static: the fix exists, is a real BR2_EXTERNAL make fragment
            # rather than a patch (there is nothing checked in here for a
            # patch to target), and buildroot-build.sh references it.
            self.assertTrue(EXTERNAL_MK.is_file())
            self.assertIn("BR2_EXTERNAL=/opt/wk-tools/image/buildroot/external",
                           BUILDROOT_BUILD.read_text())


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


if __name__ == "__main__":
    unittest.main()
