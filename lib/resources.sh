# Resource governance.
#
# One rule underpins everything here: the host GUI must stay interactive and
# must never be the process the OOM killer picks. Nothing in this file is a
# fixed constant that could be right on one machine and fatal on another --
# every number is derived from the machine it runs on.

# Headroom left to the host, never handed to a VM or a build.
#
# One core: on Apple silicon the intent is "performance cores go to the VM,
# host keeps one efficiency core" -- on this M4 (4P + 6E) the VM gets 9.
#
# A *count*, not an affinity: Virtualization.framework takes a vCPU count and
# macOS places those threads itself, so there is no way to pin vCPUs to
# performance cores. Works out in practice because the scheduler puts busy VM
# threads on P-cores, but this is not hard partitioning.
#
# Memory reserve stays generous: a desktop that has swapped is unusable in a
# way a desktop short of one core is not.
WK_RESERVE_CORES="${WK_RESERVE_CORES:-1}"
WK_RESERVE_MB="${WK_RESERVE_MB:-12288}"

# A headless machine needs far less: the wk VM has no GUI to protect, and the
# macOS host already subtracted its own reserve when sizing the VM -- reserving
# again inside would double-count it and starve containers.
# Zero cores held back inside the VM: the workspace is the only workload there
# and the VM's kernel/podman are not CPU-bound; holding one back costs ~11% of
# the build for nothing.
WK_HEADLESS_RESERVE_CORES="${WK_HEADLESS_RESERVE_CORES:-0}"
WK_HEADLESS_RESERVE_MB="${WK_HEADLESS_RESERVE_MB:-2048}"

# The marker is written by the provisioning playbook: an explicit fact about
# the machine rather than a guess from $DISPLAY.
#
# A workspace counts for the same reason: the host already subtracted its own
# reserve when it sized this guest or container's cgroup, so a second
# desktop-sized reserve here double-counts it. Darwin-specific -- avail_mem_mb()
# reads MemAvailable and the cgroup on Linux, but on macOS can only subtract
# the reserve from the total. Measured in a 20 GB guest: `wk build mac-release`
# sized itself from 8192 MB and picked -j5, where the same build driven from
# the host picked -j9. A guest with a window on screen is still not a machine
# with a desktop session to protect.

# Where the headless marker lives, in one place.
#
# Not three spellings -- `/var/lib/wk/.headless` here, `$WK_STORE/.headless` in
# the Linux machine stage, `{{ wk_root }}/.headless` in the podman VM's
# playbook -- agreeing only when $WK_STORE happens to be /var/lib/wk. On a
# workstation whose store is under XDG they do not, so a marker could sit in
# one path while the code that acts on it reads the other. Both are checked:
# the fixed path because the VM's playbook writes it before any wk has run,
# the store path because that is this machine's own state.
#
# $WK_STORE is not always set and command -v guards in_workspace: this file is
# sourced on its own by host scripts that have no reason to know about
# workspaces.
headless_markers() { printf '%s\n' "${WK_STORE:-}/.headless" /var/lib/wk/.headless; }

is_headless() {
    local m
    for m in $(headless_markers); do
        [ "$m" = "/.headless" ] && continue
        [ -f "$m" ] && return 0
    done
    command -v in_workspace >/dev/null 2>&1 && in_workspace
}

reserve_cores() { is_headless && echo "$WK_HEADLESS_RESERVE_CORES" || echo "$WK_RESERVE_CORES"; }
reserve_mb()    { is_headless && echo "$WK_HEADLESS_RESERVE_MB"    || echo "$WK_RESERVE_MB"; }

# Working set of a C++ compile job, for the CMake ports.
#
# 4 GB (sized for the handful of enormous DFG/FTL TUs and link steps) applied
# to every job is far too pessimistic: it caps an 18 GB container at 4 jobs on
# an 8-core machine. Typical WebKit TUs peak nearer 1-1.5 GB, so 1.5 GB keeps
# the cap honest without throttling the common case; the cgroup clamp in
# build/build-in-target.sh is the real safety net.
#
# The Apple build wants twice this and says so itself (config_mb_per_job,
# build/configs.sh), which is why this records whether the value was *asked
# for*: a default may be replaced by the config, an explicit
# `WK_MB_PER_JOB=3072 wk build ...` may not.
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

