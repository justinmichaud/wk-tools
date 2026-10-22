"""The buildroot image and slot builds as they run inside a workspace (lib/wk/sysimage/buildroot_ws.py)."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.clock import FakeClock  # noqa: E402
from wk.machine import Fake, Result, isolated_module  # noqa: E402
from wk.sysimage import Failed, buildroot_ws as bt  # noqa: E402

TOOLS, SRC = "/opt/wk-tools", "/src/WebKit"
NAME = "wpewebkit-2.46-buildroot-rpi3-32"
WORK = SRC + "/WebKitBuild/buildroot/" + NAME
OUT = WORK + "/output"
TCF = OUT + "/host/share/buildroot/toolchainfile.cmake"
ENV = {"BR2_DL_DIR": "/cache/buildroot/dl", "BR2_CCACHE_DIR": "/cache/buildroot/ccache", "WK_MIRROR": "/mirror/WebKit.git"}
SHA, COMMIT = "ab" * 32, "c" * 40
EXT = "BR2_EXTERNAL=%s/image/buildroot/external" % TOOLS
GUARD = ("env", "WK_MB_PER_JOB=%d" % bt.MB_PER_JOB)
KT = "/cache/buildroot/dl/wk-kernel-6.1.tar"


class WsWorld(Fake):
    """A workspace an in-workspace builder's `Build` runs in."""

    BUILD, PARSE, ENV = bt.Build, staticmethod(bt.parse), ENV

    def __init__(self):
        super().__init__("ws")
        self.clock = FakeClock()

    def build(self, argv, **preset):
        b = self.BUILD(self.PARSE(argv), self, dict(self.ENV), self.clock, TOOLS)
        b.p = dict(b.p, **preset)
        return b

    def ran(self, *prefix):
        return [list(e[1]) for e in self.effects if e[0] in ("run", "run_tty") and tuple(e[1][:len(prefix)]) == prefix]

    def go(self, argv, **preset):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            try:
                self.build(argv, **preset).run()
            except Failed as e:
                return out.getvalue(), str(e)
        return out.getvalue(), None


class World(WsWorld):
    """A workspace with the tree cloned; make and the guarded build answer as buildroot would."""

    def __init__(self):
        super().__init__()
        self.dirs.update({SRC, WORK + "/.git", WORK + "/package", TOOLS + "/image/buildroot/tree-patches"})
        self.files.update({TOOLS + "/" + bt.TS_REL: 'TS_VERSION = "1.2.3"\nTS_SHA256_arm = "%s"\n' % SHA,
                           TOOLS + "/image/buildroot/tree-patches/0001-fdo.patch": "",
                           TOOLS + "/image/buildroot/external/external.desc": "name: WK\n"})
        self.mtime, self.sha = None, SHA
        for p in (["git"], ["gcc"], ["test"], ["tar"], ["cp"], ["install"], ["chmod"], ["curl"], ["mv"], ["python3"], ["du"]):
            self.answer(p)
        self.answer(["git", "-C", WORK, "apply", "--reverse", "--check"], rc=1)
        self.answer(["git", "-C", SRC, "status", "--porcelain"], out="")
        self.react(["sha256sum"], lambda a, f: Result(0, "%s  %s\n" % (f.sha, a[1])))
        self.react(["make"], self._make)
        self.react(GUARD, self._build)
        self.react(["find"], lambda a, f: Result(0, "a.ko\n") if "-printf" not in a else
                   Result(0, "%d.5\n" % f.mtime) if f.mtime is not None and a[1] in f.files else Result(1))
        stage = WORK + "/wk-kernel"
        self.react(["tar", "-C", stage, "-xf"], lambda a, f: (f.files.update({stage + "/boot/zImage": "", stage + "/dtb/bcm2710-rpi-3-b.dtb": ""}),
                                                           f.dirs.add(stage + "/lib/modules/6.1"), Result(0))[-1])

    def _make(self, argv, f):
        if argv[-1].endswith("_defconfig"):
            f.files[WORK + "/.config"] = 'BR2_ROOTFS_POST_IMAGE_SCRIPT="board/raspberrypi3/post-image.sh"\n'
        return Result(0)

    def _build(self, argv, f):
        f.dirs.add(OUT + "/images")
        f.files[OUT + "/images/sdcard.img"] = ""
        f.mtime = int(f.clock.now()) + 60
        return Result(0)

    def image(self, *more, **preset):
        return self.go(["image", NAME, "--jobs", "8"] + list(more), **dict({"BR_OVERLAY_TAILSCALE": ""}, **preset))

    def conf(self, key):
        return bt.config_value(self.files[WORK + "/.config"], key)


