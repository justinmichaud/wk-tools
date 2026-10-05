"""Tests for the provisioning half of `wk run --profile` and its neighbours --
docs/Urgent/HANDOFF-profile.md, HANDOFF-debug.md and HANDOFF-memory.md."""

import platform
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, clean_env, fake_workspace

sys.path.insert(0, str(REPO / "lib"))
from wk import profile  # noqa: E402

RUN = REPO / "cmd" / "run"
PLOTTER = REPO / "container" / "bin" / "plot-memory-log.py"
KNOWN_MODES = list(profile.MODES)


def run_profile(*args, env=None, timeout=30):
    e = clean_env(env)
    cp = subprocess.run(
        [str(RUN), *args],
        cwd=str(REPO),
        env=e,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return cp


class ProfileModeRefusalTest(unittest.TestCase):
    def test_unknown_mode_is_refused_naming_the_valid_ones(self):
        cp = run_profile("--profile=bogus", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such mode", cp.stdout, cp.stdout)
        for mode in KNOWN_MODES:
            self.assertIn(mode, cp.stdout, f"refusal does not name '{mode}': {cp.stdout}")

    def test_known_mode_gets_past_the_mode_check(self):
        cp = run_profile("--profile=sampling", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotIn("no such mode", cp.stdout, cp.stdout)


class ProfileBrowserProcessTest(unittest.TestCase):

    PORT_PROCESS_NAMES = {
        "wpe-release": {"web": "WPEWebProcess", "network": "WPENetworkProcess", "gpu": "WPEGPUProcess"},
        "gtk-release": {"web": "WebKitWebProcess", "network": "WebKitNetworkProcess", "gpu": "WebKitGPUProcess"},
    }

    def test_process_ui_prefixes_samply_via_mini_browser_prefix(self):
        with fake_workspace() as ws:
            for preset in self.PORT_PROCESS_NAMES:
                cp = ws.run("run", "--preset", preset, "--browser", "--process", "ui",
                             "--profile=samply", "--dry-run")
                self.assertEqual(cp.returncode, 0, f"{preset}: {cp.stdout}")
                self.assertIn("WEBKIT_MINI_BROWSER_PREFIX=", cp.stdout, f"{preset}: {cp.stdout}")
                self.assertIn("samply record", cp.stdout, f"{preset}: {cp.stdout}")
                self.assertIn("run-minibrowser", cp.stdout, f"{preset}: {cp.stdout}")
                launch_line = next(l for l in cp.stdout.splitlines() if "run-minibrowser" in l)
                self.assertNotIn("samply", launch_line, f"{preset}: {cp.stdout}")

    def test_process_web_network_gpu_attach_by_pid_after_launch(self):
        with fake_workspace() as ws:
            for preset, names in self.PORT_PROCESS_NAMES.items():
                for process, proc_name in names.items():
                    args = ["run", "--preset", preset, "--browser", "--profile=samply", "--dry-run"]
                    if process != "web":
                        args += ["--process", process]
                    cp = ws.run(*args)
                    self.assertEqual(cp.returncode, 0, f"{preset}/{process}: {cp.stdout}")
                    self.assertIn(proc_name, cp.stdout, f"{preset}/{process}: {cp.stdout}")
                    self.assertIn("pgrep", cp.stdout, f"{preset}/{process}: {cp.stdout}")
                    self.assertIn("samply record", cp.stdout, f"{preset}/{process}: {cp.stdout}")
                    self.assertIn(" -p ", cp.stdout, f"{preset}/{process}: {cp.stdout}")


    @unittest.skipUnless(platform.system() == "Darwin",
                         "a mac-* preset is refused off an Apple host")
    def test_apple_port_browser_path_is_unchanged(self):
        with fake_workspace() as ws:
            cp = ws.run("run", "--profile", "--preset", "mac-release", "--browser", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("MiniBrowser.app/Contents/MacOS/MiniBrowser", cp.stdout, cp.stdout)
            self.assertIn("--url", cp.stdout, cp.stdout)
            self.assertNotIn("WEBKIT_MINI_BROWSER_PREFIX", cp.stdout, cp.stdout)

            cp = ws.run("run", "--profile", "--preset", "mac-release", "--browser", "--process", "web", "--dry-run")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("not wired up for the Apple ports", cp.stdout, cp.stdout)
            self.assertIn("--attach", cp.stdout, cp.stdout)

    def test_process_with_a_non_wrapper_mode_refuses_naming_the_remedy(self):
        with fake_workspace() as ws:
            cp = ws.run("run", "--preset", "gtk-release", "--browser", "--process", "ui",
                         "--profile=sampling", "--dry-run")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("meaningless", cp.stdout, cp.stdout)
            self.assertIn("--profile=samply", cp.stdout, cp.stdout)

            cp = ws.run("run", "--preset", "gtk-release", "--browser", "--profile=sampling", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)

    def test_unknown_process_value_is_refused(self):
        cp = run_profile("--profile", "--process", "bogus", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such process", cp.stdout, cp.stdout)
        for p in ("ui", "web", "network", "gpu"):
            self.assertIn(p, cp.stdout, f"refusal does not name '{p}': {cp.stdout}")

    def test_process_without_browser_is_refused(self):
        cp = run_profile("--profile", "--process", "ui", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--browser", cp.stdout, cp.stdout)

    def test_heaptrack_massif_browser_on_cmake_ports_refuses_owed(self):
        with fake_workspace() as ws:
            for mode in ("heaptrack", "massif"):
                cp = ws.run("run", "--preset", "gtk-release", "--browser", "--profile=" + mode, "--dry-run")
                self.assertNotEqual(cp.returncode, 0, f"{mode}: {cp.stdout}")
                self.assertIn("not wired up", cp.stdout, f"{mode}: {cp.stdout}")


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
