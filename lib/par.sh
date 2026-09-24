# A job writes to its own fd 3 file: two writers on one byte stream interleave mid-line; stdin /dev/null, or ssh takes SIGTTIN.
_par_dir=""
_par_names=""
_par_pids=""

par_begin() {
    _par_dir=$(mktemp -d "${TMPDIR:-/tmp}/wk-par.XXXXXX")
    _par_names=""
    _par_pids=""
}

par_cleanup() { par_end; }   # wk_atexit: a killed run leaves nothing behind

par_run() {  # <name> <command...>: <name> is both the record file and the listing position
    local n="$1"; shift
    ( set +e   # `-e` off outside and on inside, and not an `if` (bash suppresses `-e` when tested): an exiting job must still leave the .rc marker
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

_par_status=""
par_wait() {  # leaves `<name> <rc>` pairs in $_par_status, in start order
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
