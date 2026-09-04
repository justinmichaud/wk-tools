# Shared helpers. Sourced by `wk`, `setup`, and everything under cmd/.
# Keep this small: logging, idempotent file operations, and OS detection.

set -euo pipefail

WK_ROOT="${WK_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
export WK_ROOT

# --- plumbing, documented once here rather than at every read site -----------
# WK_MACHINE, WK_IN_VM, WK_ROOT, WK_STORE, WK_DEBUG, WK_QUIET, WK_FORCE, WK_YES,
# WK_CMD: set by the dispatcher or a driver for its own children, so a delegated
# `wk` sees the same choice the top-level invocation made. WK_TS_AUTHKEY,
# WK_IMAGE_MARKER and WK_SESSION_MODE_FILE each override a path, for a machine
# that keeps it elsewhere or for a test.

# --- logging -----------------------------------------------------------------
# Colours only when stderr is a tty, so logs stay clean when redirected.
if [ -t 2 ]; then
    _c_dim=$'\033[2m'; _c_red=$'\033[31m'; _c_yel=$'\033[33m'
    _c_grn=$'\033[32m'; _c_off=$'\033[0m'
else
    _c_dim=''; _c_red=''; _c_yel=''; _c_grn=''; _c_off=''
fi

# WK_QUIET drops narration; warn()/die() still print.
log()  { [ -z "${WK_QUIET:-}" ] && printf '%s\n' "$*" >&2 || true; }
info() { [ -z "${WK_QUIET:-}" ] && printf '%s==>%s %s\n' "$_c_grn" "$_c_off" "$*" >&2 || true; }
warn() { printf '%swarning:%s %s\n' "$_c_yel" "$_c_off" "$*" >&2; }
die()  { printf '%serror:%s %s\n' "$_c_red" "$_c_off" "$*" >&2; exit 1; }
debug() { [ -n "${WK_DEBUG:-}" ] && printf '%s  %s%s\n' "$_c_dim" "$*" "$_c_off" >&2 || true; }

# Every mutating helper below bumps this, so `setup` can prove a re-run is a
# no-op.
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

# Whether gh can reach the API as somebody. `gh auth status` exits 0 for a
# *configured* account whose token has since expired, saying "invalid" only in
# prose on stdout; one request at the cheapest endpoint separates the two.
gh_authenticated() {
    have gh && gh api user >/dev/null 2>&1
}

# The one ssh connect timeout, read in every ssh wrapper across the tree.
# WK_SSH_TIMEOUT overrides the 10s default.
wk_ssh_timeout() { printf '%s' "${WK_SSH_TIMEOUT:-10}"; }

# The variables the dispatcher exports for the one command it is running. A
# long-lived program a command starts must not inherit them, or every `wk` typed
# in it is about that one workspace. tests/support.py reads this list.
WK_DISPATCH_VARS="WK_NAME WK_TARGET WK_TARGET_KIND WK_ROOT WK_FORCE WK_QUIET WK_ROW_LABEL WK_HOST_SELF WK_IN_VM"

# wk_exec_clean <command...> -- exec it with none of WK_DISPATCH_VARS set.
wk_exec_clean() {
    local v unset_args=""
    for v in $WK_DISPATCH_VARS; do unset_args="$unset_args -u $v"; done
    # shellcheck disable=SC2086 -- one -u per variable, deliberately split.
    exec env $unset_args "$@"
}

# --- idempotent file operations ----------------------------------------------

# Symlinks destination -> source. Existing real files are moved aside once,
# never clobbered; an already-correct symlink is left alone.
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
        # Keep the first backup; a later run must not overwrite it.
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
# Both steps are checked rather than left to errexit: a caller inside `$( )` has
# none. A caller that names a mode is *asserting* it on every run, not only at
# creation, or an existing 0755 directory stays readable by every account on the
# machine for ever. WK_DRY_RUN mutates nothing.
ensure_dir() {
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
    # Only where the mode is not already the one asked for: inside the podman
    # machine the store's secrets directory is the host's, mounted read-only and
    # already 0700, and a chmod of it fails on a read-only filesystem.
    [ -z "$mode" ] || [ "$(file_mode "$d")" = "${mode#0}" ] \
        || chmod "$mode" "$d" || die "cannot set mode $mode on $d"
}

