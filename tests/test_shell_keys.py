"""shell/bashrc's line editor: Home/End, the delete keys and word movement stay bound whichever keymap zsh starts in
(viins when $EDITOR contains "vi"), and bash binds word movement too."""
import re
import shutil
import subprocess
import unittest

from tests.support import REPO, WkTest

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
