set -euo pipefail

WK_ROOT="${WK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export WK_ROOT

if [ -t 2 ]; then
    _c_dim=$'\033[2m'; _c_red=$'\033[31m'; _c_yel=$'\033[33m'
    _c_grn=$'\033[32m'; _c_off=$'\033[0m'
else
    _c_dim=''; _c_red=''; _c_yel=''; _c_grn=''; _c_off=''
fi

log()  { [ -z "${WK_QUIET:-}" ] && printf '%s\n' "$*" >&2 || true; }
info() { [ -z "${WK_QUIET:-}" ] && printf '%s==>%s %s\n' "$_c_grn" "$_c_off" "$*" >&2 || true; }
warn() { printf '%swarning:%s %s\n' "$_c_yel" "$_c_off" "$*" >&2; }
err()  { printf '%serror:%s %s\n' "$_c_red" "$_c_off" "$*" >&2; }   # an error this command goes on past
die()  { err "$*"; exit 1; }
debug() { [ -n "${WK_DEBUG:-}" ] && printf '%s  %s%s\n' "$_c_dim" "$*" "$_c_off" >&2 || true; }

WK_CHANGES=0
changed() { WK_CHANGES=$((WK_CHANGES + 1)); info "$*"; }
unchanged() { debug "ok: $*"; }

# Findings, tab-separated `<state> <what> <remedy>`, state ok | wrong | note, one per line -- a remedy wrapped over two lines loses its second half. Returns how many were wrong.
render_findings() {
    local state what remedy bad=0
    while IFS="$(printf '\t')" read -r state what remedy; do
        [ -n "$state" ] || continue
        case "$state" in
            ok)    printf '  %sok%s    %s\n' "$_c_grn" "$_c_off" "$what" >&2 ;;
            wrong) printf '  %s--%s    %s\n' "$_c_red" "$_c_off" "$what" >&2
                   [ -z "$remedy" ] || printf '        -> %s\n' "$remedy" >&2
                   bad=$((bad + 1)) ;;
            note)  printf '  %s??%s    %s\n' "$_c_yel" "$_c_off" "$what" >&2
                   [ -z "$remedy" ] || printf '        %s\n' "$remedy" >&2 ;;
        esac
    done
    return "$bad"
}

wk_os() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux)  echo linux ;;
        *)      die "unsupported OS: $(uname -s)" ;;
    esac
}

is_macos() { [ "$(wk_os)" = macos ]; }
is_linux() { [ "$(wk_os)" = linux ]; }

have() { command -v "$1" >/dev/null 2>&1; }

wk_ssh_timeout() { printf '%s' "${WK_SSH_TIMEOUT:-10}"; }

link_config() { # <src> <dst> -- symlink dst -> src, moving any real dst aside once
    local src="$1" dst="$2"

    [ -e "$src" ] || die "link_config: missing source $src"

    if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
        unchanged "link $dst"
        return 0
    fi

    mkdir -p "$(dirname "$dst")"

    if [ -e "$dst" ] || [ -L "$dst" ]; then
        local backup="$dst.wk-backup"
        if [ ! -e "$backup" ]; then
            mv "$dst" "$backup"
            warn "moved existing $dst -> $backup"
        else
            rm -rf "$dst"
        fi
    fi

    ln -sfn "$src" "$dst"
    changed "link $dst -> $src"
}

write_file() { # <dst> [mode] -- write stdin only when content or mode differs
    local dst="$1" mode="${2:-0644}" tmp
    tmp="$(mktemp)"
    cat >"$tmp"

    if [ -f "$dst" ] && cmp -s "$tmp" "$dst"; then
        chmod "$mode" "$dst"
        rm -f "$tmp"
        unchanged "write $dst"
        return 0
    fi

    mkdir -p "$(dirname "$dst")"
    chmod "$mode" "$tmp"
    mv "$tmp" "$dst"
    changed "write $dst"
}

ensure_dir() { # <path> [mode] -- mode, if named, is asserted on every run
    local d="$1" mode="${2:-}"
    if [ -n "${WK_DRY_RUN:-}" ]; then
        if [ -d "$d" ]; then
            unchanged "dir $d"
        else
            log "dry run: would create $d (${mode:-0755})"
        fi
        return 0
    fi
    if [ -d "$d" ]; then
        unchanged "dir $d"
    else
        mkdir -p "$d" || die "cannot create $d"
        chmod "${mode:-0755}" "$d" || die "cannot set mode ${mode:-0755} on $d"
        changed "create $d"
    fi
    # In the podman machine the keyring is a read-only host mount, 0700.
    [ -z "$mode" ] || [ "$(file_mode "$d")" = "${mode#0}" ] \
        || chmod "$mode" "$d" || die "cannot set mode $mode on $d"
}

# GNU and BSD stat take different flags, so both readers are python3's os.stat; empty when absent.
file_mode() { # <path> -- the rwx bits in octal, no leading zero (`700`)
    python3 -c 'import os, sys
try: print("%o" % (os.stat(sys.argv[1]).st_mode & 0o777))
except OSError: pass' "$1"
}

file_owner() { # <path> -- the owning account's name
    python3 -c 'import os, pwd, sys
try: print(pwd.getpwuid(os.stat(sys.argv[1]).st_uid).pw_name)
except (OSError, KeyError): pass' "$1"
}