# --- sizes -------------------------------------------------------------------
# `stat -c %s` is GNU, `stat -f %z` is BSD, and the fleet spans both. GNU first:
# it fails outright on BSD, while GNU `stat -f` means *filesystem* status and
# would print a filesystem block for the file instead of its size.
file_bytes() {
    stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0
}

# The permission bits alone, as octal without a leading zero (`700`).
file_mode() {
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

# json_merge_list <key> <file...> -- {"<key>": [...]} merged from files each
# holding zero or more JSON documents concatenated with no delimiter.
json_merge_list() { # <key> <file...>
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

# --- misc --------------------------------------------------------------------
# One `key=value` field from KEY=VALUE text on stdin, split only on the first
# "=". Exits 0 when the key is absent -- an optional field is not an error.
kv_get() {
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit }'
}

kv_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    kv_get "$k" < "$f"
}


# Here, not in cmd/status, because the *dispatcher* needs it too: on macOS a
# listing is assembled by two processes and neither renders it.
status_render() {
    local mode="$1" recs="$2"
    # The stream itself renders nothing: it *is* the answer.
    [ "$mode" != records ] || { cat "$recs"; return 0; }
    require python3 "python3 renders 'wk status'; it ships with macOS and with every
    distribution here, so a machine without it is a machine with something else wrong"
    python3 "$WK_ROOT/lib/status-view.py" "$mode" "$recs" \
        ${WK_STATUS_PORT:+--port "$WK_STATUS_PORT"} \
        ${WK_STATUS_INTERVAL:+--interval "$WK_STATUS_INTERVAL"} \
        ${WK_STATUS_HTML_OUT:+--out "$WK_STATUS_HTML_OUT"}
}

# Which view a bare `wk status` is, decided in one place since both cmd/status
# (Linux) and the dispatcher (macOS) render. *Sets a variable* rather than
# printing: inside `MODE=$(status_default_mode)` fd 1 is a pipe, so `[ -t 1 ]`
# would always say "not a terminal".
status_default_mode() {
    WK_STATUS_DEFAULT_MODE=text
    if [ -n "${WK_STATUS_VIEW:-}" ]; then
        WK_STATUS_DEFAULT_MODE="$WK_STATUS_VIEW"
        return 0
    fi
    [ -t 1 ]               || return 0
    [ -z "${CI:-}" ]       || return 0
    [ -z "${NO_COLOR:-}" ] || return 0
    # Guarded: this file is sourced by things that do not source lib/target.sh.
    if command -v in_workspace >/dev/null 2>&1 && in_workspace; then return 0; fi
    if [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && [ -z "${DISPLAY:-}" ]; then return 0; fi
    WK_STATUS_DEFAULT_MODE=web
    return 0
}
WK_STATUS_DEFAULT_MODE=text

# Lower-case: macOS reports the hostname with whatever capitalisation it was
# set up with ("Tolken"), while ssh aliases and target confs are lower-case.
wk_machine_name() {
    { hostname -s 2>/dev/null || echo here; } | tr '[:upper:]' '[:lower:]'
}

# The PATH entry if "Install CLI" ran, else the binary inside the app bundle:
# a drag-installed Zed.app has no symlink but is still installed.
zed_cli() {
    if have zed; then echo zed; return 0; fi
    local c=/Applications/Zed.app/Contents/MacOS/cli
    [ -x "$c" ] && { echo "$c"; return 0; }
    return 1
}

# Names become container names, directory names and ssh host aliases.
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

# ssh concatenates its command arguments with spaces and hands the result to a
# remote shell, so anything with a space or a quote is mangled without this.
sh_quote() {
    local arg out='' sep=''
    for arg in "$@"; do
        out="$out$sep'$(printf '%s' "$arg" | sed "s/'/'\\\\''/g")'"
        sep=' '
    done
    printf '%s' "$out"
}

# The dispatcher's global flags as an assignment prefix for the `wk` on the far
# side of a hop. Environment, not arguments: an older copy over there ignores a
# variable it does not know and dies on a flag it does not.
wk_forwarded_env() {
    printf '%s' "${WK_DEBUG:+WK_DEBUG=1 }${WK_QUIET:+WK_QUIET=1 }${WK_YES:+WK_YES=1 }${WK_FORCE:+WK_FORCE=1 }"
}

# --- which lldb, decided where it is going to run ------------------------------
# A shell fragment setting $LLDB to a debugger that starts in the workspace, or
# failing with a reason. `lldb` is not the answer: the wkdev image puts
# /opt/swift/usr/bin first on PATH, and that lldb is linked against libxml2.so.2
# while the image ships libxml2.so.16. So a candidate is *run*, not found.
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

# `~/.lldbinit` here sets follow-fork-mode child, which walks the debugger out
# of the UI process the moment MiniBrowser forks its network process. `-O` runs
# after the init file, so this wins whatever the user's own lldb setup is.
lldb_pin_opts() {
    printf '%s' "-O 'settings set target.process.follow-fork-mode parent'"
}

confirm() {
    local prompt="$1"
    [ -n "${WK_YES:-}" ] && return 0

    # No tty means nobody can answer. Decline rather than block.
    if [ ! -t 0 ]; then
        warn "$prompt -- declining (no terminal; re-run interactively, or set WK_YES=1)"
        return 1
    fi

    printf '%s [y/N] ' "$prompt" >&2
    local reply
    read -r reply || return 1
    case "$reply" in [yY]*) return 0 ;; *) return 1 ;; esac
}

