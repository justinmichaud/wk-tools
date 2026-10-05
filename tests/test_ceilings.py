"""Every wait in a probe has a ceiling, and the ceiling reaches the whole"""
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
        r = Local().run(["bash", "-c", "sleep 131.5 & wait"], timeout=1)
        self.assertEqual(r.rc, TIMED_OUT)
        time.sleep(0.2)
        left = subprocess.run(["pgrep", "-f", r"^sleep 131\.5$"], capture_output=True, text=True).stdout.split()
        if left:
            subprocess.run(["pkill", "-f", r"^sleep 131\.5$"])
        self.assertEqual(left, [], "the ceiling killed the child and left a grandchild running")


    def test_no_fleet_probe_can_outlive_its_ceiling(self):
        wk = self.tmp / "lib" / "wk"
        wk.mkdir(parents=True)
        (wk / "__init__.py").write_text("")
        (wk / "__main__.py").write_text("import time\ntime.sleep(600)\n")
        self.assertIsNone(status.fleet_probe(self.tmp, "rpi4", 0.5, env={}))
        rec = status.fleet_record("rpi4", {"role": "bench-device"}, None, 0)
        self.assertEqual((rec["machine"], rec["mode"]), ("rpi4", "no answer within 0s"))

    def test_status_parallel_keeps_starting_order(self):
        w = status.Walk(REPO, env={"WK_ROW_LABEL": "here", "WK_IMAGE_MARKER": str(self.tmp / "none")},
                        fleet=False, devices=False)
        w.places = lambda: ["a", "b", "c"]
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


if __name__ == "__main__":
    unittest.main()
