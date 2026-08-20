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

# One `key=value` field out of a small text file; empty when the file or the
# key is missing. Comments and blank lines are ignored, so every file written
# in this format can explain itself, and a key this reader has never heard of
# is simply not asked for -- which is what makes a file written by last
# month's wk-tools readable by today's.
#
# The one parser for every such file in the tree: the workspace and remote
# markers (marker_field, lib/target.sh) and the status files (status_field,
# lib/detach.sh) are both this. Two copies of a tolerant parse are two
# tolerances that drift.
kv_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    awk -F= -v k="$k" '$1 == k { sub(/^[^=]*=/, ""); print; exit }' "$f"
}


# zed's CLI: the PATH entry when the user has run "Install CLI", otherwise the
# binary inside the app bundle. A drag-installed Zed.app has no symlink and is
# still installed -- cmd/doctor already accepts it as such, so the commands
# that launch it have to as well.
zed_cli() {
    if have zed; then echo zed; return 0; fi
    local c=/Applications/Zed.app/Contents/MacOS/cli
    [ -x "$c" ] && { echo "$c"; return 0; }
    return 1
}

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

# Quote arguments so they survive reconstruction inside another shell -- ssh
# concatenates its command arguments with spaces and hands the result to a
# remote shell, so anything with a space or a quote is mangled without this.
sh_quote() {
    local arg out='' sep=''
    for arg in "$@"; do
        out="$out$sep'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
        sep=' '
    done
    printf '%s' "$out"
}

confirm() {
    local prompt="$1"
    [ -n "${WK_YES:-}" ] && return 0

    # No tty means nobody can answer. Decline rather than block: a prompt that
    # waits forever in a script or a background run is worse than a decision
    # not taken, and every caller treats "no" as the safe outcome.
    if [ ! -t 0 ]; then
        warn "$prompt -- declining (no terminal; re-run interactively, or set WK_YES=1)"
        return 1
    fi

    printf '%s [y/N] ' "$prompt" >&2
    local reply
    read -r reply || return 1
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# --- barriers, and getting past one in a hurry --------------------------------
#
# A barrier is a refusal that exists because of a rule rather than because the
# command cannot proceed: an unfiltered guest, a machine where sudo needs no
# password, a flag that would silently build the wrong thing. Correctness
# failures are not barriers -- "no such workspace" is not something to force
# past, and neither is a missing base snapshot.
#
# `wk <command> --force` turns every barrier in that command into a warning.
# The warning is deliberately loud and is *repeated when the command ends*,
# because the whole point of forcing one is that you are in a hurry, and a
# single line at the top of a long build is a line nobody sees again. The
# condition itself stays visible too: `wk doctor` recomputes it from the
# machine on every run, so a forced bypass never becomes a fact anything
# remembers -- there is nothing to remember, only something still true.
_WK_FORCED=""

_forced_summary() {
    [ -n "$_WK_FORCED" ] || return 0
    printf '%swarning:%s this command was forced past %s barrier(s):\n' \
        "$_c_yel" "$_c_off" "$(printf '%s' "$_WK_FORCED" | grep -c '^-')" >&2
    printf '%s\n' "$_WK_FORCED" >&2
}

# barrier <message...> -- refuse, or warn loudly and continue under --force.
barrier() {
    if [ -z "${WK_FORCE:-}" ]; then
        die "$*
    --force proceeds anyway, with a warning."
    fi
    warn "FORCED past a barrier: $*"
    _WK_FORCED="$_WK_FORCED
- $(printf '%s' "$*" | head -1)"
    # Composes with any EXIT trap a lock has taken: both are ours, and
    # release_locks is idempotent.
    trap '_forced_summary; _lock_release_all 2>/dev/null || true' EXIT
    return 0
}

# --- locks --------------------------------------------------------------------
#
# Rule 4: one lock per mutated resource, and a lock is not state -- it dies
# with its holder.
#
# One mechanism, an atomic mkdir with the holder's pid inside it, on every
# host. `flock` was the obvious choice and was tried first; it is wrong here,
# and the reason is worth keeping:
#
#   a flock is held by the open file description, so *every process that
#   inherits the descriptor* holds it. `wk new` starts a container, and podman
#   leaves `conmon` behind supervising it -- with our lock fd inherited and the
#   lock held for as long as the workspace exists. Measured 2026-08-19: after
#   one `wk new`, conmon held ws-<name>.lock and the next `wk build` on that
#   workspace waited on it forever. bash cannot mark a redirection
#   close-on-exec, so there is no fix that keeps the fd; `flock --close` only
#   applies to flock's own `-c` command form, which would mean re-exec'ing
#   every command under it.
#
# mkdir has no descriptor to inherit. It is atomic on every filesystem here,
# the holder's pid goes inside, and a lock whose pid is gone is broken by the
# next taker -- which is what makes it die with its holder even on kill -9
# (verified: the next taker proceeds immediately). The cost is that there is no
# shared mode, so `-s` is accepted and serialises anyway: it only ever waits
# more than asked, never less.
#
# Locks live under the invoking machine's own state directory and are keyed by
# that machine's hostname, because a home directory can be shared by several
# build machines over NFS -- and a pid is only meaningful on the machine it
# came from. Two machines under one home then lock their own resources, which
# is what "per mutated resource" means when the resource is per machine.
wk_lock_dir() { echo "${WK_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/locks}"; }

_WK_LOCK_HELD=""

_lock_path() {
    local d h
    d=$(wk_lock_dir)
    mkdir -p "$d" 2>/dev/null || true
    h=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo local)
    echo "$d/$1@$h.lock"
}

