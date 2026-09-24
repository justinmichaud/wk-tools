"""The headless marker: one formula, `${WK_STORE:-/var/lib/wk}/.headless`
(lib/wk/resources.py), which a temp WK_STORE exercises directly and which
degrades to the fixed path when WK_STORE is unset. host/linux/machine.sh reads
it through lib/resources.sh's headless_marker.
"""

import sys
import unittest

from tests.support import REPO, bash, temp_store

sys.path.insert(0, str(REPO / "lib"))
from wk import resources  # noqa: E402
from wk.machine import Fake  # noqa: E402


class HeadlessMarkerTest(unittest.TestCase):
    def _marker(self, env=None):
        cp = bash(f'set -euo pipefail\n. "{REPO}/lib/common.sh"\n. "{REPO}/lib/resources.sh"\nheadless_marker\n', env=env)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp.stdout.strip()

    def test_the_marker_is_under_the_store(self):
        with temp_store() as store:
            self.assertEqual(self._marker({"WK_STORE": store["path"].as_posix()}),
                             f'{store["path"]}/.headless')

    def test_unset_WK_STORE_falls_back_to_var_lib_wk(self):
        self.assertEqual(self._marker(), "/var/lib/wk/.headless")

    def test_the_marker_present_is_headless_and_absent_is_not(self):
        fake = Fake()
        r = resources.Resources(fake, {"WK_STORE": "/s", "HOME": "/h"}, "linux")
        self.assertFalse(r.is_headless())
        fake.files["/s/.headless"] = ""
        self.assertTrue(r.is_headless())


if __name__ == "__main__":
    unittest.main()
