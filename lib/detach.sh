# One detach primitive, and the status-file schema every detached thing writes.
# For `wk build --babysit`, `wk new`, `wk build --detach`: nohup with stdin
# closed and both streams redirected, a pid for liveness, and a reader that
# treats the file as a claim and the process table as the fact. Not a daemon
# (README.md, "the state rules").
#   detach_run <status-file|''> <log> -- cmd...  start it; prints the pid
#   status_write <file> <key=value>...          atomic write, adds updated=
#   status_field <file> <key>                   one field, tolerantly
#   detach_alive <file> [fallback-pid]          is the driving process still here?
#   detach_wait <file> <log> [timeout] [pid]    follow the log; prints the final state
# The schema (README.md, "the state rules"); every reader ignores unknown keys:
#   state=…      a hint; evidence decides
#   pid=…        the driving process, for liveness -- this host only
#   log=…        where the words went
#   stage=…      what it is doing now, for a human
#   updated=…    absolute UTC, for display and staleness only

# The states that mean "not finished"; writers and the waiter must agree.
_DETACH_RUNNING=" starting creating running building fixing "

status_field() { kv_field "$1" "$2"; }

# Atomic: the tmp file is in the same directory as the target, so the rename
# cannot cross a filesystem and cannot be half-done.
status_write() {
    local f="$1" tmp kv
    shift
    tmp="$f.tmp.$$"
    {
        for kv in "$@"; do printf '%s\n' "$kv"; done
        printf 'updated=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$f"
}

# The caller's own pid wins over the file's: something that forked a process
# knows which one it's waiting for better than a maybe-stale file can.
detach_alive() {
    local p="${2:-}"
    [ -n "$p" ] || p=$(status_field "$1" pid)
    [ -n "$p" ] || return 1
    kill -0 "$p" 2>/dev/null
}

# build_live <status-file> [log] -- is that build actually running *now*?
# A `running` status file is a claim, not a fact: a build carries no pid
# deliberately, since a pid on one end of an ssh is not a fact on the other.
# State must say running *and* the log must have moved within WK_STALL_SECONDS.

# Seconds since a log last moved; `stat` spells this two ways (GNU vs. BSD).
log_age() { # <log>
    local log="$1" now mtime
    [ -f "$log" ] || return 1
    now=$(date +%s)
    mtime=$(stat -c %Y "$log" 2>/dev/null || stat -f %m "$log" 2>/dev/null || echo "$now")
    printf '%s' $(( now - mtime ))
}

build_live() { # <status-file> [log]
    local sf="$1" log="${2:-}" st now mtime
    [ -f "$sf" ] || return 1
    st=$(status_field "$sf" state)
    case " $st " in
        *" running "*|*" building "*) ;;
        *) return 1 ;;
    esac
    [ -n "$log" ] || log="$(dirname "$sf")/build.log"
    local age; age=$(log_age "$log") || return 1
    [ "$age" -le "${WK_STALL_SECONDS:-300}" ]
}

# detach_run <status-file> <log> -- cmd...
# nohup, stdin closed, both streams into the log: must survive its parent's
# terminal going away, must never reach for a tty it does not have (a
# SIGTTIN in the background), and output must land where it can be read.
# The status file's *content* is written by the child. This only removes
# last run's file (and truncates the log), since a stale one is misread as
# this run's `state=creating` with a pid killed minutes ago.
# An empty status-file argument means "not mine to touch": `wk build
# --detach`'s carries no pid (liveness is the build log's age instead,
# cmd/status), so removing it would delete a build running right now.
detach_run() {
    local sf="$1" log="$2"
    shift 2
    [ "${1:-}" = -- ] && shift
    [ $# -gt 0 ] || die "detach_run: nothing to run"

    ensure_dir "$(dirname "$log")" >/dev/null
    # Unless it describes something still running: overwriting a racing
    # winner's own record would be the worse of the two mistakes.
    [ -z "$sf" ] || detach_alive "$sf" || rm -f "$sf"
    : > "$log" 2>/dev/null || true

    nohup "$@" >> "$log" 2>&1 < /dev/null &
    printf '%s' "$!"
}

# detach_wait <status-file> <log> [timeout] [pid]
# Follows the run and prints the state it ended in -- `crashed` if the
# process is gone without having written one, `timeout` if it outlasted the
# bound, else whatever the child said. Foreground on purpose (CLAUDE.md,
# crash-only): if this end dies, only the waiting dies.
#
# So INT/TERM here must not touch the job being waited on -- only the local
# `tail -f` reader this function itself started. Composes if the caller has
# already registered its own on_interrupt handler (hold_lock, say): both run.
detach_wait() {
    local sf="$1" log="$2" timeout="${3:-0}" pid="${4:-}"
    local st waited=0 tail_pid=""

    # Streamed rather than summarised, from the first line.
    if [ -f "$log" ]; then
        tail -n +1 -f "$log" >&2 &
        tail_pid=$!
    fi

    _detach_wait_interrupted() { [ -z "$tail_pid" ] || kill "$tail_pid" 2>/dev/null || true; }
    on_interrupt _detach_wait_interrupted

    while :; do
        st=$(status_field "$sf" state)
        case "$_DETACH_RUNNING" in
            *" ${st:-starting} "*) ;;
            *) break ;;
        esac

        if ! detach_alive "$sf" "$pid"; then
            # One more pass before calling it: the child may be in the middle
            # of writing its final state right now.
            wk_sleep 1
            st=$(status_field "$sf" state)
            case "$_DETACH_RUNNING" in
                *" ${st:-starting} "*) st=crashed ;;
            esac
            break
        fi

        if [ "$timeout" -gt 0 ] && [ "$waited" -ge "$timeout" ]; then
            st=timeout
            break
        fi
        wk_sleep 1
        waited=$((waited + 1))
    done

    if [ -n "$tail_pid" ]; then
        # A moment for the child's last lines to reach the log first.
        wk_sleep 1
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    fi

    printf '%s' "${st:-unknown}"
}

