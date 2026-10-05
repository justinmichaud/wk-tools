"""There is no image store: a build's output lives where its builder left it, and every reader finds it there
(`wk boot` reads the device, a write derives the profile from the path)."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, WkTest, rand_suffix, run, scratch_dir, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import images  # noqa: E402
from wk.machine import Local  # noqa: E402
from wk.sysimage import write  # noqa: E402
from wk.store import Store  # noqa: E402
from wk.sysimage import ls  # noqa: E402

# Answers the card helper's read of wk-image.id off partition 1 of the board's medium; partition 3 is bare.
_SSH_STUB = '''#!/bin/sh
case "$*" in
  *"boot-read /dev/sda 1 wk-image.id"*) echo "{fake_id}" ;;
  *wk-image.id*) : ;;
  *) : ;;
esac
'''

_FAKE_NODE_CONF = '''ssh={ssh}
kind=board
driver=rpi5-usb
device=/dev/sda
root=/dev/nvme0n1p2
profile=webkit-2.52-yocto-rpi5-64
mac=02:00:00:00:00:01
bridge=""
role=workstation
os=any
volume=""
dtb=bcm2712-rpi-5-b.dtb
bench_ssh=""
net=wifi
note="fake bench board"
'''


class TestBootArmDefaultsToDeviceImage(WkTest):
    """The default system is what the device holds; a named --system is checked against it."""

    def boot(self, fake_id, *args):
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
                stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = "fakerpi5" + rand_suffix(3)
            (machdir / (name + ".conf")).write_text(_FAKE_NODE_CONF.format(ssh=name))
            return run("boot", name, *args, "--dry-run", timeout=30,
                       env={"WK_MACHINES_DIR": str(machdir), "PATH": "%s:/usr/bin:/bin" % binp})

    def test_the_device_s_id_is_armed_and_another_is_refused_naming_the_write(self):
        fake_id, wanted = ("webkit-2.52-yocto-rpi5-64-" + rand_suffix(12) for _ in range(2))
        cp = self.boot(fake_id)
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn(fake_id, cp.stdout)
        cp = self.boot(fake_id, "--system", wanted)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        for words in (fake_id, wanted, "wk sysimage write --from"):
            self.assertIn(words, cp.stdout)


class TestProfileFromWorkspacePath(unittest.TestCase):
    def test_a_full_path_names_its_profile(self):
        w = write.Write(REPO, {"WK_ROOT": str(REPO)}, Local(), None)
        for path, want in (
                ("/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/CrossToolChains/rpi5/build/image/"
                 "webkit-2.52-yocto-rpi5-64.wic.xz", "webkit-2.52-yocto-rpi5-64"),
                ("/var/lib/wk/ws/buildroot-webkit-2.52-buildroot-rpi5-64/build/buildroot/rpi5/output/images/sdcard.img",
                 "webkit-2.52-buildroot-rpi5-64")):
            with self.subTest(path=path), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(w.profile("", path)[0], want)


class TestScanFindsWhatTheBuildersLeave(unittest.TestCase):
    def _scan(self, store):
        return ls.scan(Local(), Store({"WK_STORE": str(store)}))

    def test_a_workspace_that_built_nothing_is_not_a_row(self):
        with scratch_dir() as d:
            (d / "ws" / "jsc-release" / "build").mkdir(parents=True)
            self.assertEqual(self._scan(d), [])

    def test_an_image_workspace_with_no_image_gets_a_placeholder(self):
        with scratch_dir() as d:
            for ws, sub in (("yocto-webkit-2.52-yocto-rpi3-32", "build"),
                            ("yocto-webkit-2.52-yocto-rpi4-64", "build/CrossToolChains/rpi4-64bits-mesa/build/image"),
                            ("buildroot-wpewebkit-2.38-buildroot-rpi4-32", "build")):
                (d / "ws" / ws / sub).mkdir(parents=True)
            self.assertEqual(self._scan(d), [
                ls.Image("buildroot", "buildroot-wpewebkit-2.38-buildroot-rpi4-32", None),
                ls.Image("yocto", "yocto-webkit-2.52-yocto-rpi3-32", None),
                ls.Image("yocto", "yocto-webkit-2.52-yocto-rpi4-64", None)])


if __name__ == "__main__":
    unittest.main()
