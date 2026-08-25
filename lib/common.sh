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

# --- sizes -------------------------------------------------------------------
#
# `stat -c %s` is GNU, `stat -f %z` is BSD, and `numfmt` is GNU only -- "not on
# macOS and never will be", as cmd/disk puts it, which carries its own copy of
# the second half for KiB. Both halves live here because the image store spans
# both kinds of machine in one operation: an image is built on a Linux host and
# the write that follows is driven from a Mac, so every size printed on that
# path was a `stat: illegal option -- c` waiting to happen. It was: `wk sysimage
# write --dry-run` could not print its own plan.
# GNU first, BSD second, and the order is the whole correctness of this.
# `stat -c %s` is the GNU spelling and fails outright on BSD/macOS, which is
# what makes it safe to try first. The reverse is not true: GNU `stat -f` means
# *filesystem* status and reads its argument as a path, so `stat -f %z file`
# looks for a file called `%z`, prints the filesystem block for `file` on
# stdout, and exits non-zero -- so with BSD first, every Linux caller got a
# paragraph of filesystem statistics followed by the number it asked for.
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
# This machine's name, as everything else in this repository spells a machine:
# lower-case, short. macOS reports the hostname with whatever capitalisation it
# was set up with ("Tolken") while the ssh aliases, the target confs and the
# fleet entries are all lower-case -- and two spellings of one machine in one
# listing read as two machines.
# The one way this repository turns a stream of status records into something a
# person looks at -- a table, a page, or a served page that keeps itself current.
#
# Here rather than in cmd/status because the *dispatcher* needs it too: on a
# macOS host a listing is assembled by two processes (this machine's targets out
# here, its containers inside the podman VM) and neither of them is the thing
# that renders it.
#
# python3 rather than more shell, and no third-party library: aligning columns
# across a listing whose widths are not known until it is complete, and emitting
# a page, are both a page of python and neither is a page of bash. Nothing on
# the fleet may need `pip install` to run `wk status` -- every machine here has
# python3 and none of them is allowed to need anything else.
status_render() {
    local mode="$1" recs="$2"
    # The stream itself renders nothing: it *is* the answer, and passing it
    # through the renderer would merge away the thing being looked at.
    [ "$mode" != records ] || { cat "$recs"; return 0; }
    require python3 "python3 renders 'wk status'; it ships with macOS and with every
    distribution here, so a machine without it is a machine with something else wrong"
    python3 "$WK_ROOT/lib/status-view.py" "$mode" "$recs" \
        ${WK_STATUS_PORT:+--port "$WK_STATUS_PORT"} \
        ${WK_STATUS_INTERVAL:+--interval "$WK_STATUS_INTERVAL"} \
        ${WK_STATUS_HTML_OUT:+--out "$WK_STATUS_HTML_OUT"}
}

# Which view a bare `wk status` is, decided in one place because two decide it:
# cmd/status renders on Linux and the dispatcher renders on a macOS host, and a
# default that differed between them would make `wk status` mean two things on
# one fleet.
#
# The page is the primary view -- it is where the colour and the structure are,
# and a fleet is a thing you look *at* rather than parse. But this command's
# stdout is also read: `wk selftest` parses the table, `wk status --wait`
# branches on the exit code, and an agent driving this repository has neither a
# browser nor a terminal. A served page would hang all three where a table
# returned. So the page is the default only where every one of these holds, and
# each is a way somebody is not a person at a terminal:
#
#   a tty on stdout    a pipe or a redirect is a program reading this.
#   nothing said       WK_STATUS_VIEW is the explicit answer, and CI is
#                      automation saying so about itself. NO_COLOR is the
#                      convention for "render me plainly", and a page is the
#                      opposite of that.
#   somewhere to open it   inside a workspace there is no browser and no display
#                      (and no host network to reach one on), and over ssh with
#                      no DISPLAY the browser would open on the machine nobody
#                      is sitting at.
#
# `--text` asks for the table from a terminal anyway, and `--web` asks for the
# page from anywhere; this only decides what a bare invocation means.
#
# It *sets a variable* rather than printing the answer, and that is the whole
# reason it is shaped this way: both callers would naturally write
# `MODE=$(status_default_mode)`, and inside a command substitution fd 1 is a
# pipe -- so `[ -t 1 ]` would be asking about the pipe this function's own
# answer travels down, and would say "not a terminal" on a terminal, every
# time. A variable is read by the substitution's subshell just as well
# (a subshell inherits the shell's variables), and cannot be wrong.
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

