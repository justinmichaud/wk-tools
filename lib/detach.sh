# One detach primitive, and the status-file schema every detached thing writes.
#
# Two commands here outlive the connection that started them -- `wk build
# --babysit` and now `wk new` -- and a third (`wk build --detach`) hands the
# work to the far machine and returns. Each had grown its own nohup, its own
# status file and its own idea of what "still running" means, and the
# babysitter's was the only one with the details right: nohup with stdin closed
# and both streams redirected, a pid written for liveness, and a reader that
# treats the file as a claim and the process table as the fact.
#
# So it is extracted rather than copied. What it is *not* is a daemon: nothing
# here runs between commands, nothing supervises anything, and a status file is
# never consulted for a decision that evidence next to the artifact can answer
# (docs/HANDOFF-workspace-state.md, "The rules").
#
#   detach_run <status-file> <log> -- cmd...    start it; prints the pid
#   status_write <file> <key=value>...          atomic write, adds updated=
#   status_field <file> <key>                   one field, tolerantly
#   detach_alive <file> [fallback-pid]          is the driving process still here?
#   detach_wait <file> <log> [timeout] [pid]    follow the log; prints the final state
#
# The schema (docs/HANDOFF-workspace-state.md, "The status-file schema"):
#
#   state=…      a hint; evidence decides
#   pid=…        the driving process, for liveness -- this host only
#   log=…        where the words went
#   stage=…      what it is doing now, for a human
#   updated=…    absolute UTC, for display and staleness only
#
# Anything else a concern needs is its own business, and every reader ignores
# keys it does not know.

# The states that mean "not finished". Named here because the writers and the
# waiter have to agree, and a waiter that guesses would either return early on
# a state it had not heard of or hang forever on one it had.
_DETACH_RUNNING=" starting creating running building fixing "

status_field() { kv_field "$1" "$2"; }

# Atomic, because a truncated status file is an expected input on a machine that
# can lose power mid-write: the tmp file is in the same directory as the target,
# so the rename cannot cross a filesystem and cannot be half-done.
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

# Is the driving process still here?
#
# The caller's own pid wins over the file's, when it has one: something that
# started a process by fork knows exactly which process it is waiting for, and
# a file it has not written yet -- or has not written *since the last run* --
# cannot tell it anything better. Everything else (`wk status`, `wk rm`) did
# not start it and has only the file.
detach_alive() {
    local p="${2:-}"
    [ -n "$p" ] || p=$(status_field "$1" pid)
    [ -n "$p" ] || return 1
    kill -0 "$p" 2>/dev/null
}

# detach_run <status-file> <log> -- cmd...
#
# nohup, stdin closed, both streams into the log. All three matter: the process
# has to survive its parent's terminal going away, must never reach for a tty
# it does not have (that earns a SIGTTIN in the background), and its output has
# to land somewhere a human can read afterwards -- there is no terminal to
# print to by then.
#
# The status file's *content* is written by the child, not here: it is the child
# that takes the lock on what it mutates, and a status file with two writers is
# a status file that reports whichever of them wrote last. What happens here is
# the other half of that -- last run's file is removed, for the same reason the
# log is truncated: this is a new run, and the previous attempt's words are not
# this one's.
#
# Not tidiness. Measured 2026-08-19, re-running `wk new` over a half-made
# workspace: the waiter's first poll read the *previous* attempt's file, found
# `state=creating` with a pid that had been killed minutes ago, and reported
# the run it had just started as crashed -- while the new driver went on to
# finish the workspace perfectly. A stale `state=failed` would have done the
# same thing faster.
detach_run() {
    local sf="$1" log="$2"
    shift 2
    [ "${1:-}" = -- ] && shift
    [ $# -gt 0 ] || die "detach_run: nothing to run"

    ensure_dir "$(dirname "$log")" >/dev/null
    # Unless it describes something still running: two commands starting the
    # same work is a race the caller should have refused, and if one slips
    # through, deleting the winner's record of itself is the worst of the two
    # available mistakes.
    detach_alive "$sf" || rm -f "$sf"
    : > "$log" 2>/dev/null || true

    nohup "$@" >> "$log" 2>&1 < /dev/null &
    printf '%s' "$!"
}

# detach_wait <status-file> <log> [timeout] [pid]
#
# Follow the detached run in the foreground and print the state it ended in --
# `crashed` when the process is gone without having written one, `timeout` when
# it outlasted the bound. Never a state of its own invention otherwise: the
# child says how it ended and this reports it.
#
# Foreground on purpose (docs/HANDOFF-workspace-state.md, step 3): if this end
# dies, only the waiting dies. The work continues, and re-running the same
# command is the resume.
detach_wait() {
    local sf="$1" log="$2" timeout="${3:-0}" pid="${4:-}"
    local st waited=0 tail_pid=""

    # The log is the only progress there is -- the child is a normal command
    # writing to it -- so it is streamed rather than summarised. Started from
    # the first line, because the child has usually written some by now.
    if [ -f "$log" ]; then
        tail -n +1 -f "$log" >&2 &
        tail_pid=$!
    fi

    while :; do
        st=$(status_field "$sf" state)
        case "$_DETACH_RUNNING" in
            *" ${st:-starting} "*) ;;
            *) break ;;
        esac

        if ! detach_alive "$sf" "$pid"; then
            # One more pass before calling it: the child may be in the middle
            # of writing its final state right now.
            sleep 1
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
        sleep 1
        waited=$((waited + 1))
    done

    if [ -n "$tail_pid" ]; then
        # A moment for the child's last lines to reach the log before the
        # follower is stopped, or the words that explain a failure are the ones
        # that get cut.
        sleep 1
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    fi

    printf '%s' "${st:-unknown}"
}
