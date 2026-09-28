"""Target-kind dispatch and the small pure classifiers a target driver is
picked from: `Registry.kind` (lib/wk/targets.py: container|vm|remote|local
are built in, anything else needs a conf and falls through to `remote` when
the conf names no kind), `Remote.is_local` (is this process running on the
remote machine itself), and `root_class` (lib/wk/sysimage/write.py: what kind
of device a kernel cmdline's `root=` names). Each is driven directly, with no
target loaded and no network -- these are the pure decisions the rest of the
driver machinery calls through.

Run: python3 -m unittest tests.test_owed_dispatch -v
"""
import sys
import unittest

from tests.support import REPO, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import targets  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.sysimage import write  # noqa: E402


class TestTargetKind(unittest.TestCase):
    def _kind(self, name, registry):
        return targets.Registry(REPO, env={"WK_MACHINES_DIR": str(registry), "HOME": "/nonexistent"}, machine=Fake()).kind(name)

    def test_the_four_built_in_kinds_name_themselves(self):
        with scratch_dir() as reg:
            for kind in ("container", "vm", "remote", "local"):
                with self.subTest(kind=kind):
                    self.assertEqual(self._kind(kind, reg), kind)

    def test_an_unregistered_name_fails_rather_than_guessing(self):
        with scratch_dir() as reg:
            self.assertIsNone(self._kind("nosuchtarget", reg))

    def test_a_conf_that_names_a_kind_is_believed(self):
        with scratch_dir() as reg:
            (reg / "buildbox1.conf").write_text('kind=build\ndriver="remote"\n')
            self.assertEqual(self._kind("buildbox1", reg), "remote")

    def test_a_conf_that_names_no_kind_falls_through_to_remote(self):
        with scratch_dir() as reg:
            (reg / "plainbox.conf").write_text('kind=build\nhost="plainbox"\n')
            self.assertEqual(self._kind("plainbox", reg), "remote")


class TestRemoteIsLocal(unittest.TestCase):
    def _is_local(self, remote_local):
        with scratch_dir() as state:
            env = {"HOME": "/nonexistent", "XDG_STATE_HOME": str(state), "WK_REMOTE_MARKER": "/nonexistent/.wk-remote",
                   "WK_REMOTE_HOST": "buildbox1"}
            if remote_local:
                env["WK_REMOTE_LOCAL"] = "1"
            return targets.Remote("buildbox1", str(REPO), env, Fake()).is_local

    def test_true_once_the_remote_marker_says_this_is_the_machine(self):
        self.assertTrue(self._is_local(True))

    def test_false_for_a_plain_ssh_driven_target(self):
        self.assertFalse(self._is_local(False))


class TestImageRootClass(unittest.TestCase):
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
                self.assertEqual(write.root_class(spec), want)


if __name__ == "__main__":
    unittest.main()
