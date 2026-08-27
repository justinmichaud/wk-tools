"""Tests for the provisioning half of `wk profile` and its neighbours --
docs/Urgent/HANDOFF-profile.md, HANDOFF-debug.md and HANDOFF-memory.md.

Nothing here builds, runs, or benchmarks WebKit, and nothing here touches a
real workspace, the real quiesce helper, or a real sudoers rule: every check
is either a static read of a file this repo ships, or a direct invocation of
a script with no workspace behind it, the same shape tests/test_quiesce.py
already uses for the other privileged helper.

Run: python3 -m unittest tests.test_profile_debug -v
"""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.support import REPO, WK, fake_workspace, run

PROFILE = REPO / "cmd" / "profile"
QUIESCE_PRIV = REPO / "admin" / "wk-quiesce-priv"
PLOTTER = REPO / "container" / "bin" / "plot-memory-log.py"
CONTAINERFILES = [
    REPO / "container" / "buildroot" / "Containerfile",
    REPO / "container" / "yocto" / "Containerfile",
]

# The modes cmd/profile's own header (and its MODES variable) name today.
# 'native' is a platform question, not a mode of its own -- see cmd/profile's
# comment just above its MODES case -- so it is checked for separately.
KNOWN_MODES = ["sampling", "bytecode", "samply", "instruments", "heaptrack", "massif"]


