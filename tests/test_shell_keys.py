"""shell/bashrc's zsh line editor: the keymap is chosen once, and Home/End
and the delete keys stay bound after it.

`bindkey -e` links a fresh emacs keymap over `main`, discarding anything bound
into the previous one -- so a binding made before the keymap is chosen survives
only on a machine that happened to start in emacs mode already. zsh starts in
viins when $EDITOR/$VISUAL contains "vi", which is what a host without helix
gets, so the order in that file is what makes Home behave the same everywhere.

Run: python3 -m unittest tests.test_shell_keys -v
"""
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
}


def bindkeys(editor, home):
    """Every binding an interactive zsh has after sourcing the rc, with
    $EDITOR/$VISUAL set to `editor` -- which is what decides whether zsh's
    startup keymap is emacs or viins."""
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
    # Both editors, because the bug this covers was invisible under one of
    # them: helix present -> emacs keymap -> the bindings happened to survive.
    def test_keys_are_bound_whichever_keymap_zsh_starts_in(self):
        for editor in ("vim", "hx"):
            binds = bindkeys(editor, str(self.tmp))
            for action, seqs in WANTED.items():
                for seq in seqs:
                    with self.subTest(editor=editor, key=seq):
                        self.assertEqual(
                            binds.get(seq), action,
                            f"EDITOR={editor}: {seq} is not bound to {action}")

    def test_nothing_chooses_a_keymap_after_the_keys_are_bound(self):
        """One keymap decision, above the bindings -- a second `bindkey -e`
        or -v anywhere below would throw them away again."""
        text = RC.read_text()
        choose = text.index("bindkey -e")
        first_bind = min(text.index(f'bindkey "{seq}"')
                         for seqs in WANTED.values() for seq in seqs)
        self.assertLess(choose, first_bind,
                        "shell/bashrc binds keys before it chooses the keymap")
        after = text[first_bind:]
        for bad in ("bindkey -e", "bindkey -v", "bindkey -A"):
            self.assertNotIn(bad, after,
                             f"shell/bashrc runs `{bad}` after binding keys")
