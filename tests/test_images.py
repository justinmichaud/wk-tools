"""lib/wk/images.py: the profile loader, and every name derived from a profile --
the spec, the image workspace, its slots and collections -- plus the bash shims
lib/image.sh and image/profiles.sh keep for their unported callers.

Run: python3 -m unittest tests.test_images -v
"""
import contextlib
import io
import re
import sys
import unittest

from tests.support import REPO, WkTest, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import act  # noqa: E402
from wk import images  # noqa: E402

PROFILE = "webkit-2.52-yocto-rpi5-64"
WS = "yocto-" + PROFILE
BUILDROOT_PROFILE = "wpewebkit-2.38-buildroot-rpi3-32"
BUILDROOT_WS = "buildroot-" + BUILDROOT_PROFILE
STORE = {"WK_STORE": "/store", "WK_ROOT": str(REPO)}
SHIM_FILES = (REPO / "lib" / "image.sh", REPO / "image" / "profiles.sh")


class ScratchRoot(WkTest):
    """A WK_ROOT of this test's own, holding only the confs it writes."""

    def setUp(self):
        super().setUp()
        (self.tmp / "image" / "configs").mkdir(parents=True)
        self.env = {"WK_ROOT": str(self.tmp)}

    def conf(self, name, text):
        (self.tmp / "image" / "configs" / (name + ".conf")).write_text(text)


class TestTheLoader(ScratchRoot):
    def test_a_field_the_conf_leaves_out_is_its_default(self):
        self.conf("p", "# p -- a profile\nIMG_BUILDER=yocto\n")
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

    def test_a_retired_name_names_its_replacement(self):
        with self.assertRaises(images.Tombstone) as cm:
            images.load("rpi5-perf", self.env)
        self.assertIn("webkit-2.52-yocto-rpi5-64", str(cm.exception))

    def test_a_conf_is_literals_of_known_fields(self):
        for text in ("IMG_BUILDER=$HOME\n", "IMG_NOPE=1\n", "IMG_ARCH=a b\n",
                     'CFG_NEEDS="never closed\n', "just prose\n"):
            with self.subTest(text=text):
                self.conf("p", text)
                with self.assertRaises(images.ConfError):
                    images.load("p", self.env)

    def test_the_shell_text_evals_back_to_the_same_values(self):
        self.conf("p", 'FET_NOTE="the phone\'s eMMC"\nCFG_NEEDS="a\n  b"\n')
        text = images.shell_text(images.load("p", self.env))
        cp = bash(text + 'printf "%s|%s" "$FET_NOTE" "$CFG_NEEDS"')
        self.assertEqual(cp.stdout, "the phone's eMMC|a\n  b")


class TestTheListing(ScratchRoot):
    def test_each_profile_is_listed_with_its_header_and_what_it_needs(self):
        self.conf("a", "# a -- the first\nIMG_BUILDER=yocto\n")
        self.conf("b", '# b -- the second\nCFG_NEEDS="a defconfig"\n')
        self.assertEqual(images.listing(self.env).splitlines(), [
            "a", "            the first",
            "b", "            the second",
            "            -- not buildable yet; 'wk sysimage build b' says what it needs"])

    def test_origins_branches_are_those_an_origin_profile_tracks(self):
        self.conf("a", "CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n")
        self.conf("b", "CFG_REMOTE=origin\nCFG_BRANCH=webkitglib/2.52\n")
        self.conf("c", "CFG_REMOTE=wpe\nCFG_BRANCH=wpe-2.46\n")
        self.assertEqual(images.origin_branches(self.env), ["webkitglib/2.52"])


class TestPgoWanted(unittest.TestCase):
    def test_a_yocto_profile_from_2_52_is_profile_guided(self):
        self.assertTrue(images.pgo_wanted("yocto", "2.52"))
        self.assertTrue(images.pgo_wanted("yocto", "2.60"))

    def test_an_older_release_or_another_builder_is_not(self):
        self.assertFalse(images.pgo_wanted("yocto", "2.46"))
        self.assertFalse(images.pgo_wanted("buildroot", "2.52"))
        self.assertFalse(images.pgo_wanted("yocto", ""))


