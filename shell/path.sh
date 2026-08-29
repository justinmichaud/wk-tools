#!/bin/sh
# wk-tools/shell/path.sh -- what a wk shell has on PATH, decided in one place.
#
# Sourced with WK_TOOLS_DIR set to the checkout. Two callers, which are the two
# ways a command here acquires a PATH: shell/bashrc, read by every interactive
# and login shell, and container/proxy/ensure-bridge.sh, which every exec into
# a container goes through and which reads no rc file at all.
#
# bin/ goes on PATH, never the checkout root. A directory found on PATH is a
# command to zsh: it execs it, gets EACCES and reports "permission denied", so
# a root on PATH turns every top-level directory name -- claude, build, image,
# shell, docs -- into a command that fails that way, shadowing the real one.
# bin/ holds the one entry point, `wk`. (bash skips directories in its own PATH
# search, which is why `wk claude`, which runs `bash -lc`, never saw this.)
#
# ~/.local/bin is where the Claude installer puts its launcher (a symlink into
# ~/.local/share/claude/versions); nothing else on these machines puts it on
# PATH, so `claude` is unrunnable by name without this.

# Prepended in reverse of the order they end up in: bin, container/bin, then
# ~/.local/bin, ahead of whatever the machine already had.
_wk_path_add() {
    [ -d "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

_wk_path_add "$HOME/.local/bin"
# container/bin's workspace-only helpers (strip-addresses, commit-count, ...)
# are scripts rather than verbs so the checkout can see them -- needs PATH.
_wk_path_add "$WK_TOOLS_DIR/container/bin"
_wk_path_add "$WK_TOOLS_DIR/bin"

export PATH
unset -f _wk_path_add
