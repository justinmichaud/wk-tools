# Resource governance: the host GUI must stay interactive and never be the process
# the OOM killer picks. Every WK_* below is overridable; export_target_resources
# replaces WK_AVAIL_MB/WK_CGROUP_*/WK_LOAD/WK_BUILD_MACHINE with the target's.

# Headroom left to the host, never a VM's or a build's: a *count*, since
# Virtualization.framework places threads itself. A swapped desktop is unusable.
WK_RESERVE_CORES="${WK_RESERVE_CORES:-1}"
# WK_RESERVE_CORES/WK_RESERVE_MB: held back from a GUI host.
WK_RESERVE_MB="${WK_RESERVE_MB:-12288}"

# A headless machine needs far less: the macOS host already reserved for it when
# sizing the VM, and reserving again double-counts and starves containers.
WK_HEADLESS_RESERVE_CORES="${WK_HEADLESS_RESERVE_CORES:-0}"
# WK_HEADLESS_RESERVE_CORES/WK_HEADLESS_RESERVE_MB: held back from a headless one.
WK_HEADLESS_RESERVE_MB="${WK_HEADLESS_RESERVE_MB:-2048}"

# Written by the provisioning playbook. Fixed path, matching _wk_default_store's
# default (lib/store.sh): provisioning runs before any wk command can derive $WK_STORE.
headless_marker() { printf '%s' "${WK_STORE:-/var/lib/wk}/.headless"; }

is_headless() {
    [ -f "$(headless_marker)" ] && return 0
    command -v in_workspace >/dev/null 2>&1 && in_workspace
}

reserve_cores() { is_headless && echo "$WK_HEADLESS_RESERVE_CORES" || echo "$WK_RESERVE_CORES"; }
reserve_mb()    { is_headless && echo "$WK_HEADLESS_RESERVE_MB"    || echo "$WK_RESERVE_MB"; }

# Working set of a C++ compile job. 4 GB (rare DFG/FTL TUs and link steps) applied to
# every job is too pessimistic; build/build-in-target.sh's cgroup clamp is the real
# net. WK_MB_PER_JOB_EXPLICIT keeps a config default from overriding an explicit one.
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

# A cgroup limit, when present, wins over MemAvailable, which reports the whole
# machine's free memory inside a container and can size a job count the cgroup kills.
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
    # WK_CGROUP_MB/WK_CGROUP_CORES: what the caller measured of the target's own
    # cgroup, when that is smaller than this machine.
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

# --- envelope: the cap on the VM (macOS) or container (Linux) ----------------
envelope_cores() {
    local c
    c=$(( $(host_cores) - $(reserve_cores) ))
    [ "$c" -lt 1 ] && c=1
    echo "$c"
}

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

# A remote target's numbers (load average included) replace this machine's. Call
# after `load_target` has resolved the target.
export_target_resources() {
    local name="$1"
    if [ "$WK_TARGET_KIND" = remote ]; then
        WK_AVAIL_MB=$(t_mem_mb "$name"); WK_CGROUP_CORES=$(t_cores "$name")
        WK_LOAD=$(t_load "$name")
        WK_BUILD_MACHINE="$WK_TARGET"
        export WK_AVAIL_MB WK_CGROUP_CORES WK_LOAD WK_BUILD_MACHINE
    else
        WK_CGROUP_MB=$(t_mem_mb "$name"); WK_CGROUP_CORES=$(t_cores "$name")
        export WK_CGROUP_MB WK_CGROUP_CORES
    fi
}

# --- what is already building on this machine ---------------------------------
# A build's memory is spoken for before it is used: a link step allocates late, and
# MemAvailable at the start of a second build says nothing about the first. So each
# build leaves a lock-shaped record of its budget -- `pid:<n>` for a foreground build,
# `ws:<name>:<pidfile>` for one in a workspace -- per build machine, and build_jobs
# sizes the next build against it.
builds_dir() { echo "$(wk_state_dir)/builds"; }
build_machine() { printf '%s' "${WK_BUILD_MACHINE:-$(hostname)}"; }

build_record() { # <label> <jobs> <budget-mb> <holder>
    ensure_dir "$(builds_dir)"
    printf 'label=%s\nmachine=%s\njobs=%s\nbudget_mb=%s\nholder=%s\nstarted=%s\n' \
        "$1" "$(build_machine)" "$2" "$3" "$4" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$(builds_dir)/$(date +%s)-$$-$RANDOM"
}

_build_holder_alive() { # <holder>
    local h="$1" name pidf pid
    case "$h" in
        pid:*) kill -0 "${h#pid:}" 2>/dev/null ;;
        ws:*)  h="${h#ws:}"; name="${h%%:*}"; pidf="${h#*:}"
               pid=$(tr -dc '0-9' < "$(wk_ws_dir "$name")/home/$pidf" 2>/dev/null) || return 1
               [ -n "$pid" ] || return 1
               command -v ws_target >/dev/null 2>&1 || return 1
               ( load_target "$(ws_target "$name" 2>/dev/null)" >/dev/null 2>&1 \
                 && t_exec "$name" kill -0 "$pid" ) >/dev/null 2>&1 ;;
        *) return 1 ;;
    esac
}

