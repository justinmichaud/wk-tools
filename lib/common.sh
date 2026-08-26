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
        # Keep the first backup; a later run must not overwrite it with our own symlink.
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

# --- sizes -------------------------------------------------------------------
# `stat -c %s` is GNU, `stat -f %z` is BSD; the image store spans both kinds
# of machine in one operation (built on Linux, written from a Mac), so every
# size printed needs both spellings. GNU first: it fails outright on BSD, so
# trying it first is safe. The reverse is not: GNU `stat -f` means
# *filesystem* status and reads its argument as a path, so `stat -f %z file`
# looks for a file called `%z` and prints a filesystem block for `file` --
# BSD first would give every Linux caller a paragraph of statistics before
# the number it asked for.
file_bytes() {
    stat -c %s "$1" 2>/dev/null || stat -f %z "$1" 2>/dev/null || echo 0
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

# --- misc --------------------------------------------------------------------
# One `key=value` field from KEY=VALUE text on stdin: first matching line,
# CRLF-trimmed, split only on the first "=" so a value containing "="
# survives whole. Prints nothing and still exits 0 when the key is absent --
# an optional field is not an error under `set -e`.
kv_get() {
    awk -F= -v k="$1" '$1 == k { sub(/^[^=]*=/, ""); sub(/\r$/, ""); print; exit }'
}

# The same from a small text file; empty when the file or key is missing.
# The one parser for every such file: this and `kv_get` are the only two,
# and every reader of a `key=value` record (marker_field, status_field, a
# probe captured from ssh) goes through one or the other -- two copies of a
# tolerant parse are two tolerances that drift.
kv_field() {
    local f="$1" k="$2"
    [ -f "$f" ] || return 0
    kv_get "$k" < "$f"
}


# The one way this repository turns a stream of status records into a
# table, a page, or a served page that keeps itself current. Here, not in
# cmd/status, because the *dispatcher* needs it too: on macOS a listing is
# assembled by two processes and neither renders it. python3, not more
# shell: aligning columns whose widths aren't known until complete, and
# emitting a page, are each a page of python. No fleet machine may need
# `pip install` to run `wk status` -- all have python3, none need more.
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

# Which view a bare `wk status` is, decided in one place since both
# cmd/status (Linux) and the dispatcher (macOS) render, and a default that
# differed would make `wk status` mean two things on one fleet.

# The page is primary, but stdout is also read (`wk selftest` parses the
# table, `--wait` branches on exit code, an agent has no browser), so the
# page is default only where every one of these holds: a tty on stdout;
# nothing said (WK_STATUS_VIEW/CI/NO_COLOR silent); somewhere to open it
# (no workspace display, no DISPLAY over ssh). `--text`/`--web` override.

# *Sets a variable* rather than printing: inside `MODE=$(status_default_mode)`,
# fd 1 is the pipe carrying the answer, so `[ -t 1 ]` would always say "not
# a terminal". A variable is read by the subshell just as well.
status_default_mode() {
    WK_STATUS_DEFAULT_MODE=text
    if [ -n "${WK_STATUS_VIEW:-}" ]; then
        WK_STATUS_DEFAULT_MODE="$WK_STATUS_VIEW"
        return 0
    fi
    [ -t 1 ]               || return 0
    [ -z "${CI:-}" ]       || return 0
    [ -z "${NO_COLOR:-}" ] || return 0
    # Guarded rather than assumed: this file is sourced by things that do not
    # source lib/target.sh, and a missing function under `set -u` would take the
    # whole command down to answer a question about formatting.
    if command -v in_workspace >/dev/null 2>&1 && in_workspace; then return 0; fi
    if [ -n "${SSH_CONNECTION:-}${SSH_TTY:-}" ] && [ -z "${DISPLAY:-}" ]; then return 0; fi
    WK_STATUS_DEFAULT_MODE=web
    return 0
}
WK_STATUS_DEFAULT_MODE=text

# Lower-case: macOS reports the hostname with whatever capitalisation it
# was set up with ("Tolken"), while ssh aliases and target confs are all
# lower-case -- two spellings of one machine read as two.
wk_machine_name() {
    { hostname -s 2>/dev/null || echo here; } | tr '[:upper:]' '[:lower:]'
}

# The PATH entry if "Install CLI" ran, else the binary inside the app
# bundle: a drag-installed Zed.app has no symlink but is still installed.
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

# --- which lldb, decided where it is going to run ------------------------------
# A shell fragment to put in front of a command run in a workspace, setting
# $LLDB to a debugger that starts there, or failing with a reason.

# `lldb` is not the answer: the wkdev image puts /opt/swift/usr/bin first on
# PATH, and the Swift toolchain's lldb is linked against libxml2.so.2 while
# the image ships libxml2.so.16, so it fails to load with an error that
# reads as a broken workspace rather than the wrong binary. The same image
# carries a working /usr/bin/lldb-22 that nothing reaches.

# So a candidate is *run*, not merely found: neither the shadowing nor the
# version belongs to an image this repo builds, and a pinned `lldb-22`
# would go stale at the next bump. Newest first, so a bump is picked up.
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

# Keeps the debugger on the process it was pointed at. WebKit is several
# processes, and `~/.lldbinit` here sets follow-fork-mode child
# (dotfiles/lldbinit:10) -- against a browser that walks the debugger out of
# the UI process the moment MiniBrowser forks its network process, so every
# breakpoint stops meaning anything. Stated here, not fixed there, since a
# command must not depend on a user's ~/.lldbinit; `-O` runs after the init
# file, so this wins regardless of anyone's own lldb setup.
lldb_pin_opts() {
    printf '%s' "-O 'settings set target.process.follow-fork-mode parent'"
}

confirm() {
    local prompt="$1"
    [ -n "${WK_YES:-}" ] && return 0

    # No tty means nobody can answer. Decline rather than block: a hang in a
    # script or background run is worse than a decision not taken.
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
# ASK, DO NOT INSTRUCT: a manual step named in a warning is a step that
# does not get taken, silently skipping whatever it gated. Same discipline
# as confirm(): no terminal, fail loudly rather than block. Read with
# `read -rs` (no echo), never logged or passed as an argument, written 0600
# through a umask so it is not briefly world-readable.
prompt_secret() {  # $1 = path to store at, $2 = human description, $3 = optional URL
    local path="$1" what="$2" url="${3:-}" val=""

    [ -s "$path" ] && { printf '%s' "$path"; return 0; }

    if [ ! -t 0 ]; then
        warn "$what is needed and $path does not exist."
        warn "  No terminal, so it cannot be asked for here. Re-run interactively."
        return 1
    fi

    printf '\n' >&2
    info "$what is needed, and this repository must not contain it."
    [ -n "$url" ] && log "  get one here: $url" >&2
    log "  it is stored at $path (mode 0600) and asked for only once" >&2
    printf '  paste it (input hidden, empty to skip): ' >&2
    read -rs val || return 1
    printf '\n' >&2

    [ -n "$val" ] || { warn "nothing entered; skipping"; return 1; }

    mkdir -p "$(dirname "$path")" || return 1
    ( umask 077; printf '%s\n' "$val" > "$path" ) || return 1
    chmod 0600 "$path" 2>/dev/null || true
    info "stored in $path"
    printf '%s' "$path"
}

# The tailscale auth key: one key for the whole fleet, resolved from the
# one place it lives. One key because everything wk touches is on the
# tailnet with nothing about how to reach it written down (CLAUDE.md,
# "Cattle, not pets") -- a join path with a key of its own would be a
# second copy of a credential and a path that needs somebody at a keyboard.

# What it has to be, and why:
#   tagged tag:wk    the tag *is* the permission (owned by the tailnet, not
#                    a person, never key-expires); untagged goes dark after
#                    180 days, which for a name-is-the-address board reads
#                    as dead hardware.
#   reusable         one key provisions every device; single-use means a
#                    credential fetched by hand and pasted into a script.
#   NOT ephemeral    an ephemeral node is removed from the tailnet when it
#                    goes offline, and these boards reboot between host and
#                    bench mode constantly.
#   longest expiry   only bounds enrolling *new* devices (already-joined
#                    tagged nodes are unaffected); short expiry buys
#                    nothing and costs a re-auth mid-something-else.

# Validated on the way in: a mistyped key fails `tailscale up` during first
# boot, leaving no tailnet identity and no way to report the failure.

# One definition, since `wk doctor`, `wk sysimage write` and `wk key
# tailnet` must not disagree about where it lives.
wk_tailscale_authkey_path() { printf '%s' "${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"; }

# No prompting, no side effects -- safe for `wk doctor`: a read-only report
# must never be the thing that asks for a credential. Shape checked, not
# just presence, since an empty file or a pasted error message would
# otherwise read as "provisioned".
wk_tailscale_authkey_present() {
    local p; p=$(wk_tailscale_authkey_path)
    [ -s "$p" ] || return 1
    case "$(head -1 "$p" 2>/dev/null)" in
        tskey-*) return 0 ;;
        *) return 1 ;;
    esac
}

