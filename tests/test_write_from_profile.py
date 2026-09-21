"""`wk sysimage write` takes a configuration name, and clears an automounted
card itself.

Both halves exist because the command was long enough to be pasted by hand:
its `--from` wanted a path copied out of `wk sysimage ls`, and on a machine
whose desktop session automounts every card it refused, naming an unmount the
write's own next step performs -- so the unmount was typed as a bare
privileged ssh call instead (rpi5, 2026-09-04).

A configuration names exactly one built image and image_workspace_scan
(lib/image.sh) is what finds it, so the path is recomputed here rather than
recorded.

Run: python3 -m unittest tests.test_write_from_profile -v
"""
import subprocess
import unittest

from tests.support import REPO, bash

SYSIMAGE = REPO / "cmd" / "sysimage"


# image_lane_profile (lib/image.sh) recovers a profile by matching the
# configurations this checkout defines, so the libraries that enumerate them and
# that own the lane vocabulary are sourced beside the lifted functions rather
# than stubbed: the names below are real configurations.
PRELUDE = ('. "$WK_ROOT/lib/common.sh"\n. "$WK_ROOT/lib/store.sh"\n'
           '. "$WK_ROOT/lib/target.sh"\n. "$WK_ROOT/lib/image.sh"\n')


def lift(*funcs):
    out = [PRELUDE]
    for f in funcs:
        body = subprocess.run(
            ["sed", "-n", f"/^{f}()/,/^}}/p", str(SYSIMAGE)],
            capture_output=True, text=True).stdout
        assert body.strip(), f"could not lift {f} from cmd/sysimage"
        out.append(body)
    return "\n".join(out)


# One yocto row with an image, one buildroot row with an image, and a yocto
# workspace that has none -- the three shapes image_workspace_scan emits.
STUB_SCAN = r'''
image_workspace_scan() {
    printf 'yocto\t%s\t%s\t%s\t%s\n' \
        yocto-wpewebkit-2.46-yocto-rpi5-64 /ws/a/build/image/x.wic.xz 100 now
    printf 'buildroot\t%s\t%s\t%s\t%s\n' \
        buildroot-wpewebkit-2.38-buildroot-rpi3-32 /ws/b/output/images/sdcard.img 100 now
    printf 'yocto\t%s\t-\t0\t-\n' yocto-webkit-2.52-yocto-rpi3-32
}
'''


