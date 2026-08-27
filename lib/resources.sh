# Resource governance: the host GUI must stay interactive and never be the
# process the OOM killer picks. Every number is derived from this machine.
#
# Every WK_* name below is overridable, documented here once rather than at
# each read:
#   WK_RESERVE_CORES/WK_RESERVE_MB           held back from a GUI host (below)
#   WK_HEADLESS_RESERVE_CORES/_MB            held back from a headless one
#   WK_MB_PER_JOB                            working-set estimate per compile job
#   WK_MAX_JOBS                              a hard cap on parallelism, policy not capacity
#   WK_AVAIL_MB/WK_CGROUP_MB/WK_CGROUP_CORES/WK_LOAD
#       what a build is actually sized against: set by export_target_resources
#       for a remote or container target, whose numbers replace this
#       machine's own -- not a knob most callers set directly.



# Headroom left to the host, never handed to a VM or a build. One core: on
# Apple silicon, performance cores go to the VM, host keeps an efficiency
# core (a *count*, since Virtualization.framework places threads itself).
# Memory stays generous: a swapped desktop is unusable, one short of a
# core is not.
WK_RESERVE_CORES="${WK_RESERVE_CORES:-1}"
WK_RESERVE_MB="${WK_RESERVE_MB:-12288}"

# A headless machine needs far less: no GUI to protect, and the macOS host
# already reserved for it when sizing the VM -- reserving again inside
# double-counts and starves containers.
WK_HEADLESS_RESERVE_CORES="${WK_HEADLESS_RESERVE_CORES:-0}"
WK_HEADLESS_RESERVE_MB="${WK_HEADLESS_RESERVE_MB:-2048}"

# Written by the provisioning playbook, an explicit fact rather than a
# guess from $DISPLAY. Fixed path, matching _wk_default_store's default
# (lib/store.sh): provisioning runs before any wk command and can't derive $WK_STORE.
headless_marker() { printf '%s' "${WK_STORE:-/var/lib/wk}/.headless"; }

is_headless() {
    [ -f "$(headless_marker)" ] && return 0
    command -v in_workspace >/dev/null 2>&1 && in_workspace
}

reserve_cores() { is_headless && echo "$WK_HEADLESS_RESERVE_CORES" || echo "$WK_RESERVE_CORES"; }
reserve_mb()    { is_headless && echo "$WK_HEADLESS_RESERVE_MB"    || echo "$WK_RESERVE_MB"; }

# Working set of a C++ compile job. 4 GB (sized for rare DFG/FTL TUs and
# link steps) applied to every job is too pessimistic; the cgroup clamp in
# build/build-in-target.sh is the real safety net. WK_MB_PER_JOB_EXPLICIT
# lets an explicit `WK_MB_PER_JOB=` override a config default without
# being overridden itself (config_mb_per_job, build/configs.sh).
WK_MB_PER_JOB_EXPLICIT="${WK_MB_PER_JOB:+1}"
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

# Memory actually available right now. A cgroup limit, when present, wins
# over MemAvailable, which reports the whole machine's free memory inside
# a container and can size a job count the cgroup OOM-kills.
avail_mem_mb() {
    local cg=/sys/fs/cgroup/memory.max avail=""

    # This host's free memory says nothing about a build over ssh.
    if [ -n "${WK_AVAIL_MB:-}" ]; then echo "$WK_AVAIL_MB"; return 0; fi

    if is_linux && [ -r /proc/meminfo ]; then
        avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
    else
        avail=$(( $(host_mem_mb) - $(reserve_mb) ))
    fi

    # cmd/build knows the container limit even outside the container.
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

# --- envelope ------------------------------------------------------------
# The cap applied to the VM (macOS) or container (Linux), leaving the host usable.
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
    [ "$m" -lt 2048 ] && m=$(( $(host_mem_mb) / 2 ))  # 12G could leave nothing
    echo "$m"
}

