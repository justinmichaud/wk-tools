# Resource governance.
#
# One rule underpins everything here: the host GUI must stay interactive and
# must never be the process the OOM killer picks. Nothing in this file is a
# fixed constant that could be right on one machine and fatal on another --
# every number is derived from the machine it runs on.

# Headroom left to the host, never handed to a VM or a build.
#
# One core. On Apple silicon the intent is "all the performance cores go to the
# VM, the host keeps one efficiency core" -- on this M4 that is 4P + 6E, so the
# VM gets 9 and the host keeps 1.
#
# Note this is a *count*, not an affinity. Virtualization.framework takes a vCPU
# count and macOS places those threads itself; there is no way to pin vCPUs to
# performance cores. It works out in practice because the scheduler puts busy VM
# threads on P-cores and leaves light host work on an E-core, but do not read
# this as hard partitioning.
#
# Memory reserve stays generous: a desktop that has swapped is unusable in a way
# that a desktop short of one core is not.
WK_RESERVE_CORES="${WK_RESERVE_CORES:-1}"
WK_RESERVE_MB="${WK_RESERVE_MB:-12288}"

# A headless machine needs far less. The wk VM has no GUI to protect, and the
# macOS host already subtracted its own reserve when it sized the VM -- applying
# the desktop reserve again inside would double-count it and leave containers
# with a fraction of the memory actually available.
# Zero cores held back inside the VM: the workspace is the only workload there,
# and the VM's own kernel and podman are not CPU-bound. Holding one back would
# cost ~11% of the build for nothing.
WK_HEADLESS_RESERVE_CORES="${WK_HEADLESS_RESERVE_CORES:-0}"
WK_HEADLESS_RESERVE_MB="${WK_HEADLESS_RESERVE_MB:-2048}"

# The marker is written by the provisioning playbook, so this is an explicit
# fact about the machine rather than a guess from $DISPLAY.
is_headless() { [ -f /var/lib/wk/.headless ]; }

reserve_cores() { is_headless && echo "$WK_HEADLESS_RESERVE_CORES" || echo "$WK_RESERVE_CORES"; }
reserve_mb()    { is_headless && echo "$WK_HEADLESS_RESERVE_MB"    || echo "$WK_RESERVE_MB"; }

# Working set of a C++ compile job.
#
# The old bashrc used 4 GB, which was really sized for the handful of enormous
# DFG/FTL translation units and for link steps. Applied to every job it is far
# too pessimistic: it caps an 18 GB container at 4 jobs on an 8-core machine,
# leaving half the CPU idle for the whole build. Typical WebKit TUs peak nearer
# 1-1.5 GB, so 1.5 GB keeps the cap honest without throttling the common case;
# the cgroup clamp in build/build-in-target.sh is the real safety net.
WK_MB_PER_JOB="${WK_MB_PER_JOB:-1536}"

host_cores() {
    if is_macos; then sysctl -n hw.ncpu
    else nproc
    fi
}

host_mem_mb() {
    if is_macos; then echo $(( $(sysctl -n hw.memsize) / 1024 / 1024 ))
    else awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo
    fi
}

# Memory actually available right now, as opposed to total.
#
# A cgroup limit, when present, wins over MemAvailable: inside a container the
# kernel still reports the whole machine's free memory, so sizing a build from
# MemAvailable happily picks a job count the cgroup will OOM-kill. That is not
# hypothetical -- it is exactly how the first JSC build here died.
avail_mem_mb() {
    local cg=/sys/fs/cgroup/memory.max avail=""

    if is_linux && [ -r /proc/meminfo ]; then
        avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
    else
        avail=$(( $(host_mem_mb) - $(reserve_mb) ))
    fi

    # An explicit cap from the caller (cmd/build knows the container limit even
    # though it runs outside the container).
    if [ -n "${WK_CGROUP_MB:-}" ] && [ "$WK_CGROUP_MB" -lt "$avail" ]; then
        avail=$WK_CGROUP_MB
    fi

    if [ -r "$cg" ]; then
        local limit; limit=$(cat "$cg")
        if [ "$limit" != max ]; then
            limit=$(( limit / 1024 / 1024 ))
            [ "$limit" -lt "$avail" ] && avail=$limit
        fi
    fi

    echo "$avail"
}

