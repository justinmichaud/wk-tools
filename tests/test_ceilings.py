"""Every wait in a probe has a ceiling, and the ceiling reaches the whole
process group: `capped`, the fleet probe, the tailscale walk, and `par`'s
starting-order report.

Run: python3 -m unittest tests.test_ceilings -v
"""
import os
import subprocess
import sys
import time
import unittest

from tests.support import REPO, WkTest, bash


class TestCeilings(WkTest):
    def test_capped_reaches_the_subtree(self):
        """the ceiling reaches the whole process group, grandchildren included"""
        # The pgrep patterns are anchored: this script is itself argv, so an
        # unanchored 'sleep 31.5' matches the probing shell and pkills it.
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
t0=$(date +%s)
capped 1 bash -c 'sleep 31.5 & wait' >/dev/null 2>&1 || true
d=$(( $(date +%s) - t0 ))
[ "$d" -le 4 ] || {{ echo "capped 1 took ${{d}}s"; exit 1; }}
left=$( (pgrep -f '^sleep 31\\.5$' 2>/dev/null || true) | wc -l | tr -d ' ')
[ "$left" = 0 ] || {{ pkill -f '^sleep 31\\.5$' 2>/dev/null || true
    echo "the ceiling killed the child and left $left grandchild(ren) running"; exit 1; }}
t0=$(date +%s); capped 20 true; d=$(( $(date +%s) - t0 ))
[ "$d" -le 2 ] || {{ echo "capped 20 true took ${{d}}s"; exit 1; }}
'''
        cp = bash(script, timeout=40)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_no_fleet_probe_can_outlive_its_ceiling(self):
        """no fleet probe can outlive its ceiling, and one killed for time is still named"""
        import sys
        sys.path.insert(0, str(REPO / "lib"))
        from wk import status
        r = status._bash(REPO, "sleep 30", timeout=0.5)
        self.assertEqual(r.rc, status.TIMED_OUT)
        rec = status.fleet_record("rpi4", {"NODE_ROLE": "bench-device"}, None, 0)
        self.assertEqual((rec["machine"], rec["mode"]), ("rpi4", "no answer within 0s"))

    def test_status_parallel_keeps_starting_order(self):
        """`wk status` reports parallel probes in starting order, never finishing order, and keeps the worst exit status"""
        script = f'''
set -euo pipefail
. "{REPO}/lib/common.sh"
. "{REPO}/lib/par.sh"
_slow() {{ sleep "$1"; printf '%s\\n' "$2" >&3; exit "$3"; }}
par_begin
par_run a _slow 3 a 2
par_run b _slow 2 b 4
par_run c _slow 1 c 0
par_wait
out=$(for n in a b c; do par_record "$n"; done)
[ "$out" = "$(printf 'a\\nb\\nc')" ] || {{ echo "records came back in finishing order: $(printf '%s' "$out" | tr '\\n' ' ')"; exit 1; }}
[ "$_par_status" = " a 2 b 4 c 0" ] || {{ echo "the statuses did not come back: $_par_status"; exit 1; }}
par_end
'''.replace("{TMP}", str(self.tmp))
        cp = bash(script, timeout=20)
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)

    def test_tailscale_reach_cannot_hang(self):
        """a tailscale CLI that never answers holds the fleet walk for its ceiling and no longer (lib/wk/reach.py)"""
        stub = self.tmp / "tailscale"
        stub.write_text("#!/bin/sh\nsleep 30\n")
        stub.chmod(0o755)
        t0 = time.monotonic()
        cp = subprocess.run([sys.executable, "-c", "from wk import reach; print(reach.Reach(env={'WK_TAILSCALE_TIMEOUT': '1'}).peers())"],
                            env=dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), PYTHONPATH=str(REPO / "lib")),
                            capture_output=True, text=True, timeout=20)
        self.assertEqual(cp.stdout.strip(), "[]", cp.stderr)
        self.assertLessEqual(time.monotonic() - t0, 8, "a tailscale CLI that never answers held the walk")


if __name__ == "__main__":
    unittest.main()