wk_py() { env ${WK_STORE:+"WK_STORE=$WK_STORE"} PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m "$@"; }   # <module> <args...>: lib/wk's answer to a setup stage
# `eval "$(...)"` returns 0 for a failed substitution under bash 3.2's set -e, so the assignments are taken apart from the eval.
wk_eval() { local _o; _o=$(wk_py "$@") || die "python3 -m $* failed (above), so this stage cannot know what it acts on"; eval "$_o"; }
wk_fleet() { wk_py wk.fleet "$@"; }   # machines/<name>.conf, read by lib/wk/fleet.py alone

WK_BENCH_ACCOUNT="${WK_BENCH_USER:-bench}"

wk_machine_name() { PYTHONPATH="$WK_ROOT/lib" python3 -c 'from wk import record; print(record.machine_name())'; }   # this host's name is read in one place, lib/wk/record.py

valid_name() { # names become container names, directories and ssh host aliases
    case "$1" in
        ''|*[!a-zA-Z0-9._-]*) return 1 ;;
        -*) return 1 ;;
        *) return 0 ;;
    esac
}

require_name() {
    valid_name "${1:-}" || die "invalid name '${1:-}': use [a-zA-Z0-9._-], not starting with '-'"
}

confirm() {
    local prompt="$1"
    if [ -n "${WK_DRY_RUN:-}" ]; then
        printf 'would ask: %s [y/N]\n' "$prompt" >&2
        WK_CONFIRMED=1; export WK_CONFIRMED
        return 0
    fi
    if [ -n "${WK_YES:-}" ]; then WK_CONFIRMED=1; export WK_CONFIRMED; return 0; fi

    if [ ! -t 0 ]; then
        warn "$prompt -- declining (no terminal; re-run interactively, or pass --yes)"
        return 1
    fi

    printf '%s [y/N] ' "$prompt" >&2
    local reply
    read -r reply || return 1
    case "$reply" in [yY]*) WK_CONFIRMED=1; export WK_CONFIRMED; return 0 ;; *) return 1 ;; esac
}

# Every state change goes through here and nowhere else: under --dry-run it is printed, not run, and a destructive command (declared so to the dispatcher) cannot act before confirm() has been answered.
act() { # <cmd...>
    if [ -n "${WK_DRY_RUN:-}" ]; then
        { printf 'would run:'; printf ' %q' "$@"; printf '\n'; } >&2
        return 0
    fi
    [ -z "${WK_DESTRUCTIVE:-}" ] || [ -n "${WK_CONFIRMED:-}" ] \
        || die "BUG: this command is declared destructive and acted before asking:
    $(printf ' %q' "$@")"
    debug "run:$(printf ' %q' "$@")"
    "$@"
}

wk_tailscale_authkey() { wk_py wk.tailnet authkey; }

# bash keeps only the last `trap ... EXIT`, so handlers register here, each named `<pid>:<function>`: a subshell inherits the list and the trap, and without the pid a cleanup registered there would run the parent's handlers when the subshell ends. A handler runs only in the process that asked for it, and reads the exit status from WK_EXIT_STATUS.
_WK_ATEXIT=""

_wk_run_atexit() {
    local _rc=$? _h _me="${BASHPID:-$$}"   # expanded here, not through a function: a function's answer comes back through a command substitution, whose subshell has a BASHPID of its own and is never the process asking. bash 3.2 has neither BASHPID nor an inherited EXIT trap in a subshell, so $$ is the whole answer there
    WK_EXIT_STATUS=$_rc   # published, not passed: this replaces `trap 'f $?' EXIT`
    for _h in $_WK_ATEXIT; do
        [ "${_h%%:*}" = "$_me" ] || continue
        "${_h#*:}" || true
    done
    return $_rc
}

wk_atexit() { # <function-name> -- run it when this process ends, whatever ends it
    local _me="${BASHPID:-$$}:$1"
    case " $_WK_ATEXIT " in
        *" $_me "*) return 0 ;;   # already registered here; registering is idempotent
    esac
    _WK_ATEXIT="$_WK_ATEXIT $_me"
    trap _wk_run_atexit EXIT
    return 0
}

# A forced barrier is warned about again at the end: one line atop a long build is never seen.
_WK_FORCED=""

_forced_summary() {
    [ -n "$_WK_FORCED" ] || return 0
    printf '%swarning:%s this command was forced past %s barrier(s):\n' \
        "$_c_yel" "$_c_off" "$(printf '%s' "$_WK_FORCED" | grep -c '^-')" >&2
    printf '%s\n' "$_WK_FORCED" >&2
}

WK_RETRY_EXIT=75   # lib/wk/act.py's RETRY_EXIT is the same number; tests/test_resources.py holds the two to it

barrier() { # [--retry] <message...> -- refuse, or warn loudly and continue under --force; --retry when another step ending is what changes the answer
    local status=1
    [ "${1:-}" != --retry ] || { status=$WK_RETRY_EXIT; shift; }
    if [ -z "${WK_FORCE:-}" ]; then
        err "$*
    --force proceeds anyway, with a warning."
        exit "$status"
    fi
    warn "FORCED past a barrier: $*"
    _WK_FORCED="$_WK_FORCED
- $(printf '%s' "$*" | head -1)"
    wk_atexit _forced_summary
    return 0
}

wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

# The privileged helpers' table, paths and grant check live in lib/wk/priv.py.
wk_priv_helpers() { wk_py wk.priv rows; }
wk_priv_path() { wk_py wk.priv path "$1"; }
wk_priv_sudoers() { wk_py wk.priv sudoers "$1"; }
wk_priv_answers() { wk_py wk.priv answers "$1"; }   # <helper path>
