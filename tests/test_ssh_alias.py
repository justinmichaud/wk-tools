"""The generated block in ~/.ssh/config.d/wk (lib/wk/sshalias.py), read by ssh itself.

The file is `Include`d by the user's own ssh config, so a malformed block there
is not a wk problem: ssh refuses to read the file at all and every host in it
stops resolving, wk's and everybody else's.

Run: python3 -m unittest tests.test_ssh_alias -v
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr

from tests.support import REPO

sys.path.insert(0, str(REPO / "lib"))
from wk import sshalias  # noqa: E402
from wk.act import Refused  # noqa: E402
from wk.machine import Local  # noqa: E402


class TestAnAliasWithNoAddress(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="wk-test-ssh-alias-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {"HOME": self.home}
        self.conf = sshalias.alias_path(self.env)

    def set(self, hostname, user="admin"):
        err = io.StringIO()
        with redirect_stderr(err):
            try:
                sshalias.alias_set(Local(), self.env, "demo", hostname, user, "/dev/null")
                return True, err.getvalue()
            except Refused:
                return False, err.getvalue()

    def test_no_address_or_user_is_refused_rather_than_written_and_says_what_it_would_break(self):
        for hostname, user in (("", "admin"), ("10.0.0.1", "")):
            ok, err = self.set(hostname, user)
            self.assertFalse(ok)
            self.assertIn("no address for 'demo'", err)
            self.assertIn("every other host", err)
        self.assertFalse(os.path.exists(self.conf) and "HostName" in open(self.conf).read())

    @unittest.skipUnless(shutil.which("ssh"), "no ssh here")
    def test_a_written_alias_is_a_file_ssh_can_read(self):
        self.assertTrue(self.set("10.0.0.1")[0])
        got = subprocess.run(["ssh", "-F", self.conf, "-G", "wk-demo"], capture_output=True, text=True)
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn("hostname 10.0.0.1", got.stdout)

    @unittest.skipUnless(shutil.which("ssh"), "no ssh here")
    def test_ssh_really_does_refuse_a_whole_file_for_one_empty_hostname(self):
        """The premise of the refusal, measured: if ssh ever stops doing this, the refusal can go."""
        bad = os.path.join(self.home, "bad")
        with open(bad, "w") as f:
            f.write("Host other\n    HostName 10.0.0.2\n\nHost broken\n    HostName \n")
        got = subprocess.run(["ssh", "-F", bad, "-G", "other"], capture_output=True, text=True)
        self.assertNotEqual(0, got.returncode, "ssh now tolerates an empty HostName; the refusal is obsolete")


if __name__ == "__main__":
    unittest.main()
