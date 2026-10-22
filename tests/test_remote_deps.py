"""What a shared build machine answers to remote/deps.sh and remote/probe.sh, and the distro family it resolves to."""
import os
import sys
import tempfile
from pathlib import Path

from tests.support import REPO, WkTest, bash

sys.path.insert(0, str(REPO / "lib"))

DEPS = REPO / "remote" / "deps.sh"
PROBE = REPO / "remote" / "probe.sh"

class TestTheList(WkTest):
    def test_a_derivative_resolves_to_its_parent_family(self):
        cases = [
            ("debian", "", "debian"),
            ("ubuntu", "", "debian"),
            ("raspbian", "debian", "debian"),
            ("linuxmint", "ubuntu debian", "debian"),
            ("fedora", "", "fedora"),
            ("rocky", "rhel centos fedora", "fedora"),
            ("arch", "", "arch"),
            ("plan9", "", "unknown"),
        ]
        for idv, like, want in cases:
            cp = self.bash(f'. "{DEPS}"\nwk_remote_family {idv!r} {like!r}\n')
            with self.subTest(id=idv, id_like=like):
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual(cp.stdout.strip(), want)

class TestTheProbeItself(WkTest):
    def test_it_runs_against_this_machine_and_answers_every_key(self):
        home = Path(tempfile.mkdtemp(prefix="wk-test-probe-home-"))
        self.addCleanup(bash, 'rm -rf "%s"' % home)
        (home / ".wk-agent-token").write_text("sk-the-value\n")
        cp = bash(f'cat "{DEPS}" "{PROBE}" | bash -s', env={"HOME": str(home)})
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        keys = {l.split("=", 1)[0] for l in cp.stdout.splitlines() if "=" in l}
        for want in ("host", "os", "family", "arch", "cores", "marker"):
            self.assertIn(want, keys, cp.stdout)
        self.assertIn("cred..wk-agent-token", keys, "a credential copy is reported by digest")
        self.assertNotIn("sk-the-value", cp.stdout)
