#!/bin/sh
# What a wk shell has on PATH, in one place, sourced with WK_TOOLS_DIR set to the checkout. bin/ and never the checkout root: a directory found on PATH is a command to zsh, which execs it, gets EACCES and reports "permission denied", so a root on PATH shadows every top-level directory name.

_wk_path_add() {   # an entry already on PATH is moved to the front, an inherited container/bin behind /usr/bin being a build wall the shell never reaches
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
_wk_path_add "$WK_TOOLS_DIR/container/bin"   # the build wall (wk-build-wall and the symlinks named after the tools it wraps), which works only ahead of /usr/bin
_wk_path_add "$WK_TOOLS_DIR/bin"

export PATH
unset -f _wk_path_add
