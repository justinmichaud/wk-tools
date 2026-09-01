"""The buildroot builder: image/buildroot.sh (host half, drives a container
workspace) and image/buildroot-build.sh (runs inside it), the same
host/worker split image/yocto.sh and image/yocto-build.sh use. See
docs/HANDOFF-ab-bench.md item 3 for what this exists to close out.

Four things are checked by the classes down to TestDerivedDefconfigs,
matching the four real defects found while writing this lane:

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

BuildrootBusyTest and BuildrootRefuseBusyRefusesASecondBuild cover two more
handoff items, on the driving side (image/buildroot.sh): does `kill -9`
mid-`wk sysimage build` converge on re-run, and does a second `wk sysimage
build` of the same profile refuse rather than race the first's cleanup?
Buildroot has no status file of its own -- busy-ness is decided entirely by
`ws_busy_reason` (lib/target.sh), which checks every `*.pid` file under the
workspace's home directory with `kill -0` run inside the workspace. Driven
directly here, with `t_exec` stubbed to run on this host, against a pid
file naming a pid that no longer exists (the exact shape a killed driver
leaves) and, separately, against a genuinely live one.

Run: python3 -m unittest tests.test_buildroot -v
"""
import os
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
DEAD_PID = "99999999"  # a pid essentially guaranteed not to exist

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


# --------------------------------------------------------------------------- #
# kill -9 convergence, and a second build at once -- image/buildroot.sh's
# own busy check (ws_busy_reason, lib/target.sh), not the freshness check
# above (that guards a single build's own completion; this guards two
# builds of the same workspace overlapping).
# --------------------------------------------------------------------------- #
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



class BuildrootBusyTest(WkTest):
    PRELUDE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/image/buildroot.sh"
t_exec() {{ shift; "$@"; }}
IMG_PROFILE=demo-profile
'''

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.ws = "buildrootws"
        self.home = self.store / "ws" / self.ws / "home"
        self.home.mkdir(parents=True)

    def _write_pidfile(self, stage, pid):
        (self.home / f"buildroot-{stage}.pid").write_text(f"{pid}\n")

    def _run(self, script):
        env = dict(os.environ)
        env["WK_STORE"] = str(self.store)
        env["WK_ROOT"] = str(REPO)
        for var in ("WK_NAME", "WK_TARGET", "WK_TARGET_KIND"):
            env.pop(var, None)
        return subprocess.run(
            ["bash", "-c", self.PRELUDE + script],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
        )

    def test_a_dead_pid_after_kill_9_is_not_read_as_busy(self):
        """the exact scenario: a killed build leaves a pid file naming a
        pid that no longer exists -- ws_busy_reason must not read that as
        a build still running, or a re-run would refuse forever"""
        self._write_pidfile("image", DEAD_PID)
        cp = self._run(f'ws_busy_reason {self.ws} >/dev/null && echo BUSY || echo IDLE')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "IDLE", cp.stdout + cp.stderr)

    def test_buildroot_refuse_busy_lets_a_re_run_through(self):
        """the actual guard buildroot_build calls before starting: a dead
        pid must not refuse the re-run"""
        self._write_pidfile("image", DEAD_PID)
        cp = self._run(f'buildroot_refuse_busy {self.ws} && echo OK')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "OK", cp.stdout + cp.stderr)

    def test_a_genuinely_live_pid_is_still_read_as_busy(self):
        """positive control"""
        proc = subprocess.Popen(["sleep", "60"])
        try:
            self._write_pidfile("image", proc.pid)
            cp = self._run(f'ws_busy_reason {self.ws} >/dev/null && echo BUSY || echo IDLE')
            self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
            self.assertEqual(cp.stdout.strip(), "BUSY", cp.stdout + cp.stderr)
        finally:
            proc.kill()
            proc.wait()

    def test_dry_run_after_a_kill_9_does_not_claim_a_build_is_running(self):
        """`wk sysimage build <buildroot profile> --dry-run` against a
        workspace a killed build left behind prints its plan, not a claim
        that a build is already running"""
        self._write_pidfile("image", DEAD_PID)
        env = dict(os.environ)
        env["WK_STORE"] = str(self.store)
        env["WK_TARGET"] = "container"
        for var in ("WK_NAME", "WK_TARGET_KIND"):
            env.pop(var, None)
        cp = subprocess.run(
            [str(REPO / "wk"), "sysimage", "build", "wpewebkit-2.46-buildroot-rpi3-32",
             "--workspace", self.ws, "--dry-run"],
            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60,
        )
        out = cp.stdout + cp.stderr
        self.assertEqual(cp.returncode, 0, out)
        self.assertIn("dry run", out, out)
        self.assertNotIn("already running", out, out)
        self.assertNotIn("still running", out, out)


class BuildrootRefuseBusyRefusesASecondBuild(WkTest):
    """Two `wk sysimage build` of the same buildroot profile at once:
    `buildroot_refuse_busy` is the one guard -- no lock, unlike yocto's
    `hold_lock` -- and the design it implements is a refusal, not a wait
    (README 'Build interventions': the checkout and the tree's output are
    both single-writer per workspace)."""

    PRELUDE = f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
. "{REPO}/image/buildroot.sh"
t_exec() {{ shift; "$@"; }}
IMG_PROFILE=demo-profile
'''

    def setUp(self):
        super().setUp()
        self.store = self.tmp / "store"
        self.ws = "buildrootws"
        self.home = self.store / "ws" / self.ws / "home"
        self.home.mkdir(parents=True)

    def test_refuses_and_names_the_live_job_and_its_log(self):
        proc = subprocess.Popen(["sleep", "60"])
        try:
            (self.home / "buildroot-image.pid").write_text(f"{proc.pid}\n")
            env = dict(os.environ)
            env["WK_STORE"] = str(self.store)
            env["WK_ROOT"] = str(REPO)
            cp = subprocess.run(
                ["bash", "-c", self.PRELUDE + f'buildroot_refuse_busy {self.ws}'],
                cwd=str(REPO), env=env, capture_output=True, text=True, timeout=30,
            )
            out = cp.stdout + cp.stderr
            self.assertNotEqual(cp.returncode, 0, out)
            self.assertIn("still running", out, out)
            self.assertIn("buildroot-image", out, out)
            self.assertIn(str(self.home / "buildroot-image.log"), out, out)
            self.assertIn("--stop", out, out)
        finally:
            proc.kill()
            proc.wait()



if __name__ == "__main__":
    unittest.main()
