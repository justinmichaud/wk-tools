"""helix and clangd config: container/helix/*.toml and dotfiles/zed/settings.json
parse, clangd is pointed at a real build directory in both, and
container/firstrun.sh's _install_helix names every architecture it handles
and reports (rather than silently skipping) one it does not.

Run: python3 -m unittest tests.test_editors -v
"""
import json
import subprocess
import tomllib
import unittest
from pathlib import Path

from tests.support import REPO, dispatch_vars, run, stub_path

HELIX_DIR = REPO / "container" / "helix"
FIRSTRUN = REPO / "container" / "firstrun.sh"
ZED_SETTINGS = REPO / "dotfiles" / "zed" / "settings.json"


def _strip_json_comments(text):
    """Zed's settings.json carries `//` line comments (JSON5-ish); this
    strips them without disturbing one inside a string, so the result is
    plain JSON. String-aware rather than a regex: a `//` inside a path or a
    URL is content, not a comment."""
    out = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _lift(path, func):
    """A function's body, sed'd out of a shell file -- the technique
    tests/test_wifi_seed.py and tests/test_quick.py use to call one function
    from a script directly rather than sourcing (and running) the whole
    file. Only works for a function whose closing brace is on its own line,
    which _install_helix and _install_lazygit both are."""
    return subprocess.run(
        ["sed", "-n", f"/^{func}()/,/^}}/p", str(path)],
        capture_output=True, text=True,
    ).stdout


class TestHelixConfigParses(unittest.TestCase):
    def test_config_toml_parses(self):
        with open(HELIX_DIR / "config.toml", "rb") as f:
            tomllib.load(f)

    def test_languages_toml_parses(self):
        with open(HELIX_DIR / "languages.toml", "rb") as f:
            doc = tomllib.load(f)
        self.assertIn("language-server", doc)
        self.assertIn("clangd", doc["language-server"], "languages.toml sets up no clangd server")

    def test_clangd_names_a_compile_commands_dir(self):
        """clangd needs pointing at a build directory: WebKit's tree carries
        no compile_commands.json at its root, only inside whichever build
        directory produced one (build/configs.sh's config_build_dir)."""
        with open(HELIX_DIR / "languages.toml", "rb") as f:
            doc = tomllib.load(f)
        args = doc["language-server"]["clangd"].get("args", [])
        hits = [a for a in args if a.startswith("--compile-commands-dir=")]
        self.assertEqual(len(hits), 1, f"expected exactly one --compile-commands-dir= arg, got {args}")
        # jsc-release is the config every container workspace starts on
        # (container/firstrun.sh's workspace marker); build/configs.sh maps
        # it to WebKitBuild/JSCOnly/Release.
        self.assertIn("WebKitBuild/JSCOnly/Release", hits[0])


class TestZedSettingsParses(unittest.TestCase):
    def test_settings_json_parses(self):
        text = ZED_SETTINGS.read_text()
        doc = json.loads(_strip_json_comments(text))
        self.assertIn("lsp", doc)

    def test_zed_opens_projects_trusted(self):
        """`wk zed` opens a checkout with every feature on: Zed's Restricted
        Mode is lifted only by a per-worktree click or by this setting, and
        the CLI has no flag for it."""
        doc = json.loads(_strip_json_comments(ZED_SETTINGS.read_text()))
        self.assertIs(doc["session"]["trust_all_projects"], True)

    def test_clangd_names_a_compile_commands_dir(self):
        text = ZED_SETTINGS.read_text()
        doc = json.loads(_strip_json_comments(text))
        args = doc["lsp"]["clangd"]["binary"]["arguments"]
        hits = [a for a in args if a.startswith("--compile-commands-dir=")]
        self.assertEqual(len(hits), 1, f"expected exactly one --compile-commands-dir= arg, got {args}")
        self.assertIn("WebKitBuild/JSCOnly/Release", hits[0])


# A minimal log()/warn() -- firstrun.sh's own are one-line functions
# (`log()  { printf ...; }`), which _lift's `/^}/`-anchored sed range cannot
# isolate (their closing brace shares the opening line), so they are
# reimplemented here rather than lifted.
_LOG_STUBS = """
log()  { printf '[firstrun] %s\\n' "$*"; }
warn() { printf '[firstrun] warning: %s\\n' "$*" >&2; }
"""