_lock_release_all() {
    local d
    for d in $_WK_LOCK_HELD; do rm -rf "$d"; done
    _WK_LOCK_HELD=""
}

# hold_lock <resource> [-w seconds] [-s]
#
# Takes the lock and holds it for the life of this process -- which is what a
# long, linear command wants (`wk build` locks the workspace it is building
# from the moment it starts writing status to the moment it exits). Dropped by
# an EXIT trap, and by the next taker's liveness check if that never runs.
#
# Before an `exec` that replaces this process with something long-lived (`wk
# new --zed`), call release_locks: the pid stays alive, so the lock would be
# held for as long as the editor is open.
hold_lock() {
    local res="$1" timeout=600 f waited=0 owner
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            -w) timeout="${2:-600}"; shift 2 ;;
            -s) shift ;;   # no shared mode; see above
            *) die "hold_lock: unknown option $1" ;;
        esac
    done

    f=$(_lock_path "$res")

    while ! mkdir "$f.d" 2>/dev/null; do
        owner=$(cat "$f.d/pid" 2>/dev/null || true)
        if [ -n "$owner" ] && ! kill -0 "$owner" 2>/dev/null; then
            # The holder is gone. Its lock is not state and must not outlive it.
            rm -rf "$f.d"
            continue
        fi
        if [ "$waited" -eq 0 ]; then
            info "waiting for the $res lock${owner:+ (held by pid $owner)}"
        fi
        if [ "$waited" -ge "$timeout" ]; then
            die "could not take the $res lock within ${timeout}s${owner:+ -- pid $owner still holds it}"
        fi
        sleep 1; waited=$((waited + 1))
    done

    printf '%s\n' "$$" > "$f.d/pid"
    _WK_LOCK_HELD="$_WK_LOCK_HELD $f.d"
    trap _lock_release_all EXIT
    debug "lock: $res"
}

# Drop every lock this process holds, now. For the `exec` case above, and for
# anything that has finished mutating but has work left to do.
release_locks() { _lock_release_all; }

# with_lock <resource> [-w seconds] [-s] -- cmd...
#
# The scoped form: takes the lock, runs the command, drops it. For anything
# whose critical section is smaller than the command that contains it.
with_lock() {
    local res="$1" args=""
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --) shift; break ;;
            *) args="$args $1"; shift ;;
        esac
    done
    [ $# -gt 0 ] || die "with_lock: nothing to run"
    ( hold_lock "$res" $args; "$@" )
}

# --- the BMC's DRM device -----------------------------------------------------
# The kernel driver is the discriminator, never the card number: which
# /dev/dri/cardN happens to be the ast is a property of PCI enumeration order,
# not something to hardcode. Shared by cmd/session (which points the benchmark
# compositor at it) and cmd/gui (which points a browser client's own DRM/EGL
# device inference at it -- left to guess, it picks the GPU, which is a device
# a software-rendered compositor never handed it and the picture stays blank).
bmc_drm_device() {
    local d c drv
    for d in /sys/class/drm/card[0-9]*; do
        [ -d "$d" ] || continue
        c=$(basename "$d")
        case "$c" in *-*) continue ;; esac
        drv=$(readlink -f "$d/device/driver" 2>/dev/null) || continue
        [ "$(basename "$drv" 2>/dev/null)" = ast ] && { echo "/dev/dri/$c"; return 0; }
    done
    return 1
}

# --- the graphical session's mode --------------------------------------------
# `wk session` can start the compositor a few ways, and from the Wayland
# socket alone they are indistinguishable -- which is exactly the problem,
# because none of them but one make a number that comes out meaningful. The
# privileged helper records which one it started in a file under /run, and
# this is where everything else reads it.
#
# Linux-only, and absent-means-none: on macOS, and before the first session of
# a boot, there is no file and nothing to warn about.
WK_SESSION_MODE_FILE="${WK_SESSION_MODE_FILE:-/run/wk-session-mode}"

# gpu | bmc | off | none
session_mode() {
    local m=""
    [ -r "$WK_SESSION_MODE_FILE" ] && m=$(head -1 "$WK_SESSION_MODE_FILE" 2>/dev/null | tr -dc 'a-z-')
    printf '%s' "${m:-none}"
}

# Say so, loudly, when the session in front of us is the slow one, or is not
# meant to have anything running against it at all. Returns 0 when there is
# nothing to warn about, 1 when it warned -- so a caller that must refuse (the
# benchmark runner) and a caller that only has to inform (wk enter, wk gui)
# can share one wording.
session_mode_warn() {
    case "$(session_mode)" in
        bmc)
            warn "SLOW SESSION: SOFTWARE RENDERING -- the BMC display chip, no GPU at all"
            log  "  llvmpipe is not a slow GPU, it is a different measurement: MotionMark"
            log  "  differs by ~400x. Nothing measured here means anything."
            log  "  measurable session again:  wk session on"
            return 1
            ;;
        off)
            warn "SESSION IS OFF -- this socket is the screen-off placeholder, not a session"
            log  "  its outputs are modeset off on purpose and it has no head to draw on;"
            log  "  nothing rendered into it will show up anywhere."
            log  "  a real session:  wk session on"
            return 1
            ;;
    esac
    return 0
}
