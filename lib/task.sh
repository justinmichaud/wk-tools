# One record for every long-running command: a directory under $WK_STORE/task,
# one file per field, so every write is one tmp+rename. Liveness is asked of
# the process table at read time, never stored.

command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"
command -v log_age  >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"

task_root() { printf '%s' "$WK_STORE/task"; }

# A waiter takes a stamp before spawning its driver and passes it to task_wait,
# which then ignores every record older than it: the last record of a kind and
# name is a previous run's until the driver has written its own.
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

task_begin() { # <kind> <where> <name> <kill-cmd> <log> <plan step>... -- prints the dir
    # <where>: `here` for this machine's pid, `target` for the workspace's.
    local kind="$1" where="$2" name="$3" kill="$4" log="$5"
    shift 5
    case "$where" in here|target) ;; *) die "task_begin: where is here or target, not '$where'" ;; esac
    [ $# -gt 0 ] || die "task_begin: $kind/$name declared no plan"
    [ -n "$kill" ] || die "task_begin: $kind/$name named no kill command"
    local dir; dir="$(task_root)/$(task_id "$kind" "$name")"
    _task_prune "$kind" "$name" "$dir"
    ensure_dir "$dir" >/dev/null
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
    # Past it, a reader knows the watchdog is gone rather than merely quiet.
    [ -z "${WK_ABORT_SECONDS:-}" ] || _task_put "$dir/abort_after" "$WK_ABORT_SECONDS"
    rm -f "$dir/exit" "$dir/finished"
    _task_put "$dir/step" 0
    printf '%s' "$dir"
}

task_pid() { # <dir> <pid> [machine] -- the pid doing the work, once it exists
    _task_put "$1/pid" "$2"
    [ -z "${3:-}" ] || _task_put "$1/machine" "$3"
}

task_set() { # <dir> <field> <value> -- a kind's own field: a build's config, or `where` once a job has announced the pid it runs under in the target
    _task_put "$1/$2" "$3"
}

task_step() { # <dir> <1-based index>
    _task_put "$1/step" "$2"
}

task_step_named() { # <dir> <plan step> -- by name, so a plan whose earlier step is skipped still steps to the right line
    local n
    n=$(grep -n -x -F -- "$2" "$1/plan" | cut -d: -f1 | head -1) || n=""
    [ -n "$n" ] || die "task_step_named: '$2' is not a step of ${1##*/}"
    task_step "$1" "$n"
}

task_stage() { # <dir> -- the name of the step now running
    local n; n=$(task_field "$1" step)
    case "$n" in ''|0) return 0 ;; esac
    sed -n "${n}p" "$1/plan"
}

# The first verdict stands, so one record has one author of its end: `--kill`
# records `cancelled`, and the driver whose job it stopped then reaches its own
# end with the failure that kill caused. task_begin clears the exit, so a re-run
# is not blocked by it.
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

# <how> decides a `target` pid's liveness: `pid` asks the workspace, which is
# what a command about to signal it needs; `log` reads the log's age instead, so
# a read-only report is not held up by a wedged workspace (a `t_exec` into one
# has no timeout of its own).
task_verdict() { # <dir> [pid|log] -- starting|running|silent|died|ok|the word task_end took
    local dir="$1" how="${2:-pid}" rc age
    case "$how" in pid|log) ;; *) die "task_verdict: liveness is read from the pid or the log, not '$how'" ;; esac
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
    fi
    age=$(log_age "$(task_field "$dir" log)" 2>/dev/null) || { printf 'running'; return 0; }
    [ -n "$(task_field "$dir" abort_after)" ] || { printf 'running'; return 0; }   # silence is a verdict only against a declared deadline: a session or a tunnel has no output to produce
    if [ "$age" -le "${WK_STALL_SECONDS:-300}" ]; then printf 'running'; else printf 'silent'; fi
}

task_wait() { # <kind> <name> <log> [timeout] [pid] [floor stamp] -- the verdict it ended on, or crashed/timeout
    local kind="$1" name="$2" log="$3" timeout="${4:-0}" pid="${5:-}" floor="${6:-}"
    local st waited=0 tail_pid=""

    # The hook first: a signal between starting the reader and registering it leaves a `tail -f` holding this process's stderr after it has exited.
    _task_wait_interrupted() { [ -z "$tail_pid" ] || kill "$tail_pid" 2>/dev/null || true; }
    on_interrupt _task_wait_interrupted
    if [ -f "$log" ]; then
        tail -n +1 -f "$log" >&2 & tail_pid=$!
    fi

    while :; do
        st=$(_task_wait_verdict "$kind" "$name" "$floor")
        case "$st" in starting|running|silent) ;; died) st=crashed; break ;; *) break ;; esac

        # The record is the job's own claim and the pid the fact: a driver killed
        # before it wrote one leaves `starting` forever otherwise.
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

# Exactly a stamp (and at most a pid) after the prefix, so `new-foo-` does not
# claim `new-foo-bar-...`.
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
