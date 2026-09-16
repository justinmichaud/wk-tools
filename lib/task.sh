# One record for every long-running command: a directory under $WK_STORE/task,
# one file per field, so every write is one tmp+rename. Liveness is asked of
# the process table at read time, never stored.

command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"
command -v log_age  >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"

task_root() { printf '%s' "$WK_STORE/task"; }

# A waiter's stamp precedes its driver, and task_wait ignores every record older than it: the last record of a kind and name is a previous run's until the driver writes its own.
task_stamp() { date -u +%Y%m%dT%H%M%SZ; }

task_id() { # <kind> <name> -- the pid separates two tasks begun in one second
    printf '%s-%s-%s-%s' "$(_task_slug "$1")" "$(_task_slug "$2")" "$(task_stamp)" "$$"
}

_task_slug() { printf '%s' "$1" | tr -c 'A-Za-z0-9._-' '-'; }

_task_put() { # <file> <value...> -- one field, atomically
    local f="$1"; shift
    local tmp="$f.tmp.$$"
    printf '%s\n' "$*" > "$tmp" || { rm -f "$tmp"; return 1; }
    mv "$tmp" "$f"
}

task_field() { # <dir> <field> -- empty when the field is not recorded
    [ -f "$1/$2" ] || return 0
    tr -d '\r' < "$1/$2"
}

task_begin() { # [--holds <resource>] <kind> <where> <name> <kill-cmd> <log> <plan step>... -- prints the dir; <where> is `here` for this machine's pid, `target` for the workspace's
    local holds=""
    [ "${1:-}" != --holds ] || { holds="${2:-}"; shift 2; }
    local kind="$1" where="$2" name="$3" kill="$4" log="$5"
    shift 5
    case "$where" in here|target) ;; *) die "task_begin: where is here or target, not '$where'" ;; esac
    [ $# -gt 0 ] || die "task_begin: $kind/$name declared no plan"
    [ -n "$kill" ] || die "task_begin: $kind/$name named no kill command"
    local dir; dir="$(task_root)/$(task_id "$kind" "$name")"
    _task_prune "$kind" "$name" "$dir"
    ensure_dir "$dir" >/dev/null
    [ -z "$holds" ] || _task_put "$dir/holds" "$holds"   # before the plan, which is what publishes the record to task_list: a claim readable a moment later would be a board held by nobody
    local step
    { for step in "$@"; do printf '%s\n' "$step"; done; } > "$dir/plan.tmp.$$"
    mv "$dir/plan.tmp.$$" "$dir/plan"
    _task_put "$dir/kind"    "$kind"
    _task_put "$dir/where"   "$where"
    _task_put "$dir/name"    "$name"
    _task_put "$dir/kill"    "$kill"
    _task_put "$dir/log"     "$log"
    _task_put "$dir/machine" "$(wk_machine_name)"
    [ "$where" = target ] || _task_put "$dir/pid" "$$"
    _task_put "$dir/argv"    "$(ps -o args= -p $$ 2>/dev/null | tr -d '\n')"
    _task_put "$dir/started" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    [ -z "${WK_ABORT_SECONDS:-}" ] || _task_put "$dir/abort_after" "$WK_ABORT_SECONDS"   # past it, a reader knows the watchdog is gone rather than merely quiet
    rm -f "$dir/exit" "$dir/finished"
    rm -rf "$dir/steps"
    ensure_dir "$dir/steps" >/dev/null
    printf '%s' "$dir"
}

task_pid() { # <dir> <pid> [machine] -- the pid doing the work, once it exists
    _task_put "$1/pid" "$2"
    [ -z "${3:-}" ] || _task_put "$1/machine" "$3"
}

task_set() { # <dir> <field> <value> -- a kind's own field: a build's config, or `where` once a job has announced the pid it runs under in the target
    _task_put "$1/$2" "$3"
}

# One file per step: a graph runs several at once, and two starting together
# would race a single record. A step with no file of its own has not started.
task_step_state() { # <dir> <1-based index> <running|done|failed|skipped|pending>
    _task_put "$1/steps/$2" "$3"
}

