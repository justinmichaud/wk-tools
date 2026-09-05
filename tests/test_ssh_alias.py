"""`ssh_alias_set` (lib/target.sh): the generated block in ~/.ssh/config.d/wk.

The file is `Include`d by the user's own ssh config, so a malformed block there
is not a wk problem: ssh refuses to read the file at all and every host in it
stops resolving, wk's and everybody else's. That happened -- a guest whose
address was not yet known was written as `HostName ` with no argument, and
`ssh anything` answered "no argument after keyword hostname; terminating".

Run: python3 -m unittest tests.test_ssh_alias -v
"""
import shutil
import subprocess
import unittest

from tests.support import REPO, WkTest, bash


class TestAnAliasWithNoAddress(WkTest):
    def _set(self, hostname, user="admin"):
        conf = self.tmp / "ssh" / "wk"
        cp = bash(f'''
. "$WK_ROOT/lib/common.sh"
. "$WK_ROOT/lib/store.sh"
. "$WK_ROOT/lib/target.sh"
wk_ssh_conf() {{ printf %s {str(conf)!r}; }}
ssh_alias_set demo {hostname!r} {user!r} /dev/null
echo "rc=$?"
''')
        return cp, conf

    def test_no_address_is_refused_rather_than_written(self):
        cp, conf = self._set("")
        self.assertNotIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        self.assertIn("no address for 'demo'", cp.stdout + cp.stderr)
        self.assertFalse(conf.exists() and "HostName" in conf.read_text(),
                         "a block with no address reached the file anyway")

    def test_no_user_is_refused_too(self):
        cp, _ = self._set("10.0.0.1", user="")
        self.assertNotIn("rc=0", cp.stdout, cp.stdout + cp.stderr)

    def test_the_refusal_says_what_it_would_have_broken(self):
        """Not "could not write an alias": the cost is every other host in that
        file, which is what makes this worth refusing rather than warning."""
        cp, _ = self._set("")
        self.assertIn("every other host", cp.stdout + cp.stderr)

    @unittest.skipUnless(shutil.which("ssh"), "no ssh here")
    def test_a_written_alias_is_a_file_ssh_can_read(self):
        """The property that matters, checked with ssh itself rather than by
        looking at the text: `ssh -F <file> -G <host>` parses and resolves."""
        cp, conf = self._set("10.0.0.1")
        self.assertIn("rc=0", cp.stdout, cp.stdout + cp.stderr)
        got = subprocess.run(["ssh", "-F", str(conf), "-G", "wk-demo"],
                             capture_output=True, text=True)
        self.assertEqual(0, got.returncode, got.stderr)
        self.assertIn("hostname 10.0.0.1", got.stdout)

    @unittest.skipUnless(shutil.which("ssh"), "no ssh here")
    def test_ssh_really_does_refuse_a_whole_file_for_one_empty_hostname(self):
        """The premise of the refusal above, measured rather than asserted: if
        ssh ever stops doing this, the refusal can go."""
        conf = self.tmp / "bad"
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text("Host other\n    HostName 10.0.0.2\n\nHost broken\n    HostName \n")
        got = subprocess.run(["ssh", "-F", str(conf), "-G", "other"],
                             capture_output=True, text=True)
        self.assertNotEqual(0, got.returncode,
                            "ssh now tolerates an empty HostName; the refusal is obsolete")


if __name__ == "__main__":
    unittest.main()
