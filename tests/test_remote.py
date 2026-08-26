"""cmd/selftest's `remote` section: a configured machine name resolves as a
target, and `wk status`/`wk ls` reach it. Each docstring is the
phrase of the behaviour it checks.

Every test that touches a real machine over ssh is gated on that specific
machine answering `ssh -o BatchMode=yes <name> true`
(tests.support.requires_machine) and never provisions, reboots or otherwise
mutates it -- these are read-only probes only.

Run: python3 -m unittest tests.test_remote -v
"""
import unittest

from tests.support import REPO, WkTest, bash, requires_machine


def _configured_remote_machines():
    """Every machine conf in targets/hosts whose kind resolves to `remote`
    -- pure logic, no ssh, mirrors target_kind()'s own resolution."""
    cp = bash(f'''
. "{REPO}/lib/common.sh"
. "{REPO}/lib/target.sh"
d=$(target_registry_dir)
for f in "$d"/*.conf; do
    [ -f "$f" ] || continue
    n=$(basename "$f" .conf)
    echo "$n:$(target_kind "$n" 2>/dev/null || echo '?')"
done
''')
    out = {}
    for line in cp.stdout.splitlines():
        if ":" in line:
            n, k = line.split(":", 1)
            out[n] = k
    return out


class TestRemoteConfsResolve(WkTest):
    def test_remote_confs_resolve(self):
        """a machine name is a target"""
        machines = _configured_remote_machines()
        if not machines:
            self.skipTest(f"no machines configured in {REPO / 'targets' / 'hosts'}")
        bad = [n for n, k in machines.items() if k in ("", "?")]
        self.assertEqual(bad, [], f"configured but unresolvable: {bad}")


def _t_info_reaches(name):
    """Mirrors cmd/selftest's chk_remote_reachable: load the driver for
    `name` and ask t_info about a workspace that does not exist. t_info
    itself never fails (an unreachable machine is a *word* it prints, not a
    nonzero exit) -- what this actually catches is load_target or the probe
    dying outright, which is the same thing the shell version caught."""
    cp = bash(f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/resources.sh"
. "{REPO}/lib/store.sh"
. "{REPO}/lib/target.sh"
( load_target "{name}"; t_info selftest-nonexistent >/dev/null 2>&1 )
''', timeout=30)
    return cp


class TestRemoteReachable(WkTest):
    """`wk status`/`wk ls` list what is on a configured machine"""

    @requires_machine("buildbox4")
    def test_buildbox4_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("buildbox4")
        self.assertEqual(cp.returncode, 0, f"buildbox4: {cp.stdout + cp.stderr}")

    @requires_machine("devbox-arm64-2")
    def test_devbox_arm64_2_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("devbox-arm64-2")
        self.assertEqual(cp.returncode, 0, f"devbox-arm64-2: {cp.stdout + cp.stderr}")

    @requires_machine("moose")
    def test_moose_reachable(self):
        """`wk status`/`wk ls` list what is on a configured machine"""
        cp = _t_info_reaches("moose")
        self.assertEqual(cp.returncode, 0, f"moose: {cp.stdout + cp.stderr}")


if __name__ == "__main__":
    unittest.main()