wk_tailscale_authkey() {
    local path="${WK_TS_AUTHKEY:-$HOME/.config/wk/tailscale-authkey}"
    local p
    p=$(prompt_secret "$path" \
        "A tailscale auth key -- tagged tag:wk, reusable, NOT ephemeral, longest expiry" \
        "https://login.tailscale.com/admin/settings/keys") || return 1
    case "$(head -1 "$p" 2>/dev/null)" in
        tskey-*) printf '%s' "$p"; return 0 ;;
        *) warn "$p does not look like a tailscale auth key (expected it to start 'tskey-')."
           warn "  Leaving it in place rather than deleting it -- check it and re-run."
           return 1 ;;
    esac
}

# --- at exit ------------------------------------------------------------------
# One EXIT trap for the whole process: bash keeps only the last `trap ...
# EXIT`, so several independent claimants (hold_lock, barrier, cmd/new's
# driver) each installing their own would silently disable whoever was set
# before -- for a lock, outliving the command that took it. So handlers
# register instead of trapping: idempotent, ordered, a failing handler
# can't stop the ones after it, and the exit status is preserved.
_WK_ATEXIT=""

_wk_run_atexit() {
    local _rc=$? _h
    # Published, not passed: `trap 'f $?' EXIT` is the single-claimant trap
    # this replaces.
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

# No wk_atexit_remove, deliberately: a registry anything can take entries
# out of is one where the lock release can be taken out too.

# --- interruption ---------------------------------------------------------
# One rule for the whole tree: a command that holds a lock or has started a
# background helper (caffeinate, a raiser, a watched build, a `tail -f`
# reader) traps INT/TERM, stops what it started, and ends with the signal's
# own exit code -- 130 for INT, 143 for TERM -- so a caller waiting on the
# exit status sees a signal, not whatever the next line happened to return.
#
# Ctrl-C at a real terminal reaches every process in the foreground process
# group at once, so a plain unhandled default disposition often looks fine.
# It is not the only way this process is interrupted: a supervisor that
# tracks one pid (`kill -INT $pid`, exactly what tests/test_interrupt.py and
# an agent's own tool cancellation do) signals *this* process alone. A
# background helper started with `&` is in the same process group and dies
# with it -- but a `nohup`'d or otherwise detached one, or a helper on a
# machine reached over ssh, is not, and default disposition leaves it
# running. Hence explicit cleanup rather than relying on group delivery.
#
# `on_interrupt <fn>` registers a handler exactly like `wk_atexit` registers
# one for EXIT (composing, most-recent-first, idempotent per name is not
# required since a scope normally registers once) -- but for INT and TERM. On
# either signal every registered handler runs once, then `exit` is called
# with the signal's code, which also fires the ordinary EXIT trap
# (wk_atexit's _lock_release_all, _forced_summary) exactly as a normal exit
# would. A registered handler needs no re-raise of its own.
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

# True once this process has caught INT or TERM -- for anything that wants to
# know rather than simply being torn down (nothing here needs that today, but
# a handler run from _wk_interrupt can check before doing further work).
interrupted() { [ -n "$_WK_INTERRUPTED" ]; }

# wk_sleep <seconds> -- `sleep`, but in <=1s chunks.
#
# A signal delivered to this process alone (not its process group) still only
# runs a trap once the *foreground* command returns -- bash defers a pending
# trap until the command it interrupted finishes (bash(1), SIGNALS) -- so a
# single `sleep 30` in a poll loop can leave Ctrl-C looking hung for up to
# 30s even with a trap installed. Chunking is what makes on_interrupt's
# promise ("ends promptly") true for every poll loop in this tree rather than
# only the ones a real terminal's process-group delivery happens to cover.
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
# `ConnectTimeout` covers only the TCP connect, so a host that accepts port
# 22 and then says nothing (a phone half-asleep) leaves ssh on its own
# timers. Worse, options on the command line don't reach a *jump* hop:
# `ssh -o ConnectTimeout=4 rpi4-test` builds its ProxyJump with nothing
# else, so the hop through the phone runs unbounded, free to hang on a
# host-key prompt nothing in the 4-second budget can stop (a jump hop reads
# only its own `Host` stanza, dotfiles/ssh/config; this is the backstop).

# The whole process group is killed, not just the child: TERM to a subshell
# alone leaves its ssh running, holding the terminal. `set -m` gives the
# child a process group of its own so one signal
# reaches everything it started; the plain `kill` after it is the fallback
# for a shell with no job control.
capped() { # <seconds> <cmd...>
    local secs="$1"; shift
    # Job control on for exactly one background start, then restored: a
    # caller with it on must not have it switched off underneath it.
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
# A barrier is a refusal that exists because of a rule, not because the
# command cannot proceed (an unfiltered guest, passwordless sudo). Correctness
# failures are not barriers -- "no such workspace" is not something to force.

# `wk <command> --force` turns every barrier into a warning, loud and
# *repeated when the command ends* -- a single line atop a long build is a
# line nobody sees again. `wk doctor` recomputes the condition on every
# run, so a forced bypass never becomes a fact anything remembers.
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
    # Composes with any lock this command has taken, and with any other
    # handler: both are registrations under one trap (wk_atexit above).
    wk_atexit _forced_summary
    return 0
}

# --- locks --------------------------------------------------------------------
# Rule 4: one lock per mutated resource, dies with its holder. A symlink
# whose target string names the holder, not `flock` (its fd is inherited by
# every child -- podman's `conmon` would hold ours for as long as a
# workspace exists -- and macOS ships no flock(1)) and not an atomic
# `mkdir`+pid-file (a kill between the two leaves an unnameable lock). `ln
# -s` is atomic *and* carries the payload, so a lock never exists without a
# holder, nothing is inherited, nothing needs installing; read with
# readlink, never resolved. A dead holder's lock is broken by a rename over
# the dead link (never unlink-then-create), so it dies with its holder even
# under kill -9, and racing breakers settle it by reading back what they wrote.

# Keyed by hostname under the invoking machine's state directory, since a
# home directory can be shared over NFS and a pid means nothing off its own
# machine. Cannot serialise against work that is not a process here (a
# detached build outlives its starting command) -- that half is evidence at
# the artifact instead (ws_busy_reason, lib/target.sh).

# Lives here, not lib/store.sh: a helper behind a higher-sourced file
# disappears silently for a command sourcing a lower file without it (`wk`
# sources common.sh but not always store.sh). common.sh is the floor.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

wk_lock_dir() { echo "${WK_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/locks}"; }

# Two lists answering two questions: _WK_LOCK_HELD is what *this* scope
# must release when it ends; _WK_LOCK_MINE is what this process already
# holds, for re-entrancy. They differ inside `with_lock`, which clears the
# first (its subshell must not release the caller's locks) and keeps the
# second (a `with_lock X` inside a command already holding X must not wait
# for itself). Re-entrancy is decided from this list, never the pid in the
# lock: subshells share `$$`, so parallel ones taking the same lock would
# each recognise their parent's pid and walk straight in.
_WK_LOCK_HELD=""
_WK_LOCK_MINE=""
_WK_LOCK_PAYLOAD=""

# This scope's identity, written into every lock and compared before
# releasing one or believing it won a race to break a dead one. Two fields:
# pid= (is the holder alive? `$$`, for a *later* command's liveness check)
# and tok= (am I the one that wrote this? `$$` can't say, since every
# subshell of one command shares it -- two breaking the same dead lock at
# once would write identical payloads and both think they won; tok is four
# bytes of urandom, taken once per scope). Computed once and kept, by a call
# never a substitution -- `$(...)` runs in a subshell, so a payload minted
# there is lost the moment it's read.
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
        # Only if still ours: a lock lost to another taker's dead-holder
        # check belongs to them now.
        [ "$(readlink "$f" 2>/dev/null || true)" = "$(_lock_payload)" ] || continue
        rm -f "$f"
    done
    _WK_LOCK_HELD=""
    _WK_LOCK_MINE=""
    return 0
}

