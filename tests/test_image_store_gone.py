"""The image store is gone (docs/HANDOFF-fleet.md, "The image store is being
removed"): a build's output lives wherever its builder left it -- a
workspace, a build host -- and every reader gets there by scanning or by
`--from <path>`, never by looking an id up in a catalogue.

Covers: none of the store's functions survive anywhere in the tree except as
a caller wk-tools does not own (cmd/pi, container/broker/wk-broker.py -- both
outside this change's edit scope, and both named in docs/HANDOFF-fleet.md as
owed); `wk sysimage rm` is a tombstone naming `wk rm`; `wk boot`'s default
system is read off the device rather than looked up, and a named --system is
checked against it; `_ws_profile`/`_profile_from_path` (cmd/sysimage) derive
a profile from both a yocto and a buildroot workspace path.

Run: python3 -m unittest tests.test_image_store_gone -v
"""
import re
import subprocess
import unittest

from tests.support import REPO, WkTest, bash, rand_suffix, run, scratch_dir, shell_files, stub_path

# The functions the image store used to be built from (lib/image.sh) --
# every one of them either has no reason to exist without a catalogue to
# read, or (image_dir/image_disk/image_manifest/...) named a path inside one.
_RETIRED_FUNCTIONS = (
    "image_dir",
    "image_manifest",
    "image_complete",
    "image_ids",
    "image_rubble",
    "image_latest",
    "manifest_get",
    "image_verify",
    "image_store_dir",
)


class TestNoStoreFunctionDefinitionRemains(unittest.TestCase):
    """A function *definition* -- 'name() {' or 'name () {' -- not a mention:
    the word can still appear in a refusal or a comment (a tombstone), and
    cmd/pi and container/broker/wk-broker.py still *call* manifest_get and
    image_ids respectively (owed work outside this change's file list,
    docs/HANDOFF-fleet.md), which this test does not police."""

    def test_no_definition_of_any_retired_function(self):
        pattern = re.compile(
            r"^\s*(?:" + "|".join(_RETIRED_FUNCTIONS) + r")\s*\(\)\s*\{",
            re.MULTILINE,
        )
        bad = []
        for f in shell_files():
            try:
                text = f.read_text(errors="replace")
            except OSError:
                continue
            for m in pattern.finditer(text):
                bad.append(f"{f.relative_to(REPO)}: {m.group(0).strip()}")
        self.assertEqual(bad, [], "retired function(s) still defined:\n" + "\n".join(bad))


class TestSysimageRmIsATombstone(WkTest):
    def test_rm_names_wk_rm_instead(self):
        cp = run("sysimage", "rm", "some-workspace", env={"WK_ROOT": str(REPO)})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("wk rm", cp.stdout, cp.stdout)
        self.assertIn("does not exist", cp.stdout, cp.stdout)


# A stub ssh that never executes the remote script it is handed -- for
# 'wk boot's b_device_image (boot/machines.sh), which mounts the boot
# partition for real on a machine that has one; here there is no such device,
# so any attempt to actually run that script (mount, findmnt) would just fail
# differently on whatever this test happens to run on. Instead: recognise the
# one script by a string only it contains, and hand back a canned id: this is
# the same "answer the identifiable call, succeed silently otherwise" shape
# tests/test_disk_logic.py uses for a stubbed sfdisk.
_SSH_STUB = '''#!/bin/sh
case "$*" in
  *wk-image.id*) echo "{fake_id}" ;;
  *) : ;;
esac
'''

_FAKE_MACH_CONF = '''MACH_SSH={ssh}
MACH_DRIVER=rpi5-usb
MACH_DEVICE=/dev/sda
MACH_ROOT=/dev/nvme0n1p2
MACH_PROFILE=webkit-2.52-yocto-rpi5-64
MACH_MAC=02:00:00:00:00:01
MACH_BRIDGE=""
MACH_ROLE=workstation
MACH_OS=any
MACH_LOCAL=""
MACH_VOLUME=""
MACH_DTB=bcm2712-rpi-5-b.dtb
MACH_BENCH_SSH=""
MACH_NET=wifi
MACH_NOTE="fake bench board"
'''


