# The task record for a bash caller, each function one call into lib/wk/record.py; device_hold leaves its record in WK_DEVICE_TASK.

command -v kv_field >/dev/null 2>&1 || . "$WK_ROOT/lib/common.sh"
command -v wk_ws_dir >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
command -v _caller_shell >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"

_task_py() {
    env PYTHONPATH="$WK_ROOT/lib" ${WK_STORE:+"WK_STORE=$WK_STORE"} ${WK_ABORT_SECONDS:+"WK_ABORT_SECONDS=$WK_ABORT_SECONDS"} \
        ${WK_STALL_SECONDS:+"WK_STALL_SECONDS=$WK_STALL_SECONDS"} ${WK_TASK_ASK_SECONDS:+"WK_TASK_ASK_SECONDS=$WK_TASK_ASK_SECONDS"} \
        python3 -m wk.record "$@"
}

_task_shell() { if declare -F t_exec >/dev/null; then _caller_shell "$@"; else "$@"; fi; }   # a record in a workspace is asked through the target this shell loaded

task_field()      { _task_py field "$@"; }
task_begin()      { _task_shell _task_py begin --pid "$$" --argv "$(ps -o args= -p $$ 2>/dev/null | tr -d '\n')" "$@"; }   # [--holds <resource>] <kind> <here|target> <name> <kill-cmd> <log> <plan step>...
task_pid()        { _task_py pid "$@"; }          # <dir> <pid> [machine]
task_set()        { _task_py set "$@"; }          # <dir> <field> <value>
task_step_state() { _task_py step-state "$@"; }   # <dir> <1-based index> <running|done|failed|skipped|pending>
task_step_event() { _task_py step-event "$@"; }   # <dir> <1-based index> <lib/wk/sched.py event>
task_step()       { _task_py step "$@"; }         # <dir> <1-based index>
task_step_named() { _task_py step-named "$@"; }   # <dir> <plan step>
task_step_now()   { _task_py step-now "$@"; }
task_stage()      { _task_py stage "$@"; }
task_end()        { _task_py end "$@"; }          # <dir> <exit status or word>
task_alive()      { _task_shell _task_py alive "$@"; }
task_verdict()    { _task_shell _task_py verdict "$@"; }   # <dir> [pid|capped]
task_find()       { _task_py find "$@"; }         # <kind> <name>
task_list()       { _task_py list; }

device_release() { [ -z "${WK_DEVICE_TASK:-}" ] || task_end "$WK_DEVICE_TASK" "${WK_EXIT_STATUS:-0}"; WK_DEVICE_TASK=""; return 0; }

device_hold() { # <machine> <kind> <name> <kill-cmd> <log> <plan step>... -- exported, so what this driver runs inherits it
    WK_DEVICE_TASK=$(_task_py hold --pid "$$" --argv "$(ps -o args= -p $$ 2>/dev/null | tr -d '\n')" "$@") || exit $?
    [ -n "$WK_DEVICE_TASK" ] || return 0
    WK_DEVICE_HELD="device:$1"; export WK_DEVICE_HELD
    wk_atexit device_release
}
