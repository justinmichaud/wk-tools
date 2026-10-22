"""lib/wk/images.py: the preset loader, and every name derived from a preset."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act, images  # noqa: E402

PRESET, BUILDROOT = "webkit-2.52-yocto-rpi5-64", "wpewebkit-2.38-buildroot-rpi3-32"
WS, BWS = "yocto-" + PRESET, "buildroot-" + BUILDROOT
STORE = {"WK_STORE": "/store", "WK_ROOT": str(REPO)}


class TestTheLoader(WkTest):
    """A WK_ROOT of this test's own, holding only the confs it writes."""

    def load(self, text, name="p", **more):
        (self.tmp / "image" / "presets").mkdir(parents=True, exist_ok=True)
        for n, t in dict(more, **{name: text}).items():
            (self.tmp / "image" / "presets" / (n + ".conf")).write_text(t)
        return images.load(name, {"WK_ROOT": str(self.tmp)})

    def test_a_field_left_out_is_its_default_and_a_second_load_inherits_nothing(self):
        p = self.load('# p -- a preset\nIMG_BUILDER=yocto\nCFG_NEEDS="one\n    two"  # why\n')
        self.assertEqual((p["IMG_WATCHDOG"], p["YOC_REMOTE"], p["CFG_NEEDS"], p["IMG_SPEC_DIR"]),
                         ("300", "origin", "one\n    two", str(self.tmp / "image" / "p")))
        self.assertEqual(self.load("IMG_BUILDER=guest\n", name="b")["CFG_NEEDS"], "")
        self.assertEqual(images.listing({"WK_ROOT": str(self.tmp)}).splitlines(),
                         ["b", "            ", "p", "            a preset", "            -- not buildable yet; 'wk sysimage build p' says what it needs"])

    def test_a_conf_is_literals_of_known_fields_and_a_name_none_answers_to_is_a_lookup_error(self):
        for text in ("IMG_BUILDER=$HOME\n", "IMG_NOPE=1\n", "IMG_ARCH=a b\n", 'CFG_NEEDS="never closed\n', "just prose\n"):
            with self.subTest(text=text), self.assertRaises(images.ConfError):
                self.load(text)
        for name in ("nosuch", "../configs/p", ""):
            with self.assertRaises(LookupError):
                images.load(name, {"WK_ROOT": str(self.tmp)})
        (self.tmp / "image" / "presets" / "p.conf").write_text("IMG_BUILDER=yocto\nIMG_NOPE=1\n")
        with contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(act.Refused):
            images.image_ws("p", {"WK_ROOT": str(self.tmp)})
        self.assertIn("p.conf:2: IMG_NOPE is not an image preset field", err.getvalue())

    def test_origin_s_branches_are_those_an_origin_preset_tracks(self):
        self.load("CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n", b="CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n",
                  c="CFG_REMOTE=wpe\nCFG_BRANCH=wpe-2.46\n")
        self.assertEqual(images.origin_branches({"WK_ROOT": str(self.tmp)}), ["webkitglib/2.52"])


class TestTheNames(unittest.TestCase):
    def test_the_spec_and_the_image_workspace(self):
        self.assertEqual((images.spec_preset(PRESET + "@moose"), images.spec_machine(PRESET + "@moose"), images.spec_machine(PRESET)),
                         (PRESET, "moose", ""))
        self.assertEqual([images.spec_place(m, "here", "container") for m in ("here", "moose")], ["container", "moose"])
        self.assertEqual([images.image_ws(s, STORE) for s in (PRESET, BUILDROOT, PRESET + "@moose", "macos-guest-base", "nosuch")],
                         [WS, BWS, WS, "", ""])
        self.assertEqual([images.ws_preset(w, STORE) for w in (WS, WS + "-base", WS + "-oc", "jsc-release", "yocto-nosuch")],
                         [PRESET, PRESET, PRESET + "-oc", None, None])
        self.assertEqual([images.ws_arg(a, STORE) for a in ([PRESET, "--workspace", WS + "-b"], [PRESET, "--slot", "x", "--workspace=" + WS + "-c"],
                                                             [PRESET, "--slot", "base"], ["--slot", "base"], [])], [WS + "-b", WS + "-c", WS, "", ""])

    def test_every_path_is_keyed_on_the_workspace(self):
        """One image preset may have a workspace per arm, and a slot or a collection in one is not the other's."""
        self.assertEqual([images.slot_dir(w, "base", STORE) for w in (WS + "-pr", BWS + "-pr", "jsc-release")],
                         ["/store/ws/%s-pr/build/wk-slots/base" % WS, "/store/ws/%s-pr/build/buildroot/%s/output/wk-slots/base" % (BWS, BUILDROOT), None])
        self.assertEqual(images.pgo_dir(WS + "-base", "pr", STORE), "/store/ws/%s-base/build/wk-pgo/pr" % WS)
        self.assertEqual((images.instr_slot("base"), images.measured_slot("base-instr"), images.measured_slot("base")), ("base-instr", "base", "base"))

    def test_pgo_the_machine_holding_it_and_a_slot_name(self):
        self.assertEqual([images.pgo_wanted(b, r) for b, r in (("yocto", "2.52"), ("yocto", "2.60"), ("yocto", "2.46"), ("buildroot", "2.52"), ("yocto", ""))],
                         [True, True, False, False, False])
        self.assertEqual([images.ws_machine(s, p, "here") for s, p in (("moose", "elsewhere"), ("", "elsewhere"), ("", "container"), ("", "local"))],
                         ["moose", "elsewhere", "here", "here"])
        self.assertEqual(images.build_resource("moose"), "machine:moose")
        for good in ("base", "pr-2", "a.b_c"):
            images.check_slot_name(good)
        for bad in ("", "-x", ".x", "a/b", "a b"):
            with self.subTest(slot=bad), self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()):
                images.check_slot_name(bad)


if __name__ == "__main__":
    unittest.main()
