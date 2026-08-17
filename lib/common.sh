# Shared helpers. Sourced by `wk`, `setup`, and everything under cmd/.
# Keep this small: logging, idempotent file operations, and OS detection.

set -euo pipefail

WK_ROOT="${WK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export WK_ROOT

# --- logging -----------------------------------------------------------------
# Colours only when stderr is a tty, so logs stay clean when redirected.
if [ -t 2 ]; then
    _c_dim=$'\033[2m'; _c_red=$'\033[31m'; _c_yel=$'\033[33m'
    _c_grn=$'\033[32m'; _c_off=$'\033[0m'
else
    _c_dim=''; _c_red=''; _c_yel=''; _c_grn=''; _c_off=''
fi

log()  { printf '%s\n' "$*" >&2; }
info() { printf '%s==>%s %s\n' "$_c_grn" "$_c_off" "$*" >&2; }
warn() { printf '%swarning:%s %s\n' "$_c_yel" "$_c_off" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$_c_red" "$_c_off" "$*" >&2; exit 1; }
debug() { [ -n "${WK_DEBUG:-}" ] && printf '%s  %s%s\n' "$_c_dim" "$*" "$_c_off" >&2 || true; }

# `setup` reports whether it changed anything, so a second run can prove it is
# a no-op. Every mutating helper below bumps this.
WK_CHANGES=0
changed() { WK_CHANGES=$((WK_CHANGES + 1)); info "$*"; }
unchanged() { debug "ok: $*"; }

# --- os ----------------------------------------------------------------------
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

require() {
    have "$1" || die "${2:-$1 is required but not installed}"
}

# --- idempotent file operations ----------------------------------------------

# link_config <source-in-repo> <destination>
# Symlinks destination -> source. Existing real files are moved aside once,
# never clobbered; an already-correct symlink is left alone so re-runs are free.
link_config() {
    local src="$1" dst="$2"

    [ -e "$src" ] || die "link_config: missing source $src"

    if [ -L "$dst" ] && [ "$(readlink "$dst")" = "$src" ]; then
        unchanged "link $dst"
        return 0
    fi

    mkdir -p "$(dirname "$dst")"

    if [ -e "$dst" ] || [ -L "$dst" ]; then
        local backup="$dst.wk-backup"
        # Keep the first backup taken; later runs must not overwrite it with a
        # symlink we ourselves created.
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

# write_file <destination> <mode>  (content on stdin)
# Writes only when content or mode differs, so re-runs report no change.
write_file() {
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

# ensure_dir <path> [mode]
ensure_dir() {
    local d="$1" mode="${2:-0755}"
    if [ -d "$d" ]; then
        unchanged "dir $d"
    else
        mkdir -p "$d"
        chmod "$mode" "$d"
        changed "create $d"
    fi
}

# --- misc --------------------------------------------------------------------

# Names become container names, directory names and ssh host aliases, so keep
# them to a conservative character set.
valid_name() {
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
    [ -n "${WK_YES:-}" ] && return 0
    printf '%s [y/N] ' "$prompt" >&2
    local reply
    read -r reply || return 1
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}
