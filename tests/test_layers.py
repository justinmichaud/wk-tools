"""A layer is reached through its own Python API: no command runs `wk status` as a subprocess to read"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.support import REPO, load_cmd

sys.path.insert(0, str(REPO / "lib"))
from wk import priv  # noqa: E402


def no_subprocess(*a, **kw):
    raise AssertionError("a subprocess ran: %r" % (a[0],))


class TestZedStripsTheDispatchersVariables(unittest.TestCase):
    def test_without_reading_common_sh(self):
        zed = load_cmd("zed")
        seen = {}
        with tempfile.TemporaryDirectory() as empty, mock.patch.object(zed, "ROOT", empty), \
                mock.patch.object(zed.Local, "exec", lambda self, argv, env: seen.update(env=env)), \
                mock.patch.dict(os.environ, {"WK_NAME": "a", "WK_PRESET": "Debug", "KEEP": "1"}):
            zed.exec_clean(["zed", "url"])
        self.assertNotIn("WK_NAME", seen["env"])
        self.assertNotIn("WK_PRESET", seen["env"])
        self.assertEqual("1", seen["env"]["KEEP"])


class TestPrivilegedHelpersAreAskedOfPython(unittest.TestCase):
    def test_bash_answers_what_python_does(self):
        rows = subprocess.run(["bash", "-c", '. "%s/lib/common.sh"; wk_priv_helpers; wk_priv_path wk-boot-priv; wk_priv_sudoers wk-boot-priv' % REPO],
                              capture_output=True, text=True).stdout
        want = "".join("%s %s %s\n" % (n, w, what) for n, w, what in priv.HELPERS) + priv.path("wk-boot-priv") + "\n" + priv.sudoers("wk-boot-priv") + "\n"
        self.assertEqual(want, rows)

    def test_doctor_runs_no_bash_for_them(self):
        with mock.patch.object(subprocess, "run", no_subprocess):
            self.assertEqual(3, len(priv.helpers()))


if __name__ == "__main__":
    unittest.main()
