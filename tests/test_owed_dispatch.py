"""Driver dispatch and the small pure classifiers a place driver is"""
import sys
import unittest

from tests.support import REPO, scratch_dir

sys.path.insert(0, str(REPO / "lib"))
from wk import places  # noqa: E402
from wk.machine import Fake  # noqa: E402
from wk.sysimage import write  # noqa: E402


class TestPlaceKind(unittest.TestCase):
    def _kind(self, name, registry):
        return places.Registry(REPO, env={"WK_MACHINES_DIR": str(registry), "HOME": "/nonexistent"}, machine=Fake()).kind(name)

    def test_a_built_in_names_itself_a_conf_is_believed_and_nothing_is_guessed(self):
        cases = [(k, None, k) for k in ("container", "vm", "remote", "local")]
        cases += [("nosuchtarget", None, None), ("buildbox1", 'kind=build\ndriver="remote"\n', "remote"),
                  ("plainbox", 'kind=build\nhost="plainbox"\n', "remote")]
        for name, conf, want in cases:
            with self.subTest(name=name), scratch_dir() as reg:
                if conf:
                    (reg / (name + ".conf")).write_text(conf)
                self.assertEqual(self._kind(name, reg), want)


class TestRemoteIsLocal(unittest.TestCase):
    def test_local_exactly_when_the_remote_marker_says_this_is_the_machine(self):
        for remote_local in (True, False):
            with self.subTest(remote_local=remote_local), scratch_dir() as state:
                env = {"HOME": "/nonexistent", "XDG_STATE_HOME": str(state), "WK_REMOTE_MARKER": "/nonexistent/.wk-remote",
                       "WK_REMOTE_HOST": "buildbox1", **({"WK_REMOTE_LOCAL": "1"} if remote_local else {})}
                self.assertEqual(places.Remote("buildbox1", str(REPO), env, Fake()).is_local, remote_local)


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
