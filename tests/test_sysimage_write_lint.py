"""`lint.one_wifi_reader`: a WiFi credential is read in one place, the card helper, as root on the machine holding
the reader (admin/wk-card-priv's `_host_wifi`); every other seed takes its credential from there.

Run: python3 tests/run.py --lint -k test_sysimage_write_lint
"""
import re
import unittest

from tests.support import REPO, owed

TIER = "lint"
ROOTS = ("admin", "bench", "boot", "bridge", "build", "cmd", "container", "host", "image", "lib", "remote", "targets", "vm")
READER = re.compile(r"etc/netplan/\*\.yaml|find-generic-password|wireless-security\.psk|--show-secrets")
THE_READER = "admin/wk-card-priv"


def readers():
    found = []
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            try:
                text = path.read_text()
            except (UnicodeDecodeError, OSError):
                continue
            if READER.search(text):
                found.append(str(path.relative_to(REPO)))
    return found


class TestOneWifiReader(unittest.TestCase):
    def test_the_card_helper_reads_the_credential(self):
        self.assertIn(THE_READER, readers())

    @owed("lib/wk/sysimage/pmos_build.py reads the build host's netplan for a phone's uplink (its own one reader, "
          "shared by the band check and the image seeding, but still a second one against this rule), and "
          "lib/wk/sysimage/macvolume.py reads the System keychain for a bench volume's; each is a second reader")
    def test_nothing_else_does(self):
        self.assertEqual([p for p in readers() if p != THE_READER], [])


if __name__ == "__main__":
    unittest.main()