def run_profile(*args, env=None, timeout=30):
    """Invoke cmd/profile directly, bypassing the ./wk dispatcher -- which
    is what lets a mode-refusal test run without a real workspace: the
    dispatcher's own name resolution (`ws_exists`) never gets a chance to
    reject the name first, and cmd/profile's own mode check runs (and dies)
    before anything in it needs a workspace to actually exist."""
    e = dict(os.environ)
    e.pop("WK_MARKER", None)
    e.pop("XDG_STATE_HOME", None)
    e.pop("WK_STORE", None)
    if env:
        e.update(env)
    cp = subprocess.run(
        [str(PROFILE), *args],
        cwd=str(REPO),
        env=e,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    return cp


class ProfileHelpTest(unittest.TestCase):
    def test_help_lists_every_mode_and_carries_no_typo_mode(self):
        """`wk profile -h` lists the modes and the file contains no typo mode"""
        cp = run("profile", "-h")
        for mode in KNOWN_MODES:
            self.assertIn(mode, cp.stdout, f"'{mode}' missing from 'wk profile -h': {cp.stdout}")

        # docs/Urgent/HANDOFF-profile.md's own defect: 'strongrefs' as a typo'd
        # stand-in for JSC_enableStrongRefTracker. It was never wired in as a
        # mode -- this is the static half of that check, over the file itself
        # rather than just the help text, so a typo'd mode added anywhere in
        # the script (not only the header comment) would still be caught.
        text = PROFILE.read_text()
        self.assertNotIn("strongrefs", text, "cmd/profile mentions a 'strongrefs' mode that was never wired in")

    def test_mode_list_in_header_matches_the_MODES_variable(self):
        """the header comment's mode list and the MODES variable never drift apart"""
        text = PROFILE.read_text()
        modes_line = next(l for l in text.splitlines() if l.startswith("MODES="))
        declared = modes_line.split("=", 1)[1].strip('"').split()
        self.assertEqual(sorted(declared), sorted(KNOWN_MODES))


class ProfileModeRefusalTest(unittest.TestCase):
    def test_unknown_mode_is_refused_naming_the_valid_ones(self):
        """unknown --mode is refused naming the valid ones"""
        cp = run_profile("--mode", "bogus", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such mode", cp.stdout, cp.stdout)
        for mode in KNOWN_MODES:
            self.assertIn(mode, cp.stdout, f"refusal does not name '{mode}': {cp.stdout}")

    def test_known_mode_gets_past_the_mode_check(self):
        """a known --mode is never refused by the mode check itself"""
        # Past the mode check it needs a real workspace (ws_target etc.),
        # which this test does not have -- so it dies later, on something
        # else. The point here is narrower: "no such mode" must never fire
        # for a mode this file itself declares valid.
        cp = run_profile("--mode", "sampling", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotIn("no such mode", cp.stdout, cp.stdout)


class ProfileBrowserProcessTest(unittest.TestCase):
    """`wk profile <ws> --browser --process <p>` on the CMake ports (GTK/WPE)
    -- docs/Urgent/HANDOFF-linux-minibrower.md's "which process" decision
    (docs/defects 13: web is the priority, but the other three are cheap
    enough to wire up too). Every check here is a --dry-run against a
    FakeWorkspace (tests/support.py): a marker naming a checkout that exists,
    so config_load/t_exec resolve without a real workspace, VM or ssh."""

    # config -> {process: the name config_<process>_process_name reports}
    PORT_PROCESS_NAMES = {
        "wpe-release": {"web": "WPEWebProcess", "network": "WPENetworkProcess", "gpu": "WPEGPUProcess"},
        "gtk-release": {"web": "WebKitWebProcess", "network": "WebKitNetworkProcess", "gpu": "WebKitGPUProcess"},
    }

    def test_process_ui_prefixes_samply_via_mini_browser_prefix(self):
        """--process ui puts samply in WEBKIT_MINI_BROWSER_PREFIX, not in front of
        run-minibrowser itself"""
        with fake_workspace() as ws:
            for config in self.PORT_PROCESS_NAMES:
                cp = ws.run("profile", "--config", config, "--browser", "--process", "ui",
                             "--mode", "samply", "--dry-run")
                self.assertEqual(cp.returncode, 0, f"{config}: {cp.stdout}")
                self.assertIn("WEBKIT_MINI_BROWSER_PREFIX=", cp.stdout, f"{config}: {cp.stdout}")
                self.assertIn("samply record", cp.stdout, f"{config}: {cp.stdout}")
                self.assertIn("run-minibrowser", cp.stdout, f"{config}: {cp.stdout}")
                # the prefix wraps MiniBrowser, not the launcher: samply must not
                # also appear as a bare prefix of the run-minibrowser line itself
                launch_line = next(l for l in cp.stdout.splitlines() if "run-minibrowser" in l)
                self.assertNotIn("samply", launch_line, f"{config}: {cp.stdout}")

    def test_process_web_network_gpu_attach_by_pid_after_launch(self):
        """--process web (the default)/network/gpu launch MiniBrowser, then
        `samply record -p <pid>` the newest matching process by name"""
        with fake_workspace() as ws:
            for config, names in self.PORT_PROCESS_NAMES.items():
                for process, proc_name in names.items():
                    args = ["profile", "--config", config, "--browser", "--mode", "samply", "--dry-run"]
                    if process != "web":
                        args += ["--process", process]
                    cp = ws.run(*args)
                    self.assertEqual(cp.returncode, 0, f"{config}/{process}: {cp.stdout}")
                    self.assertIn(proc_name, cp.stdout, f"{config}/{process}: {cp.stdout}")
                    self.assertIn("pgrep", cp.stdout, f"{config}/{process}: {cp.stdout}")
                    self.assertIn("samply record", cp.stdout, f"{config}/{process}: {cp.stdout}")
                    self.assertIn(" -p ", cp.stdout, f"{config}/{process}: {cp.stdout}")

    def test_default_process_is_web(self):
        """with no --process at all, --browser --mode samply attaches to the web process"""
        with fake_workspace() as ws:
            cp = ws.run("profile", "--config", "wpe-release", "--browser", "--mode", "samply", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("WPEWebProcess", cp.stdout, cp.stdout)
            self.assertNotIn("WPENetworkProcess", cp.stdout, cp.stdout)
            self.assertNotIn("WPEGPUProcess", cp.stdout, cp.stdout)

    def test_apple_port_browser_path_is_unchanged(self):
        """mac-* --browser still profiles MiniBrowser directly, exactly as before"""
        with fake_workspace() as ws:
            cp = ws.run("profile", "--config", "mac-release", "--browser", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("MiniBrowser.app/Contents/MacOS/MiniBrowser", cp.stdout, cp.stdout)
            self.assertIn("--url", cp.stdout, cp.stdout)
            self.assertNotIn("WEBKIT_MINI_BROWSER_PREFIX", cp.stdout, cp.stdout)

            # --process is not wired up for the Apple ports -- refused, naming the
            # --attach remedy, rather than silently ignored
            cp = ws.run("profile", "--config", "mac-release", "--browser", "--process", "web", "--dry-run")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("not wired up for the Apple ports", cp.stdout, cp.stdout)
            self.assertIn("--attach", cp.stdout, cp.stdout)

    def test_process_with_a_non_wrapper_mode_refuses_naming_the_remedy(self):
        """--process with --mode sampling (an environment-variable mode, not a
        process wrapper) is refused, naming --mode samply as the remedy"""
        with fake_workspace() as ws:
            cp = ws.run("profile", "--config", "gtk-release", "--browser", "--process", "ui",
                         "--mode", "sampling", "--dry-run")
            self.assertNotEqual(cp.returncode, 0, cp.stdout)
            self.assertIn("meaningless", cp.stdout, cp.stdout)
            self.assertIn("--mode samply", cp.stdout, cp.stdout)

            # sampling/bytecode --browser on the CMake ports keeps working when
            # --process is not given at all -- only the explicit combination refuses
            cp = ws.run("profile", "--config", "gtk-release", "--browser", "--mode", "sampling", "--dry-run")
            self.assertEqual(cp.returncode, 0, cp.stdout)

    def test_unknown_process_value_is_refused(self):
        """unknown --process is refused naming the valid ones"""
        cp = run_profile("--process", "bogus", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("no such process", cp.stdout, cp.stdout)
        for p in ("ui", "web", "network", "gpu"):
            self.assertIn(p, cp.stdout, f"refusal does not name '{p}': {cp.stdout}")

    def test_process_without_browser_is_refused(self):
        """--process without --browser names nothing to point at, and is refused"""
        cp = run_profile("--process", "ui", "--dry-run", env={"WK_NAME": "test-ws"})
        self.assertNotEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("--browser", cp.stdout, cp.stdout)

    def test_heaptrack_massif_browser_on_cmake_ports_refuses_owed(self):
        """heaptrack/massif --browser on the CMake ports refuses -- not wired up
        yet, rather than silently building a broken command line"""
        with fake_workspace() as ws:
            for mode in ("heaptrack", "massif"):
                cp = ws.run("profile", "--config", "gtk-release", "--browser", "--mode", mode, "--dry-run")
                self.assertNotEqual(cp.returncode, 0, f"{mode}: {cp.stdout}")
                self.assertIn("not wired up", cp.stdout, f"{mode}: {cp.stdout}")


class QuiesceHelperVerbTest(unittest.TestCase):
    def test_perf_verbs_are_in_the_fixed_verb_list(self):
        """the helper's new verb is in its fixed list"""
        # static: read admin/wk-quiesce-priv rather than run it -- it must
        # run as root and this suite never elevates.
        text = QUIESCE_PRIV.read_text()
        self.assertIn("Linux:perf-on)", text, "perf-on is not in the Linux dispatch table")
        self.assertIn("Linux:perf-off)", text, "perf-off is not in the Linux dispatch table")
        # Fixed verb, not a passthrough: the dispatch table matches a literal
        # case label, never a variable -- "$action" or "$1" appearing where
        # 'perf-on'/'perf-off' should be would mean an argument reaches a
        # command, which is exactly what CLAUDE.md's shape rule forbids.
        self.assertNotIn('"$1")', text)
        self.assertNotIn('"$action")', text)


class ContainerfileToolsTest(unittest.TestCase):
    def test_containerfiles_install_every_named_tool(self):
        """the Containerfile installs each named tool"""
        # static: these are build recipes, not run here.
        for path in CONTAINERFILES:
            text = path.read_text()
            for tool in ("heaptrack", "valgrind", "sysprof"):
                self.assertIn(tool, text, f"{path} does not install {tool}")
            # samply ships no .deb; it is a pinned, checksummed release
            # fetch, so the source is the thing to check for instead of a
            # package name.
            self.assertIn("mstange/samply", text, f"{path} does not fetch samply from its pinned release")
            self.assertIn("sha256", text, f"{path} fetches samply without a checksum")

    def test_firstrun_installs_every_named_tool(self):
        """container/firstrun.sh -- the mechanism that actually reaches the
        workspace 'wk profile' runs against -- carries the same set."""
        text = (REPO / "container" / "firstrun.sh").read_text()
        for tool in ("heaptrack", "valgrind", "sysprof"):
            self.assertIn(tool, text, f"firstrun.sh does not install {tool}")
        self.assertIn("mstange/samply", text)


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
        """the plotter parses a small synthetic log and writes an SVG/HTML"""
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
            # --max-seconds 1.5 drops the t=2.0 sample -- nothing downstream
            # of that point should appear as a plotted value.
            self.assertNotIn("shared", content)  # --include excluded it too

    def test_plots_a_synthetic_log_to_html(self):
        """a non-.svg --out wraps the same chart in a minimal HTML page"""
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
        """an --include naming a column absent from the log's header is refused"""
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
