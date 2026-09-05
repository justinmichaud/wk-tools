#!/usr/bin/env bash
# Watches a build's memory from inside the machine that is building. Started by build/guard.sh just before the build execs, so <pid> is the build and every compiler is a descendant of it. Over `budget` (the tree's own RSS), or with the machine under `floor` -- memory we are not using is not ours either on a shared machine -- it kills the build rather than leave the OOM killer to pick somebody else's work. Output is on stderr, and `wk build` lifts the peak line into the status file. bash 3.2, this running in macOS guests as well as Linux.

set -uo pipefail

PID="${1:?usage: mem-watchdog.sh <pid> <budget-mb> [floor-mb]}"
BUDGET="${2:?budget in MB}"
FLOOR="${3:-0}"
INTERVAL="${WK_MEM_INTERVAL:-30}"

_tree() {   # every process in the tree under $1: pid list plus total RSS in MB. One `ps` per sample walked in awk rather than a pgrep recursion, eight passes covering any depth
    ps -eo pid=,ppid=,rss= 2>/dev/null | awk -v root="$1" '
        { pid[NR] = $1; ppid[NR] = $2; rss[NR] = $3; n = NR }
        END {
            want[root] = 1
            for (pass = 0; pass < 8; pass++)
                for (i = 1; i <= n; i++)
                    if (want[ppid[i]]) want[pid[i]] = 1
            total = 0; list = ""
            for (i = 1; i <= n; i++)
                if (want[pid[i]]) { total += rss[i]; list = list " " pid[i] }
            printf "%d%s\n", total / 1024, list
        }'
}

_avail_mb() {   # what the machine has left; macOS has no MemAvailable, so nothing comes back there
    [ -r /proc/meminfo ] || return 0
    awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo
}

peak=0
reported=0

_kill_tree() {   # TERM before KILL: a half-written object file fails the *next* build, not this one
    local pids="$1" p
    for p in $pids; do
        [ "$p" = "$$" ] && continue   # not ourselves: this watchdog is inside the tree it measures
        kill -TERM "$p" 2>/dev/null || true
    done
    local i=0
    while [ "$i" -lt 10 ]; do
        kill -0 "$PID" 2>/dev/null || return 0
        sleep 1; i=$((i + 1))
    done
    for p in $pids; do kill -KILL "$p" 2>/dev/null || true; done
}

while kill -0 "$PID" 2>/dev/null; do
    sample=$(_tree "$PID")
    rss=${sample%% *}
    pids=${sample#* }
    [ -n "$rss" ] || rss=0
    [ "$rss" -gt "$peak" ] && peak=$rss

    if [ "$peak" -gt $(( reported + reported / 10 + 64 )) ]; then
        echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB" >&2
        reported=$peak
    fi

    if [ "$rss" -gt "$BUDGET" ]; then
        echo "wk: MEMORY LIMIT: this build's tree is using ${rss}MB, over its ${BUDGET}MB budget" >&2
        echo "wk: killing it now rather than leaving the OOM killer to choose" >&2
        echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB (killed)" >&2
        _kill_tree "$pids"
        exit 0
    fi

    if [ "$FLOOR" -gt 0 ]; then
        avail=$(_avail_mb)
        if [ -n "$avail" ] && [ "$avail" -lt "$FLOOR" ]; then
            echo "wk: MEMORY LIMIT: the machine has ${avail}MB free, under the ${FLOOR}MB floor" >&2
            echo "wk: this build is using ${rss}MB of it; killing it rather than taking the machine down" >&2
            echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB (killed)" >&2
            _kill_tree "$pids"
            exit 0
        fi
    fi

    sleep "$INTERVAL"
done

echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB" >&2