# --- secrets this repository needs but must never contain ---------------------
# ASK, DO NOT INSTRUCT: a manual step named in a warning is a step that does not
# get taken. Read with `read -rs`, never logged or passed as an argument,
# written 0600 through a umask. The asking is separated from the storing because
# not every secret lands in a file this process can write.
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

# The tailscale auth key: one key for the whole fleet. What it has to be:
#   tagged tag:wk    the tag *is* the permission; untagged goes dark after
#                    180 days, which for a name-is-the-address board reads
#                    as dead hardware.
#   reusable         one key provisions every device.
#   NOT ephemeral    an ephemeral node is removed when it goes offline, and
#                    these boards reboot between host and bench mode constantly.
#   longest expiry   only bounds enrolling *new* devices.
wk_tailscale_authkey_path() { printf '%s' "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"; }

# Why this value is not the credential wk wants, or nothing when it is. The
# prefix is all that can be checked here, and worth checking because tailscale
# spells three very different powers the same way: an auth key enrolls a node,
# an API access token administers the tailnet, an OAuth client secret mints
# tokens of its own, and all three start `tskey-`. Tagged, reusable, ephemeral
# and expiry cannot be read from the key, so those are reported as unverified.
wk_tailscale_key_reject() { # <key>
    case "${1:-}" in
        tskey-auth-?*) return 0 ;;
        tskey-api-*)
            printf '%s' "that is an API access token (tskey-api-...), not an auth key.
    It administers the whole tailnet -- it can add and delete devices, rewrite
    the ACLs and mint further keys -- and this one is copied onto every card
    written from here. An auth key can only enroll a node." ;;
        tskey-client-*|tskey-oauth-*)
            printf '%s' "that is an OAuth client secret, not an auth key.
    It mints auth keys and API tokens of its own, so a board holding it holds
    everything they can do. An auth key can only enroll a node." ;;
        tskey-*)
            printf '%s' "that starts 'tskey-' but is not an auth key: an auth key is
    'tskey-auth-<id>-<secret>'." ;;
        "") printf '%s' "there is nothing there." ;;
        *)  printf '%s' "that does not look like a tailscale key at all (they start 'tskey-auth-')." ;;
    esac
    return 1
}

# No prompting, no side effects -- safe for `wk doctor`. Shape checked, since an
# empty file would otherwise read as "provisioned".
wk_tailscale_authkey_present() {
    local p; p=$(wk_tailscale_authkey_path)
    [ -s "$p" ] || return 1
    wk_tailscale_key_reject "$(head -1 "$p" 2>/dev/null)" >/dev/null
}