wk_machine_name() {
    { hostname -s 2>/dev/null || echo here; } | tr '[:upper:]' '[:lower:]'
}

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
# A shell fragment to put in front of a command being run in a workspace. It
# sets $LLDB to a debugger that starts there, or fails the command with a reason
# instead of leaving one to be worked out from a loader error.
#
# `lldb` is not the answer, and is wrong in the least helpful way available. The
# wkdev image puts /opt/swift/usr/bin first on PATH, and the Swift toolchain's
# lldb is linked against libxml2.so.2 while the image ships libxml2.so.16 -- so
# it does not start at all:
#
#   lldb: error while loading shared libraries: libxml2.so.2: cannot open
#         shared object file: No such file or directory
#
# before saying anything about itself, which reads as a broken workspace rather
# than as the wrong binary. That same image carries a working /usr/bin/lldb-22
# that nothing was reaching (measured in a 2.53-v9 container, 2026-08-24).
#
# So a candidate is *run* rather than merely found, and neither the shadowing
# nor the version number is written down: both belong to an image this repo does
# not build, and a pinned `lldb-22` would be a second copy of a fact that goes
# stale at the next bump. Newest first, so a bump is picked up rather than
# stepped over. macOS has one lldb, it is first, and it answers.
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

# lldb options that keep the debugger on the process it was pointed at, as words
# for the command line.
#
# WebKit is several processes and the debugger has to be told which one it is
# for. `~/.lldbinit` here says `settings set target.process.follow-fork-mode
# child` (dotfiles/lldbinit:10, carried in from a personal dotfile), and against
# a browser that is not a preference, it is a bug: MiniBrowser's first act is to
# fork a network process, so the debugger walks out of the UI process a second
# after `run` and every breakpoint that was set in it stops meaning anything.
#
# Stated here rather than fixed there because a command must not depend on what
# a user's ~/.lldbinit happens to say, in either direction: `-O` runs after the
# init file, so this wins wherever it is used, and nothing about someone's own
# lldb setup has to change for `wk gui --lldb` to land in the right process.
lldb_pin_opts() {
    printf '%s' "-O 'settings set target.process.follow-fork-mode parent'"
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

# --- secrets this repository needs but must never contain ---------------------
#
# ASK, DO NOT INSTRUCT. The failure this exists to end: a provisioning step that
# needs a credential printed four lines telling somebody to create a file by
# hand, warned when the file was absent, and carried on. It was absent every
# time for two days, so the one item it gated -- a tailnet identity for the
# benchmark install, and with it any way to observe a run -- never happened,
# while everything around it got done. A warning that names a manual step is a
# step that does not get taken.
#
# Same discipline as confirm(): no terminal means nobody can answer, so fail
# loudly instead of blocking forever. An unattended lane that stops on a hidden
# prompt is a hang, and a hang is worse than a refusal.
#
# The value is read with `read -rs` (no echo), never logged, never passed as an
# argument, and written 0600 through a umask so it is not briefly world-readable.
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

# The tailscale auth key: one key for the whole fleet, resolved from the one
# place it lives, by every path that joins anything to the tailnet.
#
# There is one because of the rule it serves: everything wk touches is on the
# tailnet, and a node is reached by its tailnet name with nothing about how to
# reach it written down (CLAUDE.md, "Cattle, not pets"). A join path with a key
# of its own is a second copy of a credential -- the one kind of state this
# repository refuses outright -- and, less obviously, a path that cannot run
# without somebody at a keyboard.
#
# What the key has to be, and why each word of it is load-bearing:
#
#   tagged tag:wk    the tag *is* the permission. A tagged node is owned by the
#                    tailnet rather than by a person, gets only what the policy
#                    grants tag:wk, and -- the part that matters here -- never
#                    key-expires. An untagged node works for 180 days and then
#                    goes dark, which for a board whose name is its whole
#                    address reads as dead hardware (`wk bridge` says the same
#                    at length, having been bitten).
#   reusable         one key provisions every device. A single-use key means a
#                    credential fetched by hand per board, which is the thing
#                    that ends up pasted into a script.
#   NOT ephemeral    an ephemeral node is *removed* from the tailnet when it goes
#                    offline, and its name stops resolving with it. These boards
#                    reboot between host mode and bench mode constantly; a name
#                    that survives only while the device is up is not an address.
#   longest expiry   the key's own expiry only bounds how long it can enrol new
#                    devices (90 days is tailscale's ceiling); already-joined
#                    tagged nodes are unaffected. Short expiry buys nothing and
#                    costs a re-auth in the middle of something else.
#
# Validated on the way in, because the failure it prevents is discovered after a
# reboot on a machine with no way to report it: a mistyped key means `tailscale
# up` fails during first boot of an install that then has no tailnet identity --
# which is precisely the state that makes the failure invisible.
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
#
# One EXIT trap for the whole process, and a list of handlers under it.
#
# There was one trap and several claimants: `hold_lock` installed
# `trap _lock_release_all EXIT`, and so did `barrier`, `cmd/new`'s driver,
# `cmd/image`'s seed cleanup and four commands' prefetch reapers -- and bash
# keeps only the last one set. Every one of those was correct on its own and
# silently disabled whichever had been set before it, which for a lock means
# the lock outlives the command that took it and is cleared only by the next
# taker's liveness check. That works (that is why the bug was invisible), but
# it turns "the lock dies with its holder" from a property into a repair.
#
# So handlers register instead of trapping. Registration is idempotent and
# ordered, a handler that fails cannot stop the ones after it, and the exit
# status is preserved -- a trap that ends in a non-zero command would otherwise
# change what the command exits with.
_WK_ATEXIT=""

_wk_run_atexit() {
    local _rc=$? _h
    # Published rather than passed: a handler that reports how the command
    # ended (cmd/new's driver) needs it, and `trap 'f $?' EXIT` -- the only way
    # to pass it as an argument -- is exactly the single-claimant trap this
    # replaces.
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

# Deliberately *not* offered: a wk_atexit_remove. `cmd/new`'s refusal path
# wants "do not record a failure I did not cause", which is a decision the
# handler itself can make from its own state -- and a registry anything can
# take entries out of is one where the lock release can be taken out too.

# --- a hard ceiling on wall time ----------------------------------------------
#
# `timeout(1)` is GNU and absent on macOS, and per-tool timeout flags cannot be
# trusted. ssh is the case that matters: `ConnectTimeout` covers the TCP connect
# and nothing after it, so a host that accepts port 22 and then says nothing
# leaves ssh waiting on its own timers -- and a phone half-way into suspend does
# exactly that. (BSD `nc -w` has the same shape of lie: it documents itself as
# bounding connects and ran 75 s against a black-holed address here, against
# 1.05 s for `-G1`.) So the ceiling lives here rather than in any tool's own
# flag.
#
# Worse, an ssh option is not even a bound on the whole connection: options
# given on the command line do not reach a *jump* hop. `ssh -o ConnectTimeout=4
# -o BatchMode=yes rpi4-test` builds its ProxyJump as `ssh -l user -p 22 -W
# host:port <bridge>` and passes nothing else, so the hop through the phone runs
# with the defaults -- unbounded, and free to ask a question on the terminal.
# That is what hung `wk status`'s fleet block (2026-08-24): a host-key prompt
# for the bridge, from a probe whose output was going to a file, with nothing in
# the fleet listing's 4-second budget able to stop it. The options a jump hop
# does read are the ones in its own `Host` stanza (dotfiles/ssh/config sets
# them there for the bridges), and this is the backstop for everything that
# cannot be reached that way.
#
# The whole process group is killed, not just the child: `capped 5 probe` where
# `probe` is a function runs it in a subshell, and TERM to the subshell alone
# leaves its ssh running -- holding the terminal, printing into a listing that
# has already moved on. `set -m` gives the child a process group of its own so
# one signal reaches everything it started; the plain `kill` after it is the
# fallback for a shell that gave us no job control.
capped() { # <seconds> <cmd...>
    local secs="$1"; shift
    # Job control is turned on for exactly one background start and then put
    # back the way it was found -- a caller that had it on (nothing here does,
    # but this is a shared helper) must not have it switched off underneath it.
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
    # Composes with any lock this command has taken, and with any other
    # handler: both are registrations under one trap (wk_atexit above).
    wk_atexit _forced_summary
    return 0
}

# --- locks --------------------------------------------------------------------
#
# Rule 4: one lock per mutated resource, and a lock is not state -- it dies
# with its holder.
#
# One mechanism on every host: a symlink whose target string names the holder.
# Two earlier attempts are recorded here, because both were tried and both were
# wrong in ways that only appear when something is killed.
#
#   `flock` is held by the open file description, so *every process that
#   inherits the descriptor* holds it. `wk new` starts a container, and podman
#   leaves `conmon` behind supervising it -- with our lock fd inherited and the
#   lock held for as long as the workspace exists. Measured 2026-08-19: after
#   one `wk new`, conmon held ws-<name>.lock and the next `wk build` on that
#   workspace waited on it forever. bash cannot mark a redirection
#   close-on-exec, so there is no fix that keeps the fd; `flock --close` only
#   applies to flock's own `-c` command form, which would mean re-exec'ing
#   every command under it. It is also one more thing to install -- macOS
#   ships no flock(1) at all.
#
#   an atomic `mkdir` with the holder's pid written inside it fixes the
#   inheritance and introduces a window: the directory exists for a moment
#   before the pid file in it does, and a process killed inside that window
#   leaves a lock whose holder cannot be named -- indistinguishable from a live
#   one, and so waited out in full. Found 2026-08-19 by a `wk rm` that sat out
#   its entire timeout on rubble (docs/HANDOFF-yocto.md).
#
# A symlink has neither problem. `ln -s` is atomic *and* carries the payload
# with it, so a lock can never exist without a holder written in it; there is
# no descriptor for a child to inherit; and it needs nothing installed. The
# payload is read with readlink and never resolved -- the target is not a path
# and nothing here follows it.
#
# A lock whose holder is gone is broken by the next taker, which is what makes
# it die with its holder even under kill -9. Breaking it is a rename over the
# dead link rather than an unlink and a re-create, so there is never an instant
# with no lock at all, and two takers breaking the same lock settle it by
# reading back what they wrote: one sees its own payload and proceeds, the
# other sees the winner's and goes back to waiting.
#
# The cost is that there is no shared mode, so `-s` is accepted and serialises
# anyway: it only ever waits more than asked, never less.
#
# Locks live under the invoking machine's own state directory and are keyed by
# that machine's hostname, because a home directory can be shared by several
# build machines over NFS -- and a pid is only meaningful on the machine it
# came from. Two machines under one home then lock their own resources, which
# is what "per mutated resource" means when the resource is per machine.
#
# What a lock here cannot do is serialise against work that is not a process on
# this machine: a build detached into a container or handed to a build box
# outlives the command that started it, and nothing here can be held by
# something that is not here. That half is evidence at the artifact instead --
# ws_busy_reason in lib/target.sh -- and the two are used together.
# This machine's own state directory, and the parent of wk_lock_dir's.
#
# It lives here, and not in lib/store.sh where it was, because it had already
# been moved once for this exact failure and the move did not go far enough.
# store.sh's own comment records the first round: a helper reachable only
# through lib/target.sh silently disappeared for every command that sourced
# image.sh without target.sh, resolving the image store to `/images` and
# pruning nothing, with no error but a `command not found` on stderr.
#
# The second round was the same shape one level up. `wk` sources common.sh and
# target.sh but *not* store.sh, and target_all() in target.sh calls this -- so
# on a macOS host, where `wk ls` and `wk status` walk targets inside `wk`
# itself rather than exec'ing cmd/ls (which does source store.sh), the walk
# died on `wk_state_dir: command not found` and reported a truncated,
# confident-looking workspace list. Found 2026-08-22 on tolken, where it hid
# the two macOS guests the benchmark lane needs.
#
# common.sh is the floor: every file that sources store.sh sources this too
# (checked, all 35 of them), so there is no lower level for it to fall out of.
wk_state_dir() { echo "${XDG_STATE_HOME:-$HOME/.local/state}/wk"; }

wk_lock_dir() { echo "${WK_LOCK_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/wk/locks}"; }

# Two lists, because they answer two different questions.
#
#   _WK_LOCK_HELD   what *this* scope must release when it ends.
#   _WK_LOCK_MINE   what this process already holds, for re-entrancy.
#
# They differ inside `with_lock`, which clears the first (its subshell must not
# release the caller's locks) and keeps the second (a `with_lock X` inside a
# command already holding X must not wait for itself).
#
# Re-entrancy is decided from this list and never from the pid in the lock,
# and that is the whole reason the list exists: subshells share `$$`, so a
# command that starts several of them in parallel -- each taking the same lock
# before touching the same file -- would have every one of them recognise its
# own parent's pid and walk straight in. Measured: twelve concurrent takers,
# twelve simultaneous critical sections, a counter that ended at 1.
_WK_LOCK_HELD=""
_WK_LOCK_MINE=""
_WK_LOCK_PAYLOAD=""

# This scope's identity, written into every lock it takes and compared before
# releasing one, or before believing it won a race to break a dead one.
#
# Two fields, because the two questions have different answers:
#
#   pid=  is the holder still alive? `$$` is the command's pid, which is what
#         a *later* command's liveness check needs.
#   tok=  am I the one that wrote this? `$$` cannot answer that: every
#         subshell of one command shares it, so two of them breaking the same
#         dead lock at the same moment would write byte-identical payloads and
#         both read one back and conclude they had won. Measured: twelve
#         takers, one counter, a final value of 9. The token is four bytes of
#         urandom, taken once per scope.
#
# Computed once and kept: it is an identity, not a timestamp, and a lock taken
# twice must read as the same holder both times.
# Made once, by a call and never by a substitution: `$(...)` runs in a subshell,
# so a payload minted in there would be lost the moment it was read and the
# next read would mint a different one -- which is the same identity crisis the
# token exists to prevent, arrived at from the other side. hold_lock calls this
# first, in its own scope, and everything after it only reads the variable.
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
        # Only if it is still ours. A lock we were judged dead for and lost to
        # another taker belongs to that taker now, and removing it would leave
        # the resource unprotected while it is being written.
        [ "$(readlink "$f" 2>/dev/null || true)" = "$(_lock_payload)" ] || continue
        rm -f "$f"
    done
    _WK_LOCK_HELD=""
    _WK_LOCK_MINE=""
    return 0
}

# Break a lock whose holder is gone -- exactly once, however many takers saw
# the same dead lock at the same moment.
#
# "Is the holder dead" and "replace it" are two operations, and the gap between
# them is a race with every other taker that read the same thing: A replaces the
# dead lock and starts working; B, which read that same dead payload a moment
# earlier, replaces *A's* fresh lock and starts working too. Measured under the
# contention check in `wk selftest --quick`: twelve takers, eleven critical
# sections, one increment silently lost. Reading the link back after the rename
# does not fix it -- at the instant A read it back, A really had won.
#
# So the replacement is a compare-and-swap, and the thing that makes it atomic
# is one more of the same atomic create: a short-lived breaker lock, held for
# the microseconds of a readlink and a rename. Inside it, the swap happens only
# if the lock still holds the *same* dead payload the caller saw -- so a taker
# whose observation has been overtaken does nothing at all and goes back to
# waiting, which is now the truth.
_lock_break() {
    local f="$1" seen="$2" bf="$f.breaking" tmp bpid rc=1

    if ! ln -s "$(_lock_payload)" "$bf" 2>/dev/null; then
        # Someone else is breaking it. Unless they died in the two syscalls it
        # takes, in which case this clears the way and the caller comes round
        # again -- the same rule as the lock itself, applied to the breaker.
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
#
# Takes the lock and holds it for the life of this process -- which is what a
# long, linear command wants (`wk build` locks the workspace it is building
# from the moment it starts writing status to the moment it exits). Dropped by
# an EXIT handler, and by the next taker's liveness check if that never runs.
#
# Re-entrant: a second call for a lock this process has already taken returns
# at once. Without that, a command that takes the same resource twice waits on
# itself until the timeout -- a deadlock against nobody. "Already taken" is
# this process's own list and not the pid in the lock; see _WK_LOCK_MINE.
#
# Before an `exec` that replaces this process with something long-lived (`wk
# new --zed`), call release_locks: the pid stays alive, so the lock would be
# held for as long as the editor is open.
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
            # Dealt with *before* the `ln` and not after it, because `ln -s x
            # somedir` does not fail -- it creates a link inside the directory
            # and reports success, which would hand this process a lock that
            # somebody else is holding.
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
                # The holder is gone. Its lock is not state and must not
                # outlive it.
                _lock_break "$f" "$owner" && break
                continue
            fi

            if [ -z "$opid" ]; then
                # Something at the path with no holder written in it: not a
                # lock this or any earlier wk-tools wrote. There is nothing to
                # wait for, and waiting would be waiting on nobody.
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
    # A subshell, so the EXIT handler that drops the lock runs when the command
    # ends rather than when this process does. `$$` is still this process's pid
    # inside it, which is the right holder to record: the subshell is where the
    # work happens, but the lifetime the lock is bounded by is this one.
    #
    # The held-list is cleared inside it, and that is not tidiness: the subshell
    # inherits this process's list, and the handler at its end would otherwise
    # drop every lock the *caller* still holds -- in the middle of the caller's
    # critical section, from a scope that has no idea it exists.
    ( _WK_LOCK_HELD=""; hold_lock "$res" $args; "$@" )
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

# --- dates, on two platforms that disagree about them -------------------------
#
# GNU date and BSD date share no syntax for the two conversions this repo needs,
# and the failure is not subtle: `date -u -d @1786800736` on macOS prints
# "illegal option -- d" and a usage block. That is how `wk boot rpi5 --status`
# came to be unrunnable from this Mac -- the fleet is meant to be drivable from
# either workstation, and one of them could not read a machine's boot time.
#
# GNU form first, BSD second, because the Linux workstation runs these far more
# often; either way the caller gets a string or nothing.
epoch_to_utc() {
    date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
        || true
}

# An ISO-8601 UTC stamp (the format everything here writes) back to seconds.
# Prints 0 when it cannot be parsed, because every caller is comparing it and a
# comparison against nothing is a shell error rather than a false answer.
utc_to_epoch() {
    date -u -d "$1" +%s 2>/dev/null \
        || date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null \
        || echo 0
}

# --- which role is this machine in? ------------------------------------------
#
# A machine that has been booted into an image (docs/HANDOFF-boot.md) says
# so in one file, written by the image and absent from every normal install.
# One name for it, because three things read it: the boot driver deciding which
# role answered, `wk bench staged` refusing to call a workstation run a
# bare-metal one, and the image's own provisioning writing it.
#
# Overridable so both can be exercised without a second machine -- the runner
# was tested by pointing it at a marker in a scratch directory, which is the
# only way to reach bench mode's code path from host mode.
WK_IMAGE_MARKER="${WK_IMAGE_MARKER:-/etc/wk-image}"

# The bench system's own id, or empty in host mode.
wk_image_id() { sed -n 's/^id=//p' "$WK_IMAGE_MARKER" 2>/dev/null || true; }
# The profile it was built from -- `perf-linux-rpi5`, `downstream-yocto-...`.
# Two systems from two profiles are two configurations of the machine (clocks,
# fan policy, kernel), so a run records this beside the id: the id says which
# artifact, the profile says which configuration it stands for.
wk_image_profile() { sed -n 's/^profile=//p' "$WK_IMAGE_MARKER" 2>/dev/null || true; }
in_bench_mode() { [ -f "$WK_IMAGE_MARKER" ]; }

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
