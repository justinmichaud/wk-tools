"""helix and Zed config parse and point clangd at a real build directory, and firstrun's editor installs fetch the
matching release or report an architecture they do not handle."""
import json
import re
import subprocess
import tomllib
import unittest

from tests.support import REPO, DISPATCH_VARS, run, stub_path

HELIX_DIR = REPO / "container" / "helix"
FIRSTRUN = REPO / "container" / "firstrun.sh"
ZED_SETTINGS = REPO / "dotfiles" / "zed" / "settings.json"
# The build directory of jsc-release, the config every container workspace starts on.
BUILD_DIR = "WebKitBuild/JSCOnly/Release"


def zed_settings():
    """Zed's settings.json less its `//` comments; a `//` inside a string is content."""
    text = re.sub(r'(?m)^((?:[^"/\n]|"(?:\\.|[^"\\])*")*)//.*$', r"\1", ZED_SETTINGS.read_text())
    return json.loads(text)


def compile_commands_dir(args):
    (hit,) = [a for a in args if a.startswith("--compile-commands-dir=")]
    return hit


class TestEditorConfig(unittest.TestCase):
    def test_helix_parses_and_points_clangd_at_the_build(self):
        with open(HELIX_DIR / "config.toml", "rb") as f:
            tomllib.load(f)
        with open(HELIX_DIR / "languages.toml", "rb") as f:
            doc = tomllib.load(f)
        self.assertIn(BUILD_DIR, compile_commands_dir(doc["language-server"]["clangd"].get("args", [])))

    def test_zed_parses_trusts_projects_and_points_clangd_at_the_build(self):
        doc = zed_settings()
        self.assertIs(doc["session"]["trust_all_projects"], True)
        self.assertIn(BUILD_DIR, compile_commands_dir(doc["lsp"]["clangd"]["binary"]["arguments"]))


# firstrun.sh's own log()/warn() are one-liners the sed range below cannot lift.
_LOG_STUBS = """
log()  { printf '[firstrun] %s\\n' "$*"; }
warn() { printf '[firstrun] warning: %s\\n' "$*" >&2; }
"""


def _run_install_fn(func, arch):
    """firstrun.sh's `func` alone, under a stubbed `uname -m` and a curl/sudo that log and fail."""
    scripts = {
        "uname": f'case "$1" in -m) echo "{arch}" ;; *) exit 1 ;; esac\n',
        "curl": 'printf "%s\\n" "$*" >> "$CURL_LOG"; exit 1\n',
        "sudo": 'printf "%s\\n" "$*" >> "$SUDO_LOG"; exit 1\n',
        "install": 'printf "%s\\n" "$*" >> "$INSTALL_LOG"\n',
    }
    with stub_path(scripts) as binp:
        body = subprocess.run(["sed", "-n", f"/^{func}()/,/^}}/p", str(FIRSTRUN)],
                              capture_output=True, text=True).stdout
        cp = subprocess.run(
            ["/bin/bash", "-c", f"set -uo pipefail\n{_LOG_STUBS}\n{body}\n{func}\n"],
            capture_output=True, text=True, timeout=30,
            env={"PATH": f"{binp}:/usr/bin:/bin", "CURL_LOG": str(binp / "curl.log"),
                 "SUDO_LOG": str(binp / "sudo.log"), "INSTALL_LOG": str(binp / "install.log")})
        logs = [(binp / n).read_text().splitlines() if (binp / n).exists() else [] for n in ("curl.log", "sudo.log")]
        return cp, logs[0], logs[1]


class TestEditorInstallArch(unittest.TestCase):
    RELEASES = {
        "_install_helix": {"x86_64": "helix-25.07-x86_64-linux.tar.xz", "aarch64": "helix-25.07-aarch64-linux.tar.xz"},
        "_install_lazygit": {"x86_64": "lazygit_0.64.1_linux_x86_64.tar.gz"},
    }

    def test_a_supported_arch_fetches_the_matching_release(self):
        for func, releases in self.RELEASES.items():
            for arch, tarball in releases.items():
                with self.subTest(func=func, arch=arch):
                    _cp, curls, _ = _run_install_fn(func, arch)
                    self.assertEqual(len(curls), 1, curls)
                    self.assertIn(tarball, curls[0])

    def test_an_unsupported_arch_is_reported_and_nothing_is_fetched(self):
        for func in self.RELEASES:
            with self.subTest(func=func):
                cp, curls, sudos = _run_install_fn(func, "armv7l")
                self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
                self.assertEqual((curls, sudos), ([], []))
                self.assertIn("armv7l", cp.stdout + cp.stderr)
                self.assertIn("not installed", cp.stdout + cp.stderr)


class TestZedInheritsNoDispatcherVariables(unittest.TestCase):
    """Every terminal Zed opens inherits its environment, so `wk zed` starts it without the dispatcher's variables."""

    def test_wk_zed_tools_starts_zed_without_wk_variables(self):
        with stub_path({"zed": 'env > "$ZED_ENV_LOG"\n'}) as binp:
            log = binp / "zed.env"
            cp = run("zed", "--tools", env={
                "PATH": f"{binp}:/usr/bin:/bin",
                "ZED_ENV_LOG": str(log),
                "WK_NAME": "leaked-ws", "WK_TARGET": "leaked-target", "WK_TARGET_KIND": "remote",
            })
            self.assertEqual(cp.returncode, 0, cp.stdout)
            env = dict(l.split("=", 1) for l in log.read_text().splitlines() if "=" in l)
        self.assertEqual(sorted(k for k in DISPATCH_VARS if k in env), [])


if __name__ == "__main__":
    unittest.main()