# --- the tailnet's control plane -----------------------------------------------
# An API access token administers the tailnet, so it lives *here only* and is
# never seeded onto a card. One power is wanted of it: retiring a node the fleet
# owns, so reprovisioning a board whose card lost its identity does not stop at
# a person with a browser (lib/tailnet.py). Optional.
# WK_TS_API_SECRET names another file to keep it in.
wk_tailscale_api_path() { printf '%s' "${WK_TS_API_SECRET:-$HOME/.config/wk/tailscale-api-key}"; }

# The mirror of wk_tailscale_key_reject: here an *auth* key is the wrong one.
wk_tailscale_api_reject() { # <key>
    case "${1:-}" in
        tskey-api-?*) return 0 ;;
        tskey-auth-*)
            printf '%s' "that is a node auth key (tskey-auth-...), not an API access token.
    An auth key enrolls a node and can do nothing else; retiring a node is an
    administrative act on the tailnet." ;;
        tskey-client-*|tskey-oauth-*)
            printf '%s' "that is an OAuth client secret, not an API access token. It mints
    tokens rather than being one; this asks for the token (tskey-api-...)." ;;
        "") printf '%s' "there is nothing there." ;;
        *)  printf '%s' "that is not a tailscale API access token (they start 'tskey-api-')." ;;
    esac
    return 1
}

# Safe for `wk doctor`. Presence is not usability: whether the tailnet still
# accepts it is a request.
wk_tailscale_api_present() {
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

# The caller has already established that the name is this fleet's;
# lib/tailnet.py adds the rest -- an exact match, never a node that is online.
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

# --- at exit ------------------------------------------------------------------
# One EXIT trap for the whole process: bash keeps only the last `trap ... EXIT`,
# so a second claimant would silently disable the first -- for a lock, outliving
# the command that took it. Handlers register instead.
_WK_ATEXIT=""

_wk_run_atexit() {
    local _rc=$? _h
    # Published, not passed: `trap 'f $?' EXIT` is the trap this replaces.
    WK_EXIT_STATUS=$_rc
    for _h in $_WK_ATEXIT; do "$_h" || true; done
    return $_rc
}

# wk_atexit <function-name>  -- run it when this process ends, whatever ends it.
wk_atexit() {
    case " $_WK_ATEXIT " in
        *" $1 "*) return 0 ;;   # already registered; registering is idempotent
    esac
    _WK_ATEXIT="$_WK_ATEXIT $1"
    trap _wk_run_atexit EXIT
    return 0
}

# No wk_atexit_remove: a registry anything can take entries out of is one where
# the lock release can be taken out too.

# --- interruption ---------------------------------------------------------
# One rule for the whole tree: a command holding a lock or a background helper
# traps INT/TERM, stops what it started, and exits with the signal's own code --
# 130 for INT, 143 for TERM. Ctrl-C at a terminal reaches the whole foreground
# group, but a supervisor tracking one pid signals *this* process alone, and a
# `nohup`'d or ssh-reached helper is not in this group at all.
# `on_interrupt <fn>` registers as `wk_atexit` does, then exits with that code.
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

# True once this process has caught INT or TERM.
interrupted() { [ -n "$_WK_INTERRUPTED" ]; }

# wk_sleep <seconds> -- `sleep` in <=1s chunks. bash defers a pending trap until
# the command it interrupted finishes (bash(1), SIGNALS), so a single `sleep 30`
# in a poll loop can leave Ctrl-C looking hung for 30s.
wk_sleep() { # <seconds>
    local remain="${1:-0}" chunk
    while [ "$remain" -gt 0 ] 2>/dev/null; do
        chunk=1; [ "$remain" -lt 1 ] && chunk="$remain"
        sleep "$chunk"
        remain=$((remain - chunk))
    done
    return 0
}