class TestTheSpec(unittest.TestCase):
    def test_the_machine_half_is_split_off_the_profile(self):
        self.assertEqual(images.spec_profile(PROFILE + "@moose"), PROFILE)
        self.assertEqual(images.spec_machine(PROFILE + "@moose"), "moose")
        self.assertEqual(images.spec_machine(PROFILE), "")

    def test_this_machines_own_name_is_its_default_target(self):
        self.assertEqual(images.spec_target("here", "here", "container"), "container")
        self.assertEqual(images.spec_target("moose", "here", "container"), "moose")


class TestTheImageWorkspace(unittest.TestCase):
    def test_it_is_its_builder_and_its_profile(self):
        self.assertEqual(images.image_ws(PROFILE, STORE), WS)
        self.assertEqual(images.image_ws(BUILDROOT_PROFILE, STORE), BUILDROOT_WS)

    def test_the_machine_half_is_no_part_of_the_name(self):
        self.assertEqual(images.image_ws(PROFILE + "@moose", STORE), WS)

    def test_a_host_builder_or_no_profile_names_none(self):
        for spec in ("bridge-pinephone", "recovery-pinephone", "nosuch", "rpi5-perf"):
            with self.subTest(spec=spec):
                self.assertEqual(images.image_ws(spec, STORE), "")

    def test_the_profile_comes_back_out_of_the_name_by_longest_match(self):
        self.assertEqual(images.ws_profile(WS, STORE), PROFILE)
        self.assertEqual(images.ws_profile(WS + "-base", STORE), PROFILE)
        self.assertEqual(images.ws_profile(WS + "-oc", STORE), PROFILE + "-oc")
        self.assertIsNone(images.ws_profile("jsc-release", STORE))
        self.assertIsNone(images.ws_profile("yocto-nosuch", STORE))

    def test_the_workspace_option_names_another(self):
        self.assertEqual(images.ws_arg([PROFILE, "--workspace", WS + "-b"], STORE), WS + "-b")
        self.assertEqual(images.ws_arg([PROFILE, "--slot", "x", "--workspace=" + WS + "-c"], STORE), WS + "-c")
        self.assertEqual(images.ws_arg([PROFILE, "--slot", "base"], STORE), WS)
        self.assertEqual(images.ws_arg(["--slot", "base"], STORE), "")
        self.assertEqual(images.ws_arg([], STORE), "")


class TestEveryPathIsKeyedOnTheWorkspace(unittest.TestCase):
    """One profile may have a workspace per arm, and a slot or a collection in
    one is not the other's."""

    def test_a_yocto_slot(self):
        self.assertEqual(images.slot_dir(WS + "-pr", "base", STORE),
                         "/store/ws/%s-pr/build/wk-slots/base" % WS)

    def test_a_buildroot_slot_keeps_the_profile_inside(self):
        self.assertEqual(images.slot_dir(BUILDROOT_WS + "-pr", "base", STORE),
                         "/store/ws/%s-pr/build/buildroot/%s/output/wk-slots/base"
                         % (BUILDROOT_WS, BUILDROOT_PROFILE))

    def test_no_image_workspace_has_no_slot(self):
        self.assertIsNone(images.slot_dir("jsc-release", "base", STORE))

    def test_a_collection(self):
        self.assertEqual(images.pgo_dir(WS + "-base", "pr", STORE), "/store/ws/%s-base/build/wk-pgo/pr" % WS)
        self.assertEqual(images.pgo_dir_in("pr"), "/src/WebKit/WebKitBuild/wk-pgo/pr")

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
    def test_a_spec_that_names_one_is_believed(self):
        self.assertEqual(images.ws_machine("moose", "elsewhere", "here"), "moose")

    def test_otherwise_the_workspaces_target(self):
        self.assertEqual(images.ws_machine("", "elsewhere", "here"), "elsewhere")

    def test_container_and_local_are_this_machine(self):
        for t in ("container", "local"):
            self.assertEqual(images.ws_machine("", t, "here"), "here")

    def test_the_scheduler_serialises_by_machine_alone(self):
        self.assertEqual(images.build_resource("moose"), "machine:moose")

    def test_a_spec_naming_another_machine_is_refused_with_the_setup(self):
        with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()):
            images.refuse_elsewhere(PROFILE + "@elsewhere", "here")
        images.refuse_elsewhere(PROFILE + "@here", "here")
        images.refuse_elsewhere(PROFILE, "here")


