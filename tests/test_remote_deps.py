"""What a shared build machine answers to remote/probe.sh, and the distro family remote/deps.sh resolves."""
import sys

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
        cp = bash(f'cat "{DEPS}" "{PROBE}" | bash -s')
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        keys = {l.split("=", 1)[0] for l in cp.stdout.splitlines() if "=" in l}
        self.assertEqual(keys, {"host", "os", "family", "arch", "cores"}, cp.stdout)