# Break a lock whose holder is gone -- exactly once, however many takers saw
# it dead at the same moment. "Is the holder dead" and "replace it" are two
# operations, and the gap between them races every other taker who read
# the same thing (B could replace A's fresh lock, thinking it's still
# dead). So the replacement is a compare-and-swap, atomic via one more
# atomic create: a short-lived breaker lock held for a readlink and a
# rename. The swap happens only if the lock still holds the *same* dead
# payload the caller saw; an overtaken observation does nothing and waits.
_lock_break() {
    local f="$1" seen="$2" bf="$f.breaking" tmp bpid rc=1

    if ! ln -s "$(_lock_payload)" "$bf" 2>/dev/null; then
        # Someone else is breaking it, unless they died in the two syscalls
        # it takes -- then this clears the way and the caller comes round again.
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
# Takes the lock for the life of this process -- what a long, linear
# command wants (`wk build` locks its workspace start to exit). Dropped by
# an EXIT handler, or the next taker's liveness check if that never runs.

# Re-entrant via _WK_LOCK_MINE (never the pid in the lock): without it, a
# command taking the same resource twice would deadlock against itself.
# Before an `exec` into something long-lived (`wk new --zed`), call
# release_locks, or the lock is held as long as the editor is open.
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
            # A lock left by a wk-tools old enough to have used the mkdir form.
            # Before the `ln`, not after: `ln -s x somedir` succeeds by
            # creating a link inside the directory instead of failing.
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
                # No holder written in it: not a lock wk-tools wrote, and
                # nothing to wait for.
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

# Drop every lock this process holds, now. For the `exec` case above, and for
# anything that has finished mutating but has work left to do.
release_locks() { _lock_release_all; }

# with_lock <resource> [-w seconds] [-s] -- cmd...
# The scoped form: takes the lock, runs the command, drops it, for a
# critical section smaller than the command containing it.
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
    # A subshell, so the EXIT handler drops the lock when the command ends,
    # not this process. `$$` inside it is still this process's pid, the
    # right holder to record. The held-list is cleared inside it since the
    # subshell inherits this process's list, and would otherwise drop every
    # lock the *caller* still holds, mid-critical-section.
    ( _WK_LOCK_HELD=""; hold_lock "$res" $args; "$@" )
}

