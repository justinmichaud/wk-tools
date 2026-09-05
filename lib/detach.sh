# One detach primitive and the status-file schema every detached thing writes: the file is a claim, the process table the fact.

command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

_DETACH_RUNNING=" starting creating running building fixing "  # writers and the waiter must agree

status_field() { kv_field "$1" "$2"; }

status_write() {  # the tmp file is in the target's own directory, so the rename cannot cross a filesystem
    local f="$1" tmp kv
    shift
    tmp="$f.tmp.$$"
    {
        for kv in "$@"; do printf '%s\n' "$kv"; done
        printf 'updated=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    } > "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$f"
}

detach_alive() {  # the caller's own pid wins over the file's: whatever forked a process knows which
    local p="${2:-}"
    [ -n "$p" ] || p=$(status_field "$1" pid)
    [ -n "$p" ] || return 1
    kill -0 "$p" 2>/dev/null
}

log_age() { # <log> -- seconds since modified; `stat` spells this two ways (GNU vs. BSD)
    local log="$1" now mtime
    [ -f "$log" ] || return 1
    now=$(date +%s)
    mtime=$(stat -c %Y "$log" 2>/dev/null || stat -f %m "$log" 2>/dev/null || echo "$now")
    printf '%s' $(( now - mtime ))
}

# A build carries no pid -- a pid on one end of an ssh is not a fact on the other -- so a `running` status file counts only while the log has moved within WK_STALL_SECONDS.
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

# nohup with stdin closed: the job outlives the parent's terminal and never reaches for a tty it lacks (SIGTTIN). An empty <status-file> means "not mine to touch".
detach_run() { # <status-file> <log> -- cmd...
    local sf="$1" log="$2"
    shift 2
    [ "${1:-}" = -- ] && shift
    [ $# -gt 0 ] || die "detach_run: nothing to run"

    ensure_dir "$(dirname "$log")" >/dev/null
    [ -z "$sf" ] || detach_alive "$sf" || rm -f "$sf"  # not while it runs: overwriting a racing winner's own record is worse
    : > "$log" 2>/dev/null || true

    nohup "$@" >> "$log" 2>&1 < /dev/null &
    printf '%s' "$!"
}

# INT/TERM must not touch the job, only the local `tail -f` reader this started.
detach_wait() { # <status-file> <log> [timeout] [pid] -- prints the state it ended in
    local sf="$1" log="$2" timeout="${3:-0}" pid="${4:-}"
    local st waited=0 tail_pid=""

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
            wk_sleep 1  # one more pass: the child may be mid-write of its final state
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
        wk_sleep 1  # a moment for the child's last lines to reach the log
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    fi

    printf '%s' "${st:-unknown}"
}

# nohup blocks the SIGHUP a closing ssh session delivers and disown drops the child from the remote job table -- without `& disown` a job dies on "Broken pipe". No `setsid`: it is not on every far side.
detach_remote() { # <ssh-fn> <log> <rc-file> -- cmd...; <ssh-fn> runs one shell command line on the far machine and prints its output
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

# Silence is reported, never acted on. Staleness keys off <log>'s size, not mtime: there is no local inode. [abort-re] is greped for every poll, since a wedged browser writes a traceback but no result.
detach_wait_remote() { # <ssh-fn> <log> <rc-file> [interval] [stream] [timeout] [abort-re]
    local sshfn="$1" log="$2" rc="$3" interval="${4:-30}"
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

        size=$("$sshfn" "wc -c < $(sh_quote "$log") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true  # || true: under pipefail a missing log would kill the caller
        now=$(date +%s)
        if [ -n "$size" ] && [ "$size" -gt "$last_size" ]; then
            [ "$stream" = 1 ] && { "$sshfn" "tail -c +$((last_size + 1)) $(sh_quote "$log")" >&2 2>/dev/null || true; }
            last_size=$size; last_change=$now; warned=0
        fi

        rcval=$("$sshfn" "cat $(sh_quote "$rc") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true
        if [ -n "$rcval" ]; then
            if [ "$stream" = 1 ]; then  # one more read: stopping at the rc file loses the last lines
                size=$("$sshfn" "wc -c < $(sh_quote "$log") 2>/dev/null" 2>/dev/null | tr -dc '0-9') || true
                [ -n "$size" ] && [ "$size" -gt "$last_size" ] \
                    && { "$sshfn" "tail -c +$((last_size + 1)) $(sh_quote "$log")" >&2 2>/dev/null || true; }
            fi
            printf '%s' "$rcval"; return 0
        fi

        idle=$(( now - last_change ))
        if [ "$idle" -ge "${WK_STALL_SECONDS:-300}" ] && [ "$warned" -eq 0 ]; then
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
