#!/usr/bin/env bash
#
# Runs inside a workspace, invoked by `wk build`. Kept separate from cmd/build
# because that half runs outside the container and decides policy, while this
# half runs inside and only carries it out.
#
# Environment supplied by cmd/build: WK_JOBS, WK_NICE, WK_BUILD_ARGS,
# WK_BUILD_CMAKE, plus the ccache and cross-build cache paths.

set -euo pipefail

SRC=${WK_SRC:-/src/WebKit}
cd "$SRC"

jobs=${WK_JOBS:-4}

# Authoritative clamp: this half runs *inside* the container, so its own cgroup
# limit is the real ceiling. Belt and braces against the caller having sized the
# build from the wrong machine's memory.
if [ -r /sys/fs/cgroup/memory.max ]; then
    limit=$(cat /sys/fs/cgroup/memory.max)
    if [ "$limit" != max ]; then
        max_jobs=$(( limit / 1024 / 1024 / ${WK_MB_PER_JOB:-4096} ))
        [ "$max_jobs" -lt 1 ] && max_jobs=1
        if [ "$jobs" -gt "$max_jobs" ]; then
            echo "clamping -j$jobs -> -j$max_jobs (cgroup limit $(( limit / 1024 / 1024 ))MB)" >&2
            jobs=$max_jobs
        fi
    fi
fi
nicelevel=${WK_NICE:-10}

# shellcheck disable=SC2206 -- deliberate word splitting of the config strings.
args=(${WK_BUILD_ARGS:-})
cmakeargs=${WK_BUILD_CMAKE:-}

# ionice keeps the build off the desktop's I/O path; choom makes the OOM killer
# choose the build rather than the session. oom_score_adj is inherited, so
# setting it on the wrapper covers every compiler process underneath.
pre=()
command -v ionice >/dev/null 2>&1 && pre+=(ionice -c3)
command -v choom  >/dev/null 2>&1 && pre+=(choom -n 500 --)

set -x
exec "${pre[@]}" nice -n "$nicelevel" \
    Tools/Scripts/build-webkit \
        "${args[@]}" \
        --export-compile-commands \
        ${cmakeargs:+--cmakeargs="$cmakeargs"} \
        --makeargs="-j$jobs" \
        "$@"
