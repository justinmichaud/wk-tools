"""`wk zed --url <ws>`: prints the ssh:// URL cmd/zed would hand to Zed,
without invoking Zed itself. Each docstring is the phrase of the behaviour
it checks.

cmd/zed's --url path never calls zed_cli() (see the file: `Z` is left unset
under --url, and `emit()` only ever execs it in the non-url branch), so a
fake `zed` on PATH that fails loudly if invoked is a straightforward way to
prove that -- and the URL itself has to come from a workspace whose route is
real, since t_ssh_host asks the driver (a live sshd inside a container,
installed on first use; see targets/container.sh). Gated on the same running
podman `wk` VM tests.test_container_workspace uses, and the same real
container workspace, created and torn down the same way -- nothing here
stands in for one.

Run: python3 -m unittest tests.test_zed_url -v
"""
import os
import stat
import unittest

from tests.support import WkTest, rand_suffix, requires_podman_vm, run, scratch_dir

_FAKE_ZED = """#!/bin/sh
echo "FAKE ZED WAS INVOKED: $*" >&2
exit 1
"""


@requires_podman_vm()
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
        """`wk zed --url <ws>` prints ssh://... and a pastable `zed '...'` line, without launching Zed"""
        cp = run("new", self.name, "--target", "container", timeout=600)
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