# --- the remote pair ----------------------------------------------------------
# For a job on a machine reached over ssh and polled to completion.
# `detach_run`/`detach_wait` above read a pid this process forked and a
# status file on this filesystem, neither of which a remote job has.
#   detach_remote <ssh-fn> <log> <rc-file> -- cmd...   start it; nothing to wait on here
#   detach_wait_remote <ssh-fn> <log> <rc-file>
#       [interval] [stream] [timeout] [abort-re]        poll to completion
#
# <ssh-fn>: any function running a shell command line on the far machine and
# printing its output -- `m_ssh`, `i_ssh`, `mac_ssh <dest>`, `_pmos_sh`.
# The completion signal is one file with a number in it -- never a marker
# line grepped out of the log, which is for a human.

# detach_remote <ssh-fn> <log> <rc-file> -- cmd...
# nohup blocks the SIGHUP the ssh session closing would deliver; disown drops
# the child from the remote shell's job table. No `setsid`: not on every far
# side reached (targets/vm.sh's macOS guest lacks it) -- without `& disown`,
# a job dies on "client_loop: send disconnect: Broken pipe" at session close.
detach_remote() {
    local sshfn="$1" log="$2" rc="$3"
    shift 3
    [ "${1:-}" = -- ] && shift
    [ $# -gt 0 ] || die "detach_remote: nothing to run"

    local inner="" a
    for a in "$@"; do inner="$inner $(sh_quote "$a")"; done
    local remote_cmd="( $inner ) > $(sh_quote "$log") 2>&1; echo \$? > $(sh_quote "$rc")"

    "$sshfn" "rm -f $(sh_quote "$rc"); nohup bash -c $(sh_quote "$remote_cmd") \
>/dev/null 2>&1 </dev/null & disown" \
        || die "detach_remote: could not start the job"
}

# detach_wait_remote <ssh-fn> <log> <rc-file> [interval] [stream] [timeout]
# Poll <rc-file> until it holds a number. Never decides a job is dead on its
# own; silence is reported, not acted on, same rule as `build_live`. Keys
# staleness off <log>'s size, not mtime, since there is no local inode.
# [stream]: bytes to stderr, for `pmos_follow` (image/pmos.sh), which wants
# watching not polling. [timeout] default 0 (unlimited). [abort-re]: a
# wedged browser writes a traceback but no result, so every poll also greps <log>.
#
# INT/TERM here does not touch the remote job either, same reasoning as
# detach_wait -- it is on another machine and outlives this ssh loop by
# design. `interval` can be tens of seconds (WK_DETACH_POLL_SECONDS default
# 30), so the loop sleeps in wk_sleep's 1s chunks: a signal aimed at this
# process alone is otherwise deferred until a whole `sleep 30` elapses.
detach_wait_remote() {
    local sshfn="$1" log="$2" rc="$3" interval="${4:-${WK_DETACH_POLL_SECONDS:-30}}"
    local stream="${5:-0}" timeout="${6:-0}" abort_re="${7:-}"
    local start now last_size=0 last_change last_beat warned=0 size rcval idle waited

    start=$(date +%s); last_change=$start; last_beat=$start

    while :; do
        wk_sleep "$interval"

        waited=$(( $(date +%s) - start ))
        if [ "$timeout" -gt 0 ] && [ "$waited" -ge "$timeout" ]; then
            printf 'timeout'; return 1
        fi

        if [ -n "$abort_re" ] \
            && "$sshfn" "grep -qiE $(sh_quote "$abort_re") $(sh_quote "$log")" 2>/dev/null; then
            printf 'aborted'; return 1
        fi

        # `|| true` is load-bearing: under pipefail, a remote read failing
        # before the job creates the log would kill the caller under `set -e`.
        size=$("$sshfn" "wc -c < $(sh_quote "$log") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true
        now=$(date +%s)
        if [ -n "$size" ] && [ "$size" -gt "$last_size" ]; then
            [ "$stream" = 1 ] && { "$sshfn" "tail -c +$((last_size + 1)) $(sh_quote "$log")" >&2 2>/dev/null || true; }
            last_size=$size; last_change=$now; warned=0
        fi

        rcval=$("$sshfn" "cat $(sh_quote "$rc") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true
        if [ -n "$rcval" ]; then
            # One more read: stopping at the rc file alone loses the last
            # lines, written in quick succession -- often what says what went wrong.
            if [ "$stream" = 1 ]; then
                size=$("$sshfn" "wc -c < $(sh_quote "$log") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true
                [ -n "$size" ] && [ "$size" -gt "$last_size" ] \
                    && { "$sshfn" "tail -c +$((last_size + 1)) $(sh_quote "$log")" >&2 2>/dev/null || true; }
            fi
            printf '%s' "$rcval"; return 0
        fi

        idle=$(( now - last_change ))
        if [ "$idle" -ge "${WK_STALL_SECONDS:-900}" ] && [ "$warned" -eq 0 ]; then
            warn "no output for ${idle}s -- not stopping it; a detached job can be
  silent for a long time. Look on the far side:  tail -f $log"
            warned=1
        fi
        if [ "$stream" != 1 ] && [ $(( now - last_beat )) -ge "${WK_HEARTBEAT_SECONDS:-300}" ]; then
            log "  ... still running ($(( (now - start) / 60 ))m)"
            last_beat=$now
        fi
    done
}