class TestArguments(unittest.TestCase):
    def test_each_incomplete_or_malformed_call_is_a_usage_error(self):
        for argv in ([], ["webkit", NAME, "--commit", COMMIT], ["image"], ["image", NAME, "--bogus"]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                bt.parse(argv)
            self.assertEqual(cm.exception.code, 2)


class TestTheImage(unittest.TestCase):
    def test_it_builds_from_its_preset_under_the_guard_with_br2_external_on_every_make(self):
        w = World()
        out, err = w.image()
        self.assertIsNone(err, out)
        self.assertIn("stage 'image' done", out)
        self.assertIn(["make", "-C", WORK, EXT, "raspberrypi3_wpe_2_46_cog_defconfig"], w.ran("make"))
        (guarded,) = w.ran(*GUARD)
        self.assertTrue([a for a in guarded if ". %s/build/guard.sh" % TOOLS in a])
        self.assertTrue({"FORCE_UNSAFE_CONFIGURE=1", "-j8", EXT} <= set(guarded))
        for key, want in (("BR2_DL_DIR", "/cache/buildroot/dl"), ("BR2_CCACHE_DIR", "/cache/buildroot/ccache"),
                          ("BR2_TARGET_LDFLAGS", "-Wl,--build-id"), ("BR2_ROOTFS_OVERLAY", "")):
            self.assertEqual(w.conf(key), want, key)
        self.assertIn("BR2_JLEVEL=8\n", w.files[WORK + "/.config"])

    def test_caches_that_are_not_set_are_refused(self):
        w = World()
        w.ENV = {}
        self.assertIn("BR2_DL_DIR", w.image()[1])
        self.assertFalse(w.ran("make"))

    def test_a_tree_patch_is_applied_once_and_one_that_does_not_apply_is_refused(self):
        w = World()
        w.answer(["git", "-C", WORK, "apply", "--reverse", "--check"])
        w.image()
        self.assertFalse([a for a in w.ran("git", "-C", WORK, "apply") if "--reverse" not in a])
        w = World()
        w.answer(["git", "-C", WORK, "apply", TOOLS + "/image/buildroot/tree-patches/0001-fdo.patch"], rc=1)
        self.assertIn("0001-fdo.patch", w.image()[1])

    def test_a_slot_s_source_override_is_dropped(self):
        w = World()
        w.files[WORK + "/local.mk"] = "WPEWEBKIT_OVERRIDE_SRCDIR = /src/WebKit\n"
        w.image()
        self.assertNotIn(WORK + "/local.mk", w.files)
        self.assertTrue([a for a in w.ran("make") if "wpewebkit-dirclean" in a])

    def test_an_image_older_than_the_stage_or_none_fails_it(self):
        w = World()
        w.react(GUARD, lambda a, f: (f.dirs.add(OUT + "/images"), f.files.__setitem__(OUT + "/images/sdcard.img", ""),
                                     setattr(f, "mtime", 1), Result(0))[-1])
        out, err = w.image()
        self.assertIn("newer than this stage", err)
        w = World()
        w.react(GUARD, lambda a, f: (f.dirs.add(OUT + "/images"), Result(0))[-1])
        self.assertIn("left nothing", w.image()[1])


class TestOverlays(unittest.TestCase):
    def test_the_tailnet_overlay_is_the_pinned_release_and_the_layer_s_join_script(self):
        w = World()
        self.assertIsNone(w.image(BR_OVERLAY_TAILSCALE="arm")[1])
        self.assertIn(["install", "-m", "0755", TOOLS + "/" + bt.TS_JOIN, WORK + "/wk-overlay-tailnet/usr/sbin/wk-tailnet-join"], w.ran("install"))
        self.assertEqual(w.conf("BR2_ROOTFS_OVERLAY"), WORK + "/wk-overlay-tailnet")

    def test_a_release_that_does_not_match_its_pin_is_refused(self):
        w = World()
        w.sha = "00" * 32
        self.assertIn("does not match its pin", w.image(BR_OVERLAY_TAILSCALE="arm")[1])
        self.assertFalse(w.ran("make"))

    def test_the_wifi_overlay_carries_the_layer_s_join_script(self):
        w = World()
        w.image("--wifi")
        self.assertIn(["install", "-m", "0755", TOOLS + "/" + bt.WIFI_JOIN, WORK + "/wk-overlay-wifi/usr/sbin/wk-wifi-join"], w.ran("install"))


class TestPinnedKernel(unittest.TestCase):
    def kernel(self, dts="bcm2710-rpi-3-b", handed=True):
        w = World()
        if handed:
            w.files[KT] = ""
        return w, w.image("--kernel-tar", KT, "--kernel-dts", dts, BR_KERNEL_RELEASE="6.1")[1]

    def test_the_modules_are_an_overlay_and_the_boot_files_a_hook_ahead_of_the_board_s(self):
        w, err = self.kernel()
        self.assertIsNone(err)
        self.assertEqual(w.conf("BR2_ROOTFS_OVERLAY"), WORK + "/wk-overlay-kernel")
        self.assertEqual(w.conf("BR2_ROOTFS_POST_IMAGE_SCRIPT"), WORK + "/wk-kernel-post-image.sh board/raspberrypi3/post-image.sh")
        self.assertIn("bcm2710-rpi-3-b.dtb", w.files[WORK + "/wk-kernel-post-image.sh"])
        self.assertNotIn("BR2_LINUX_KERNEL", w.files[WORK + "/.config"])

    def test_a_kernel_not_handed_over_or_without_its_device_tree_is_refused(self):
        self.assertIn("not at", self.kernel(handed=False)[1])
        self.assertIn("other.dtb", self.kernel(dts="other")[1])


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
        self.slot_json = '{"build_id": "b1d"}'
        self.react(["python3"], lambda a, f: (f._set_file(next(x for x in a if x.endswith("/slot.json")), f.slot_json)
                                             if "manifest" in a else None, Result(0))[-1])

    def slot(self):
        return self.go(["webkit", NAME, "--commit", COMMIT, "--slot", "base", "--jobs", "8"])


class TestTheSlot(unittest.TestCase):
    def test_it_builds_the_commit_and_reports_the_build_id_its_manifest_recorded(self):
        w = SlotWorld()
        out, err = w.slot()
        self.assertIsNone(err, out)
        self.assertIn("stage 'webkit-base' done", out)
        self.assertIn("WPEWEBKIT_OVERRIDE_SRCDIR = /src/WebKit", w.files[WORK + "/local.mk"])
        manifest = w.ran(*isolated_module(TOOLS + "/lib", "wk.slot"), "manifest")[0]
        self.assertIn(OUT + "/host/bin/arm-buildroot-linux-gnueabihf-readelf", manifest)
        self.assertIn("exec_dir=usr/libexec/wpe-webkit-1.1", manifest)
        self.assertEqual(w.files[OUT + "/wk-slots/base/files.txt"], "./usr/lib/libWPEWebKit-1.1.so.0.2.9\n")
        self.assertIn("build-id b1d", out)

    def test_each_refusal_comes_before_the_build_or_instead_of_done(self):
        for name, edit, said in (
                ("no build-id recorded", lambda w: setattr(w, "slot_json", '{"build_id": ""}'), "records no build_id"),
                ("uncommitted", lambda w: w.answer(["git", "-C", SRC, "status", "--porcelain"], out=" M x.cpp\n"), "uncommitted"),
                ("toolchain without --build-id", lambda w: w.files.__setitem__(TCF, "set(CMAKE_C_COMPILER x)\n"), "--build-id"),
                ("unfinished image", lambda w: w.files.pop(OUT + "/build/packages-file-list.txt"), "never built to the end")):
            with self.subTest(name):
                w = SlotWorld()
                edit(w)
                out, err = w.slot()
                self.assertIn(said, err or "")
                self.assertNotIn("stage 'webkit-base' done", out)


if __name__ == "__main__":
    unittest.main()
