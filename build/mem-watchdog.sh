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
# Why this exists at all: the job count is derived from what the machine has
# free at the time (lib/resources.sh), and that is a *prediction*. A link step
# that wants 12 GB, a translation unit that wants 6, or somebody else's job
# arriving on a shared box all break it after the fact -- and the ways it
# breaks are bad. On a build machine the OOM killer picks a victim that may be
# somebody else's work; on a laptop the whole desktop swaps for ten minutes.
# Killing our own build early is the polite failure, and it is the one we can
# explain afterwards.
#
# Two ways to be over:
#
#   budget   this build's own resident memory, summed across the tree. The
#            number it was sized for -- jobs x per-job working set -- which is
#            exactly the prediction being checked.
#   floor    what the *machine* has left. On a shared machine the memory we
#            are not using is not ours either, and a box driven to 200 MB free
#            is one where every other user's build is about to die too.
#
# Everything it says goes to stderr, which is the build log on both sides of an
# ssh: the log is the evidence channel this tooling already has, and a marker
# line in it survives a dropped connection, a killed driver and a reboot of the
# machine that started the build. `wk build` reads these lines back to record
# how the build ended; nothing else has to be plumbed through.
#
# bash 3.2: this runs in macOS guests as well as on Linux.

set -uo pipefail

PID="${1:?usage: mem-watchdog.sh <pid> <budget-mb> [floor-mb]}"
BUDGET="${2:?budget in MB}"
FLOOR="${3:-0}"
INTERVAL="${WK_MEM_INTERVAL:-30}"

# Every process in the tree under $1, as a space-separated pid list, and the
# total resident set in MB.
#
# One `ps` per sample, walked in awk rather than by recursing with pgrep: a
# recursive walk is one process per node per sample, and this runs beside a
# build that is using the whole machine.
#
# The fixed number of passes is the tree's depth, not its size: bash ->
# build-webkit (perl) -> ninja -> cc1plus is four, and a linker under a wrapper
# script is a couple more. Eight is slack, and a descendant deeper than that
# would only be missed from the *sum*, never mistaken for someone else's.
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

# What the machine has left. Linux answers honestly with MemAvailable, which
# already accounts for reclaimable cache; macOS has no equivalent worth the
# arithmetic, so the floor check simply does not apply there and says so by
# returning nothing.
_avail_mb() {
    [ -r /proc/meminfo ] || return 0
    awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo
}

peak=0
# The last peak written to the log. Reported as it grows rather than once at
# the end, because there is no end to report at: this process is a background
# child of a shell that `exec`s the build, so when the build dies its stderr is
# already gone. A high-water line in the log is also the only form of this that
# survives a dropped ssh -- and `wk build` reads the last one into the status
# file, which is what puts a peak in `wk status`.
reported=0

# TERM first, then KILL: ninja and build-webkit both clean up after themselves
# given a moment, and a half-written object file is a build that fails
# strangely the *next* time rather than this one.
_kill_tree() {
    local pids="$1" p
    for p in $pids; do
        # Not ourselves. This process is a child of the shell that becomes the
        # build, so it is *in* the tree it is measuring -- and it TERM'd itself
        # mid-sentence the first time, which is why the kill was reported
        # without a peak and without its final line.
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

    # A tenth more than last reported, and at least 64 MB more: enough lines to
    # follow a build's shape, few enough not to be noise in a log somebody has
    # to read.
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

# The normal end. Printed always, because "how close was that" is the question
# asked after every build that nearly did not finish, and nothing else records
# it -- `wk build` lifts this line into the status file.
echo "wk: memory: peak ${peak}MB of budget ${BUDGET}MB" >&2
