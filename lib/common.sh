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
die()  { printf '%serror:%s %s\n' "$_c_red" "$_c_off" "$*" >&2; exit 1; }
debug() { [ -n "${WK_DEBUG:-}" ] && printf '%s  %s%s\n' "$_c_dim" "$*" "$_c_off" >&2 || true; }

WK_CHANGES=0
changed() { WK_CHANGES=$((WK_CHANGES + 1)); info "$*"; }
unchanged() { debug "ok: $*"; }

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

# `gh auth status` exits 0 for a configured account whose token has expired.
gh_authenticated() {
    have gh && gh api user >/dev/null 2>&1
}

wk_ssh_timeout() { printf '%s' "${WK_SSH_TIMEOUT:-10}"; }

WK_DISPATCH_VARS="WK_NAME WK_TARGET WK_TARGET_KIND WK_ROOT WK_FORCE WK_QUIET WK_ROW_LABEL WK_HOST_SELF WK_IN_VM"

wk_exec_clean() { # <command...> -- exec with none of WK_DISPATCH_VARS set
    local v unset_args=""
    for v in $WK_DISPATCH_VARS; do unset_args="$unset_args -u $v"; done
    # shellcheck disable=SC2086 -- one -u per variable, deliberately split.
    exec env $unset_args "$@"
}

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

human_bytes() {
    awk -v b="${1:-0}" 'BEGIN {
        split("B K M G T P", u, " ")
        v = b; i = 1
        while (v >= 1024 && i < 6) { v /= 1024; i++ }
        if (i > 1 && v < 10) printf "%.1f%s\n", v, u[i]
        else                 printf "%.0f%s\n", v, u[i]
    }'
}

json_merge_list() { # <key> <file...> -- merge {"<key>": [...]} over undelimited docs
    local key="$1"; shift
    require python3 "python3 merges 'wk ls --json'; it ships with macOS and every distribution here"
    python3 -c '
import json, sys
key = sys.argv[1]
items = []
for path in sys.argv[2:]:
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        continue
    dec = json.JSONDecoder()
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            break
        obj, i = dec.raw_decode(text, i)
        items.extend(obj.get(key, []))
print(json.dumps({key: items}))
' "$key" "$@"
}

kv_get() { # <key> -- one value from KEY=VALUE stdin, split on the first "=" only
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit }'
}

kv_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    kv_get "$k" < "$f"
}


status_render() {
    local mode="$1" recs="$2"
    [ "$mode" != records ] || { cat "$recs"; return 0; }
    require python3 "python3 renders 'wk status'; it ships with macOS and with every
    distribution here, so a machine without it is a machine with something else wrong"
    python3 "$WK_ROOT/lib/status-view.py" "$mode" "$recs" \
        ${WK_STATUS_PORT:+--port "$WK_STATUS_PORT"} \
        ${WK_STATUS_INTERVAL:+--interval "$WK_STATUS_INTERVAL"} \
        ${WK_STATUS_HTML_OUT:+--out "$WK_STATUS_HTML_OUT"}
}