# The scheduler's vocabulary (lib/sched.py announces one of these per step), as
# the state a reader sees. A refused step keeps its place and starts again.
task_step_event() { # <dir> <1-based index> <scheduler event>
    local state
    case "$3" in
        start)            state=running ;;
        ok|already)       state=done ;;
        failed)           state=failed ;;
        skipped|unneeded) state=skipped ;;
        refused)          state=pending ;;
        *) die "task_step_event: '$3' is no scheduler event" ;;
    esac
    task_step_state "$1" "$2" "$state"
}

task_step() { # <dir> <1-based index> -- a plan whose steps run in order: reaching one is the ones before it having ended
    local i=1
    while [ "$i" -lt "$2" ]; do task_step_state "$1" "$i" done; i=$((i + 1)); done
    task_step_state "$1" "$2" running
}

task_step_named() { # <dir> <plan step> -- by name, so a plan whose earlier step is skipped still steps to the right line
    local n
    n=$(grep -n -x -F -- "$2" "$1/plan" | cut -d: -f1 | head -1) || n=""
    [ -n "$n" ] || die "task_step_named: '$2' is not a step of ${1##*/}"
    task_step "$1" "$n"
}

task_steps() { # <dir> -- `<index> <state>` per line in plan order, whatever wrote the record
    local n=1 line state
    [ -f "$1/plan" ] || return 0
    while IFS= read -r line; do
        state=$(task_field "$1" "steps/$n")
        printf '%s\t%s\n' "$n" "${state:-pending}"
        n=$((n + 1))
    done < "$1/plan"
}

task_step_now() { # <dir> -- the 1-based index of the first step running, empty when none is
    task_steps "$1" | awk -F'\t' '$2 == "running" { print $1; exit }'
}

task_stage() { # <dir> -- the name of each step now running, one per line
    local n
    for n in $(task_steps "$1" | awk -F'\t' '$2 == "running" { print $1 }'); do
        sed -n "${n}p" "$1/plan"
    done
}

# The first verdict stands: `--kill` records `cancelled` and the driver it stopped cannot overwrite that with the failure the kill caused; task_begin clears the exit, so a re-run is not blocked by it.
task_end() { # <dir> <exit status or word>
    [ -d "${1:-}" ] || die "task_end: '${1:-}' is no task record -- the caller holds none to end (an unset YOCTO_TASK/PGO_TASK reads like this)"
    [ ! -f "$1/exit" ] || return 0
    _task_put "$1/finished" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    _task_put "$1/exit" "$2"
}

task_alive() { # <dir> -- a `target` pid means nothing to this kernel
    local dir="$1" pid
    [ -f "$dir/exit" ] && return 1
    pid=$(task_field "$dir" pid)
    [ -n "$pid" ] || return 1
    if [ "$(task_field "$dir" where)" = target ]; then
        command -v t_exec >/dev/null 2>&1 \
            || die "task_alive: $dir runs inside a workspace and no target is loaded"
        t_exec "$(task_field "$dir" name)" kill -0 "$pid" >/dev/null 2>&1
        return $?
    fi
    kill -0 "$pid" 2>/dev/null
}

# The verdicts a task is still going under: no exit recorded and a pid that is there, or one its workspace would not answer for. `wk stop --tasks` acts on these and nothing else -- a `died`, `failed` or `oom` record is over, and its kind's kill command has nothing left to stop.
task_running() { # <verdict>
    case " starting running silent unanswered " in
        *" $1 "*) return 0 ;;
    esac
    return 1
}

