"""`wk sysimage write --from` takes a configuration name, resolved to the image its workspace holds."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402
from wk.machine import Fake, Result  # noqa: E402
from wk.sysimage import write  # noqa: E402

STORE = "/store"
YOCTO = STORE + "/ws/yocto-wpewebkit-2.46-yocto-rpi5-64/build/CrossToolChains/x/build/image/x.wic.xz"
BUILDROOT = STORE + "/ws/buildroot-wpewebkit-2.38-buildroot-rpi3-32/build/buildroot/b/output/images/sdcard.img"


class Store:
    def root(self):
        return STORE


def resolved(spec, answer=None, local=False):
    """(path or None, stderr): one yocto and one buildroot workspace with an image, a yocto one with none, and the
    image workspace's own answer to `wk sysimage path` for a configuration this machine's scan does not find."""
    m = Fake()
    for path in (YOCTO, BUILDROOT):
        m._set_file(path, "image")
    m.dirs.add(STORE + "/ws/yocto-webkit-2.52-yocto-rpi3-32")
    m.answer((os.path.join(str(REPO), "wk"), "sysimage", "path"), *answer or (1, ""))
    w = write.Write(REPO, {"WK_ROOT": str(REPO)}, m, Store())
    with mock.patch("wk.store.Store.is_local", lambda st: local), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        try:
            return w.resolve(spec), err.getvalue()
        except act.Refused:
            return None, err.getvalue()


class TestAConfigurationNamesItsImage(unittest.TestCase):
    def test_a_yocto_or_buildroot_configuration_resolves_to_its_bytes(self):
        self.assertEqual(resolved("wpewebkit-2.46-yocto-rpi5-64")[0], YOCTO)
        self.assertEqual(resolved("wpewebkit-2.38-buildroot-rpi3-32")[0], BUILDROOT)

    def test_a_workspace_with_no_image_does_not_resolve(self):
        path, err = resolved("webkit-2.52-yocto-rpi3-32")
        self.assertIsNone(path)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi3-32", err)

    def test_an_unknown_configuration_is_refused_listing_what_has_been_built(self):
        path, err = resolved("nonsense")
        self.assertIsNone(path)
        self.assertIn("neither a path nor a configuration", err)
        self.assertIn("      wpewebkit-2.46-yocto-rpi5-64\n", err)
        self.assertIn("      wpewebkit-2.38-buildroot-rpi3-32\n", err)
        self.assertNotIn("webkit-2.52-yocto-rpi3-32", err, "a configuration with no image is offered as writable")


class TestAPathIsStillAPath(unittest.TestCase):
    """A path must never be looked up as a configuration: it passes through untouched, vm: prefix included."""

    def test_every_spelling_of_a_path(self):
        for spec in ("/tmp/some.wic.xz", "vm:/var/lib/x.img", "./out.img", "../out.img"):
            self.assertEqual(resolved(spec)[0], spec)


class TestAConfigurationAnImageWorkspaceHoldsAndThisMachineCannotRead(unittest.TestCase):
    """The image workspace's machine is asked for the path (`wk sysimage path`); `vm:` says whose filesystem it is."""

    IMAGE = "/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/i.wic.xz"

    def test_the_image_workspaces_answer_is_read_as_the_vms_own_path(self):
        self.assertEqual(resolved("webkit-2.52-yocto-rpi5-64", (0, self.IMAGE + "\r\n"))[0], "vm:" + self.IMAGE)

    def test_a_store_this_machine_can_read_needs_no_prefix(self):
        self.assertEqual(resolved("webkit-2.52-yocto-rpi5-64", (0, self.IMAGE), local=True)[0], self.IMAGE)

    def test_an_image_workspace_that_holds_no_image_is_refused_naming_the_build(self):
        path, err = resolved("webkit-2.52-yocto-rpi5-64", (1, ""))
        self.assertIsNone(path)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi5-64", err)


if __name__ == "__main__":
    unittest.main()
