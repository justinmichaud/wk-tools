command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

log_age() { # <log> -- seconds since modified; `stat` spells this two ways (GNU vs. BSD)
    local log="$1" now mtime
    [ -f "$log" ] || return 1
    now=$(date +%s)
    mtime=$(stat -c %Y "$log" 2>/dev/null || stat -f %m "$log" 2>/dev/null || echo "$now")
    printf '%s' $(( now - mtime ))
}

# `-A`, not `-e`: on Darwin `-e` asks for each process's environment and reports only this user's. And `comm` is a bare name on Linux and a full path on Darwin, so the name is the last path component either way -- a pattern anchored at `^` counts zero of Xcode's linkers.
_build_ps() {  # every compiler and linker running on this machine, busiest first, as `pcpu name`
    ps -A -o pcpu=,comm= 2>/dev/null | awk '
        BEGIN { split("cc1 cc1plus lto1 clang clang++ gcc g++ cc c++ ld ld-classic \
                       ld64 ld64.lld ld.lld lld ninja xcodebuild swift-frontend", a, " ")
                for (i in a) want[a[i]] = 1 }
        { cpu = $1; $1 = ""; sub(/^ +/, ""); n = split($0, p, "/")
          if (p[n] in want) printf "%s %s\n", cpu, p[n] }' \
        | sort -rn
}

# A full-LTO link is one process at ~100% and no log output for many minutes, which these two tell apart from an idle machine. Machine-wide, though: a container build's compilers name their workspace only in their cgroup, a macOS guest's run in another kernel. So they are report text (_stall_report, lib/watchdog.sh), never a verdict.
build_processes() { _build_ps | grep -c . || true; }

busiest_process() {
    _build_ps | head -1 | awk 'NF { printf "%s at %s%% CPU", $2, $1 }'
}

detach_run() { # <log> -- cmd...
    local log="$1"
    shift
    [ "${1:-}" = -- ] && shift
    [ $# -gt 0 ] || die "detach_run: nothing to run"

    ensure_dir "$(dirname "$log")" >/dev/null
    : > "$log" 2>/dev/null || true

    nohup "$@" >> "$log" 2>&1 < /dev/null &   # nohup and stdin closed: it outlives this terminal and never reaches for a tty it lacks (SIGTTIN)
    printf '%s' "$!"
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