class TestTheDonePredicate(unittest.TestCase):
    def test_it_is_a_wk_command_routed_like_the_build(self):
        self.assertEqual(images.holds_predicate(PROFILE + "@moose", WS, ("--slot", "base")),
                         '[ "$(wk sysimage holds %s@moose --workspace %s --slot base)" = yes ]' % (PROFILE, WS))
        self.assertEqual(images.holds_predicate(PROFILE, WS, ()),
                         '[ "$(wk sysimage holds %s --workspace %s)" = yes ]' % (PROFILE, WS))


class TestNames(unittest.TestCase):
    def test_a_slot_name_is_a_directory_name_here_and_on_the_board(self):
        for good in ("base", "pr-2", "a.b_c"):
            images.check_slot_name(good)
        for bad in ("", "-x", ".x", "a/b", "a b"):
            with self.subTest(slot=bad):
                with self.assertRaises(act.Refused), contextlib.redirect_stderr(io.StringIO()):
                    images.check_slot_name(bad)

    def test_a_build_says_which_build_it_is(self):
        self.assertEqual(images.build_subject(WS, "webkit", "base", "a" * 40, "wpe-cross-pgo-collect"),
                         "slot base in %s at %s -- instrumented, to collect a profile from -- not a measurement"
                         % (WS, "a" * 12))
        self.assertEqual(images.build_subject(WS, "pgo-mix", "base", "", ""), "mixing slot base's collection in " + WS)
        self.assertEqual(images.build_subject(WS, "", "", "", ""), "build stage of " + WS)


class TestTheShims(WkTest):
    """Each bash function is one call of one verb, and every verb has a caller."""

    def shims(self):
        text = "\n".join(p.read_text() for p in SHIM_FILES)
        return dict(re.findall(r"(?m)^(image_\w+)\(\)\s*\{.*?_wk_images ([a-z-]+)", text))

    def test_every_shim_names_a_verb_and_every_verb_has_a_shim(self):
        verbs = set(self.shims().values())
        self.assertEqual(verbs - set(images.VERBS), set())
        self.assertEqual(set(images.VERBS) - verbs, set())

    def test_the_loader_shim_sets_the_fields_and_returns_or_exits_as_die_did(self):
        lib = '. "%s/image/profiles.sh"\n' % REPO
        cp = bash(lib + "image_profile_load %s && echo \"$IMG_BUILDER $IMG_WATCHDOG\"" % PROFILE)
        self.assertEqual(cp.stdout.strip(), "yocto 300", cp.stderr)
        cp = bash(lib + "image_profile_load nosuch || echo unknown")
        self.assertEqual(cp.stdout.strip(), "unknown", cp.stderr)
        cp = bash(lib + "image_profile_load rpi5-perf; echo reached")
        self.assertNotEqual(cp.returncode, 0)
        self.assertNotIn("reached", cp.stdout)
        self.assertIn("webkit-2.52-yocto-rpi5-64", cp.stderr)

    def test_the_machine_shim_asks_for_the_target_only_when_the_spec_names_none(self):
        libs = "".join('. "%s/%s"\n' % (REPO, f) for f in ("lib/common.sh", "lib/store.sh", "lib/target.sh", "lib/image.sh"))
        cp = bash(libs + "ws_target() { echo asked >&2; echo elsewhere; }\nwk_machine_name() { echo here; }\n"
                  "image_lane_machine %s moose; echo; image_lane_machine %s ''" % (WS, WS))
        self.assertEqual(cp.stdout.split(), ["moose", "elsewhere"], cp.stderr)
        self.assertEqual(cp.stderr.count("asked"), 1, cp.stderr)


if __name__ == "__main__":
    unittest.main()
