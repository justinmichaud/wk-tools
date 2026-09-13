#!/usr/bin/env bash

set -euo pipefail

TOOLS="${1:-${WK_TOOLS_DIR:-$HOME/wk-tools}}"
AGENT_RW="${2:?vm/shell-rc.sh: the directory the agent-rw share is mounted at (targets/vm.sh)}"
[ -f "$TOOLS/shell/bashrc" ] || {
    echo "vm/shell-rc.sh: no shell/bashrc under $TOOLS" >&2
    exit 1
}

line=". \"$TOOLS/shell/bashrc\""
egress='if [ -r "$HOME/.wk-egress" ]; then . "$HOME/.wk-egress"; fi'

# The CLI hashes this directory into its Keychain service name, so naming one no host item matches makes it fall back to a file an ssh session can read.
claudecred="export CLAUDE_SECURESTORAGE_CONFIG_DIR=\"$AGENT_RW\""

add() { # <rc> <line> <what it is>
    grep -qF "$2" "$1" 2>/dev/null && return 0
    printf '\n# wk-tools: %s\n%s\n' "$3" "$2" >> "$1"
}

for rc in "$HOME/.zshrc" "$HOME/.zprofile" "$HOME/.bash_profile" "$HOME/.bashrc"; do
    [ -f "$rc" ] || : > "$rc"
    if grep -q -e 'wk-tools: PATH' -e 'wk-tools: egress goes through' \
               -e 'CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude' "$rc" 2>/dev/null; then
        tmp=$(mktemp)
        grep -v -e 'wk-tools: PATH' -e 'export PATH="$HOME/.local/bin:$PATH"' \
                -e 'wk-tools: egress goes through' \
                -e 'wk-tools: the Claude credential the host writes here' \
                -e 'CLAUDE_SECURESTORAGE_CONFIG_DIR="$HOME/.claude' \
                -e 'wk-tools: the guest.s own Claude login' \
                -e '^export http_proxy=' -e '^export https_proxy=' \
                -e '^export HTTP_PROXY=' -e '^export HTTPS_PROXY=' \
                -e '^export no_proxy=' -e '^export NO_PROXY=' \
                "$rc" > "$tmp" || true
        mv "$tmp" "$rc"
    fi
    add "$rc" "$line"   "shared shell configuration"
    add "$rc" "$egress" "this machine's egress proxy, when it has one"
    add "$rc" "$claudecred" "the claude.ai login this host shares over virtiofs, not a Keychain"
done
