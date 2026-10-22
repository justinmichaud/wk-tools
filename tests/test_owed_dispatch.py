"""lib/wk/sysimage/write.py root_class: what medium an image's root spec names."""
import sys
import unittest

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk.sysimage import write  # noqa: E402


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