# --- the BMC's DRM device -----------------------------------------------------
# The kernel driver is the discriminator, never the card number: which
# /dev/dri/cardN is the ast depends on PCI enumeration order. Shared by
# cmd/session (points the benchmark compositor at it) and cmd/gui (points a
# browser client's DRM/EGL inference at it -- left to guess, it picks the
# GPU, a device the software-rendered compositor never handed it).
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
# GNU date and BSD date share no syntax for the two conversions this repo
# needs (`date -u -d @1786800736` fails outright on macOS), and the fleet
# must be drivable from either workstation. GNU form first, since the Linux
# workstation runs these far more often; either way, a string or nothing.
epoch_to_utc() {
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || true
}

# An ISO-8601 UTC stamp back to seconds. Prints 0 when unparseable, since
# every caller compares it, and a comparison against nothing is a shell
# error rather than a false answer.
utc_to_epoch() {
    date -u -d "$1" +%s 2>/dev/null \
        || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null \
        || echo 0
}

# --- which role is this machine in? ------------------------------------------
# A machine booted into an image (docs/HANDOFF-boot.md) says so in one file,
# absent from every normal install. One name for it: the boot driver, `wk
# bench staged`, and the image's provisioning all read it. Overridable so
# both roles can be exercised without a second machine.
WK_IMAGE_MARKER="${WK_IMAGE_MARKER:-/etc/wk-image}"