# --- a hard ceiling on wall time ----------------------------------------------
# `timeout(1)` is GNU, absent on macOS, and per-tool timeout flags lie: ssh's
# `ConnectTimeout` covers only the TCP connect, and a *jump* hop reads only its
# own `Host` stanza, so it runs unbounded.
#
# The whole process group is killed, not just the child: TERM to a subshell
# alone leaves its ssh running, holding the terminal. `set -m` gives the child a
# group of its own; the plain `kill` after it is the no-job-control fallback.
capped() { # <seconds> <cmd...>
    local secs="$1"; shift
    # Job control on for exactly one background start, then restored.
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

# --- barriers, and getting past one in a hurry --------------------------------
# A barrier is a refusal that exists because of a rule, not because the command
# cannot proceed (an unfiltered guest, passwordless sudo). Correctness failures
# are not barriers. `wk <command> --force` turns one into a warning, repeated
# when the command ends: a single line atop a long build is never seen again.
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
    # Composes with any lock this command has taken: both are registrations
    # under one trap (wk_atexit above).
    wk_atexit _forced_summary
    return 0
}

# --- the commit wall ---------------------------------------------------------
# "Only the person at the keyboard commits", the write-side twin of `wk push`:
# the parts of `.git` a commit, a stage, a stash, a branch move or a rebase must
# write are bound read-only while an agent holds the workspace.
WK_COMMIT_WALL_PATHS="objects refs logs HEAD packed-refs index.lock ORIG_HEAD"

# The bwrap prefix. bwrap needs no capability the sandbox lacks (measured: it
# works with an empty capability set), and its read-only binds cannot be
# unmounted, remounted, shadowed, or escaped through a nested user namespace
# (all measured in tests/test_commit_wall.py). A human `wk enter` shell is not
# wrapped. `--ro-bind-try` skips a path a fresh checkout has not created yet.
commit_wall_prefix() { # <checkout-dir> -- prints the bwrap argv prefix
    local src="$1" p ro=""
    for p in $WK_COMMIT_WALL_PATHS; do
        ro="$ro --ro-bind-try $src/.git/$p $src/.git/$p"
    done
    printf 'bwrap --dev-bind / /%s --' "$ro"
}

# --- locks --------------------------------------------------------------------
# Rule 4: one lock per mutated resource, dies with its holder. A symlink whose
# target string names the holder, not `flock` (its fd is inherited by every
# child -- podman's `conmon` would hold ours as long as a workspace exists --
# and macOS ships no flock(1)) and not `mkdir`+pid-file (a kill between the two
# leaves an unnameable lock). A dead holder's lock is broken by a rename over
# the dead link, never unlink-then-create. Keyed by hostname, a home directory
# being shareable over NFS. It cannot serialise against work that is not a
# process here; that half is evidence at the artifact (ws_busy_reason).
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

# The one directory on this device holding the credentials every workspace here
# needs. On a macOS workstation it is mounted into the podman machine at
# /var/lib/wk/secrets, so `wk key set` writes it with no VM running and a
# container reads the same bytes live. Here rather than in lib/store.sh: the
# mount source is a property of the device. WK_HOST_SECRETS: tests point this at
# a scratch directory.
wk_host_secrets() { echo "${WK_HOST_SECRETS:-${XDG_CONFIG_HOME:-$HOME/.config}/wk/secrets}"; }

# WK_LOCK_DIR: tests point this at a scratch directory.
wk_lock_dir() { echo "${WK_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/locks}"; }

# _WK_LOCK_HELD is what *this* scope must release; _WK_LOCK_MINE is what this
# process already holds, for re-entrancy. `with_lock` clears the first (its
# subshell must not release the caller's locks) and keeps the second.
# Re-entrancy is decided from this list, never the pid: subshells share `$$`.
_WK_LOCK_HELD=""
_WK_LOCK_MINE=""
_WK_LOCK_PAYLOAD=""

# This scope's identity, written into every lock: pid= (is the holder alive?)
# and tok= (am I the one that wrote this? `$$` cannot say, four bytes of urandom
# can). By a call never a substitution -- `$(...)` runs in a subshell.
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
        # Only if still ours: a lock lost to another taker's dead-holder check
        # belongs to them now.
        [ "$(readlink "$f" 2>/dev/null || true)" = "$(_lock_payload)" ] || continue
        rm -f "$f"
    done
    _WK_LOCK_HELD=""
    _WK_LOCK_MINE=""
    return 0
}

