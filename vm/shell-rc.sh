#!/usr/bin/env bash
#
# Point a macOS guest's shells at the fleet's shared rc. Runs *in* the guest,
# with the wk-tools directory as $1: over ssh from targets/vm.sh every time a
# guest starts, and once from vm/provision-base.sh while the golden base is
# being made.
#
# Two callers because the two need different things and this is one answer to
# both: the base bakes it in for every clone, and the per-start run converges a
# clone whose links point at nothing -- the rc it points at lives in
# $HOME/wk-tools, which every start resets to this tree's commit (tools_push,
# lib/tools.sh), so there is nothing here a clone could have got permanently
# wrong.
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

# Two lines, and they come from different places on purpose. The shared rc
# lives in the guest's own copy of wk-tools, which only a build refreshes.
# ~/.wk-egress is written by the host on every start (_set_guest_egress,
# targets/vm.sh) and absent on a guest that needs no proxy, so a guest is never
# left without egress waiting for a build to bring it a newer rc.
line=". \"$TOOLS/shell/bashrc\""
egress='if [ -r "$HOME/.wk-egress" ]; then . "$HOME/.wk-egress"; fi'

# The claude.ai login credential the host writes into ~/.claude on every start
# (_write_agent_secrets, targets/vm.sh), made the one the Claude CLI reads.
# This is a Mac, so the CLI's first choice is a login Keychain item -- which no
# ssh session and no editor's remote server has unlocked, and which a `claude`
# run in here could have written a stale copy into. Naming the store directory
# also names the Keychain item: the CLI appends a hash of this directory to the
# item's service name, so the lookup misses and the file is what is used.
# Here rather than in the shared rc: that one is read on every machine in the
# fleet, including the workstation whose real Keychain item must keep working.
claudecred='export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"'

add() { # <rc> <line> <what it is>
    grep -qF "$2" "$1" 2>/dev/null && return 0
    printf '\n# wk-tools: %s\n%s\n' "$3" "$2" >> "$1"
}

for rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    [ -f "$rc" ] || : > "$rc"
    # Stanzas a guest provisioned before this file existed still carries, both
    # of them now owned by something else. The PATH one would put
    # ~/.local/bin on PATH twice (shell/path.sh). The egress one holds the
    # proxy address baked in at provisioning time -- a second copy of a fact
    # the host now writes into ~/.wk-egress on every start, and one that goes
    # stale the moment the address changes.
    if grep -q -e 'wk-tools: PATH' -e 'wk-tools: egress goes through' "$rc" 2>/dev/null; then
        tmp=$(mktemp)
        grep -v -e 'wk-tools: PATH' -e 'export PATH="$HOME/.local/bin:$PATH"' \
                -e 'wk-tools: egress goes through' \
                -e '^export http_proxy=' -e '^export https_proxy=' \
                -e '^export HTTP_PROXY=' -e '^export HTTPS_PROXY=' \
                -e '^export no_proxy=' -e '^export NO_PROXY=' \
                "$rc" > "$tmp" || true
        mv "$tmp" "$rc"
    fi
    add "$rc" "$line"   "shared shell configuration"
    add "$rc" "$egress" "this machine's egress proxy, when it has one"
    add "$rc" "$claudecred" "the Claude credential the host writes here, not a Keychain"
done