# Size a run from the target it happens on. A remote target's numbers
# (load average included) replace this machine's rather than capping them,
# or a build starts as if the far end were idle. Call after `load_target`
# has resolved `name`'s target.
export_target_resources() {
    local name="$1"
    if [ "$WK_TARGET_KIND" = remote ]; then
        WK_AVAIL_MB=$(t_mem_mb "$name"); WK_CGROUP_CORES=$(t_cores "$name")
        WK_LOAD=$(t_load "$name")
        export WK_AVAIL_MB WK_CGROUP_CORES WK_LOAD
    else
        WK_CGROUP_MB=$(t_mem_mb "$name"); WK_CGROUP_CORES=$(t_cores "$name")
        export WK_CGROUP_MB WK_CGROUP_CORES
    fi
}

# --- build parallelism -------------------------------------------------------
# build_jobs [loadavg-aware]: derived from available memory first (running
# out of RAM during a link is what hangs a machine), clamped by core count
# and, on a shared machine, by load average.
build_jobs() {
    local polite="${1:-}"
    local by_mem by_cpu jobs cores

    # An explicit CPU cap: the container is limited to envelope_cores,
    # fewer than nproc, which would oversubscribe.
    cores=${WK_CGROUP_CORES:-$(host_cores)}
    by_mem=$(( $(avail_mem_mb) / WK_MB_PER_JOB ))
    by_cpu=$cores

    if [ -n "$polite" ]; then
        # Cores spoken for. Caller-supplied for a remote target;
        # /proc/loadavg here would answer for the wrong computer.
        local load
        load=${WK_LOAD:-$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)}

        # A load average decays over its window, so a killed build's cores
        # stay spoken for a minute; memory has no such lag, so memory-idle
        # with load still high means the average is stale, and is halved.
        [ "$by_mem" -ge "$cores" ] && [ "$load" -gt $(( cores / 2 )) ] && load=$(( load / 2 ))

        by_cpu=$(( cores - load ))
        # Never take more than half a shared box, however idle it looks.
        local half=$(( cores / 2 ))
        [ "$by_cpu" -gt "$half" ] && by_cpu=$half
    fi

    jobs=$by_mem
    [ "$by_cpu" -lt "$jobs" ] && jobs=$by_cpu

    # Policy, not capacity -- applied last so it caps the answer, not the inputs.
    [ -n "${WK_MAX_JOBS:-}" ] && [ "$jobs" -gt "$WK_MAX_JOBS" ] && jobs=$WK_MAX_JOBS

    [ "$jobs" -lt 1 ] && jobs=1

    echo "$jobs"
}

# Print the derivation so the choice is never a mystery when a build misbehaves.
explain_jobs() {
    local polite="${1:-}" jobs cores by_mem
    jobs=$(build_jobs "$polite")
    cores=${WK_CGROUP_CORES:-$(host_cores)}  # not the host's, when capped
    log "resources: ${jobs} jobs (cores=${cores} avail=$(avail_mem_mb)MB @ ${WK_MB_PER_JOB}MB/job${polite:+, polite, load=${WK_LOAD:-0}}${WK_MAX_JOBS:+, max $WK_MAX_JOBS})"

    # Below half the target's own cores is worth a look, unless
    # WK_MAX_JOBS is why -- named by whichever of memory or load binds.
    if [ -z "${WK_MAX_JOBS:-}" ] && [ "$jobs" -lt $(( cores / 2 )) ]; then
        by_mem=$(( $(avail_mem_mb) / WK_MB_PER_JOB ))
        if [ "$by_mem" -le "$jobs" ]; then
            warn "parallelism: ${jobs} jobs is under half of ${cores} cores -- the memory
  envelope only fits $by_mem at ${WK_MB_PER_JOB}MB/job ($(avail_mem_mb)MB available)."
        elif [ -n "$polite" ]; then
            warn "parallelism: ${jobs} jobs is under half of ${cores} cores -- load average
  ${WK_LOAD:-0} is treated as that many cores already spoken for on this shared machine."
        else
            warn "parallelism: ${jobs} jobs is under half of ${cores} cores -- ${cores} is
  this target's own ceiling (a reserve held back for the host, or a fixed vCPU/cgroup count)."
        fi
    fi

    echo "$jobs"
}