# <how> decides how long a `target` pid's workspace is waited on: `pid` for as long as it takes, which is what a command about to signal it needs; `capped` for WK_TASK_ASK_SECONDS, so a read-only report about a wedged workspace says `unanswered` rather than waiting on it (a `t_exec` has no bound of its own).
task_verdict() { # <dir> [pid|capped] -- starting|running|silent|died|unanswered|ok|the word task_end took
    local dir="$1" how="${2:-pid}" rc age
    case "$how" in pid|capped) ;; *) die "task_verdict: the pid is asked for as long as it takes or capped, not '$how'" ;; esac
    if [ -f "$dir/exit" ]; then
        rc=$(task_field "$dir" exit)
        case "$rc" in
            0) printf 'ok' ;;
            ''|*[!0-9]*) printf '%s' "$rc" ;;
            *) printf 'failed' ;;
        esac
        return 0
    fi
    [ -n "$(task_field "$dir" pid)" ] || { printf 'starting'; return 0; }
    if [ "$how" = pid ] || [ "$(task_field "$dir" where)" != target ]; then
        task_alive "$dir" || { printf 'died'; return 0; }
    else
        rc=0; capped "${WK_TASK_ASK_SECONDS:-5}" task_alive "$dir" >/dev/null 2>&1 || rc=$?
        case "$rc" in
            0) ;;
            1) printf 'died'; return 0 ;;   # the workspace answered: no such process
            *) printf 'unanswered'; return 0 ;;
        esac
    fi
    age=$(log_age "$(task_field "$dir" log)" 2>/dev/null) || { printf 'running'; return 0; }
    [ -n "$(task_field "$dir" abort_after)" ] || { printf 'running'; return 0; }   # silence is a verdict only against a declared deadline: a session or a tunnel has no output to produce
    if [ "$age" -le "${WK_STALL_SECONDS:-300}" ]; then printf 'running'; else printf 'silent'; fi
}

task_wait() { # <kind> <name> <log> [timeout] [pid] [floor stamp] -- the verdict it ended on, or crashed/timeout
    local kind="$1" name="$2" log="$3" timeout="${4:-0}" pid="${5:-}" floor="${6:-}"
    local st waited=0 tail_pid=""

    _task_wait_interrupted() { [ -z "$tail_pid" ] || kill "$tail_pid" 2>/dev/null || true; }   # registered before the reader starts: a signal between the two leaves a `tail -f` holding this process's stderr after it has exited
    on_interrupt _task_wait_interrupted
    if [ -f "$log" ]; then
        tail -n +1 -f "$log" >&2 & tail_pid=$!
    fi

    while :; do
        st=$(_task_wait_verdict "$kind" "$name" "$floor")
        case "$st" in starting|running|silent) ;; died) st=crashed; break ;; *) break ;; esac

        # The record is the job's own claim and the pid the fact: a driver killed before it wrote one leaves `starting` forever otherwise.
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            wk_sleep 1   # one more pass: the child may be mid-write of its final state
            st=$(_task_wait_verdict "$kind" "$name" "$floor")
            case "$st" in starting|running|silent|died) st=crashed ;; esac
            break
        fi

        if [ "$timeout" -gt 0 ] && [ "$waited" -ge "$timeout" ]; then st=timeout; break; fi
        wk_sleep 1
        waited=$((waited + 1))
    done

    if [ -n "$tail_pid" ]; then
        wk_sleep 1  # a moment for the child's last lines to reach the log
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    fi
    printf '%s' "$st"
}

_task_wait_verdict() { # <kind> <name> [floor stamp] -- starting until the driver has written a record
    local d; d=$(task_find "$1" "$2" "${3:-}")
    if [ -z "$d" ]; then printf 'starting'; else task_verdict "$d"; fi
}

_task_prune() { # <kind> <name> <dir> -- one per kind and name, keeping live ones
    local d want; want="$(_task_slug "$1")-$(_task_slug "$2")-"
    while IFS= read -r d; do
        [ -n "$d" ] && [ "$d" != "$3" ] || continue
        if _task_stamp_of "${d##*/}" "$want" >/dev/null; then
            task_alive "$d" || rm -rf "$d"
        fi
    done <<EOF
$(task_list)
EOF
}

task_find() { # <kind> <name> [floor stamp] -- prints the newest dir at or after the floor, or nothing
    local d last="" want stamp; want="$(_task_slug "$1")-$(_task_slug "$2")-"
    while IFS= read -r d; do
        stamp=$(_task_stamp_of "${d##*/}" "$want") || continue
        if [ -n "${3:-}" ] && [[ "$stamp" < "$3" ]]; then continue; fi
        last="$d"
    done <<EOF
$(task_list)
EOF
    [ -z "$last" ] || printf '%s' "$last"
}

