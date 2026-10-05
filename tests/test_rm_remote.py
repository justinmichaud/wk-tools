"""`wk rm` against a machine that does not answer: the record stays, and the refusal quotes ssh's own words."""
import os
import subprocess
import sys
import unittest

from tests.support import REPO, WkTest, run, stub_path

_HOSTKEY_SSH = """#!/bin/sh
echo "Host key verification failed." >&2
exit 255
"""

_MACHINE_CONF = "kind=build\ndriver=remote\nhost={host}\n"

_LOCAL_CONF = (
    "kind=build\ndriver=remote\n"
    "local=1\n"
    "root={root}\n"
    "store={store}\n"
)


class TestAnUnreachableMachineKeepsItsRecord(WkTest):
    def setUp(self):
        super().setUp()
        self.registry = self.tmp / "hosts"
        self.registry.mkdir()
        (self.registry / "fakebox.conf").write_text(_MACHINE_CONF.format(host="fakebox.invalid"))
        self.state = self.tmp / "state"
        self.record = self.state / "wk" / "remote" / "fakebox" / "ws" / "fakews"
        self.record.mkdir(parents=True)

    def _env(self, binp, **extra):
        env = {
            "WK_MACHINES_DIR": str(self.registry),
            "XDG_STATE_HOME": str(self.state),
            "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        }
        env.update(extra)
        return env

    def test_rm_refuses_with_ssh_words_and_keeps_the_record(self):
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            cp = run("rm", "fakews", env=self._env(binp, WK_YES="1", WK_PLACE="fakebox"),
                     input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("Host key verification failed.", cp.stdout, cp.stdout)
        self.assertTrue(self.record.is_dir())

    def test_the_locate_walk_names_the_machine_and_ssh_reason(self):
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            cp = subprocess.run(
                [sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1] + '/lib')\n"
                 "from wk import places\nprint(places.Registry(sys.argv[1]).locate('no-such-workspace'))", str(REPO)],
                env=dict(os.environ, **self._env(binp)), capture_output=True, text=True, timeout=120)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(cp.stdout.strip(), "[]", cp.stdout + cp.stderr)
        self.assertIn("fakebox", cp.stderr)
        self.assertIn("Host key verification failed.", cp.stderr)


if __name__ == "__main__":
    unittest.main()
