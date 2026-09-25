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

require() {
    have "$1" || die "${2:-$1 is required but not installed}"
}

wk_ssh_timeout() { printf '%s' "${WK_SSH_TIMEOUT:-10}"; }

WK_DISPATCH_VARS="WK_NAME WK_TARGET WK_TARGET_KIND WK_ROOT WK_FORCE WK_QUIET WK_DRY_RUN WK_DESTRUCTIVE WK_CONFIRMED WK_ROW_LABEL WK_HOST_SELF WK_IN_VM"

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
    # In the podman machine the secrets directory is a read-only host mount, 0700.
    [ -z "$mode" ] || [ "$(file_mode "$d")" = "${mode#0}" ] \
        || chmod "$mode" "$d" || die "cannot set mode $mode on $d"
}

file_bytes() {
    stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0
}

file_mode() { # octal permission bits alone, no leading zero (`700`)
    stat -c %a "$1" 2>/dev/null || stat -f %Lp "$1" 2>/dev/null || echo ""
}

kv_get() { # <key> -- one value from KEY=VALUE stdin, split on the first "=" only
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit }'
}

kv_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    kv_get "$k" < "$f"
}

wk_fleet() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m wk.fleet "$@"; }   # machines/<name>.conf, read by lib/wk/fleet.py alone

WK_BENCH_ACCOUNT="${WK_BENCH_USER:-bench}"

# macOS keeps the hostname's capitalisation; ssh aliases, confs and lock paths are lower (lib/wk/record.py's host_name).
wk_host_name() { { hostname -s 2>/dev/null || true; } | tr '[:upper:]' '[:lower:]'; }

wk_machine_name() {   # in the VM: the workstation that forwarded (Target.wk_cmd), the VM being that machine's container target and not one of its own -- its `localhost` hostname would put a machine nobody can act on in every listing
    if [ -n "${WK_IN_VM:-}" ] && [ -n "${WK_ROW_LABEL:-}" ]; then
        printf '%s\n' "$WK_ROW_LABEL"
        return 0
    fi
    local h
    h=$(wk_host_name)
    printf '%s\n' "${h:-here}"
}

# A script's title and synopsis for its own usage(), by shape not line number: indented lines are the synopsis, the prose after it is not. A fixed window reprints the wrong thing the first time a comment above it moves.
usage_block() { # <file>
    awk 'NR == 1 { next }
         !/^#/  { exit }
         /^#  / { seen = 1; sub(/^# ?/, ""); print; next }
         seen   { exit }
                { sub(/^# ?/, ""); print }' "$1"
}

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

sh_quote() { # ssh joins its arguments and hands them to a remote shell
    local arg out='' sep=''
    for arg in "$@"; do
        out="$out$sep'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
        sep=' '
    done
    printf '%s' "$out"
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

wk_tailscale_authkey_path() { printf '%s' "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"; }

wk_tailscale_api_path() { printf '%s' "${WK_TS_API_SECRET:-$HOME/.config/wk/tailscale-api-key}"; }

wk_tailscale_authkey() { PYTHONPATH="$WK_ROOT/lib" WK_ROOT="$WK_ROOT" python3 -m wk.tailnet authkey; }

# bash keeps only the last `trap ... EXIT`, so handlers register here instead, each one named `<pid>:<function>`. The pid is not decoration: a subshell inherits both the list and the trap, and anything registering a cleanup of its own in there re-arms the trap and would run the *parent's* handlers when the subshell ends -- which deleted cmd/ab's step file halfway through its own graph (measured 2026-09-16, bash 5.2, inside `$(ws_target ...)`). A handler runs in the process that asked for it and in no other, and reads the exit status from WK_EXIT_STATUS.
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

# Ctrl-C reaches the foreground group; a supervisor signals one pid only, and one with no tty sends HUP.
_WK_INTERRUPTED=""
_WK_ON_INTERRUPT=""

on_interrupt() { # <function-name>
    _WK_ON_INTERRUPT="$1 $_WK_ON_INTERRUPT"
    trap '_wk_interrupt INT'  INT
    trap '_wk_interrupt TERM' TERM
    trap '_wk_interrupt HUP'  HUP
}

_wk_interrupt() { # <sig>
    local sig="$1" h
    [ -z "$_WK_INTERRUPTED" ] || return 0   # a second signal mid-cleanup: ignore it
    _WK_INTERRUPTED="$sig"
    trap - INT TERM HUP
    for h in $_WK_ON_INTERRUPT; do "$h" || true; done
    case "$sig" in
        INT)  exit 130 ;;
        TERM) exit 143 ;;
        HUP)  exit 129 ;;
        *)    exit 1 ;;
    esac
}

interrupted() { [ -n "$_WK_INTERRUPTED" ]; }

