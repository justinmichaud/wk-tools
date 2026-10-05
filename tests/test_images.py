"""lib/wk/images.py: the preset loader, and every name derived from a preset."""
import contextlib
import io
import sys
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402
from wk import images, pgo  # noqa: E402
from wk.sysimage import yocto  # noqa: E402

PRESET = "webkit-2.52-yocto-rpi5-64"
WS = "yocto-" + PRESET
BUILDROOT_PRESET = "wpewebkit-2.38-buildroot-rpi3-32"
BUILDROOT_WS = "buildroot-" + BUILDROOT_PRESET
STORE = {"WK_STORE": "/store", "WK_ROOT": str(REPO)}


class ScratchRoot(WkTest):
    """A WK_ROOT of this test's own, holding only the confs it writes."""

    def setUp(self):
        super().setUp()
        (self.tmp / "image" / "presets").mkdir(parents=True)
        self.env = {"WK_ROOT": str(self.tmp)}

    def conf(self, name, text):
        (self.tmp / "image" / "presets" / (name + ".conf")).write_text(text)


class TestTheLoader(ScratchRoot):
    def test_a_field_the_conf_leaves_out_is_its_default(self):
        self.conf("p", "# p -- a preset\nIMG_BUILDER=yocto\n")
        p = images.load("p", self.env)
        self.assertEqual(p["IMG_BUILDER"], "yocto")
        self.assertEqual(p["IMG_WATCHDOG"], "300")
        self.assertEqual(p["YOC_REMOTE"], "origin")
        self.assertEqual(p["IMG_SPEC_DIR"], str(self.tmp / "image" / "p"))

    def test_a_second_load_inherits_nothing_from_the_first(self):
        self.conf("a", "IMG_MACHINE=rpi5\nCFG_NEEDS=\"x\"\n")
        self.conf("b", "IMG_BUILDER=pmos\n")
        images.load("a", self.env)
        b = images.load("b", self.env)
        self.assertEqual((b["IMG_MACHINE"], b["CFG_NEEDS"]), ("", ""))

    def test_a_quoted_value_may_span_lines(self):
        self.conf("p", 'CFG_NEEDS="one\n    two"  # why\nIMG_ARCH=arm64\n')
        p = images.load("p", self.env)
        self.assertEqual(p["CFG_NEEDS"], "one\n    two")
        self.assertEqual(p["IMG_ARCH"], "arm64")

    def test_a_name_no_conf_answers_to_is_a_lookup_error(self):
        for name in ("nosuch", "../configs/p", ""):
            with self.subTest(name=name):
                with self.assertRaises(LookupError):
                    images.load(name, self.env)

    def test_a_conf_is_literals_of_known_fields(self):
        for text in ("IMG_BUILDER=$HOME\n", "IMG_NOPE=1\n", "IMG_ARCH=a b\n",
                     'CFG_NEEDS="never closed\n', "just prose\n"):
            with self.subTest(text=text):
                self.conf("p", text)
                with self.assertRaises(images.ConfError):
                    images.load("p", self.env)


