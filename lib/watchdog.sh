# The watched run and the job stop for a bash caller, each function one call into lib/wk/job.py; cmd/bench raises WK_STALL_SECONDS/WK_ABORT_SECONDS before sourcing this.

command -v _job_py    >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
command -v task_field >/dev/null 2>&1 || . "$WK_ROOT/lib/task.sh"

WK_ABORT_SECONDS="${WK_ABORT_SECONDS:-$(_job_py abort-seconds)}"   # a record begun here carries the watchdog's deadline

watched_kill() { _job_py kill-tree "$@"; }   # <pid> <signal> -- descendants first, never this shell

run_watched() { # <logfile> -- <command...>: its status, or 124 killed for stalling; job and watcher in the background, since `wait` is what a trapped INT/TERM/HUP interrupts
    local log="$1" pid watcher rc=0; shift; [ "${1:-}" != -- ] || shift
    : > "$log"
    "$@" >>"$log" 2>&1 &
    pid=$!
    _run_watched_interrupted() {
        kill "$watcher" 2>/dev/null || true
        watched_kill "$pid" TERM; wk_sleep 2; watched_kill "$pid" KILL
        wait "$pid" 2>/dev/null || true
    }
    on_interrupt _run_watched_interrupted
    _job_py watch "$pid" "$log" &
    watcher=$!
    wait "$watcher" || rc=$?
    [ "$rc" != 124 ] || { wait "$pid" 2>/dev/null; return 124; }
    wait "$pid"
}

_job_ask() { # <verb> <args...> -- through this shell's target; a refusal ends the caller, as a die here would
    local rc=0
    _caller_shell _job_py "$@" || rc=$?
    [ "$rc" != 3 ] || exit 1
    return "$rc"
}

_job_pid_args() { _job_ask pid-args "$@"; }        # <ws> <pid> -- its command line inside the target
job_pid_adopt() { _job_ask adopt "$@"; }           # <ws> <task dir> <pid> <patterns> -- 0 when adopted
job_kill()      { _job_ask kill "$@" "$$"; }       # <ws> <task dir> <word> -- 0 when it is gone
job_stop()      { _job_ask stop "$@"; }            # <ws> <kind> -- 0 stopped, 2 nothing running, 1 it outlived a KILL
t_kill_tree()   { _job_ask kill-tree-in "$@" || true; }   # <ws> <pid> <signal>
first_error()   { _task_py first-error "$@"; }                 # <log>