# Memory actually available right now, as opposed to total.
#
# A cgroup limit, when present, wins over MemAvailable: inside a container the
# kernel still reports the whole machine's free memory, so sizing a build from
# MemAvailable can pick a job count the cgroup OOM-kills.
avail_mem_mb() {
    local cg=/sys/fs/cgroup/memory.max avail=""

    # A remote target replaces the measurement rather than capping it: what
    # this host has free says nothing about a build over ssh, and taking the
    # smaller of the two would size a 250 GB build box from a laptop.
    # WK_CGROUP_MB below stays a cap -- a container really is this machine,
    # with a limit on top.
    if [ -n "${WK_AVAIL_MB:-}" ]; then echo "$WK_AVAIL_MB"; return 0; fi

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

# Size a run from the target it will actually happen on, not from this
# machine. A remote target is somebody else's machine entirely, reached over
# ssh: its numbers replace this one's rather than capping them, and the load
# average has to come from over there too, or a build/test starts as if the
# far end were idle. Anything else -- container, vm, local -- is this machine
# with a limit on top, so its numbers stay a cap. Both `wk build` and `wk
# test` size their run this way; call this after `load_target` has resolved
# the target `name` lives on.
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
        # Treat the 1-minute load average as cores already spoken for. The
        # caller supplies it when the busy machine is not this one -- a remote
        # target's load is the only load that matters for a build running
        # there, and /proc/loadavg here answers for the wrong computer (and on
        # a macOS host does not exist at all, which reads as an idle machine).
        local load
        load=${WK_LOAD:-$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)}

        # A load average is an exponential decay over its own window, so a
        # build that was killed keeps its dead compilers' cores "spoken for"
        # for up to a minute after they are gone. Memory has no such lag -- a
        # killed process's RSS is back the instant it is reaped -- so when the
        # machine already looks memory-idle (by_mem, above, covers the whole
        # box) but the load average still claims most of it, that average is
        # almost certainly stale rather than a second build actually running,
        # and is halved rather than trusted outright.
        [ "$by_mem" -ge "$cores" ] && [ "$load" -gt $(( cores / 2 )) ] && load=$(( load / 2 ))

        by_cpu=$(( cores - load ))
        # Never take more than half a shared box, however idle it looks.
        local half=$(( cores / 2 ))
        [ "$by_cpu" -gt "$half" ] && by_cpu=$half
    fi

    jobs=$by_mem
    [ "$by_cpu" -lt "$jobs" ] && jobs=$by_cpu

    # A ceiling that is a matter of policy rather than of capacity: a shared
    # 80-core machine has the resources to hand one person 40 jobs and no
    # reason to. Applied last so it caps the answer rather than the inputs.
    [ -n "${WK_MAX_JOBS:-}" ] && [ "$jobs" -gt "$WK_MAX_JOBS" ] && jobs=$WK_MAX_JOBS

    [ "$jobs" -lt 1 ] && jobs=1

    echo "$jobs"
}

# Print the derivation so the choice is never a mystery when a build misbehaves.
explain_jobs() {
    local polite="${1:-}" jobs cores by_mem
    jobs=$(build_jobs "$polite")
    # Report the cores the job count was actually derived from. When the caller
    # supplied a cap -- a cgroup limit, or a guest's vCPU count -- printing the
    # host's core count instead makes the derivation look wrong every time.
    cores=${WK_CGROUP_CORES:-$(host_cores)}
    log "resources: ${jobs} jobs (cores=${cores} avail=$(avail_mem_mb)MB @ ${WK_MB_PER_JOB}MB/job${polite:+, polite, load=${WK_LOAD:-0}}${WK_MAX_JOBS:+, max $WK_MAX_JOBS})"

    # Below half the target's own cores is worth a person looking at, unless
    # WK_MAX_JOBS is why -- that is a deliberate policy cap, not a problem.
    # Named by whichever of memory or load is actually binding (by_mem, the
    # same figure build_jobs derived jobs from), so the fix is obvious rather
    # than a mystery: more memory / a lower WK_MB_PER_JOB, or -- on a shared
    # machine -- the load average is real and this is genuinely busy.
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