# A record whose holder is gone is removed on the way: a killed build leaves nothing.
builds_running() {
    local f label machine jobs budget holder
    [ -d "$(builds_dir)" ] || return 0
    for f in "$(builds_dir)"/*; do
        [ -f "$f" ] || continue
        label=$(kv_field "$f" label); machine=$(kv_field "$f" machine)
        jobs=$(kv_field "$f" jobs); budget=$(kv_field "$f" budget_mb); holder=$(kv_field "$f" holder)
        [ "$machine" = "$(build_machine)" ] || continue
        if _build_holder_alive "$holder"; then
            printf '%s\t%s\t%s\n' "$label" "$jobs" "$budget"
        else
            rm -f "$f"
        fi
    done
    return 0
}
build_reserved_mb()   { builds_running | awk -F'\t' '{ s += $3 } END { print s + 0 }'; }
build_reserved_jobs() { builds_running | awk -F'\t' '{ s += $2 } END { print s + 0 }'; }

# --- disk headroom -----------------------------------------------------------
# A build that fills the filesystem does not fail cleanly: bitbake halts hours in and
# a container's overlay ENOSPCs mid-link. A builder that unpacks a whole distribution
# declares its own figure (image/yocto.sh).
WK_BUILD_DISK_GB="${WK_BUILD_DISK_GB:-25}"

# The filesystem that fills is the one under the store: overlays, build trees, cache.
store_free_gb() {
    df -B1G --output=avail "${WK_STORE:-$HOME}" 2>/dev/null | tail -1 | tr -dc '0-9'
}

disk_admit() {
    local what="$1" need="${2:-$WK_BUILD_DISK_GB}" free
    free=$(store_free_gb)
    # No answer from df is not evidence of a full disk.
    [ -n "$free" ] || return 0
    [ "$free" -ge "$need" ] && return 0
    barrier "${free} GB free on ${WK_STORE:-$HOME}'s filesystem; $what wants about ${need} GB.
    It would halt part-built rather than fill the disk. 'wk gc' reclaims what
    nothing references, 'wk gc --purge-builds' the build trees images come out
    of, and 'wk disk' says where the rest went."
}

# build_admit <what> <jobs> [disk-gb] -- refuse a build the machine cannot fit beside
# those running, naming them; fewer than WK_MIN_JOBS (4) left over is a machine spoken
# for. Disk is asked here, so no build path can forget it.
build_admit() {
    local what="$1" jobs="$2" running
    disk_admit "$what" "${3:-}"
    running=$(builds_running)
    [ -n "$running" ] || return 0
    [ "$jobs" -ge "${WK_MIN_JOBS:-4}" ] && return 0
    barrier "$(build_machine)'s memory is spoken for by the build(s) already running:
$(printf '%s\n' "$running" | awk -F'\t' '{ printf "      %s (%s jobs, %s MB)\n", $1, $2, $3 }')
    $what would get $jobs job(s) of the $(avail_mem_mb) MB left. Wait for them
    ('wk status' shows a workspace's build), or --force to build that small."
}

# --- build parallelism: from the memory not already spoken for (running out of RAM
# during a link is what hangs a machine), clamped by cores left and by load --------
build_jobs() {
    local polite="${1:-}"
    local by_mem by_cpu jobs cores avail

    # The container is limited to envelope_cores, fewer than nproc, which would oversubscribe.
    cores=$(( ${WK_CGROUP_CORES:-$(host_cores)} - $(build_reserved_jobs) ))
    [ "$cores" -lt 1 ] && cores=1
    avail=$(( $(avail_mem_mb) - $(build_reserved_mb) ))
    [ "$avail" -lt 0 ] && avail=0
    by_mem=$(( avail / WK_MB_PER_JOB ))
    by_cpu=$cores

    if [ -n "$polite" ]; then
        # Caller-supplied for a remote target; /proc/loadavg here answers for the wrong machine.
        local load
        load=${WK_LOAD:-$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)}

        # A load average decays over its window, so a killed build's cores stay spoken for a
        # minute; memory-idle with load still high means the average is stale, and is halved.
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

explain_jobs() {
    local polite="${1:-}" jobs cores by_mem
    jobs=$(build_jobs "$polite")
    cores=${WK_CGROUP_CORES:-$(host_cores)}  # not the host's, when capped
    local reserved; reserved=$(build_reserved_mb)
    log "resources: ${jobs} jobs (cores=${cores} avail=$(avail_mem_mb)MB${reserved:+ minus ${reserved}MB other builds} @ ${WK_MB_PER_JOB}MB/job${polite:+, polite, load=${WK_LOAD:-0}}${WK_MAX_JOBS:+, max $WK_MAX_JOBS})"

    # Below half the target's own cores is worth a look, unless WK_MAX_JOBS is why.
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

