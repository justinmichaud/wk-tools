"""The buildroot image and slot builds as they run inside a workspace (lib/wk/sysimage/buildroot_target.py) against
a Fake machine: argument refusals, the caches, the tree and its patches, the three overlays, the pinned kernel's
post-image hook, the .config additions, BR2_EXTERNAL on every make, the image's freshness, and the slot.

Run: python3 tests/run.py -k test_buildroot_target
"""
import contextlib
import io
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import buildroot_target as bt  # noqa: E402

TOOLS = "/opt/wk-tools"
SRC = "/src/WebKit"
NAME = "wpewebkit-2.46-buildroot-rpi3-32"
WORK = SRC + "/WebKitBuild/buildroot/" + NAME
OUT = WORK + "/output"
TCF = OUT + "/host/share/buildroot/toolchainfile.cmake"
ENV = {"BR2_DL_DIR": "/cache/buildroot/dl", "BR2_CCACHE_DIR": "/cache/buildroot/ccache", "WK_MIRROR": "/mirror/WebKit.git"}
SHA = "ab" * 32
COMMIT = "c" * 40
DEFCONFIG = 'BR2_LINUX_KERNEL_INTREE_DTS_NAME="bcm2710-rpi-3-b"\nBR2_ROOTFS_POST_IMAGE_SCRIPT="board/raspberrypi3/post-image.sh"\n'
EXT = "BR2_EXTERNAL=%s/image/buildroot/external" % TOOLS


def image_args(*more):
    return bt.parse(["image", "--name", NAME, "--tree-url", "https://example/buildroot.git", "--tree-commit", "1234abcd",
                     "--defconfig", "raspberrypi3_wpe_2_46_cog_defconfig", "--external", "1", "--image", "sdcard.img",
                     "--jobs", "8"] + list(more))


def webkit_args():
    return bt.parse(["webkit", "--name", NAME, "--commit", COMMIT, "--slot", "base", "--jobs", "8"])


class World(Fake):
    """A workspace with the tree cloned and the tools pushed; make and the guarded build answer as buildroot would."""

    def __init__(self):
        super().__init__("ws")
        self.clock = FakeClock()
        self.dirs.update({SRC, WORK + "/.git", WORK + "/package", TOOLS + "/image/buildroot/tree-patches"})
        self.files.update({
            TOOLS + "/" + bt.TS_REL: 'TS_VERSION = "1.2.3"\nTS_SHA256_arm = "%s"\n' % SHA,
            TOOLS + "/image/buildroot/tree-patches/0001-fdo.patch": "",
            TOOLS + "/image/buildroot/external/external.desc": "name: WK\n",
            "/etc/os-release": 'PRETTY_NAME="Ubuntu 22.04.4 LTS"\n'})
        self.mtime = None
        self.sha = SHA
        for p in (["git"], ["gcc"], ["test"], ["tar"], ["cp"], ["install"], ["chmod"], ["curl"], ["mv"], ["python3"], ["du"]):
            self.answer(p)
        self.answer(["git", "-C", WORK, "apply", "--reverse", "--check"], rc=1)
        self.answer(["git", "-C", SRC, "status", "--porcelain"], out="")
        self.react(["sha256sum"], lambda a, f: Result(0, "%s  %s\n" % (f.sha, a[1])))
        self.react(["make"], self._make)
        self.react(["env", "WK_MB_PER_JOB=%d" % bt.MB_PER_JOB], self._build)
        self.react(["stat"], lambda a, f: Result(0, "%d\n" % f.mtime) if f.mtime is not None else Result(1))
        self.react(["find"], lambda a, f: Result(0, "a.ko\nb.ko\n"))

    def _make(self, argv, f):
        if argv[-1].endswith("_defconfig"):
            f.files[WORK + "/.config"] = DEFCONFIG
        return Result(0)

    def _build(self, argv, f):
        f.dirs.add(OUT + "/images")
        f.files[OUT + "/images/sdcard.img"] = ""
        f.mtime = int(f.clock.now()) + 60
        return Result(0)

    def build(self, a):
        return bt.Build(a, self, dict(ENV), self.clock, TOOLS)

    def ran(self, *prefix):
        return [e[1] for e in self.effects if e[0] in ("run", "run_tty") and tuple(e[1][:len(prefix)]) == prefix]


def quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
        try:
            fn(*a)
        except bt.Failed as e:
            return out.getvalue(), str(e)
    return out.getvalue(), None


class TestArguments(unittest.TestCase):
    def refused(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            bt.parse(argv)
        return cm.exception.code

    def test_a_slot_takes_a_full_sha(self):
        self.assertEqual(self.refused(["webkit", "--name", NAME, "--commit", "abc123", "--slot", "base"]), 2)

    def test_every_argument_is_required(self):
        self.assertEqual(self.refused(["webkit", "--commit", COMMIT, "--slot", "base"]), 2)
        self.assertEqual(self.refused(["image", "--name", NAME, "--tree-url", "u"]), 2)

    def test_an_unknown_option_is_a_usage_error(self):
        self.assertEqual(self.refused(["image", "--bogus"]), 2)

    def test_a_pinned_kernel_names_its_release(self):
        self.assertEqual(self.refused(["image", "--name", NAME, "--tree-url", "u", "--defconfig", "d", "--kernel-tar", "/k.tar"]), 2)


class TestTheImage(unittest.TestCase):
    def test_it_builds_and_says_it_is_done(self):
        w = World()
        out, err = quiet(w.build(image_args()).run)
        self.assertIsNone(err, out)
        self.assertIn("stage 'image' done", out)

    def test_caches_that_are_not_set_are_refused(self):
        w = World()
        b = bt.Build(image_args(), w, {}, w.clock, TOOLS)
        _, err = quiet(b.run)
        self.assertIn("BR2_DL_DIR", err)
        self.assertFalse(w.ran("make"))

    def test_br2_external_is_on_every_make(self):
        w = World()
        quiet(w.build(image_args()).run)
        makes = [a for a in w.ran("make")] + [a for a in w.ran("env", "WK_MB_PER_JOB=%d" % bt.MB_PER_JOB)]
        self.assertGreaterEqual(len(makes), 3)
        for argv in makes:
            self.assertIn(EXT, argv)

    def test_the_build_runs_under_the_memory_guard(self):
        w = World()
        quiet(w.build(image_args()).run)
        argv = w.ran("env", "WK_MB_PER_JOB=%d" % bt.MB_PER_JOB)[0]
        self.assertIn(". %s/build/guard.sh" % TOOLS, argv[4])
        self.assertIn("FORCE_UNSAFE_CONFIGURE=1", argv)
        self.assertEqual(argv[-1], "-j8")

    def test_the_config_names_the_caches_the_jobs_and_a_build_id(self):
        w = World()
        quiet(w.build(image_args()).run)
        conf = w.files[WORK + "/.config"]
        for line in ('BR2_DL_DIR="/cache/buildroot/dl"', 'BR2_CCACHE_DIR="/cache/buildroot/ccache"', "BR2_JLEVEL=8",
                     'BR2_TARGET_LDFLAGS="-Wl,--build-id"', "BR2_TARGET_ROOTFS_EXT2=y"):
            self.assertIn(line, conf)

    def test_a_tree_patch_already_applied_is_not_applied_again(self):
        w = World()
        w.answer(["git", "-C", WORK, "apply", "--reverse", "--check"])
        quiet(w.build(image_args()).run)
        self.assertFalse([a for a in w.ran("git", "-C", WORK, "apply") if "--reverse" not in a])

    def test_a_tree_patch_that_does_not_apply_is_refused(self):
        w = World()
        w.answer(["git", "-C", WORK, "apply", TOOLS + "/image/buildroot/tree-patches/0001-fdo.patch"], rc=1)
        _, err = quiet(w.build(image_args()).run)
        self.assertIn("0001-fdo.patch", err)

    def test_a_slot_s_source_override_is_dropped(self):
        w = World()
        w.files[WORK + "/local.mk"] = "WPEWEBKIT_OVERRIDE_SRCDIR = /src/WebKit\n"
        quiet(w.build(image_args()).run)
        self.assertNotIn(WORK + "/local.mk", w.files)
        self.assertTrue([a for a in w.ran("make") if "wpewebkit-dirclean" in a])


class TestFreshness(unittest.TestCase):
    def check(self, mtime, exists=True):
        w = World()
        img = OUT + "/images/sdcard.img"
        if exists:
            w.files[img] = ""
        w.mtime = mtime
        return quiet(w.build(image_args()).verify_fresh, img, 1000)[1]

    def test_an_image_newer_than_the_start_passes(self):
        self.assertIsNone(self.check(1005))

    def test_an_image_from_the_same_second_passes(self):
        self.assertIsNone(self.check(1000))

    def test_a_stale_image_is_refused(self):
        self.assertIn("older", self.check(900))

    def test_a_missing_image_is_refused(self):
        self.assertIn("does not exist", self.check(1005, exists=False))

    def test_an_image_make_left_untouched_fails_the_stage(self):
        w = World()
        w.react(["env", "WK_MB_PER_JOB=%d" % bt.MB_PER_JOB], lambda a, f: (f.dirs.add(OUT + "/images"),
                f.files.__setitem__(OUT + "/images/sdcard.img", ""), setattr(f, "mtime", 1), Result(0))[-1])
        out, err = quiet(w.build(image_args()).run)
        self.assertIn("older", err)
        self.assertNotIn("done", out)


class TestOverlays(unittest.TestCase):
    def overlay(self, conf):
        return bt.config_value(conf, "BR2_ROOTFS_OVERLAY").split()

    def test_the_tailnet_overlay_is_the_pinned_release_and_the_layer_s_join_script(self):
        w = World()
        _, err = quiet(w.build(image_args("--overlay-arch", "arm")).run)
        self.assertIsNone(err)
        self.assertTrue(w.ran("curl", "-fsSL", "-o", WORK + "/tailscale_1.2.3_arm.tgz.part"))
        self.assertIn(["install", "-m", "0755", TOOLS + "/" + bt.TS_JOIN, WORK + "/wk-overlay-tailnet/usr/sbin/wk-tailnet-join"],
                      [list(a) for a in w.ran("install")])
        self.assertEqual(self.overlay(w.files[WORK + "/.config"]), [WORK + "/wk-overlay-tailnet"])

    def test_a_release_that_does_not_match_its_pin_is_refused(self):
        w = World()
        w.sha = "00" * 32
        _, err = quiet(w.build(image_args("--overlay-arch", "arm")).run)
        self.assertIn("pinned sha256", err)
        self.assertFalse(w.ran("make"))

    def test_the_wifi_overlay_carries_the_layer_s_join_script(self):
        w = World()
        quiet(w.build(image_args("--overlay-wifi", "1")).run)
        self.assertIn(["install", "-m", "0755", TOOLS + "/" + bt.WIFI_JOIN, WORK + "/wk-overlay-wifi/usr/sbin/wk-wifi-join"],
                      [list(a) for a in w.ran("install")])

    def test_no_overlay_no_line(self):
        w = World()
        quiet(w.build(image_args()).run)
        self.assertEqual(self.overlay(w.files[WORK + "/.config"]), [])


class TestPinnedKernel(unittest.TestCase):
    KT = "/cache/buildroot/dl/wk-kernel-6.1.tar"

    def world(self):
        w = World()
        w.files[self.KT] = ""
        stage = WORK + "/wk-kernel"
        w.react(["tar", "-C", stage, "-xf"], lambda a, f: (f.files.update({stage + "/boot/zImage": "",
                                                                         stage + "/dtb/bcm2710-rpi-3-b.dtb": ""}),
                                                           f.dirs.add(stage + "/lib/modules/6.1"), Result(0))[-1])
        return w

    def args(self):
        return image_args("--kernel-tar", self.KT, "--kernel-release", "6.1")

    def test_the_modules_are_an_overlay_and_the_boot_files_a_hook_ahead_of_the_board_s(self):
        w = self.world()
        _, err = quiet(w.build(self.args()).run)
        self.assertIsNone(err)
        conf = w.files[WORK + "/.config"]
        self.assertEqual(bt.config_value(conf, "BR2_ROOTFS_OVERLAY").split(), [WORK + "/wk-overlay-kernel"])
        self.assertEqual(bt.config_value(conf, "BR2_ROOTFS_POST_IMAGE_SCRIPT"),
                         WORK + "/wk-kernel-post-image.sh board/raspberrypi3/post-image.sh")
        self.assertIn("bcm2710-rpi-3-b.dtb", w.files[WORK + "/wk-kernel-post-image.sh"])

    def test_a_kernel_the_driver_did_not_hand_over_is_refused(self):
        w = World()
        _, err = quiet(w.build(self.args()).run)
        self.assertIn("not at", err)

    def test_a_kernel_without_the_board_s_device_tree_is_refused(self):
        w = self.world()
        w.react(["make"], lambda a, f: (f.files.__setitem__(WORK + "/.config", 'BR2_LINUX_KERNEL_INTREE_DTS_NAME="other"\n')
                                        if a[-1].endswith("_defconfig") else None, Result(0))[-1])
        _, err = quiet(w.build(self.args()).run)
        self.assertIn("other.dtb", err)


class SlotWorld(World):
    def __init__(self):
        super().__init__()
        root = OUT + "/wk-slots/base/root"
        self.files.update({OUT + "/build/packages-file-list.txt": "wpewebkit,./usr/lib/libWPEWebKit-1.1.so.0.2.9\nbusybox,./bin/sh\n",
                           OUT + "/target/usr/lib/libWPEWebKit-1.1.so.0.2.9": "",
                           TCF: 'set(CMAKE_C_COMPILER "/x/host/bin/arm-buildroot-linux-gnueabihf-gcc")\n-Wl,--build-id\n'})
        self.react(["tar", "-C", root, "-xf"], lambda a, f: ([f._set_file(root + p, "") for p in (
            "/usr/lib/libWPEWebKit-1.1.so.0.2.9", "/usr/lib/wpe-webkit-1.1/injected-bundle/libWPEInjectedBundle.so",
            "/usr/libexec/wpe-webkit-1.1/WPEWebProcess")], Result(0))[-1])


class TestTheSlot(unittest.TestCase):
    def test_it_builds_the_commit_and_describes_it(self):
        w = SlotWorld()
        out, err = quiet(w.build(webkit_args()).run)
        self.assertIsNone(err, out)
        self.assertIn("stage 'webkit-base' done", out)
        self.assertIn("WPEWEBKIT_OVERRIDE_SRCDIR = /src/WebKit", w.files[WORK + "/local.mk"])
        manifest = w.ran("python3", TOOLS + "/lib/wkslot.py", "manifest")[0]
        self.assertIn(OUT + "/host/bin/arm-buildroot-linux-gnueabihf-readelf", manifest)
        self.assertIn("exec_dir=usr/libexec/wpe-webkit-1.1", manifest)
        self.assertEqual(w.files[OUT + "/wk-slots/base/files.txt"], "./usr/lib/libWPEWebKit-1.1.so.0.2.9\n")

    def test_uncommitted_changes_are_refused(self):
        w = SlotWorld()
        w.answer(["git", "-C", SRC, "status", "--porcelain"], out=" M Source/x.cpp\n")
        _, err = quiet(w.build(webkit_args()).run)
        self.assertIn("uncommitted", err)
        self.assertFalse(w.ran("env"))

    def test_an_image_without_a_build_id_is_refused(self):
        w = SlotWorld()
        w.files[TCF] = "set(CMAKE_C_COMPILER x)\n"
        _, err = quiet(w.build(webkit_args()).run)
        self.assertIn("--build-id", err)

    def test_an_unfinished_image_is_refused(self):
        w = SlotWorld()
        del w.files[OUT + "/build/packages-file-list.txt"]
        _, err = quiet(w.build(webkit_args()).run)
        self.assertIn("never built to the end", err)


if __name__ == "__main__":
    unittest.main()
