# The guard every heavy build step inside a target runs under -- guard_jobs, guard_exec, guard_run -- sourced from /opt/wk-tools inside the target. bash 3.2, this running in macOS guests as well as on Linux. guard_jobs clamps to the target's own cgroup memory limit, the authoritative ceiling: inside a container the kernel reports the whole machine's MemAvailable.

guard_jobs() {
    local jobs="$1" limit max_jobs
    if [ -r /sys/fs/cgroup/memory.max ]; then
        limit=$(cat /sys/fs/cgroup/memory.max)
        if [ "$limit" != max ]; then
            max_jobs=$(( limit / 1024 / 1024 / ${WK_MB_PER_JOB:-4096} ))
            [ "$max_jobs" -lt 1 ] && max_jobs=1
            if [ "$jobs" -gt "$max_jobs" ]; then
                echo "wk: clamping -j$jobs -> -j$max_jobs (cgroup limit $(( limit / 1024 / 1024 ))MB)" >&2
                jobs=$max_jobs
            fi
        fi
    fi
    printf '%s' "$jobs"
}

_guard_watch() { # <pid> <jobs> -- the watchdog is pointed at <pid>, which `exec` preserves
    local budget watchdog
    if [ -n "${WK_MEM_BUDGET_MB:-}" ]; then
        budget=$WK_MEM_BUDGET_MB
    else
        budget=$(( $2 * ${WK_MB_PER_JOB:-1536} ))
        [ "$budget" -lt 8192 ] && budget=8192
    fi
    watchdog="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/mem-watchdog.sh"
    [ -x "$watchdog" ] || return 0
    "$watchdog" "$1" "$budget" "${WK_MEM_FLOOR_MB:-2048}" &
    echo "wk: memory watchdog: budget ${budget}MB, floor ${WK_MEM_FLOOR_MB:-2048}MB, sampled every ${WK_MEM_INTERVAL:-30}s" >&2
}

_guard_prefix() {   # ionice keeps the build off the desktop's I/O path and choom makes the OOM killer choose the build over the session; neither exists on macOS
    local pre=""
    command -v ionice >/dev/null 2>&1 && pre="ionice -c3"
    command -v choom  >/dev/null 2>&1 && pre="$pre choom -n 500 --"
    printf '%s nice -n %s' "$pre" "${WK_NICE:-10}"
}

guard_exec() {
    local jobs="$1"; shift; [ "${1:-}" = -- ] && shift
    _guard_watch "$$" "$jobs"
    # shellcheck disable=SC2046 -- the prefix is a deliberate list of bare words.
    exec $(_guard_prefix) "$@"
}

guard_run() {
    local jobs="$1"; shift; [ "${1:-}" = -- ] && shift
    # shellcheck disable=SC2046
    $(_guard_prefix) "$@" &
    local pid=$!
    _guard_watch "$pid" "$jobs"
    wait "$pid"
}