# Exactly a stamp (and at most a pid) after the prefix, so `new-foo-` does not claim `new-foo-bar-...`.
_task_stamp_of() { # <record id> <kind-name- prefix> -- the id's stamp, or 1 when the id is another task's
    local rest="${1#"$2"}" stamp
    [ "$rest" != "$1" ] || return 1
    stamp="${rest%%-*}"
    case "$stamp" in [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z) ;; *) return 1 ;; esac
    case "${rest#"$stamp"}" in ''|-[0-9]*) ;; *) return 1 ;; esac
    printf '%s' "$stamp"
}

task_list() { # every record, oldest id first
    local d
    for d in "$(task_root)"/*/; do
        [ -f "$d/plan" ] || continue
        printf '%s\n' "${d%/}"
    done
}

# A board is a fleet resource: one task drives it at a time, and the claim is the record that declares it (`holds`) -- so there is no second store to keep in step, a holder is live by construction, and a killed driver holds nothing.
task_holders() { # <resource> -- <id>\t<machine>\t<kind> <name>\t<kill> per live task here holding it
    local d
    while IFS= read -r d; do
        [ -n "$d" ] || continue
        [ "$(task_field "$d" holds)" = "$1" ] || continue   # before the verdict: a `target` record costs a t_exec to judge
        task_running "$(task_verdict "$d" capped)" || continue
        printf '%s\t%s\t%s %s\t%s\n' "${d##*/}" "$(task_field "$d" machine)" \
            "$(task_field "$d" kind)" "$(task_field "$d" name)" "$(task_field "$d" kill)"
    done <<INNER
$(task_list)
INNER
}

# A peer that cannot be asked is a row of its own (`unknown`), never silence: an unread machine is not a free board.
fleet_holders() { # <resource> -- the same question asked of every peer workstation, each through its own wk
    command -v peer_workstations >/dev/null 2>&1 \
        || die "fleet_holders: the peers are asked through lib/target.sh, and none is loaded"
    task_holders "$1"
    local p why rows
    for p in $(peer_workstations); do
        ( load_target "$p" >/dev/null 2>&1
          why=$(machine_answers "$p" 2>&1) \
              || { _task_unknown "$p" "$(printf '%s' "$why" | tr '\t' ' ')"; exit 0; }
          rows=$(t_wk status --holds "$1" 2>/dev/null) \
              || { _task_unknown "$p" "it answers, but its wk does not read --holds: wk sync --tools $p"; exit 0; }
          [ -z "$rows" ] || printf '%s\n' "$rows" | tr -d '\r' )
    done
}

_task_unknown() { printf '?\t%s\tunknown\t%s\n' "$1" "$2"; }

device_release() { [ -z "${WK_DEVICE_TASK:-}" ] || task_end "$WK_DEVICE_TASK" "${WK_EXIT_STATUS:-0}"; WK_DEVICE_TASK=""; return 0; }

# Sets WK_DEVICE_TASK to the record holding the board, and exports the claim as WK_DEVICE_HELD so the commands one driver runs inherit it: `wk pi bench --ab-systems` runs `wk boot` for each leg, and a claim that refused its own holder would deadlock there.
device_hold() { # <machine> <kind> <name> <kill-cmd> <log> <plan step>...
    local machine="$1" kind="$2" name="$3" kill="$4" log="$5"; shift 5
    local res="device:$machine" id who what stop held="" quiet="" rows
    WK_DEVICE_TASK=""
    [ "${WK_DEVICE_HELD:-}" != "$res" ] || return 0
    rows=$(fleet_holders "$res") || exit $?   # a die inside a substitution ends only that subshell
    while IFS="$(printf '\t')" read -r id who what stop; do
        [ -n "$id" ] || continue
        case "$what" in
            unknown) quiet="$quiet
    $who -- $stop" ;;
            *)       held="$held
    $what on $who -- stop it there:  $stop" ;;
        esac
    done <<INNER
$rows
INNER
    [ -z "$quiet" ] || warn "a machine that could be driving $machine could not be asked:$quiet"
    [ -z "$held" ] || barrier "$machine is a fleet resource and another live task holds it:$held
    Two drivers on one board make both results junk."
    [ -z "${WK_DRY_RUN:-}" ] || return 0
    WK_DEVICE_HELD="$res"; export WK_DEVICE_HELD
    WK_DEVICE_TASK=$(task_begin --holds "$res" "$kind" here "$name" "$kill" "$log" "$@")
    wk_atexit device_release
}
