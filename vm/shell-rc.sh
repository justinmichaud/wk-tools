#!/usr/bin/env bash
#
# Point a macOS guest's shells at the fleet's shared rc. Runs *in* the guest,
# with the wk-tools directory as $1: over ssh from targets/vm.sh every time a
# guest starts, and once from vm/provision-base.sh while the golden base is
# being made.
#
# Two callers because the two need different things and this is one answer to
# both: the base bakes it in for every clone, and the per-start run converges a
# guest that was cloned before this file existed -- the rc it points at lives
# in $HOME/wk-tools, which `wk build` re-rsyncs anyway, so there is nothing
# here a clone could have got permanently wrong.
#
# The rc itself (shell/bashrc -> shell/path.sh) is where PATH is decided:
# ~/.local/bin for the Claude launcher, bin/ for `wk`. Without it `wk build`
# inside a guest is "command not found".
#
# All four files, and that is the point: `bash -lc` (every t_exec) reads
# .bash_profile, a login zsh reads .zprofile, and an editor's terminal pane is
# no login shell at all -- it reads .zshrc alone.
#
# bash 3.2: the macOS system bash, and there is no other one here.

set -euo pipefail

TOOLS="${1:-${WK_TOOLS_DIR:-$HOME/wk-tools}}"
[ -f "$TOOLS/shell/bashrc" ] || {
    echo "vm/shell-rc.sh: no shell/bashrc under $TOOLS" >&2
    exit 1
}

line=". \"$TOOLS/shell/bashrc\""
for rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    [ -f "$rc" ] || : > "$rc"
    # The stanza this replaces, from a guest provisioned before shell/bashrc
    # was wired in here. Left behind it would put ~/.local/bin on PATH twice.
    if grep -q 'wk-tools: PATH' "$rc" 2>/dev/null; then
        tmp=$(mktemp)
        grep -v -e 'wk-tools: PATH' -e 'export PATH="$HOME/.local/bin:$PATH"' "$rc" > "$tmp" || true
        mv "$tmp" "$rc"
    fi
    grep -qF "$TOOLS/shell/bashrc" "$rc" 2>/dev/null && continue
    printf '\n# wk-tools shared shell configuration\n%s\n' "$line" >> "$rc"
done