# Sets a variable, not stdout: in `$(...)` fd 1 is a pipe and `[ -t 1 ]` lies.
status_default_mode() {
    WK_STATUS_DEFAULT_MODE=text
    if [ -n "${WK_STATUS_VIEW:-}" ]; then
        WK_STATUS_DEFAULT_MODE="$WK_STATUS_VIEW"
        return 0
    fi
    [ -t 1 ]               || return 0
    [ -z "${CI:-}" ]       || return 0
    [ -z "${NO_COLOR:-}" ] || return 0
    if command -v in_workspace >/dev/null 2>&1 && in_workspace; then return 0; fi
    if [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && [ -z "${DISPLAY:-}" ]; then return 0; fi
    WK_STATUS_DEFAULT_MODE=web
    return 0
}
WK_STATUS_DEFAULT_MODE=text

# macOS keeps the hostname's capitalisation; ssh aliases and confs are lower.
wk_machine_name() {
    { hostname -s 2>/dev/null || echo here; } | tr '[:upper:]' '[:lower:]'
}

# A script's title and synopsis for its own usage(), by shape not line number: indented lines are the synopsis, the prose after it is not. A fixed window reprints the wrong thing the first time a comment above it moves.
usage_block() { # <file>
    awk 'NR == 1 { next }
         !/^#/  { exit }
         /^#  / { seen = 1; sub(/^# ?/, ""); print; next }
         seen   { exit }
                { sub(/^# ?/, ""); print }' "$1"
}

zed_cli() { # a drag-installed Zed.app has no PATH symlink but is installed
    if have zed; then echo zed; return 0; fi
    local c=/Applications/Zed.app/Contents/MacOS/cli
    [ -x "$c" ] && { echo "$c"; return 0; }
    return 1
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

# An older `wk` across a hop ignores an unknown variable, dies on a flag.
wk_forwarded_env() {
    printf '%s' "${WK_DEBUG:+WK_DEBUG=1 }${WK_QUIET:+WK_QUIET=1 }${WK_YES:+WK_YES=1 }${WK_FORCE:+WK_FORCE=1 }"
}

# A candidate is run, not found: the wkdev image puts /opt/swift/usr/bin first
# on PATH and that lldb links libxml2.so.2 while the image ships libxml2.so.16.
lldb_prelude() {
    cat <<'EOF'
LLDB=""
for _c in lldb $(ls /usr/bin/lldb-[0-9]* 2>/dev/null | sort -Vr); do
    command -v "$_c" >/dev/null 2>&1 || continue
    "$_c" --version >/dev/null 2>&1 && { LLDB="$_c"; break; }
done
[ -n "$LLDB" ] || { printf 'error: no lldb here that will start -- `lldb` resolves to %s\n' \
    "$(command -v lldb || echo 'nothing')" >&2; exit 127; }
EOF
}

# `~/.lldbinit` sets follow-fork-mode child; `-O` runs after the init file.
lldb_pin_opts() {
    printf '%s' "-O 'settings set target.process.follow-fork-mode parent'"
}

confirm() {
    local prompt="$1"
    [ -n "${WK_YES:-}" ] && return 0

    if [ ! -t 0 ]; then
        warn "$prompt -- declining (no terminal; re-run interactively, or set WK_YES=1)"
        return 1
    fi

    printf '%s [y/N] ' "$prompt" >&2
    local reply
    read -r reply || return 1
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

prompt_secret_value() {  # $1 = human description, $2 = optional URL or command
    local what="$1" url="${2:-}" val=""

    if [ ! -t 0 ]; then
        warn "$what is needed and is not stored yet."
        warn "  No terminal, so it cannot be asked for here. Re-run interactively."
        return 1
    fi

    printf '\n' >&2
    info "$what is needed, and this repository must not contain it."
    [ -n "$url" ] && log "  get one here: $url" >&2
    log "  it is stored 0600 and asked for only once" >&2
    printf '  paste it (input hidden, empty to skip): ' >&2
    read -rs val || return 1
    printf '\n' >&2

    [ -n "$val" ] || { warn "nothing entered; skipping"; return 1; }
    printf '%s' "$val"
}

prompt_secret() {  # $1 = path to store at, $2 = human description, $3 = optional URL
    local path="$1" what="$2" url="${3:-}" val=""

    [ -s "$path" ] && { printf '%s' "$path"; return 0; }

    val=$(prompt_secret_value "$what" "$url") || return 1

    mkdir -p "$(dirname "$path")" || return 1
    ( umask 077; printf '%s\n' "$val" > "$path" ) || return 1
    chmod 0600 "$path" 2>/dev/null || true
    info "stored in $path"
    printf '%s' "$path"
}

wk_tailscale_authkey_path() { printf '%s' "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"; }

wk_tailscale_key_reject() { # <key> -- prints why it is not the credential wanted
    wk_cred_reject tailnet "${1:-}"
}

wk_cred_reject() { # <rule name> <value> -- exit 0 if accepted, else print why
    local line
    line=$(printf '%s' "$2" | python3 "$WK_ROOT/lib/credcheck.py" check "$1")
    case "$line" in
        bad*) printf '%s' "${line#*$'\t'}"; return 1 ;;
    esac
    return 0
}

wk_tailscale_authkey_present() {
    local p; p=$(wk_tailscale_authkey_path)
    [ -s "$p" ] || return 1
    wk_tailscale_key_reject "$(head -1 "$p" 2>/dev/null)" >/dev/null
}

wk_tailscale_api_path() { printf '%s' "${WK_TS_API_SECRET:-$HOME/.config/wk/tailscale-api-key}"; }

wk_tailscale_api_reject() { # <key> -- here an *auth* key is the wrong one
    wk_cred_reject tailnet-api "${1:-}"
}

wk_tailscale_api_present() { # presence only; whether the tailnet accepts it is a request
    local p; p=$(wk_tailscale_api_path)
    [ -s "$p" ] || return 1
    wk_tailscale_api_reject "$(head -1 "$p" 2>/dev/null)" >/dev/null
}

wk_tailscale_api_key() {
    local path; path=$(wk_tailscale_api_path)
    local p why
    p=$(prompt_secret "$path" \
        "A tailscale API access token -- it stays on this machine and is never written to a card" \
        "https://login.tailscale.com/admin/settings/keys") || return 1
    if why=$(wk_tailscale_api_reject "$(head -1 "$p" 2>/dev/null)"); then
        printf '%s' "$p"; return 0
    fi
    warn "$p is not usable: $why"
    warn "  Leaving it in place rather than deleting it -- check it and re-run."
    return 1
}

wk_tailnet_retire() { # <name>
    WK_TS_API_SECRET_FILE="$(wk_tailscale_api_path)" \
        python3 "$WK_ROOT/lib/tailnet.py" retire "$1"
}

wk_tailscale_authkey() {
    local path="${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"
    local p why
    p=$(prompt_secret "$path" \
        "A tailscale auth key -- tagged tag:wk, reusable, NOT ephemeral, longest expiry" \
        "https://login.tailscale.com/admin/settings/keys") || return 1
    if why=$(wk_tailscale_key_reject "$(head -1 "$p" 2>/dev/null)"); then
        printf '%s' "$p"; return 0
    fi
    warn "$p is not usable: $why"
    warn "  Leaving it in place rather than deleting it -- check it and re-run."
    return 1
}

# bash keeps only the last `trap ... EXIT`, so handlers register here instead.
_WK_ATEXIT=""

_wk_run_atexit() {
    local _rc=$? _h
    WK_EXIT_STATUS=$_rc   # published, not passed: this replaces `trap 'f $?' EXIT`
    for _h in $_WK_ATEXIT; do "$_h" || true; done
    return $_rc
}

wk_atexit() { # <function-name> -- run it when this process ends, whatever ends it
    case " $_WK_ATEXIT " in
        *" $1 "*) return 0 ;;   # already registered; registering is idempotent
    esac
    _WK_ATEXIT="$_WK_ATEXIT $1"
    trap _wk_run_atexit EXIT
    return 0
}

# Ctrl-C reaches the foreground group; a supervisor signals one pid only.
_WK_INTERRUPTED=""
_WK_ON_INTERRUPT=""

on_interrupt() { # <function-name>
    _WK_ON_INTERRUPT="$1 $_WK_ON_INTERRUPT"
    trap '_wk_interrupt INT'  INT
    trap '_wk_interrupt TERM' TERM
}

_wk_interrupt() { # <sig>
    local sig="$1" h
    [ -z "$_WK_INTERRUPTED" ] || return 0   # a second signal mid-cleanup: ignore it
    _WK_INTERRUPTED="$sig"
    trap - INT TERM
    for h in $_WK_ON_INTERRUPT; do "$h" || true; done
    case "$sig" in
        INT)  exit 130 ;;
        TERM) exit 143 ;;
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

# A forced barrier is warned about again at the end: one line atop a long build
# is never seen.
_WK_FORCED=""

_forced_summary() {
    [ -n "$_WK_FORCED" ] || return 0
    printf '%swarning:%s this command was forced past %s barrier(s):\n' \
        "$_c_yel" "$_c_off" "$(printf '%s' "$_WK_FORCED" | grep -c '^-')" >&2
    printf '%s\n' "$_WK_FORCED" >&2
}

barrier() { # <message...> -- refuse, or warn loudly and continue under --force
    if [ -z "${WK_FORCE:-}" ]; then
        die "$*
    --force proceeds anyway, with a warning."
    fi
    warn "FORCED past a barrier: $*"
    _WK_FORCED="$_WK_FORCED
- $(printf '%s' "$*" | head -1)"
    wk_atexit _forced_summary
    return 0
}

WK_COMMIT_WALL_PATHS="objects refs logs HEAD packed-refs index.lock ORIG_HEAD"

# bwrap's read-only binds resist unmount, remount, shadowing and nested user
# namespaces, with an empty capability set (tests/test_commit_wall.py).
commit_wall_prefix() { # <checkout-dir> -- prints the bwrap argv prefix
    local src="$1" p ro=""
    for p in $WK_COMMIT_WALL_PATHS; do
        ro="$ro --ro-bind-try $src/.git/$p $src/.git/$p"
    done
    printf 'bwrap --dev-bind / /%s --' "$ro"
}

# A lock is a symlink whose target names the holder. Not `flock`: its fd is
# inherited by every child (`conmon` would hold ours for the workspace's life)
# and macOS ships none. Keyed by hostname: NFS homes.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

# Mounted into the podman machine at /var/lib/wk/secrets, so `wk key set` works
# with no VM running and a container reads the same bytes live.
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
    h=$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo local)
    echo "$d/$1@$h.lock"
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

# "Is it dead" then "replace it" races every other taker, so the replacement is
# a compare-and-swap made atomic by a short-lived breaker lock.
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
    local res="$1" timeout=600 f owner opid started announced=""
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
        if [ -d "$f" ] && [ ! -L "$f" ]; then
            # Checked before the `ln`: `ln -s x somedir` links *inside* it.
            opid=$(cat "$f/pid" 2>/dev/null | tr -dc '0-9') || true
            if [ -z "$opid" ] || ! kill -0 "$opid" 2>/dev/null; then
                rm -rf "$f"; continue
            fi
        elif ln -s "$(_lock_payload)" "$f" 2>/dev/null; then
            break
        else
            owner=$(readlink "$f" 2>/dev/null || true)
            opid=$(_lock_pid_of "$owner")

            if [ -n "$opid" ] && ! kill -0 "$opid" 2>/dev/null; then
                _lock_break "$f" "$owner" && break
                continue
            fi

            if [ -z "$opid" ]; then
                warn "clearing a lock file with no holder in it: $f"
                rm -rf "$f"; continue
            fi
        fi

        if [ -z "$announced" ]; then
            announced=1
            info "waiting for the $res lock${opid:+ (held by pid $opid)}"
        fi
        if [ "$(( $(date +%s) - started ))" -ge "$timeout" ]; then
            die "could not take the $res lock within ${timeout}s${opid:+ -- pid $opid still holds it}"
        fi
        sleep 1
    done

    _WK_LOCK_HELD="$_WK_LOCK_HELD $f"
    _WK_LOCK_MINE="$_WK_LOCK_MINE $f"
    wk_atexit _lock_release_all
    debug "lock: $res"
}

release_locks() { _lock_release_all; }

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

WK_IMAGE_MARKER="${WK_IMAGE_MARKER:-/etc/wk-image}"

wk_image_id() { kv_field "$WK_IMAGE_MARKER" id 2>/dev/null || true; }   # the bench system's own id, or empty in host mode
wk_image_profile() { kv_field "$WK_IMAGE_MARKER" profile 2>/dev/null || true; }
in_bench_mode() { [ -f "$WK_IMAGE_MARKER" ]; }

# The compositor's start modes are indistinguishable at the Wayland socket, and
# only one of them makes a meaningful number.
WK_SESSION_MODE_FILE="${WK_SESSION_MODE_FILE:-/run/wk-session-mode}"

session_mode() { # gpu | bmc | off | none
    local m=""
    [ -r "$WK_SESSION_MODE_FILE" ] && m=$(head -1 "$WK_SESSION_MODE_FILE" 2>/dev/null | tr -dc 'a-z-')
    printf '%s' "${m:-none}"
}

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
