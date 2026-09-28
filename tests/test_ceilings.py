"""Every wait in a probe has a ceiling, and the ceiling reaches the whole
process group: Local.run's timeout, the fleet probe, the tailscale walk, and
`wk status`'s starting-order report. Each stub hangs for longer than the
runner's budget, so a ceiling that did not hold fails the test there.

Run: python3 -m unittest tests.test_ceilings -v
"""
import os
import subprocess
import sys
import time
import unittest

from tests.support import REPO, WkTest

sys.path.insert(0, str(REPO / "lib"))
from wk import status  # noqa: E402
from wk.machine import TIMED_OUT, Local  # noqa: E402


class TestCeilings(WkTest):
    def test_the_ceiling_reaches_the_subtree(self):
        """the ceiling reaches the whole process group, grandchildren included"""
        # The pgrep pattern is anchored so it matches the sleep alone, not a shell naming it.
        r = Local().run(["bash", "-c", "sleep 131.5 & wait"], timeout=1)
        self.assertEqual(r.rc, TIMED_OUT)
        time.sleep(0.2)
        left = subprocess.run(["pgrep", "-f", r"^sleep 131\.5$"], capture_output=True, text=True).stdout.split()
        if left:
            subprocess.run(["pkill", "-f", r"^sleep 131\.5$"])
        self.assertEqual(left, [], "the ceiling killed the child and left a grandchild running")

    def test_a_command_inside_its_ceiling_is_not_held_to_it(self):
        self.assertEqual(Local().run(["true"], timeout=600).rc, 0)

    def test_no_fleet_probe_can_outlive_its_ceiling(self):
        """no fleet probe can outlive its ceiling, and one killed for time is still named"""
        boot = self.tmp / "lib" / "wk" / "boot"
        boot.mkdir(parents=True)
        (boot.parent / "__init__.py").write_text("")
        (boot / "__init__.py").write_text("")
        (boot / "cli.py").write_text("import time\ntime.sleep(600)\n")
        self.assertIsNone(status.fleet_probe(self.tmp, "rpi4", 0.5, env={}))
        rec = status.fleet_record("rpi4", {"role": "bench-device"}, None, 0)
        self.assertEqual((rec["machine"], rec["mode"]), ("rpi4", "no answer within 0s"))

    def test_status_parallel_keeps_starting_order(self):
        """`wk status` reports parallel probes in starting order, never finishing order, and keeps the worst exit status"""
        w = status.Walk(REPO, env={"WK_ROW_LABEL": "here", "WK_IMAGE_MARKER": str(self.tmp / "none")},
                        fleet=False, devices=False)
        w.targets = lambda: ["a", "b", "c"]
        w._is_here = lambda t: False
        delays = {"a": (0.6, 2), "b": (0.3, 4), "c": (0, 0)}

        def job(tname, name):
            def run():
                time.sleep(delays[tname][0])
                return [status.Rec("raw", machine="here", text=tname).done()], delays[tname][1]
            return run
        w._job = job
        recs = list(w.records(markers=False))
        self.assertEqual([r["text"] for r in recs if r["kind"] == "raw"], ["a", "b", "c"])
        self.assertEqual(recs[-1], {"kind": "exit", "code": 4})

    def test_tailscale_reach_cannot_hang(self):
        """a tailscale CLI that never answers holds the fleet walk for its ceiling and no longer (lib/wk/reach.py)"""
        stub = self.tmp / "tailscale"
        stub.write_text("#!/bin/sh\nsleep 600\n")
        stub.chmod(0o755)
        cp = subprocess.run([sys.executable, "-c", "from wk import reach; print(reach.Reach(env={'WK_TAILSCALE_TIMEOUT': '1'}).peers())"],
                            env=dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), PYTHONPATH=str(REPO / "lib")),
                            capture_output=True, text=True)
        self.assertEqual(cp.stdout.strip(), "[]", cp.stderr)


if __name__ == "__main__":
    unittest.main()