def _run_install_fn(func, arch, extra_scripts=None):
    """Runs container/firstrun.sh's `func` in isolation against a stubbed
    `uname -m` (the one input _install_helix/_install_lazygit branch on) and
    a `curl` that records its own arguments and fails -- proving, for a
    given arch, whether the function even tries a download and what URL it
    asks for, without needing a real network or a real release tarball."""
    scripts = {
        "uname": f'case "$1" in -m) echo "{arch}" ;; *) exit 1 ;; esac\n',
        "curl": 'printf "%s\\n" "$*" >> "$CURL_LOG"; exit 1\n',
        "sudo": 'printf "%s\\n" "$*" >> "$SUDO_LOG"; exit 1\n',
        "install": 'printf "%s\\n" "$*" >> "$INSTALL_LOG"\n',
    }
    scripts.update(extra_scripts or {})
    with stub_path(scripts) as binp:
        curl_log = binp / "curl.log"
        sudo_log = binp / "sudo.log"
        install_log = binp / "install.log"
        body = _lift(FIRSTRUN, func)
        script = f'''
set -uo pipefail
{_LOG_STUBS}
{body}
{func}
'''
        cp = subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True, text=True, timeout=30,
            env={
                "PATH": f"{binp}:/usr/bin:/bin",
                "CURL_LOG": str(curl_log),
                "SUDO_LOG": str(sudo_log),
                "INSTALL_LOG": str(install_log),
            },
        )
        curls = curl_log.read_text().splitlines() if curl_log.exists() else []
        sudos = sudo_log.read_text().splitlines() if sudo_log.exists() else []
        return cp, curls, sudos


class TestInstallHelixArch(unittest.TestCase):
    """container/firstrun.sh's _install_helix: docs/Nice to have/HANDOFF-helix.md
    -- an unsupported architecture used to fall through a bare `return 0`,
    a silent degrade indistinguishable from "already installed". It now logs
    why and continues."""

    def test_names_aarch64_and_x86_64(self):
        text = FIRSTRUN.read_text()
        body = _lift(FIRSTRUN, "_install_helix")
        self.assertIn("_install_helix", body, "could not lift _install_helix out of firstrun.sh")
        for arch in ("aarch64", "x86_64"):
            self.assertIn(arch, body, f"_install_helix does not name {arch}")
        self.assertIn("_install_helix", text)

    def test_supported_arch_fetches_the_matching_release(self):
        cp, curls, _sudos = _run_install_fn("_install_helix", "x86_64")
        self.assertEqual(cp.returncode, 1, cp.stdout + cp.stderr)  # curl was made to fail
        self.assertEqual(len(curls), 1, f"expected one curl call, got {curls}")
        self.assertIn("helix-25.07-x86_64-linux.tar.xz", curls[0])

        cp, curls, _sudos = _run_install_fn("_install_helix", "aarch64")
        self.assertEqual(len(curls), 1, f"expected one curl call, got {curls}")
        self.assertIn("helix-25.07-aarch64-linux.tar.xz", curls[0])

    def test_unsupported_arch_logs_and_does_not_fetch(self):
        """armhf (uname -m: armv7l) has no upstream helix release
        (github.com/helix-editor/helix/releases); this must be reported, not
        silently skipped, and must not attempt a download at all."""
        cp, curls, sudos = _run_install_fn("_install_helix", "armv7l")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(curls, [], "an unsupported arch should never invoke curl")
        self.assertEqual(sudos, [], "an unsupported arch should never invoke sudo/install")
        out = cp.stdout + cp.stderr
        self.assertIn("armv7l", out, f"the log line does not name the unsupported arch: {out!r}")
        self.assertIn("not installed", out, f"no explanation logged for the unsupported arch: {out!r}")


class TestInstallLazygitArch(unittest.TestCase):
    """Same shape as _install_helix, and the same fix: an unsupported arch
    is reported, not silent."""

    def test_names_aarch64_and_x86_64(self):
        body = _lift(FIRSTRUN, "_install_lazygit")
        self.assertIn("_install_lazygit", body, "could not lift _install_lazygit out of firstrun.sh")
        for arch in ("aarch64", "x86_64"):
            self.assertIn(arch, body, f"_install_lazygit does not name {arch}")
        self.assertIn("sha256", body, "_install_lazygit does not verify a checksum")

    def test_supported_arch_fetches_the_matching_release(self):
        cp, curls, _sudos = _run_install_fn("_install_lazygit", "x86_64")
        self.assertEqual(len(curls), 1, f"expected one curl call, got {curls}")
        self.assertIn("lazygit_0.64.1_linux_x86_64.tar.gz", curls[0])

    def test_unsupported_arch_logs_and_does_not_fetch(self):
        cp, curls, sudos = _run_install_fn("_install_lazygit", "armv7l")
        self.assertEqual(cp.returncode, 0, cp.stdout + cp.stderr)
        self.assertEqual(curls, [], "an unsupported arch should never invoke curl")
        self.assertEqual(sudos, [], "an unsupported arch should never invoke sudo/install")
        out = cp.stdout + cp.stderr
        self.assertIn("armv7l", out)
        self.assertIn("not installed", out)


if __name__ == "__main__":
    unittest.main()

class TestZedInheritsNoDispatcherVariables(unittest.TestCase):
    """Zed outlives `wk zed` and every terminal it opens inherits its
    environment; a WK_NAME/WK_TARGET left in it makes every later `wk` about
    that one workspace on that one machine (wk_exec_clean, lib/common.sh)."""

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
        leaked = sorted(k for k in dispatch_vars() if k in env)
        self.assertEqual(leaked, [], f"zed inherited {leaked}")
