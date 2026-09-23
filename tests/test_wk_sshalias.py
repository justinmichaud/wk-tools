"""lib/wk/sshalias.py writes the block lib/target.sh's ssh_alias_set writes,
byte for byte, takes it out the way ssh_alias_remove does, and refuses an
empty HostName or User with the same words, since ssh would refuse the whole
file and every host in it.

Run: python3 tests/run.py -k tests.test_wk_sshalias
"""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

from tests.support import REPO, bash

sys.path.insert(0, str(REPO / "lib"))
from wk import machine, sshalias  # noqa: E402
from wk.act import Refused  # noqa: E402

OTHERS = "Host other\n    HostName 1.2.3.4\n\nHost wk-demo\n    HostName old\n    User old\n\nHost tail\n    User t\n"
CASES = (
    ("plain", "ssh_alias_set demo 10.0.0.1 admin", ("demo", "10.0.0.1", "admin"), {}),
    ("identity_and_extra", "ssh_alias_set demo 10.0.0.2 root /k/id 'ProxyJump gw' 'Port 2222'",
     ("demo", "10.0.0.2", "root"), {"identity": "/k/id", "extra": ("ProxyJump gw", "Port 2222")}),
    ("extra_alone", "ssh_alias_set demo 10.0.0.4 u '' 'Port 22'", ("demo", "10.0.0.4", "u"), {"extra": ("Port 22",)}),
    ("replacing", "ssh_alias_set demo 10.0.0.3 u", ("demo", "10.0.0.3", "u"), {}),
    ("removing", "ssh_alias_remove demo", None, {}),
)
SOURCE = '. "$WK_ROOT/lib/common.sh"; . "$WK_ROOT/lib/store.sh"; . "$WK_ROOT/lib/target.sh"\n'


class TestAgainstBash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="wk-test-sshalias-")
        self.addCleanup(machine.Local().remove, self.tmp)

    def home(self, side, case):
        return os.path.join(self.tmp, side, case)

    def test_every_block_is_byte_identical_to_the_bash_one(self):
        script = SOURCE
        for case, call, _, _ in CASES:
            for side in ("sh", "py"):
                home = self.home(side, case)
                if case in ("replacing", "removing"):
                    os.makedirs(os.path.join(home, ".ssh", "config.d"))
                    with open(sshalias.alias_path({"HOME": home}), "w") as f:
                        f.write(OTHERS)
            script += "HOME=%s %s || exit 1\n" % (self.home("sh", case), call)
        cp = bash(script, env={"HOME": self.home("sh", "none")})
        self.assertEqual(cp.returncode, 0, cp.stderr)
        local = machine.Local()
        for case, _, args, kw in CASES:
            env = {"HOME": self.home("py", case)}
            if args is None:
                sshalias.alias_remove(local, env, "demo")
            else:
                sshalias.alias_set(local, env, *args, **kw)
            with self.subTest(case=case):
                with open(sshalias.alias_path({"HOME": self.home("sh", case)}), "rb") as f:
                    theirs = f.read()
                with open(sshalias.alias_path(env), "rb") as f:
                    ours = f.read()
                self.assertEqual(ours, theirs)
        self.assertEqual(oct(os.stat(os.path.dirname(sshalias.alias_path({"HOME": self.home("py", "plain")}))).st_mode & 0o777), "0o700")


class TestOnTheFake(unittest.TestCase):
    def setUp(self):
        self.fake = machine.Fake("box")
        self.fake.answer(["chmod"])
        self.env = {"HOME": "/home/u"}
        self.conf = sshalias.alias_path(self.env)
        self.flags = mock.patch.dict(os.environ, {}, clear=False)
        self.flags.start()
        self.addCleanup(self.flags.stop)
        os.environ.pop("WK_DRY_RUN", None)

    def stderr(self, fn):
        buf = io.StringIO()
        with redirect_stderr(buf):
            fn()
        return buf.getvalue()

    def test_no_address_or_user_is_refused_with_the_words_bash_uses_and_nothing_is_written(self):
        for hostname, user in (("", "admin"), ("10.0.0.1", "")):
            err = self.stderr(lambda: self.assertRaises(
                Refused, sshalias.alias_set, self.fake, self.env, "demo", hostname, user))
            self.assertIn("no address for 'demo', so no ssh alias was written: an empty HostName\n"
                          "    makes ssh refuse to read %s at all, and with it every other host in it" % self.conf, err)
        self.assertEqual(self.fake.effects, [])

    def test_the_directory_is_made_private_once(self):
        sshalias.alias_set(self.fake, self.env, "a", "10.0.0.1", "u")
        d = os.path.dirname(self.conf)
        self.assertEqual(self.fake.effects, [("mkdir", d), ("run", ("chmod", "0700", d)), ("write", self.conf)])
        sshalias.alias_set(self.fake, self.env, "b", "10.0.0.2", "u")
        self.assertEqual(self.fake.effects[3:], [("write", self.conf)])
        self.assertEqual(self.fake.files[self.conf].count("Host wk-"), 2)

    def test_a_directory_that_cannot_be_made_private_is_a_refusal(self):
        self.fake.answer(["chmod"], rc=1, err="chmod: not permitted")
        err = self.stderr(lambda: self.assertRaises(
            Refused, sshalias.alias_set, self.fake, self.env, "a", "10.0.0.1", "u"))
        self.assertIn("cannot set mode 0700 on %s" % os.path.dirname(self.conf), err)

    def test_removing_an_absent_block_writes_nothing(self):
        sshalias.alias_remove(self.fake, self.env, "demo")
        self.fake.files[self.conf] = "Host other\n    HostName 1.2.3.4\n"
        sshalias.alias_remove(self.fake, self.env, "demo")
        self.assertEqual(self.fake.effects, [])
        sshalias.alias_remove(self.fake, self.env, "other")
        self.assertEqual(self.fake.effects, [])

    def test_removing_takes_only_the_named_block(self):
        self.fake.files[self.conf] = OTHERS
        sshalias.alias_remove(self.fake, self.env, "demo")
        self.assertEqual(self.fake.files[self.conf], "Host other\n    HostName 1.2.3.4\n\nHost tail\n    User t\n")

    def test_a_dry_run_writes_nothing(self):
        os.environ["WK_DRY_RUN"] = "1"
        self.fake.files[self.conf] = OTHERS
        self.fake.dirs.add(os.path.dirname(self.conf))
        sshalias.alias_set(self.fake, self.env, "demo", "10.0.0.9", "u")
        self.assertEqual(self.fake.files[self.conf], OTHERS)
        self.assertEqual([e[0] for e in self.fake.effects], ["write"])


if __name__ == "__main__":
    unittest.main()
