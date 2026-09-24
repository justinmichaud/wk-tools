# Detached jobs for a bash caller: each function is one call into lib/wk/job.py, the one implementation.

command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"

_job_py() {
    env PYTHONPATH="$WK_ROOT/lib" ${WK_STORE:+"WK_STORE=$WK_STORE"} ${WK_POLL_SECONDS:+"WK_POLL_SECONDS=$WK_POLL_SECONDS"} \
        ${WK_STALL_SECONDS:+"WK_STALL_SECONDS=$WK_STALL_SECONDS"} ${WK_ABORT_SECONDS:+"WK_ABORT_SECONDS=$WK_ABORT_SECONDS"} \
        ${WK_HEARTBEAT_SECONDS:+"WK_HEARTBEAT_SECONDS=$WK_HEARTBEAT_SECONDS"} ${WK_KILL_WAIT:+"WK_KILL_WAIT=$WK_KILL_WAIT"} \
        python3 -m wk.job "$@"
}

_caller_shell() { # <cmd...> -- what Python reaches a workspace or a far machine through: this shell's functions and state, which live nowhere else
    local f rc=0
    f=$(mktemp "${TMPDIR:-/tmp}/wk-shell.XXXXXX") || return 1
    { declare -p; declare -f; } > "$f" 2>/dev/null
    WK_CALLER_SHELL="$f" "$@" || rc=$?
    rm -f "$f"
    return "$rc"
}

log_age()     { PYTHONPATH="$WK_ROOT/lib" python3 -m wk.record log-age "$@"; }   # <log> -- seconds since modified
detach_run()  { local log="$1"; shift; [ "${1:-}" != -- ] || shift; _job_py detach "$log" -- "$@"; }   # <log> -- cmd...
detach_remote() { # <ssh-fn> <log> <rc-file> -- cmd...; <ssh-fn> runs one shell line on the far machine
    local fn="$1" log="$2" rc="$3"; shift 3; [ "${1:-}" != -- ] || shift
    _caller_shell _job_py remote "$fn" "$log" "$rc" -- "$@"
}
detach_wait_remote() { _caller_shell _job_py wait-remote "$@"; }   # <ssh-fn> <log> <rc-file> [interval] [stream] [timeout] [abort-re]