# --- envelope ----------------------------------------------------------------
# The cap applied to the VM (macOS) or the container (Linux). Deliberately
# leaves the host enough to stay usable while a full build runs.

envelope_cores() {
    local c
    c=$(( $(host_cores) - $(reserve_cores) ))
    [ "$c" -lt 1 ] && c=1
    echo "$c"
}

# Reported by ./setup so the split is visible rather than implied.
describe_cores() {
    if is_macos && [ -n "$(sysctl -n hw.perflevel0.logicalcpu 2>/dev/null)" ]; then
        printf '%s P + %s E' \
            "$(sysctl -n hw.perflevel0.logicalcpu)" \
            "$(sysctl -n hw.perflevel1.logicalcpu)"
    else
        printf '%s cores' "$(host_cores)"
    fi
}

envelope_mem_mb() {
    local m
    m=$(( $(host_mem_mb) - $(reserve_mb) ))
    # On a small machine, reserving 12G could leave nothing. Fall back to half.
    [ "$m" -lt 2048 ] && m=$(( $(host_mem_mb) / 2 ))
    echo "$m"
}

# --- build parallelism -------------------------------------------------------
# build_jobs [loadavg-aware]
#
# Derived from available memory first (running out of RAM during a link is what
# actually hangs a machine), then clamped by core count. On shared machines the
# current load average is subtracted so other users keep their share.
build_jobs() {
    local polite="${1:-}"
    local by_mem by_cpu jobs cores

    # Prefer an explicit CPU cap from the caller. The container is limited to
    # envelope_cores, which is fewer than the machine reports, so sizing from
    # nproc oversubscribes: the WPE build ran -j8 against a 7-CPU cgroup.
    cores=${WK_CGROUP_CORES:-$(host_cores)}
    by_mem=$(( $(avail_mem_mb) / WK_MB_PER_JOB ))
    by_cpu=$cores

    if [ -n "$polite" ]; then
        # Treat the 1-minute load average as cores already spoken for.
        local load
        load=$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)
        by_cpu=$(( cores - load ))
        # Never take more than half a shared box, however idle it looks.
        local half=$(( cores / 2 ))
        [ "$by_cpu" -gt "$half" ] && by_cpu=$half
    fi

    jobs=$by_mem
    [ "$by_cpu" -lt "$jobs" ] && jobs=$by_cpu
    [ "$jobs" -lt 1 ] && jobs=1

    echo "$jobs"
}

# Print the derivation so the choice is never a mystery when a build misbehaves.
explain_jobs() {
    local polite="${1:-}" jobs
    jobs=$(build_jobs "$polite")
    # Report the cores the job count was actually derived from. When the caller
    # supplied a cap -- a cgroup limit, or a guest's vCPU count -- printing the
    # host's core count instead makes the derivation look wrong every time.
    log "resources: ${jobs} jobs (cores=${WK_CGROUP_CORES:-$(host_cores)} avail=$(avail_mem_mb)MB @ ${WK_MB_PER_JOB}MB/job${polite:+, polite})"
    echo "$jobs"
}

# --- launching ---------------------------------------------------------------
# nice_run <nice-level> <command...>
#
# Wraps a build so it loses every scheduling contest against the desktop. On
# Linux this also means the OOM killer reaps the build rather than the session:
# oom_score_adj is inherited by children, so setting it on the wrapper covers
# the whole compile tree.
nice_run() {
    local level="$1"; shift

    # Built as a string rather than an array: bash 3.2 (still the macOS system
    # bash) errors on "${arr[@]}" for an empty array under `set -u`.
    local pre=""
    if is_linux; then
        have ionice && pre="ionice -c3"
        have choom  && pre="$pre choom -n 500 --"
    fi

    # shellcheck disable=SC2086 -- pre is a deliberate list of bare words.
    $pre nice -n "$level" "$@"
}