# The bench system's own id, or empty in host mode.
wk_image_id() { kv_field "$WK_IMAGE_MARKER" id 2>/dev/null || true; }
# The profile it was built from. Two systems from two profiles are two
# configurations (clocks, fan policy, kernel), so a run records this beside
# the id: the id says which artifact, the profile which configuration.
wk_image_profile() { kv_field "$WK_IMAGE_MARKER" profile 2>/dev/null || true; }
in_bench_mode() { [ -f "$WK_IMAGE_MARKER" ]; }

# --- the graphical session's mode --------------------------------------------
# `wk session` can start the compositor a few ways, indistinguishable from
# the Wayland socket alone, and only one makes a number that comes out
# meaningful. The privileged helper records which one in a file under /run.
# Linux-only, absent-means-none: no file on macOS or before the first session.
WK_SESSION_MODE_FILE="${WK_SESSION_MODE_FILE:-/run/wk-session-mode}"

# gpu | bmc | off | none
session_mode() {
    local m=""
    [ -r "$WK_SESSION_MODE_FILE" ] && m=$(head -1 "$WK_SESSION_MODE_FILE" 2>/dev/null | tr -dc 'a-z-')
    printf '%s' "${m:-none}"
}

# Says so, loudly, when the session is the slow one or has nothing to run
# against. Returns 0/1 so a caller that must refuse and one that only informs
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
