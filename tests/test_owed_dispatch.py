"""Target-kind dispatch and the small pure classifiers a target driver is
picked from: `target_kind` (lib/target.sh: container|vm|remote|local are
built in, anything else needs a conf and falls through to `remote` when the
conf names no kind), `arch_is_native` (lib/arch.sh), `_remote_is_local`
(targets/remote.sh: is this process running on the remote machine itself),
and `image_root_class` (lib/image.sh: what kind of device a kernel cmdline's
`root=` names). Each is driven directly, sourced with no target loaded and no
network -- these are the pure decisions the rest of the driver machinery
calls through.

Owed by docs/HANDOFF-test-runner.md: "the target-kind dispatch
(container|vm|remote|local), arch_is_native, _remote_is_local,
image_root_class".

Run: python3 -m unittest tests.test_owed_dispatch -v
"""
import unittest

from tests.support import REPO, WkTest, bash, scratch_dir


class TestTargetKind(WkTest):
    def _kind(self, name, registry=None):
        env = {"WK_TARGET_REGISTRY": str(registry)} if registry else None
        return bash(
            f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
target_kind {name}
''',
            env=env,
        )

    def test_the_four_built_in_kinds_name_themselves(self):
        for kind in ("container", "vm", "remote", "local"):
            with self.subTest(kind=kind):
                cp = self._kind(kind)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(cp.stdout.strip(), kind)

    def test_an_unregistered_name_fails_rather_than_guessing(self):
        with scratch_dir() as reg:
            cp = self._kind("nosuchtarget", registry=reg)
        self.assertNotEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "")

    def test_a_conf_that_names_a_kind_is_believed(self):
        with scratch_dir() as reg:
            (reg / "buildbox1.conf").write_text('WK_TARGET_KIND="remote"\n')
            cp = self._kind("buildbox1", registry=reg)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "remote")

    def test_a_conf_that_names_no_kind_falls_through_to_remote(self):
        with scratch_dir() as reg:
            (reg / "plainbox.conf").write_text('WK_REMOTE_HOST="plainbox"\n')
            cp = self._kind("plainbox", registry=reg)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "remote")


class TestArchIsNative(WkTest):
    def _native(self, arg):
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/arch.sh"
arch_is_native {arg} && echo YES || echo NO
''')

    def test_no_argument_defaults_to_native(self):
        cp = self._native("")
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_native_is_native(self):
        cp = self._native("native")
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_armhf_is_not_native(self):
        cp = self._native("armhf")
        self.assertEqual(cp.stdout.strip(), "NO", cp.stdout + cp.stderr)


class TestRemoteIsLocal(WkTest):
    def _is_local(self, remote_local):
        env = {"WK_TARGET": "buildbox1"}
        if remote_local:
            env["WK_REMOTE_LOCAL"] = "1"
        return bash(
            '''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/target.sh"
. "$WK_ROOT/targets/remote.sh"
_remote_is_local && echo YES || echo NO
''',
            env=env,
        )

    def test_true_once_the_remote_marker_says_this_is_the_machine(self):
        cp = self._is_local(True)
        self.assertEqual(cp.stdout.strip(), "YES", cp.stdout + cp.stderr)

    def test_false_for_a_plain_ssh_driven_target(self):
        cp = self._is_local(False)
        self.assertEqual(cp.stdout.strip(), "NO", cp.stdout + cp.stderr)


class TestImageRootClass(WkTest):
    def _class(self, spec):
        return bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/image.sh"
image_root_class {spec!r}
''')

    def test_every_class_this_function_can_return(self):
        cases = {
            "": "unknown",
            "LABEL=rootfs": "portable",
            "UUID=1234-5678": "portable",
            "PARTUUID=deadbeef-02": "portable",
            "/dev/nfs": "network",
            "nfsroot=1.2.3.4:/x": "network",
            "/dev/mmcblk0p2": "mmc",
            "/dev/sda2": "usb",
            "/dev/nvme0n1p2": "nvme",
            "something-else": "unknown",
        }
        for spec, want in cases.items():
            with self.subTest(spec=spec):
                cp = self._class(spec)
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(cp.stdout.strip(), want, f"{spec!r}: {cp.stdout + cp.stderr}")


if __name__ == "__main__":
    unittest.main()
