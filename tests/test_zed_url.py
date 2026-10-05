"""`wk zed --url <ws>`: prints the ssh:// URL cmd/zed would hand to Zed,
without invoking Zed itself. Each docstring is the phrase of the behaviour
it checks."""
import os
import stat
import unittest

from tests.support import WkTest, rand_suffix, requires_container_place, run, scratch_dir

_FAKE_ZED = """#!/bin/sh
echo "FAKE ZED WAS INVOKED: $*" >&2
exit 1
"""


@requires_container_place()
class TestZedUrl(WkTest):
    def setUp(self):
        super().setUp()
        self.name = f"wk-test-{rand_suffix()}"
        self._created = False

    def tearDown(self):
        if self._created:
            cp = run("rm", self.name, env={"WK_YES": "1"})
            if cp.returncode != 0:
                print(f"[teardown] 'wk rm {self.name}' exited {cp.returncode}: {cp.stdout + cp.stderr}")
        super().tearDown()

    def test_url_prints_ssh_url_and_never_execs_zed(self):
        cp = run("new", self.name, "--on", "container", timeout=600)
        self._created = cp.returncode == 0
        self.assertEqual(cp.returncode, 0, f"wk new failed: {cp.stdout + cp.stderr}")
        run("status", self.name, "--wait", "--timeout", "300")

        with scratch_dir() as bindir:
            fake = bindir / "zed"
            fake.write_text(_FAKE_ZED)
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

            env = {"PATH": f"{bindir}:{os.environ.get('PATH', '')}"}
            cp = run("zed", "--url", self.name, env=env, timeout=60)

        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("ssh://", cp.stdout)
        self.assertNotIn("FAKE ZED WAS INVOKED", cp.stdout)


if __name__ == "__main__":
    unittest.main()
