#!/usr/bin/env bash
#
# Watch a build's memory, from inside the machine that is building.
#
#   mem-watchdog.sh <pid> <budget-mb> [floor-mb]
#
# Started in the background by build/build-in-target.sh just before it execs
# the build, so <pid> is the build itself and every compiler underneath is a
# descendant of it.
#
# The job count is a *prediction* (lib/resources.sh, sized from memory free
# at the time) that a hungry link step or a shared machine's arriving job
# can break -- left unwatched, the OOM killer picks a victim that may be
# somebody else's work. Killing our own build early is the polite failure.
#
# Two ways to be over: `budget`, this build's own resident memory; `floor`,
# what the *machine* has left, since on a shared machine memory we aren't
# using isn't ours either.
#
# Goes to stderr, the build log on both sides of an ssh, so a marker line
# survives a dropped connection; `wk build` reads it into the status file.
#
# bash 3.2: this runs in macOS guests as well as on Linux.

set -uo pipefail

PID="${1:?usage: mem-watchdog.sh <pid> <budget-mb> [floor-mb]}"
BUDGET="${2:?budget in MB}"
FLOOR="${3:-0}"
INTERVAL="${WK_MEM_INTERVAL:-30}"

# Every process in the tree under $1, as a pid list plus total RSS in MB. One
# `ps` per sample, walked in awk rather than recursing with pgrep (a process
# per node per sample). Eight passes covers any real tree depth.
_tree() {
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

# What the machine has left. macOS has no MemAvailable equivalent, so the
# floor check doesn't apply there and this returns nothing.
_avail_mb() {
    [ -r /proc/meminfo ] || return 0
    awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo
}

peak=0
# Last peak logged, reported as it grows: no "end" to report once the build
# `exec`s over the shell that started this child.
reported=0

# TERM first, then KILL: a half-written object file fails the *next* build
# strangely rather than this one.
_kill_tree() {
    local pids="$1" p
    for p in $pids; do
        # Not ourselves: this process is a child of the shell that becomes
        # the build, so it is *in* the tree it is measuring.
        [ "$p" = "$$" ] && continue
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

    # A tenth more than last reported, and at least 64 MB: enough lines to
    # follow a build's shape without being noise in a log somebody reads.
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

# Printed always: `wk build` lifts this into the status file.
echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB" >&2