# bash defers a pending trap until the interrupted command finishes (bash(1)).
wk_sleep() { # <seconds> -- sleep in <=1s chunks
    local remain="${1:-0}" chunk
    while [ "$remain" -gt 0 ] 2>/dev/null; do
        chunk=1; [ "$remain" -lt 1 ] && chunk="$remain"
        sleep "$chunk"
        remain=$((remain - chunk))
    done
    return 0
}

# `timeout(1)` is GNU, absent on macOS. TERM to the subshell alone leaves its
# ssh holding the terminal, so the group is signalled; `set -m` makes one.
capped() { # <seconds> <cmd...>
    local secs="$1"; shift
    local jc=""; case "$-" in *m*) jc=on ;; esac
    set -m
    "$@" &
    local pid=$! rc=0
    [ -n "$jc" ] || set +m
    ( sleep "$secs"; kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null ) \
        >/dev/null 2>&1 &
    local wd=$!
    wait "$pid" 2>/dev/null || rc=$?
    kill -TERM "$wd" 2>/dev/null
    wait "$wd" 2>/dev/null || true
    return "$rc"
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

# A lock is a symlink whose target names the holder. Not `flock`: its fd is
# inherited by every child (`conmon` would hold ours for the workspace's life)
# and macOS ships none. Keyed by hostname: NFS homes.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

# Mounted into the podman machine at /var/lib/wk/secrets, so `wk key set` works with no VM running and a container reads the same bytes live.
# The privileged helpers, one row each: <name> <platform> <what it is for>. A helper whose
# sudoers rule is out-ranked is installed, executable and useless, so what is ever asked of
# one is whether it answers.
wk_priv_helpers() {
    cat <<'ROWS'
wk-quiesce-priv any wk quiesce / wk session
wk-card-priv linux wk sysimage (writing a card)
wk-boot-priv any wk boot (arming the firmware, restarting a machine)
ROWS
}

wk_priv_path() { printf '/usr/local/libexec/%s' "$1"; }

wk_priv_sudoers() { local n="${1#wk-}"; printf '/etc/sudoers.d/zzz-wk-%s' "${n%-priv}"; }

wk_priv_answers() { # <helper path> -- the rule, never a run: `sudo -n <helper>` succeeds for anything while a timestamp is cached, and `./setup` holds that window open on purpose (2026-09-08: it reported a helper working whose rule granted `root`)
    sudo -n -l 2>/dev/null \
        | awk -v p="$1" '/NOPASSWD:/ { for (i = 1; i <= NF; i++) if ($i == p) found = 1 }
                         END { exit !found }'
}

wk_host_secrets() { echo "${WK_HOST_SECRETS:-${XDG_CONFIG_HOME:-$HOME/.config}/wk/secrets}"; }

wk_lock_dir() { echo "${WK_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/locks}"; }

_WK_LOCK_HELD=""
_WK_LOCK_MINE=""
_WK_LOCK_PAYLOAD=""

_lock_init() {
    [ -n "$_WK_LOCK_PAYLOAD" ] && return 0
    local tok
    tok=$( (od -An -N4 -tx1 /dev/urandom 2>/dev/null || echo "$$ $RANDOM") | tr -dc 'a-f0-9')
    _WK_LOCK_PAYLOAD="pid=$$ tok=${tok:-$$} at=$(date -u +%Y-%m-%dT%H:%M:%SZ) cmd=${WK_CMD:-wk}"
    return 0
}

_lock_payload() { _lock_init; printf '%s' "$_WK_LOCK_PAYLOAD"; }

_lock_token() { printf '%s' "$_WK_LOCK_PAYLOAD" | sed -n 's/.*tok=\([a-f0-9]*\).*/\1/p'; }

_lock_pid_of() { printf '%s' "${1:-}" | sed -n 's/^pid=\([0-9][0-9]*\).*/\1/p'; }

_lock_path() {
    local d h
    d=$(wk_lock_dir)
    mkdir -p "$d" 2>/dev/null || true
    h=$(wk_host_name)
    echo "$d/$1@${h:-local}.lock"
}

_lock_release_all() {
    local f
    [ -n "$_WK_LOCK_HELD" ] || return 0
    for f in $_WK_LOCK_HELD; do
        [ "$(readlink "$f" 2>/dev/null || true)" = "$(_lock_payload)" ] || continue
        rm -f "$f"
    done
    _WK_LOCK_HELD=""
    _WK_LOCK_MINE=""
    return 0
}

# "Is it dead" then "replace it" races every other taker, so the replacement is a compare-and-swap made atomic by a short-lived breaker lock.
_lock_break() {
    local f="$1" seen="$2" bf="$f.breaking" tmp bpid rc=1

    if ! ln -s "$(_lock_payload)" "$bf" 2>/dev/null; then
        bpid=$(_lock_pid_of "$(readlink "$bf" 2>/dev/null || true)")
        if [ -z "$bpid" ] || ! kill -0 "$bpid" 2>/dev/null; then rm -rf "$bf"; fi
        return 1
    fi

    if [ "$(readlink "$f" 2>/dev/null || true)" = "$seen" ]; then
        tmp="$f.new.$(_lock_token)"
        rm -f "$tmp"
        if ln -s "$(_lock_payload)" "$tmp" 2>/dev/null && mv -f "$tmp" "$f" 2>/dev/null; then
            rc=0
        else
            rm -f "$tmp"
        fi
    fi

    rm -f "$bf"
    return $rc
}

