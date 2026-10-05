"""container/bin/plot-memory-log.py: a memory log, plotted."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

PLOTTER = REPO / "container" / "bin" / "plot-memory-log.py"


class PlotMemoryLogTest(unittest.TestCase):
    def _synthetic_log(self, path):
        path.write_text(
            "# t rss dirty shared\n"
            "0.0 41943040 20971520 8388608\n"
            "0.5 43121200 21004288 8388608\n"
            "1.0 44100000 21500000 8400000\n"
            "1.5 45000000 22000000 8400000\n"
            "2.0 44500000 21800000 8400000\n"
        )

    def test_plots_a_synthetic_log_to_svg(self):
        with tempfile.TemporaryDirectory(prefix="wk-test-memlog-") as d:
            d = Path(d)
            log = d / "mem.log"
            self._synthetic_log(log)
            out = d / "chart.svg"

            cp = subprocess.run(
                ["python3", str(PLOTTER), str(log), "--include", "rss,dirty",
                 "--max-seconds", "1.5", "--out", str(out)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
            )
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertTrue(out.exists(), cp.stdout)
            content = out.read_text()
            self.assertIn("<svg", content)
            self.assertIn("rss", content)
            self.assertIn("dirty", content)
            self.assertNotIn("shared", content)  # --include excluded it too

    def test_plots_a_synthetic_log_to_html(self):
        with tempfile.TemporaryDirectory(prefix="wk-test-memlog-") as d:
            d = Path(d)
            log = d / "mem.log"
            self._synthetic_log(log)
            out = d / "chart.html"

            cp = subprocess.run(
                ["python3", str(PLOTTER), str(log), "--out", str(out)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
            )
            self.assertEqual(cp.returncode, 0, cp.stdout)
            content = out.read_text()
            self.assertIn("<!doctype html>", content.lower())
            self.assertIn("<svg", content)

    def test_unknown_include_column_is_refused(self):
        with tempfile.TemporaryDirectory(prefix="wk-test-memlog-") as d:
            d = Path(d)
            log = d / "mem.log"
            self._synthetic_log(log)
            cp = subprocess.run(
                ["python3", str(PLOTTER), str(log), "--include", "nonexistent", "--out", str(d / "o.svg")],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30,
            )
            self.assertNotEqual(cp.returncode, 0)
            self.assertIn("nonexistent", cp.stdout)


if __name__ == "__main__":
    unittest.main()
