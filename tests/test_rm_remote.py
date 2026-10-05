"""`wk rm` against a machine that does not answer: the record stays, and the refusal quotes ssh's own words."""
import os
import unittest

from tests.support import WkTest, run, stub_path

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

    def test_rm_refuses_with_ssh_words_and_keeps_the_record(self):
        with stub_path({"ssh": _HOSTKEY_SSH}) as binp:
            env = {"WK_MACHINES_DIR": str(self.registry), "XDG_STATE_HOME": str(self.state), "WK_YES": "1",
                   "WK_PLACE": "fakebox", "PATH": f"{binp}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
            cp = run("rm", "fakews", env=env, input="", timeout=120)
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("Host key verification failed.", cp.stdout, cp.stdout)
        self.assertTrue(self.record.is_dir())


if __name__ == "__main__":
    unittest.main()