# Break a lock whose holder is gone -- exactly once, however many takers saw it
# dead at the same moment. The gap between "is it dead" and "replace it" races
# every other taker, so the replacement is a compare-and-swap made atomic by a
# short-lived breaker lock.
_lock_break() {
    local f="$1" seen="$2" bf="$f.breaking" tmp bpid rc=1

    if ! ln -s "$(_lock_payload)" "$bf" 2>/dev/null; then
        # Someone else is breaking it, unless they died in the two syscalls it
        # takes -- then this clears the way and the caller comes round again.
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

# hold_lock <resource> [-w seconds] [-s]
# Takes the lock for the life of this process; dropped by an EXIT handler, or by
# the next taker's liveness check. Re-entrant via _WK_LOCK_MINE, or a command
# taking the same resource twice would deadlock against itself. Before an `exec`
# into something long-lived, call release_locks.
hold_lock() {
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
            # A lock left by an older mkdir-form holder, checked before the
            # `ln`: `ln -s x somedir` succeeds by creating a link inside it.
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
                # The holder is gone; its lock must not outlive it.
                _lock_break "$f" "$owner" && break
                continue
            fi

            if [ -z "$opid" ]; then
                # Not a lock wk-tools wrote, and nothing to wait for.
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

# Drop every lock this process holds, now.
release_locks() { _lock_release_all; }

# Reading a lock without taking it, for a reporting command: a dead holder's
# lock reads as no lock here, exactly as the next taker treats it.
lock_holder_pid() { # <lock file>
    local line
    line=$(readlink "$1" 2>/dev/null || cat "$1/payload" 2>/dev/null || true)
    printf '%s' "$line" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p'
}
lock_alive() { # <resource>
    local pid
    pid=$(lock_holder_pid "$(_lock_path "$1")")
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

# with_lock <resource> [-w seconds] [-s] -- cmd...   the scoped form.
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
    # A subshell, so the EXIT handler drops the lock when the command ends, not
    # this process. The held-list is cleared inside it, or it would drop every
    # lock the caller still holds.
    ( _WK_LOCK_HELD=""; hold_lock "$res" $args; "$@" )
}

# --- the BMC's DRM device -----------------------------------------------------
# The kernel driver is the discriminator, never the card number: which
# /dev/dri/cardN is the ast depends on PCI enumeration order.
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

# --- dates, on two platforms that disagree about them -------------------------
# GNU date and BSD date share no syntax for the two conversions this repo needs
# (`date -u -d @1786800736` fails outright on macOS). GNU form first.
epoch_to_utc() {
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || true
}

# Prints 0 when unparseable, since every caller compares it and a comparison
# against nothing is a shell error rather than a false answer.
utc_to_epoch() {
    date -u -d "$1" +%s 2>/dev/null \
        || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null \
        || echo 0
}

# --- which role is this machine in? ------------------------------------------
# A machine booted into an image says so in one file, absent from a normal
# install. WK_IMAGE_MARKER overrides the path.
WK_IMAGE_MARKER="${WK_IMAGE_MARKER:-/etc/wk-image}"

wk_image_id() { kv_field "$WK_IMAGE_MARKER" id 2>/dev/null || true; }   # the bench system's own id, or empty in host mode
# Two systems from two profiles are two configurations (clocks, fan policy,
# kernel), so a run records this beside the id.
wk_image_profile() { kv_field "$WK_IMAGE_MARKER" profile 2>/dev/null || true; }
in_bench_mode() { [ -f "$WK_IMAGE_MARKER" ]; }

# --- the graphical session's mode --------------------------------------------
# `wk session` can start the compositor a few ways, indistinguishable from the
# Wayland socket alone, and only one makes a meaningful number. The privileged
# helper records which one in a file under /run.
WK_SESSION_MODE_FILE="${WK_SESSION_MODE_FILE:-/run/wk-session-mode}"

# gpu | bmc | off | none
session_mode() {
    local m=""
    [ -r "$WK_SESSION_MODE_FILE" ] && m=$(head -1 "$WK_SESSION_MODE_FILE" 2>/dev/null | tr -dc 'a-z-')
    printf '%s' "${m:-none}"
}

# Returns 0/1 so a caller that must refuse and one that only informs can share
# one wording.
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
