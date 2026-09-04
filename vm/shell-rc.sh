#!/usr/bin/env bash
#
# Point a macOS guest's shells at the fleet's shared rc. Runs in the guest with
# the wk-tools directory as $1, from vm/provision-base.sh while the golden base
# is made and over ssh from targets/vm.sh on every start.
#
# All four files, because `bash -lc` (every t_exec) reads .bash_profile, a login
# zsh reads .zprofile, and an editor's terminal pane reads .zshrc alone.
# bash 3.2: the macOS system bash, and there is no other one here.

set -euo pipefail

TOOLS="${1:-${WK_TOOLS_DIR:-$HOME/wk-tools}}"
[ -f "$TOOLS/shell/bashrc" ] || {
    echo "vm/shell-rc.sh: no shell/bashrc under $TOOLS" >&2
    exit 1
}

# The shared rc lives in the guest's own copy of wk-tools, which only a build
# refreshes; ~/.wk-egress is written by the host on every start.
line=". \"$TOOLS/shell/bashrc\""
egress='if [ -r "$HOME/.wk-egress" ]; then . "$HOME/.wk-egress"; fi'

# The guest's own claude.ai login: a credential the CLI rotates is never copied
# in from the host, so `claude auth login` runs in here once and what it leaves
# lands in this directory. ~/.claude-login and not ~/.claude, where the host
# removes the row's file on every start. On a Mac the CLI's first choice is a
# login Keychain item, which no ssh session has unlocked; naming the store
# directory also names the Keychain item, since the CLI appends a hash of the
# directory to the item's service name, so the lookup misses and the file is
# used. Here rather than in the shared rc, which the workstation reads too.
claudecred='export CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude-login"'

add() { # <rc> <line> <what it is>
    grep -qF "$2" "$1" 2>/dev/null && return 0
    printf '\n# wk-tools: %s\n%s\n' "$3" "$2" >> "$1"
}

for rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    [ -f "$rc" ] || : > "$rc"
    # Stanzas a guest may carry that something else now owns: `add` matches a
    # line, so a stanza saying the same thing differently would stay and be read
    # too. The PATH one puts ~/.local/bin on PATH twice; the egress one bakes in
    # a proxy address; the credential one points the CLI at ~/.claude.
    if grep -q -e 'wk-tools: PATH' -e 'wk-tools: egress goes through' \
               -e 'CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"' "$rc" 2>/dev/null; then
        tmp=$(mktemp)
        grep -v -e 'wk-tools: PATH' -e 'export PATH="$HOME/.local/bin:$PATH"' \
                -e 'wk-tools: egress goes through' \
                -e 'wk-tools: the Claude credential the host writes here' \
                -e 'CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude"' \
                -e '^export http_proxy=' -e '^export https_proxy=' \
                -e '^export HTTP_PROXY=' -e '^export HTTPS_PROXY=' \
                -e '^export no_proxy=' -e '^export NO_PROXY=' \
                "$rc" > "$tmp" || true
        mv "$tmp" "$rc"
    fi
    add "$rc" "$line"   "shared shell configuration"
    add "$rc" "$egress" "this machine's egress proxy, when it has one"
    add "$rc" "$claudecred" "the guest's own Claude login, not a Keychain"
done
