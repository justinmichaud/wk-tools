# The host GUI must stay interactive and never be the process the OOM killer picks. Every WK_* here is overridable.

# Held back from a GUI host, and a *count* of cores because Virtualization.framework places threads itself. A swapped desktop is unusable.
WK_RESERVE_CORES="${WK_RESERVE_CORES:-1}"
WK_RESERVE_MB="${WK_RESERVE_MB:-12288}"

# A headless machine needs far less: the macOS host reserved for it when sizing the VM, and reserving again double-counts and starves containers.
WK_HEADLESS_RESERVE_CORES="${WK_HEADLESS_RESERVE_CORES:-0}"
WK_HEADLESS_RESERVE_MB="${WK_HEADLESS_RESERVE_MB:-2048}"

# Written by the provisioning playbook, at a fixed path matching lib/store.sh's default: provisioning runs before any wk command can derive $WK_STORE.
headless_marker() { printf '%s' "${WK_STORE:-/var/lib/wk}/.headless"; }

is_headless() {
    [ -f "$(headless_marker)" ] && return 0
    command -v in_workspace >/dev/null 2>&1 && in_workspace
}

reserve_cores() { is_headless && echo "$WK_HEADLESS_RESERVE_CORES" || echo "$WK_RESERVE_CORES"; }
reserve_mb()    { is_headless && echo "$WK_HEADLESS_RESERVE_MB"    || echo "$WK_RESERVE_MB"; }

# The working set of a C++ compile job: the 4 GB a rare DFG/FTL TU or link step takes is too pessimistic for every job, and build/build-in-target.sh's cgroup clamp is the real net. WK_MB_PER_JOB_EXPLICIT keeps a config default from overriding an explicit one.
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

# A cgroup limit, when present, wins over MemAvailable, which inside a container reports the whole machine's free memory and can size a job count the cgroup kills.
avail_mem_mb() {
    local cg=/sys/fs/cgroup/memory.max avail=""

    if [ -n "${WK_AVAIL_MB:-}" ]; then echo "$WK_AVAIL_MB"; return 0; fi  # this host's free memory says nothing about a build over ssh

    if is_linux && [ -r /proc/meminfo ]; then
        avail=$(awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
    else
        avail=$(( $(host_mem_mb) - $(reserve_mb) ))
    fi

    if [ -n "${WK_CGROUP_MB:-}" ] && [ "$WK_CGROUP_MB" -lt "$avail" ]; then  # what the caller measured of the target's own cgroup, which cmd/build knows from outside it
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

envelope_cores() {  # the cap on the VM (macOS) or container (Linux)
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

export_target_resources() {  # a remote target's numbers, load average included, replace this machine's; call after `load_target`
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

# A build's memory is spoken for before it is used -- a link step allocates late, so MemAvailable at the start of a second build says nothing about the first -- so each build leaves a record of its budget per build machine and build_jobs sizes the next one against it.
builds_dir() { echo "$(wk_state_dir)/builds"; }
build_machine() { printf '%s' "${WK_BUILD_MACHINE:-$(hostname)}"; }

build_record() { # <label> <jobs> <budget-mb> <holder>
    ensure_dir "$(builds_dir)"
    printf 'label=%s\nmachine=%s\njobs=%s\nbudget_mb=%s\nholder=%s\nstarted=%s\n' \
        "$1" "$(build_machine)" "$2" "$3" "$4" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        > "$(builds_dir)/$(date +%s)-$$-$RANDOM"
}

_build_holder_alive() { # <holder>: pid:<n> for a foreground build, ws:<name>:<pidfile> for one in a workspace
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

builds_running() {  # a record whose holder is gone is removed on the way: a killed build leaves nothing
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

# A build that fills the filesystem does not fail cleanly: bitbake halts hours in and a container's overlay ENOSPCs mid-link. A builder that unpacks a whole distribution declares its own figure.
WK_BUILD_DISK_GB="${WK_BUILD_DISK_GB:-25}"

store_free_gb() {  # the filesystem that fills is the one under the store: overlays, build trees, cache. `df -Pk` is the one spelling both dfs have: BSD df has neither -B nor --output and exits 64 on them, which under pipefail ended every macOS build before it started
    df -Pk "${WK_STORE:-$HOME}" 2>/dev/null \
        | awk 'NR == 2 { printf "%d\n", ($4 + 1048575) / 1048576 }' || true
}

disk_admit() {
    local what="$1" need="${2:-$WK_BUILD_DISK_GB}" free
    free=$(store_free_gb)
    [ -n "$free" ] || return 0  # no answer from df is not evidence of a full disk
    [ "$free" -ge "$need" ] && return 0
    barrier "${free} GB free on ${WK_STORE:-$HOME}'s filesystem; $what wants about ${need} GB.
    It would halt part-built rather than fill the disk. 'wk gc' reclaims what
    nothing references, 'wk gc --purge-builds' the build trees images come out
    of, and 'wk disk' says where the rest went."
}

build_admit() {  # <what> <jobs> [disk-gb]: refuse a build the machine cannot fit beside those running, naming them; under WK_MIN_JOBS left over is a machine spoken for. Disk is asked here, so no build path can forget it
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

build_jobs() {  # from the memory not already spoken for -- running out of RAM during a link is what hangs a machine -- clamped by the cores left and by load
    local polite="${1:-}"
    local by_mem by_cpu jobs cores avail

    cores=$(( ${WK_CGROUP_CORES:-$(host_cores)} - $(build_reserved_jobs) ))  # the container is limited to envelope_cores, fewer than nproc, which would oversubscribe
    [ "$cores" -lt 1 ] && cores=1
    avail=$(( $(avail_mem_mb) - $(build_reserved_mb) ))
    [ "$avail" -lt 0 ] && avail=0
    by_mem=$(( avail / WK_MB_PER_JOB ))
    by_cpu=$cores

    if [ -n "$polite" ]; then
        local load
        load=${WK_LOAD:-$(awk '{print int($1)}' /proc/loadavg 2>/dev/null || echo 0)}  # caller-supplied for a remote target; /proc/loadavg here answers for the wrong machine

        # A load average decays over its window, so a killed build's cores stay spoken for a minute: memory-idle with load still high means a stale average, and it is halved.
        [ "$by_mem" -ge "$cores" ] && [ "$load" -gt $(( cores / 2 )) ] && load=$(( load / 2 ))

        by_cpu=$(( cores - load ))
        local half=$(( cores / 2 ))  # never more than half a shared box, however idle it looks
        [ "$by_cpu" -gt "$half" ] && by_cpu=$half
    fi

    jobs=$by_mem
    [ "$by_cpu" -lt "$jobs" ] && jobs=$by_cpu

    [ -n "${WK_MAX_JOBS:-}" ] && [ "$jobs" -gt "$WK_MAX_JOBS" ] && jobs=$WK_MAX_JOBS  # policy, not capacity: last, so it caps the answer and not the inputs

    [ "$jobs" -lt 1 ] && jobs=1

    echo "$jobs"
}

explain_jobs() {
    local polite="${1:-}" jobs cores by_mem
    jobs=$(build_jobs "$polite")
    cores=${WK_CGROUP_CORES:-$(host_cores)}  # not the host's, when capped
    local reserved; reserved=$(build_reserved_mb)
    log "resources: ${jobs} jobs (cores=${cores} avail=$(avail_mem_mb)MB${reserved:+ minus ${reserved}MB other builds} @ ${WK_MB_PER_JOB}MB/job${polite:+, polite, load=${WK_LOAD:-0}}${WK_MAX_JOBS:+, max $WK_MAX_JOBS})"

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

