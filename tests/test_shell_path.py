"""The shell a wk machine gets: shell/path.sh's PATH and bin/ directory, shell/bashrc's line editor keys, and the
helix and Zed configs with firstrun's editor installs."""
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
import unittest

from tests.support import FLEET_ENV, REPO, WkTest, stub_path

sys.path.insert(0, str(REPO / "lib"))
from wk import fleet  # noqa: E402


def path_from(rc, home):
    """The PATH a non-interactive shell ends up with after sourcing `rc` from a stripped environment."""
    cp = subprocess.run(
        ["bash", "-c", f'. "{rc}" && printf %s "$PATH"'],
        cwd=str(REPO),
        env={"HOME": home, "PATH": "/usr/bin:/bin"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
    )
    assert cp.returncode == 0, cp.stdout
    return cp.stdout.split(":")


class TestBinDir(WkTest):
    def test_bin_holds_only_a_relative_wk_link(self):
        self.assertEqual(sorted(p.name for p in (REPO / "bin").iterdir()), ["wk"])
        self.assertEqual(os.readlink(REPO / "bin" / "wk"), "../wk")

    def test_an_overlay_root_of_symlinks_stays_its_own_root(self):
        """A WK_ROOT built out of symlinks to another checkout answers from its own registry."""
        root = self.tmp / "overlay"
        (root / "machines").mkdir(parents=True)
        for entry in REPO.iterdir():
            if entry.name in ("machines", ".git", "__pycache__"):
                continue
            (root / entry.name).symlink_to(entry)
        (root / "machines" / "overlaybox.conf").write_text(
            "kind=build\ndriver=remote\nhost=overlaybox\nroot=/tmp/x\n")

        env = {"HOME": str(self.tmp), "PATH": "/usr/bin:/bin"}
        cp = subprocess.run([str(root / "wk"), "key", "sudo", "status", "--on", "overlaybox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertNotIn("unknown place", cp.stdout)
        real = fleet.Fleet(REPO, FLEET_ENV).names(fleet.PLACE_KINDS)
        cp = subprocess.run([str(root / "wk"), "key", "sudo", "status", "--on", "nosuchbox"],
                            cwd="/", env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertIn("overlaybox", cp.stdout)
        for n in real:
            self.assertNotIn(n, cp.stdout)

    def test_wk_finds_its_root_through_the_symlink(self):
        cp = subprocess.run(
            [str(REPO / "bin" / "wk"), "doctor", "--probe-tools"],
            cwd="/", env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        self.assertIn("sha=", cp.stdout)


class TestPath(WkTest):
    def setUp(self):
        super().setUp()
        self.home = str(self.tmp)
        os.makedirs(os.path.join(self.home, ".local", "bin"))

    def test_bashrc_puts_bin_and_local_bin_on_path_never_the_root_and_keeps_the_rest_in_order(self):
        got = path_from(REPO / "shell" / "bashrc", self.home)
        for want in (str(REPO / "bin"), str(REPO / "container" / "bin"),
                     os.path.join(self.home, ".local", "bin")):
            self.assertIn(want, got)
        self.assertNotIn(str(REPO), got)
        self.assertLess(got.index("/usr/bin"), got.index("/bin"), got)

    def test_path_is_added_once(self):
        cp = subprocess.run(
            ["bash", "-c", f'. "{REPO}/shell/bashrc" && . "{REPO}/shell/bashrc" && printf %s "$PATH"'],
            cwd=str(REPO), env={"HOME": self.home, "PATH": "/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        entries = cp.stdout.split(":")
        self.assertEqual(entries.count(str(REPO / "bin")), 1, cp.stdout)

    def test_an_inherited_entry_moves_ahead_of_usr_bin_so_ninja_is_the_walls(self):
        binp = str(REPO / "container" / "bin")
        cp = subprocess.run(["bash", "-c", f'. "{REPO}/shell/bashrc" && printf "%s\\n" "$PATH" && command -v ninja'],
                            cwd=str(REPO), env={"HOME": self.home, "PATH": f"/usr/bin:{binp}:/bin"},
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60)
        path, ninja = cp.stdout.strip().splitlines()[-2:]
        entries = path.split(":")
        self.assertEqual(1, entries.count(binp), cp.stdout)
        self.assertLess(entries.index(binp), entries.index("/usr/bin"), cp.stdout)
        self.assertEqual(binp + "/ninja", ninja)

    def test_local_bin_absent_is_not_added(self):
        shutil.rmtree(os.path.join(self.home, ".local"))
        got = path_from(REPO / "shell" / "bashrc", self.home)
        self.assertNotIn(os.path.join(self.home, ".local", "bin"), got)

    @unittest.skipIf(shutil.which("zsh") is None, "no zsh on this machine")
    def test_zsh_does_not_exec_a_repo_directory_as_a_command(self):
        got = ":".join(path_from(REPO / "shell" / "bashrc", self.home))
        cp = subprocess.run(
            ["zsh", "-f", "-c", "claude --version"],
            cwd=str(REPO), env={"HOME": self.home, "PATH": got},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
        self.assertNotEqual(cp.returncode, 126, cp.stdout)
        self.assertNotIn("permission denied", cp.stdout.lower())

    def test_the_workspace_only_tools_go_on_path_only_in_a_workspace(self):
        ws = str(REPO / "container" / "bin" / "ws")
        self.assertNotIn(ws, path_from(REPO / "shell" / "bashrc", self.home))
        open(os.path.join(self.home, ".wk-workspace"), "w").close()
        self.assertIn(ws, path_from(REPO / "shell" / "bashrc", self.home))


RC = REPO / "shell" / "bashrc"


# The keys a person expects to work, and every encoding a terminal may send
# them in: application cursor mode (^[O…) is what zle's own `smkx` asks the
# terminal for, so it is not optional.
WANTED = {
    "beginning-of-line": ["^[[H", "^[OH", "^[[1~", "^[[7~"],
    "end-of-line":       ["^[[F", "^[OF", "^[[4~", "^[[8~"],
    "delete-char":       ["^[[3~"],
    "backward-delete-char": ["^?", "^H"],
    "backward-word": ["^[[1;3D", "^[^[[D", "^[[1;5D"],
    "forward-word":  ["^[[1;3C", "^[^[[C", "^[[1;5C"],
}


# Raw bytes: `bind -p` renders a leading ESC as `\e` or `\M-` depending on the terminal's meta setting.
BASH_WANTED = {
    "backward-word": ["\x1b[1;3D", "\x1b\x1b[D", "\x1b[1;5D"],
    "forward-word":  ["\x1b[1;3C", "\x1b\x1b[C", "\x1b[1;5C"],
}


def decode_bind_p_keyseq(seq):
    """The raw bytes a `bind -p` quoted key spec represents."""
    out = bytearray()
    i = 0
    while i < len(seq):
        if seq[i] == "\\" and i + 1 < len(seq):
            nxt = seq[i + 1]
            if nxt == "e":
                out.append(0x1B); i += 2
            elif nxt == "C" and seq[i + 2:i + 3] == "-":
                out.append(ord(seq[i + 3].upper()) & 0x1F); i += 4
            elif nxt == "M" and seq[i + 2:i + 3] == "-":
                out.append(0x1B); i += 3
            else:
                out.append(ord(nxt)); i += 2
        else:
            out.append(ord(seq[i])); i += 1
    return bytes(out)


def bindkeys(editor, home):
    """Every binding an interactive zsh has after sourcing the rc; `editor` decides its startup keymap."""
    cp = subprocess.run(
        ["zsh", "-f", "-i", "-c", f'source "{RC}"; bindkey'],
        cwd=str(REPO),
        env={"HOME": home, "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "TERM": "xterm-256color", "EDITOR": editor, "VISUAL": editor},
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
    )
    assert cp.returncode == 0, cp.stdout
    out = {}
    for line in cp.stdout.splitlines():
        if line.startswith('"'):
            seq, _, action = line[1:].partition('" ')
            out[seq] = action.strip()
    return out


@unittest.skipUnless(shutil.which("zsh"), "no zsh on this machine")
class TestZshKeys(WkTest):
    def test_keys_are_bound_whichever_keymap_zsh_starts_in(self):
        for editor in ("vim", "hx"):
            binds = bindkeys(editor, str(self.tmp))
            for action, seqs in WANTED.items():
                for seq in seqs:
                    with self.subTest(editor=editor, key=seq):
                        self.assertEqual(
                            binds.get(seq), action,
                            f"EDITOR={editor}: {seq} is not bound to {action}")


@unittest.skipUnless(shutil.which("bash"), "no bash on this machine")
class TestBashKeys(WkTest):
    def test_alt_and_ctrl_arrow_word_movement_bound(self):
        cp = subprocess.run(
            ["bash", "--noprofile", "--norc", "-i", "-c",
             f'source "{RC}"; bind -p | grep -F word'],
            cwd=str(REPO),
            env={"HOME": str(self.tmp), "NO_ZSH": "1",
                 "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
                 "TERM": "xterm-256color"},
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=120,
        )
        self.assertEqual(cp.returncode, 0, cp.stdout)
        binds = {}
        for line in cp.stdout.splitlines():
            m = re.match(r'"((?:[^"\\]|\\.)*)": (\S+)', line)
            if m:
                binds[decode_bind_p_keyseq(m.group(1))] = m.group(2)
        for action, seqs in BASH_WANTED.items():
            for seq in seqs:
                with self.subTest(key=repr(seq)):
                    self.assertEqual(
                        binds.get(seq.encode()), action,
                        f"{seq!r} is not bound to {action}")


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
