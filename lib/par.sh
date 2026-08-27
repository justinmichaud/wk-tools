# Several probes at once, without shuffling the listing. Sourced by
# cmd/status, which defines `bump <rc>` (the worst status seen so far) and
# writes each job's records to fd 3 -- one file per job (fd 3 is a byte
# stream; two writers interleave mid-line), concatenated in start order so
# the listing never reshuffles. `wait` is always by pid, never bare, which
# would also catch the fleet probes left running beside the walk. Every
# job's stdin is /dev/null: concurrent ssh must not compete for a
# terminal, or a background reader is stopped by SIGTTIN.
_par_dir=""
_par_names=""
_par_pids=""

par_begin() {
    _par_dir=$(mktemp -d "${TMPDIR:-/tmp}/wk-par.XXXXXX")
    _par_names=""
    _par_pids=""
}

par_cleanup() { [ -z "$_par_dir" ] || rm -rf "$_par_dir"; }  # wk_atexit: a killed run leaves nothing behind

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

par_join() {
    local n p rc
    for p in $_par_pids; do rc=0; wait "$p" || rc=$?; bump "$rc"; done
    for n in $_par_names; do cat "$_par_dir/$n" >&3 2>/dev/null || true; done
    rm -rf "$_par_dir"
    _par_dir=""; _par_names=""; _par_pids=""
    return 0
}

# par_join_stream -- the same batch, but a job's records reach fd 3 in
# completion order, not start order, so a slow job no longer holds back
# everything after it. A `{"kind":"flush"}` record follows each job's
# records, telling a streaming reader (lib/status-view.py) it's safe to
# draw what has accumulated. Polled, not `wait -n`: bash 3.2 has no such builtin.
par_join_stream() {
    local n p rc pending="$_par_names" next
    while [ -n "$pending" ]; do
        next=""
        for n in $pending; do
            if [ -f "$_par_dir/$n.rc" ]; then
                rc=$(cat "$_par_dir/$n.rc" 2>/dev/null); rc="${rc:-4}"
                bump "$rc"
                cat "$_par_dir/$n" >&3 2>/dev/null || true
                printf '{"kind":"flush"}\n' >&3
            else
                next="$next $n"
            fi
        done
        pending="$next"
        [ -z "$pending" ] || sleep 0.1
    done
    for p in $_par_pids; do wait "$p" 2>/dev/null || true; done
    rm -rf "$_par_dir"
    _par_dir=""; _par_names=""; _par_pids=""
    return 0
}
