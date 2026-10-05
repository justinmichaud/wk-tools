"""container/bin/plot-memory-log.py: a memory log, plotted."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO

PLOTTER = REPO / "container" / "bin" / "plot-memory-log.py"


class PlotMemoryLogTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory(prefix="wk-test-memlog-")
        self.addCleanup(d.cleanup)
        self.dir = Path(d.name)
        self.log = self.dir / "mem.log"
        self.log.write_text(
            "# t rss dirty shared\n"
            "0.0 41943040 20971520 8388608\n"
            "0.5 43121200 21004288 8388608\n"
            "1.0 44100000 21500000 8400000\n"
            "1.5 45000000 22000000 8400000\n"
            "2.0 44500000 21800000 8400000\n"
        )

    def plot(self, out, *args):
        return subprocess.run(["python3", str(PLOTTER), str(self.log), *args, "--out", str(self.dir / out)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)

    def test_plots_a_synthetic_log_to_svg(self):
        cp = self.plot("chart.svg", "--include", "rss,dirty", "--max-seconds", "1.5")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        content = (self.dir / "chart.svg").read_text()
        self.assertIn("<svg", content)
        self.assertIn("rss", content)
        self.assertIn("dirty", content)
        self.assertNotIn("shared", content)

    def test_plots_a_synthetic_log_to_html(self):
        cp = self.plot("chart.html")
        self.assertEqual(cp.returncode, 0, cp.stdout)
        content = (self.dir / "chart.html").read_text()
        self.assertIn("<!doctype html>", content.lower())
        self.assertIn("<svg", content)

    def test_unknown_include_column_is_refused(self):
        cp = self.plot("o.svg", "--include", "nonexistent")
        self.assertNotEqual(cp.returncode, 0)
        self.assertIn("nonexistent", cp.stdout)


if __name__ == "__main__":
    unittest.main()
