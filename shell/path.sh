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
# search, which is why `wk ai claude`, which runs `bash -lc`, never saw this.)
#
# ~/.local/bin is where the Claude installer puts its launcher (a symlink into
# ~/.local/share/claude/versions); nothing else on these machines puts it on
# PATH, so `claude` is unrunnable by name without this.

# Prepended in reverse of the order they end up in: bin, container/bin, then
# ~/.local/bin, ahead of whatever the machine already had.
#
# An entry already on PATH is *moved* to the front, not left where it is. This
# decides which binary a name resolves to, and an inherited container/bin
# sitting behind /usr/bin is a build wall the shell never reaches -- silently,
# since `ninja` still runs. Removing and prepending gives the same PATH from
# either starting point.
_wk_path_add() {
    [ -d "$1" ] || return 0
    _wk_pa_kept=""
    _wk_pa_rest="$PATH"
    while [ -n "$_wk_pa_rest" ]; do
        _wk_pa_head=${_wk_pa_rest%%:*}
        case "$_wk_pa_rest" in
            *:*) _wk_pa_rest=${_wk_pa_rest#*:} ;;
            *)   _wk_pa_rest="" ;;
        esac
        [ "$_wk_pa_head" = "$1" ] && continue
        _wk_pa_kept="${_wk_pa_kept:+$_wk_pa_kept:}$_wk_pa_head"
    done
    PATH="$1${_wk_pa_kept:+:$_wk_pa_kept}"
    unset _wk_pa_kept _wk_pa_rest _wk_pa_head
}

_wk_path_add "$HOME/.local/bin"
# container/bin's workspace-only helpers (strip-addresses, commit-count, ...)
# are scripts rather than verbs so the checkout can see them -- needs PATH.
# It also holds the build wall (container/bin/wk-build-wall and the symlinks
# named after the tools it wraps), which only works while this stays *ahead* of
# /usr/bin: an agent's `ninja` has to reach the wall before the real ninja.
_wk_path_add "$WK_TOOLS_DIR/container/bin"
_wk_path_add "$WK_TOOLS_DIR/bin"

export PATH
unset -f _wk_path_add