hold_lock() { # <resource> [-w seconds] [-s]
    local res="$1" timeout=600 f owner opid started announced="" unreadable=""
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            -w) timeout="${2:-600}"; shift 2 ;;
            -s) shift ;;   # no shared mode; see above
            *) die "hold_lock: unknown option $1" ;;
        esac
    done

    _lock_init
    f=$(_lock_path "$res")
    started=$(date +%s)

    case " $_WK_LOCK_MINE " in
        *" $f "*) debug "lock: $res (already held here)"; return 0 ;;
    esac

    while :; do
        unreadable=""
        if [ -d "$f" ] && [ ! -L "$f" ]; then
            # Checked before the `ln`: `ln -s x somedir` links *inside* it.
            opid=$(cat "$f/pid" 2>/dev/null | tr -dc '0-9') || true
            if [ -z "$opid" ] || ! kill -0 "$opid" 2>/dev/null; then
                rm -rf "$f"; continue
            fi
        elif ln -s "$(_lock_payload)" "$f" 2>/dev/null; then
            break
        elif owner=$(readlink "$f" 2>/dev/null); then
            opid=$(_lock_pid_of "$owner")

            if [ -n "$opid" ] && ! kill -0 "$opid" 2>/dev/null; then
                _lock_break "$f" "$owner" && break
                continue
            fi

            if [ -z "$opid" ]; then
                warn "clearing a lock file with no holder in it: $f"
                rm -rf "$f"; continue
            fi
        else
            # A transient read failure is not evidence of free: it could hide a live hold, so it is kept, not cleared.
            opid=""; unreadable=1
        fi

        if [ -z "$announced" ]; then
            announced=1
            if [ -n "$unreadable" ]; then
                info "waiting for the $res lock (its holder cannot be read)"
            else
                info "waiting for the $res lock${opid:+ (held by pid $opid)}"
            fi
        fi
        if [ "$(( $(date +%s) - started ))" -ge "$timeout" ]; then
            if [ -n "$unreadable" ]; then
                die "could not take the $res lock within ${timeout}s -- its holder cannot be read"
            fi
            die "could not take the $res lock within ${timeout}s${opid:+ -- pid $opid still holds it}"
        fi
        sleep 1
    done

    _WK_LOCK_HELD="$_WK_LOCK_HELD $f"
    _WK_LOCK_MINE="$_WK_LOCK_MINE $f"
    wk_atexit _lock_release_all
    debug "lock: $res"
}

lock_holder_pid() { # <lock file> -- read without taking; a dead holder reads as none
    local line
    line=$(readlink "$1" 2>/dev/null || cat "$1/payload" 2>/dev/null || true)
    printf '%s' "$line" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p'
}
lock_alive() { # <resource>
    local pid
    pid=$(lock_holder_pid "$(_lock_path "$1")")
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

with_lock() { # <resource> [-w seconds] [-s] -- cmd...   the scoped form
    local res="$1" args=""
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --) shift; break ;;
            *) args="$args $1"; shift ;;
        esac
    done
    [ $# -gt 0 ] || die "with_lock: nothing to run"
    ( _WK_LOCK_HELD=""; hold_lock "$res" $args; "$@" )
}

# GNU and BSD date share no syntax here (`date -u -d @0` fails on macOS).
epoch_to_utc() {
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || true
}

utc_to_epoch() {
    date -u -d "$1" +%s 2>/dev/null \
        || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null \
        || echo 0
}

tart_bin() {   # tart is a signed .app reached through a symlink no non-interactive PATH carries, so `command -v tart` is not the answer; every reader of "is tart here" calls this
    local p
    if command -v tart >/dev/null 2>&1; then p=$(command -v tart)
    elif [ -x "$HOME/.local/bin/tart" ]; then p="$HOME/.local/bin/tart"
    elif [ -x "$HOME/.local/share/tart/tart.app/Contents/MacOS/tart" ]; then
        p="$HOME/.local/share/tart/tart.app/Contents/MacOS/tart"
    else return 1
    fi
    # `readlink -f` only grew symlink-chain resolution on recent macOS, and bash 3.2 must still work.
    if readlink -f "$p" >/dev/null 2>&1; then readlink -f "$p"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$p"
    else echo "$p"
    fi
}

WK_IMAGE_MARKER="${WK_IMAGE_MARKER:-/etc/wk-image}"

wk_image_id() { kv_field "$WK_IMAGE_MARKER" id 2>/dev/null || true; }   # the bench system's own id, or empty in host mode

