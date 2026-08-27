"""Tests for lib/resources.sh's headless marker.

`headless_marker` (formerly `headless_markers`, plural) used to OR together
two independently-checked spellings of one fact -- `$WK_STORE/.headless` and
a fixed `/var/lib/wk/.headless` -- which could disagree on a workstation
whose store lives under XDG. It is now one formula,
`${WK_STORE:-/var/lib/wk}/.headless`, which a temp WK_STORE exercises
directly and which degrades to the old fixed path when WK_STORE is unset.
"""

import unittest
from pathlib import Path

from tests.support import REPO, bash, temp_store


class HeadlessMarkerTest(unittest.TestCase):
    def _run(self, script, env=None):
        cp = bash(
            f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
{script}
''',
            env=env,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        return cp

    def test_marker_absent_is_not_headless(self):
        with temp_store() as store:
            cp = self._run(
                'echo "marker=$(headless_marker)"\n'
                'is_headless && echo VERDICT=HEADLESS || echo VERDICT=NOT_HEADLESS\n',
                env={"WK_STORE": store["path"].as_posix()},
            )
        self.assertIn(f'marker={store["path"]}/.headless', cp.stdout)
        self.assertIn("VERDICT=NOT_HEADLESS", cp.stdout)

    def test_marker_present_is_headless(self):
        with temp_store() as store:
            (store["path"] / ".headless").touch()
            cp = self._run(
                'is_headless && echo VERDICT=HEADLESS || echo VERDICT=NOT_HEADLESS\n',
                env={"WK_STORE": store["path"].as_posix()},
            )
        self.assertIn("VERDICT=HEADLESS", cp.stdout)

    def test_marker_path_is_under_the_temp_store(self):
        """The marker really is $WK_STORE/.headless, not some other path
        that happened to also be empty in the previous test."""
        with temp_store() as store:
            marker = store["path"] / ".headless"
            self.assertFalse(marker.exists())
            marker.touch()
            cp = self._run(
                'is_headless && echo VERDICT=HEADLESS || echo VERDICT=NOT_HEADLESS\n',
                env={"WK_STORE": store["path"].as_posix()},
            )
        self.assertIn("VERDICT=HEADLESS", cp.stdout)

    def test_unset_WK_STORE_falls_back_to_var_lib_wk(self):
        cp = self._run('echo "marker=$(headless_marker)"\n')
        self.assertIn("marker=/var/lib/wk/.headless", cp.stdout)

    def test_exactly_one_path_is_consulted(self):
        # static: the function used to OR two independently-checked
        # spellings together (a loop over headless_markers()); it is now a
        # single formula and a single printf.
        text = Path(f"{REPO}/lib/resources.sh").read_text()
        start = text.index("headless_marker()")
        end = text.index("\n", start)
        body = text[start:end]
        self.assertEqual(body.count(".headless"), 1, body)
        self.assertEqual(body.count("printf"), 1, body)
        self.assertNotIn("headless_markers", text)


if __name__ == "__main__":
    unittest.main()
