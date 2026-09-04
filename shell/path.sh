#!/bin/sh
# wk-tools/shell/path.sh -- what a wk shell has on PATH, decided in one place.
# Sourced with WK_TOOLS_DIR set to the checkout, by shell/bashrc and by
# container/proxy/ensure-bridge.sh, which reads no rc file at all.
#
# bin/ goes on PATH, never the checkout root: a directory found on PATH is a
# command to zsh, which execs it, gets EACCES and reports "permission denied",
# so a root on PATH shadows every top-level directory name. (bash skips
# directories in its own PATH search.)

# An entry already on PATH is moved to the front rather than left: an inherited
# container/bin behind /usr/bin is a build wall the shell never reaches.
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
# container/bin holds the build wall (wk-build-wall and the symlinks named after
# the tools it wraps), which works only while this stays ahead of /usr/bin.
_wk_path_add "$WK_TOOLS_DIR/container/bin"
_wk_path_add "$WK_TOOLS_DIR/bin"

export PATH
unset -f _wk_path_add