class TestABrokenPresetIsNoWorkspaceQuestion(ScratchRoot):
    def test_a_conf_that_does_not_parse_is_refused_rather_than_read_as_building_on_the_host(self):
        self.conf("p", "IMG_BUILDER=yocto\nIMG_NOPE=1\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(act.Refused):
            images.image_ws("p", self.env)
        self.assertIn("p.conf:2: IMG_NOPE is not an image preset field", err.getvalue())
        self.assertEqual(images.image_ws("nosuch", self.env), "")


class TestTheListing(ScratchRoot):
    def test_each_preset_is_listed_with_its_header_and_what_it_needs(self):
        self.conf("a", "# a -- the first\nIMG_BUILDER=yocto\n")
        self.conf("b", '# b -- the second\nCFG_NEEDS="a defconfig"\n')
        self.assertEqual(images.listing(self.env).splitlines(), [
            "a", "            the first",
            "b", "            the second",
            "            -- not buildable yet; 'wk sysimage build b' says what it needs"])

    def test_origins_branches_are_those_an_origin_preset_tracks(self):
        self.conf("a", "CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n")
        self.conf("b", "CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n")
        self.conf("c", "CFG_REMOTE=wpe\nCFG_BRANCH=wpe-2.46\n")
        self.assertEqual(images.origin_branches(self.env), ["webkitglib/2.52"])


class TestPgoWanted(unittest.TestCase):
    def test_a_yocto_preset_from_2_52_is_profile_guided_and_nothing_else_is(self):
        for builder, release, wanted in (("yocto", "2.52", True), ("yocto", "2.60", True), ("yocto", "2.46", False),
                                         ("buildroot", "2.52", False), ("yocto", "", False)):
            with self.subTest(builder=builder, release=release):
                self.assertEqual(wanted, images.pgo_wanted(builder, release))


class TestTheSpec(unittest.TestCase):
    def test_the_machine_half_is_split_off_the_preset(self):
        self.assertEqual(images.spec_preset(PRESET + "@moose"), PRESET)
        self.assertEqual(images.spec_machine(PRESET + "@moose"), "moose")
        self.assertEqual(images.spec_machine(PRESET), "")

    def test_this_machines_own_name_is_its_default_place(self):
        self.assertEqual(images.spec_place("here", "here", "container"), "container")
        self.assertEqual(images.spec_place("moose", "here", "container"), "moose")


class TestTheImageWorkspace(unittest.TestCase):
    def test_it_is_its_builder_and_its_preset(self):
        self.assertEqual(images.image_ws(PRESET, STORE), WS)
        self.assertEqual(images.image_ws(BUILDROOT_PRESET, STORE), BUILDROOT_WS)

    def test_the_machine_half_is_no_part_of_the_name(self):
        self.assertEqual(images.image_ws(PRESET + "@moose", STORE), WS)

    def test_a_host_builder_or_no_preset_names_none(self):
        for spec in ("bridge-pinephone", "recovery-pinephone", "nosuch"):
            with self.subTest(spec=spec):
                self.assertEqual(images.image_ws(spec, STORE), "")

    def test_the_preset_comes_back_out_of_the_name_by_longest_match(self):
        self.assertEqual(images.ws_preset(WS, STORE), PRESET)
        self.assertEqual(images.ws_preset(WS + "-base", STORE), PRESET)
        self.assertEqual(images.ws_preset(WS + "-oc", STORE), PRESET + "-oc")
        self.assertIsNone(images.ws_preset("jsc-release", STORE))
        self.assertIsNone(images.ws_preset("yocto-nosuch", STORE))

    def test_the_workspace_option_names_another(self):
        self.assertEqual(images.ws_arg([PRESET, "--workspace", WS + "-b"], STORE), WS + "-b")
        self.assertEqual(images.ws_arg([PRESET, "--slot", "x", "--workspace=" + WS + "-c"], STORE), WS + "-c")
        self.assertEqual(images.ws_arg([PRESET, "--slot", "base"], STORE), WS)
        self.assertEqual(images.ws_arg(["--slot", "base"], STORE), "")
        self.assertEqual(images.ws_arg([], STORE), "")


class TestEveryPathIsKeyedOnTheWorkspace(unittest.TestCase):
    """One image preset may have a workspace per arm, and a slot or a collection in
    one is not the other's."""

    def test_a_yocto_slot(self):
        self.assertEqual(images.slot_dir(WS + "-pr", "base", STORE),
                         "/store/ws/%s-pr/build/wk-slots/base" % WS)

    def test_a_buildroot_slot_keeps_the_preset_inside(self):
        self.assertEqual(images.slot_dir(BUILDROOT_WS + "-pr", "base", STORE),
                         "/store/ws/%s-pr/build/buildroot/%s/output/wk-slots/base"
                         % (BUILDROOT_WS, BUILDROOT_PRESET))

    def test_no_image_workspace_has_no_slot(self):
        self.assertIsNone(images.slot_dir("jsc-release", "base", STORE))

    def test_a_collection(self):
        self.assertEqual(images.pgo_dir(WS + "-base", "pr", STORE), "/store/ws/%s-base/build/wk-pgo/pr" % WS)
        self.assertEqual(pgo.pgo_dir_in("pr"), "/src/WebKit/WebKitBuild/wk-pgo/pr")

    def test_the_instrumented_slot_is_the_measured_slots_name_and_the_suffix(self):
        self.assertEqual(images.instr_slot("base"), "base-instr")
        self.assertEqual(images.measured_slot("base-instr"), "base")
        self.assertEqual(images.measured_slot("base"), "base")


class TestTheToolchain(WkTest):
    def test_it_is_held_only_with_the_helpers_marker_and_a_setup_script(self):
        env = {"WK_STORE": str(self.tmp)}
        d = self.tmp / "ws" / WS / "build" / "CrossToolChains" / "t" / "build" / "toolchain"
        d.mkdir(parents=True)
        self.assertFalse(images.toolchain_holds(WS, "t", env))
        (d / ".toolchain_path_configured").write_text("")
        self.assertFalse(images.toolchain_holds(WS, "t", env))
        (d / "environment-setup-cortexa76").write_text("")
        self.assertTrue(images.toolchain_holds(WS, "t", env))


class TestWhichMachineHoldsIt(unittest.TestCase):
    def test_the_spec_then_the_workspaces_place_and_container_or_local_is_this_machine(self):
        for spec, place, machine in (("moose", "elsewhere", "moose"), ("", "elsewhere", "elsewhere"),
                                     ("", "container", "here"), ("", "local", "here")):
            with self.subTest(spec=spec, place=place):
                self.assertEqual(machine, images.ws_machine(spec, place, "here"))

    def test_the_scheduler_serialises_by_machine_alone(self):
        self.assertEqual(images.build_resource("moose"), "machine:moose")


class TestNames(unittest.TestCase):
    def test_a_slot_name_is_a_directory_name_here_and_on_the_board(self):
        for good in ("base", "pr-2", "a.b_c"):
            images.check_slot_name(good)
        for bad in ("", "-x", ".x", "a/b", "a b"):
            with self.subTest(slot=bad):
                with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()):
                    images.check_slot_name(bad)

    def test_a_build_says_which_build_it_is(self):
        self.assertEqual(yocto.build_subject(WS, "webkit", "base", "a" * 40, "wpe-cross-pgo-collect"),
                         "slot base in %s at %s -- instrumented, to collect a profile from -- not a measurement"
                         % (WS, "a" * 12))
        self.assertEqual(yocto.build_subject(WS, "pgo-mix", "base", "", ""), "mixing slot base's collection in " + WS)
        self.assertEqual(yocto.build_subject(WS, "", "", "", ""), "build stage of " + WS)


if __name__ == "__main__":
    unittest.main()