class TestAConfigurationNamesItsImage(unittest.TestCase):
    def _run(self, call):
        return bash(lift("_image_path", "_built_profiles")
                    + STUB_SCAN + "\n" + call)

    def test_a_yocto_configuration_resolves_to_its_bytes(self):
        cp = self._run("_image_path profile wpewebkit-2.46-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("/ws/a/build/image/x.wic.xz", cp.stdout.strip())

    def test_a_buildroot_configuration_resolves_too(self):
        """One resolver for both builders, as the scan has one shape."""
        cp = self._run("_image_path profile wpewebkit-2.38-buildroot-rpi3-32")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("/ws/b/output/images/sdcard.img", cp.stdout.strip())

    def test_a_workspace_with_no_image_does_not_resolve(self):
        """`-` is the scan's word for "built nothing yet"; it is not a path."""
        cp = self._run("_image_path profile webkit-2.52-yocto-rpi3-32")
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual("", cp.stdout.strip())

    def test_an_unknown_configuration_does_not_resolve(self):
        cp = self._run("_image_path profile nonsense")
        self.assertNotEqual(cp.returncode, 0)

    def test_the_refusal_lists_what_has_been_built(self):
        cp = self._run("_built_profiles")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        names = cp.stdout.split()
        self.assertIn("wpewebkit-2.46-yocto-rpi5-64", names)
        self.assertIn("wpewebkit-2.38-buildroot-rpi3-32", names)
        self.assertNotIn("webkit-2.52-yocto-rpi3-32", names,
                         "a configuration with no image is offered as writable")


class TestAPathIsStillAPath(unittest.TestCase):
    """A path must never be looked up as a configuration: `_from_resolve`
    passes it through untouched, vm: prefix included."""

    def _run(self, spec):
        return bash(lift("_image_path", "_from_resolve")
                    + STUB_SCAN
                    + '\ninfo() { :; }\ndie() { echo "$*" >&2; exit 1; }\n'
                    + f"_from_resolve {spec}")

    def test_absolute_path(self):
        cp = self._run("/tmp/some.wic.xz")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual("/tmp/some.wic.xz", cp.stdout)

    def test_vm_path(self):
        cp = self._run("vm:/var/lib/x.img")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual("vm:/var/lib/x.img", cp.stdout)

    def test_relative_path(self):
        cp = self._run("./out.img")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual("./out.img", cp.stdout)

    def test_a_configuration_becomes_its_path(self):
        cp = self._run("wpewebkit-2.46-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stderr)
        self.assertEqual("/ws/a/build/image/x.wic.xz", cp.stdout)


class TestAnAutomountedCardIsCleared(unittest.TestCase):
    """The card helper's gate refuses a mounted medium, and that refusal is
    right; what was wrong is asking it before the write's own unmount."""

    def setUp(self):
        body = SYSIMAGE.read_text()
        self.fn = body[body.index("cmd_write_from()"):]
        self.fn = self.fn[:self.fn.index("\n# ")]

    def test_the_unmount_comes_before_the_safety_check(self):
        self.assertLess(self.fn.index("disk_unmount"),
                        self.fn.index("disk_refuse_unless_safe"),
                        "the write still asks about a card it has not unmounted")

    def test_a_dry_run_unmounts_nothing(self):
        """A preview changes nothing, so the early unmount is skipped there."""
        early = self.fn[:self.fn.index("disk_refuse_unless_safe")]
        self.assertIn('[ -n "$dry" ] || disk_unmount', early,
                      "a dry run would unmount the card")

    def test_a_dry_run_still_reports_the_step(self):
        """It is a step the real write takes, so the preview must name it."""
        self.assertIn('[ -z "$dry" ] || disk_unmount', self.fn)

    def test_the_unmount_is_not_done_twice(self):
        self.assertEqual(2, self.fn.count("disk_unmount "),
                         "the write unmounts more than once per run")

    def test_the_gate_still_refuses_a_mounted_card_to_anyone_else(self):
        """The rule lives where the privilege is; only the caller's order
        changed."""
        helper = (REPO / "admin" / "wk-card-priv").read_text()
        self.assertIn("mounted filesystem(s) on it", helper)


if __name__ == "__main__":
    unittest.main()


class TestAConfigurationTheLaneHoldsAndThisMachineCannotRead(unittest.TestCase):
    """`write` runs on the host holding the card reader, and on a macOS
    workstation the lane is in the podman VM, whose store this side cannot
    read: `--from <configuration>` refused with "no workspace here has built
    it yet" while `wk sysimage ls` was printing that very image, and the only
    spelling that worked was the `--from vm:<path> --profile <name>` pair
    (measured 2026-09-17). So the lane is asked for the path in its own
    spelling -- `wk sysimage path`, routed like `holds` -- and `vm:` says
    whose filesystem it is on."""

    STUB_WK = '#!/bin/sh\nprintf "%s" "$WK_TEST_ANSWER"\nexit $WK_TEST_RC\n'

    def _run(self, spec, answer, local="", wk_rc=0):
        """The routed question stubbed: what the machine holding the lane
        answers is not what _image_path can see from here. WK_ROOT points at
        a tree of symlinks to this one whose `wk` is the stub, since that is
        how _from_resolve asks."""
        import os
        import tempfile
        script = (lift("_image_path", "_built_profiles", "_from_resolve")
                  + '. "$WK_ROOT/image/profiles.sh"\n'
                  + 'info() { :; }\ndie() { echo "$*" >&2; exit 1; }\n'
                  + 'image_workspace_scan() { :; }\n'
                  + 'store_is_local() { [ -n "$WK_TEST_LOCAL" ]; }\n'
                  + "_from_resolve " + spec)
        with tempfile.TemporaryDirectory() as d:
            for entry in os.listdir(REPO):
                if entry != "wk":
                    os.symlink(REPO / entry, os.path.join(d, entry))
            wk = os.path.join(d, "wk")
            with open(wk, "w") as f:
                f.write(self.STUB_WK)
            os.chmod(wk, 0o755)
            return bash(script, env={
                "WK_ROOT": d, "WK_TEST_ANSWER": answer,
                "WK_TEST_RC": str(wk_rc), "WK_TEST_LOCAL": local,
            })

    def test_the_lanes_answer_is_read_as_the_vms_own_path(self):
        cp = self._run("webkit-2.52-yocto-rpi5-64",
                       "/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/i.wic.xz")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(
            "vm:/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/build/i.wic.xz",
            cp.stdout)

    def test_a_store_this_machine_can_read_needs_no_prefix(self):
        cp = self._run("webkit-2.52-yocto-rpi5-64", "/var/lib/wk/ws/x/i.wic.xz",
                       local="yes")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("/var/lib/wk/ws/x/i.wic.xz", cp.stdout)

    def test_a_lane_that_holds_no_image_is_refused_naming_the_build(self):
        cp = self._run("webkit-2.52-yocto-rpi5-64", "", wk_rc=1)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk sysimage build webkit-2.52-yocto-rpi5-64", cp.stderr)
