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


def lift(*funcs):
    out = []
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
        return bash(lift("_ws_profile", "_profile_image_path", "_built_profiles")
                    + STUB_SCAN + "\n" + call)

    def test_a_yocto_configuration_resolves_to_its_bytes(self):
        cp = self._run("_profile_image_path wpewebkit-2.46-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("/ws/a/build/image/x.wic.xz", cp.stdout.strip())

    def test_a_buildroot_configuration_resolves_too(self):
        """One resolver for both builders, as the scan has one shape."""
        cp = self._run("_profile_image_path wpewebkit-2.38-buildroot-rpi3-32")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual("/ws/b/output/images/sdcard.img", cp.stdout.strip())

    def test_a_workspace_with_no_image_does_not_resolve(self):
        """`-` is the scan's word for "built nothing yet"; it is not a path."""
        cp = self._run("_profile_image_path webkit-2.52-yocto-rpi3-32")
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual("", cp.stdout.strip())

    def test_an_unknown_configuration_does_not_resolve(self):
        cp = self._run("_profile_image_path nonsense")
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
        return bash(lift("_ws_profile", "_profile_image_path", "_from_resolve")
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
