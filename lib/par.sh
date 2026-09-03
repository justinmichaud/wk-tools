# Several probes at once, without shuffling the listing: every ssh a walk of
# the fleet needs goes out together, and one machine that will not answer
# costs the walk its own timeout instead of everybody's.
#
# A job writes its records to fd 3 -- one file per job (fd 3 is a byte
# stream; two writers interleave mid-line), read back in start order so the
# listing never reshuffles. Two ways to read them: `par_join`, for a caller
# that puts every record on fd 3 itself and defines `bump <rc>` (the worst
# status seen so far, cmd/status); or `par_wait` and `par_record <name>`, for
# one that reads each job's answer itself (ws_locate, lib/target.sh). `wait`
# is always by pid, never bare, which would also catch the fleet probes left
# running beside the walk. Every job's stdin is /dev/null: concurrent ssh
# must not compete for a terminal, or a background reader is stopped by
# SIGTTIN.
#
# One batch at a time per shell -- the state below is the batch. A job is a
# subshell, so a batch a job opens is its own and cannot disturb the one it
# was started from.
_par_dir=""
_par_names=""
_par_pids=""

par_begin() {
    _par_dir=$(mktemp -d "${TMPDIR:-/tmp}/wk-par.XXXXXX")
    _par_names=""
    _par_pids=""
}

par_cleanup() { par_end; }   # wk_atexit: a killed run leaves nothing behind

# par_run <name> <command...> -- <name> is both the file and the position
# in the finished listing, unique among the batch. Each job drops a
# marker (`$n.rc`) the instant it's done, which par_join_stream polls to
# hand its records to fd 3 without waiting on jobs still running.
par_run() {
    local n="$1"; shift
    # `-e` off around the job, on inside it: any exit must still leave the
    # marker or par_join_stream waits forever. Not an `if`: bash
    # suppresses `-e` throughout a tested command, subshell included.
    ( set +e
      ( set -e; "$@" ) 3> "$_par_dir/$n"; _rc=$?
      printf '%s' "$_rc" > "$_par_dir/$n.rc"
      exit "$_rc" ) </dev/null &
    _par_names="$_par_names $n"
    _par_pids="$_par_pids $!"
}

par_end() {
    [ -z "$_par_dir" ] || rm -rf "$_par_dir"
    _par_dir=""; _par_names=""; _par_pids=""; _par_status=""
    return 0
}

# par_wait -- wait for the whole batch, leaving `<name> <rc>` pairs in
# $_par_status in start order. A variable and not stdout because `wait`
# answers only about this shell's own children: a command substitution
# would be asking about pids that are not its own.
_par_status=""
par_wait() {
    local n p rc
    _par_status=""
    set -- $_par_pids
    for n in $_par_names; do
        p="$1"; shift
        rc=0; wait "$p" || rc=$?
        _par_status="$_par_status $n $rc"
    done
    return 0
}

par_record() { cat "$_par_dir/$1" 2>/dev/null || true; }   # one job's records, for a caller reading them itself

par_join() {
    local n
    par_wait
    set -- $_par_status
    while [ $# -gt 0 ]; do bump "$2"; shift 2; done
    for n in $_par_names; do par_record "$n" >&3; done
    par_end
}

# par_join_stream -- the same batch, but a job's records reach fd 3 in
# completion order, not start order, so a slow job no longer holds back
# everything after it. A `{"kind":"flush"}` record follows each job's
# records, naming the job, so a streaming reader (lib/status-view.py) can
# draw a machine once every job feeding it is in. Polled, not `wait -n`:
# bash 3.2 has no such builtin.
par_join_stream() {
    local n p rc pending="$_par_names" next
    while [ -n "$pending" ]; do
        next=""
        for n in $pending; do
            if [ -f "$_par_dir/$n.rc" ]; then
                rc=$(cat "$_par_dir/$n.rc" 2>/dev/null); rc="${rc:-4}"
                bump "$rc"
                par_record "$n" >&3
                printf '{"kind":"flush","job":"%s"}\n' "$n" >&3
            else
                next="$next $n"
            fi
        done
        pending="$next"
        [ -z "$pending" ] || sleep 0.1
    done
    for p in $_par_pids; do wait "$p" 2>/dev/null || true; done
    par_end
}