class TestBootArmDefaultsToDeviceImage(WkTest):
    """cmd_arm's default system is what the device already holds
    (b_device_image), not a store lookup; a named --system is checked
    against that same evidence rather than trusted."""

    def _env(self, machdir, binp):
        return {
            "WK_MACHINES_DIR": str(machdir),
            "PATH": f"{binp}:/usr/bin:/bin",
        }

    def test_dry_run_arms_the_id_the_device_reports(self):
        fake_id = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = f"fakerpi5{rand_suffix(3)}"
            (machdir / f"{name}.conf").write_text(_FAKE_MACH_CONF.format(ssh=name))
            cp = run("boot", name, "--dry-run", env=self._env(machdir, binp), timeout=30)
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(fake_id, cp.stdout, cp.stdout)
            self.assertNotIn("sysimage build", cp.stdout, cp.stdout)

    def test_a_named_system_that_differs_is_refused_and_names_the_write(self):
        fake_id = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        wanted = f"webkit-2.52-yocto-rpi5-64-{rand_suffix(12)}"
        with scratch_dir(prefix="wk-test-machines-") as machdir, \
             stub_path({"ssh": _SSH_STUB.format(fake_id=fake_id)}) as binp:
            name = f"fakerpi5{rand_suffix(3)}"
            (machdir / f"{name}.conf").write_text(_FAKE_MACH_CONF.format(ssh=name))
            cp = run("boot", name, "--system", wanted, "--dry-run",
                      env=self._env(machdir, binp), timeout=30)
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn(fake_id, cp.stdout, cp.stdout)
            self.assertIn(wanted, cp.stdout, cp.stdout)
            self.assertIn("wk sysimage write --from", cp.stdout, cp.stdout)


class TestProfileFromWorkspacePath(unittest.TestCase):
    """_ws_profile (cmd/sysimage) derives a profile from a workspace name for
    both builders that leave images inside one (image_workspace_scan,
    lib/image.sh); _profile_from_path is the same derivation from a full
    path and calls through it -- lifted together, sed's the idiom
    tests/test_quick.py uses for 'bump' (cmd/status)."""

    def _lift(self):
        body = subprocess.run(
            ["sed", "-n", "/^_ws_profile()/,/^}/p", str(REPO / "cmd" / "sysimage")],
            capture_output=True, text=True,
        ).stdout
        self.assertTrue(body.strip(), "could not lift _ws_profile from cmd/sysimage")
        body2 = subprocess.run(
            ["sed", "-n", "/^_profile_from_path()/,/^}/p", str(REPO / "cmd" / "sysimage")],
            capture_output=True, text=True,
        ).stdout
        self.assertTrue(body2.strip(), "could not lift _profile_from_path from cmd/sysimage")
        return body + "\n" + body2

    def setUp(self):
        self.prelude = self._lift()

    def _run(self, *args):
        script = self.prelude + "\n" + " ".join(args)
        return bash(script)

    def test_yocto_workspace_name(self):
        cp = self._run("_ws_profile", "yocto-webkit-2.52-yocto-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-yocto-rpi5-64")

    def test_buildroot_workspace_name(self):
        cp = self._run("_ws_profile", "buildroot-webkit-2.52-buildroot-rpi5-64")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-buildroot-rpi5-64")

    def test_unrelated_workspace_name_is_not_a_profile(self):
        cp = self._run("_ws_profile", "jsc-release")
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_profile_from_a_full_yocto_path(self):
        cp = self._run(
            "_profile_from_path",
            "/var/lib/wk/ws/yocto-webkit-2.52-yocto-rpi5-64/changes/WebKitBuild/"
            "CrossToolChains/rpi5/build/image/webkit-2.52-yocto-rpi5-64.wic.xz",
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-yocto-rpi5-64")

    def test_profile_from_a_full_buildroot_path(self):
        cp = self._run(
            "_profile_from_path",
            "/var/lib/wk/ws/buildroot-webkit-2.52-buildroot-rpi5-64/changes/WebKitBuild/"
            "buildroot/rpi5/output/images/sdcard.img",
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "webkit-2.52-buildroot-rpi5-64")


if __name__ == "__main__":
    unittest.main()
