# Target driver: a disposable macOS VM, for building the Apple ports.
#
# Uses Tart. Four facts shape this driver:
#
#   Apple permits exactly two *running* macOS VMs per host, and
#   Virtualization.framework enforces it -- a third fails with VZErrorDomain
#   code 6. The limit is on running guests, not created ones, so it is checked
#   in t_start rather than t_create: refusing to create a VM you have room to
#   create would be its own kind of wrong.
#
#   `tart clone` uses APFS copy-on-write, so cloning a prepared image is
#   effectively free. That is the whole storage design: a golden base VM is
#   built once, with Xcode and a WebKit checkout already in it, and every
#   workspace is a clone of it. Creating a workspace therefore never clones a
#   repository -- the property the Linux overlay scheme exists to provide,
#   obtained here from the filesystem instead.
#
#   `tart exec` runs commands through the guest agent, which the Cirrus Labs
#   images ship. That is what makes unattended provisioning possible at all:
#   the ssh key is injected over the agent, so nothing ever has to type the
#   image's default password.
#
#   A macOS guest cannot be firewalled from outside. See "isolation" below.
#
# --- isolation ---------------------------------------------------------------
#
# Be precise about what this target gives you, because it is NOT what a Linux
# workspace gives you:
#
#   yes  a VM boundary. The guest has no view of the host filesystem -- no
#        --dir mounts are ever passed, so /Users is unreachable by construction.
#   yes  disposability. `wk rm` deletes the whole guest.
#   NO   filtered egress. The Linux workspaces are confined by nftables in the
#        podman VM's root network namespace, which the workspace does not own.
#        A macOS guest has no equivalent: there is no shared netns to filter
#        from outside, and pf inside the guest is modifiable by anything
#        running as root there -- which includes whatever you are sandboxing.
#
# So a macOS workspace is a blast-radius boundary, not a network boundary.
# `wk claude` says so out loud rather than implying the Linux guarantees.

WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode:26.5}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"
WK_VM_USER="${WK_VM_USER:-admin}"

# The golden base gets the full envelope, because provisioning it ends in a
# complete WebKit build (see _prebuild_base) rather than just a clone.
#
# Resolved lazily, not at source time. Sourcing a driver must not require the
# caller to have sourced lib/resources.sh first -- `wk start` and `wk stop`
# load this file only to stop a guest, and calling envelope_cores() while the
# file is being read made them fail with "command not found".
WK_VM_BASE_CPUS="${WK_VM_BASE_CPUS:-}"
WK_VM_BASE_MEM_MB="${WK_VM_BASE_MEM_MB:-}"
_base_cpus()   { echo "${WK_VM_BASE_CPUS:-$(envelope_cores)}"; }
_base_mem_mb() { echo "${WK_VM_BASE_MEM_MB:-$(envelope_mem_mb)}"; }

# What the base is built with before it is sealed. Every workspace inherits the
# result, so this is the single most valuable thing in the image.
#
# Empty disables it -- the base is then source-only and the first build in each
# workspace is a cold 99-minute one.
WK_VM_BASE_PREBUILD="${WK_VM_BASE_PREBUILD:-mac-release}"

# Disk, and this one is not optional.
#
# The prepared image ships a 140 GB disk of which the system and Xcode already
# occupy ~76 GB. Measured on this host, a guest needs:
#
#   system + Xcode      76 GB
#   WebKit checkout     19 GB   (12 GB of it .git)
#   compilation cache   11 GB   (Xcode CAS + module cache, after a full build)
#   Release build tree  39 GB
#   Debug build tree   ~78 GB   (unstripped, full DWARF; ~2x Release)
#
# Room for two builds plus margin is therefore ~278 GB, rounded to 320. The
# stock 140 GB does not even fit one: the first real build here ran 38 minutes
# and died with "No space left on device", which xcodebuild reported as "the
# Xcode build system has crashed" -- pointing nowhere near the cause.
#
# This is a ceiling, not an allocation. The disk is sparse and the clones are
# copy-on-write, so an unused gigabyte here costs nothing. What it can do is
# overcommit the *host*, which _check_host_disk is for.
WK_VM_DISK_GB="${WK_VM_DISK_GB:-320}"

# Host space that must remain for a guest to have somewhere to grow into. A
# sparse 320 GB disk on a host with 10 GB free is a build failure waiting to
# happen, and it fails as an I/O error inside the guest rather than as anything
# that names the real problem.
WK_HOST_FREE_WARN_GB="${WK_HOST_FREE_WARN_GB:-80}"
WK_HOST_FREE_MIN_GB="${WK_HOST_FREE_MIN_GB:-25}"

# Host-side state for this target. $WK_STORE defaults to /var/lib/wk, which is
# right inside the podman VM and wrong on a macOS workstation -- vm workspaces
# run from the host, where nothing may write outside $HOME.
WK_STORE="${WK_VM_STORE:-$(wk_state_dir)}"
WK_VM_DIR="$WK_STORE/vm"
WK_VM_KEY="$WK_VM_DIR/id_ed25519"

# --- the tart binary ---------------------------------------------------------
# Not installed by ./setup: it needs the com.apple.security.virtualization
# entitlement, so it has to stay inside the signed app bundle Cirrus Labs ships
# rather than being copied to a bare path. ~/.local is where a hand-installed
# bundle lands; PATH wins if the user installed it some other way.
_tart_bin() {
    if command -v tart >/dev/null 2>&1; then command -v tart
    elif [ -x "$HOME/.local/bin/tart" ]; then echo "$HOME/.local/bin/tart"
    elif [ -x "$HOME/.local/share/tart/tart.app/Contents/MacOS/tart" ]; then
        echo "$HOME/.local/share/tart/tart.app/Contents/MacOS/tart"
    else return 1
    fi
}

_tart() {
    local bin
    bin=$(_tart_bin) || die "tart is not installed.
    Install the signed bundle (it needs the virtualization entitlement, so the
    .app must stay intact):
      curl -fsSLO https://github.com/openai/tart/releases/latest/download/tart.tar.gz
      tar -xzf tart.tar.gz -C ~/.local/share/tart/
      ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart
    Licence: FSL-1.1-ALv2. Internal use is a Permitted Purpose; see docs/macos-vm.md."
    "$bin" "$@"
}

_vm() { echo "wk-$1"; }

# `tart list` returns both local VMs and cached OCI images in one array, and
# spells the Source field "local" for the former but "OCI" for the latter --
# so the comparison is case-folded rather than trusting either spelling.
_vm_json() { _tart list --format json 2>/dev/null || echo '[]'; }
_local_vms() { _vm_json | jq '[.[]|select((.Source|ascii_downcase)=="local")]'; }

_vm_state() {
    _local_vms | jq -r --arg n "$1" '[.[]|select(.Name==$n)][0].State // "absent"'
}

_running_count() {
    _local_vms | jq -r '[.[]|select(.State=="running")]|length'
}

# --- contract ----------------------------------------------------------------

t_src()   { echo "/Users/$WK_VM_USER/WebKit"; }
t_tools() { echo "/Users/$WK_VM_USER/wk-tools"; }

# `wk new` resolves a base *snapshot* for the overlay scheme. There are no
# snapshots here: the golden VM is the base, and cloning it is the checkout.
t_needs_base() { return 1; }

# Only what this target actually uses. The full store layout is built around
# the overlay scheme -- a bare mirror, snapshots, the ccache and the Yocto and
# buildroot download caches -- and none of it exists for a macOS VM, whose
# equivalents all live inside the guest. Creating those directories anyway
# would leave a tree of empty stubs that look like they mean something.
t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
    ensure_dir "$WK_VM_DIR" 0700
}

t_list() {
    # The golden base is infrastructure, not a workspace, and listing it invites
    # someone to `wk rm` it -- which would throw away the only expensive thing
    # here.
    _local_vms | jq -r --arg base "$WK_VM_BASE" '
        .[]|select(.Name|startswith("wk-"))
           |select(.Name != $base)
           |"\(.Name|ltrimstr("wk-"))\t\(.State)"'
}

t_info() { _vm_state "$(_vm "$1")"; }

t_ssh_host() {
    local ip; ip=$(_ip "$1") || return 1
    echo "$WK_VM_USER@$ip"
}

t_create() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" = absent ] || die "workspace '$name' already exists"

    _ensure_base

    # Not fatal -- creating costs nothing but disk, and the guest limit applies
    # to running VMs. Said here anyway so the ceiling is never a surprise at
    # the point where it does bite.
    local running; running=$(_running_count)
    [ "${running:-0}" -ge "$WK_VM_MAX" ] && \
        warn "$running macOS VM(s) already running; you will have to stop one before starting '$name'"

    info "cloning $WK_VM_BASE -> $v (APFS copy-on-write)"
    local t0; t0=$(date +%s)
    _tart clone "$WK_VM_BASE" "$v"
    _tart set "$v" --cpu "$(_vm_cpus)" --memory "$(_vm_mem_mb)" --random-mac --random-serial
    debug "clone took $(( $(date +%s) - t0 ))s"

    # A clone that took minutes fell back to a real copy instead of using
    # clonefile(2), and the disk cost then multiplies per workspace. Worth
    # knowing immediately rather than when the disk fills.
    [ $(( $(date +%s) - t0 )) -gt 60 ] && \
        warn "the clone took $(( $(date +%s) - t0 ))s -- APFS copy-on-write may not be in play; check disk use"

    ensure_dir "$(wk_ws_dir "$name")"
}

t_start() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" != absent ] || die "no such workspace: $name"
    if [ "$(_vm_state "$v")" = running ]; then
        _ip "$name"
        return 0
    fi

    _check_guest_limit
    _check_memory_budget "$name" "$(t_mem_mb "$name")"
    _check_host_disk

    _boot "$v" 180
}

# _boot <vm-name> <seconds> -- start a guest if it is not already up, and echo
# its address once ssh answers. Shared by t_start and base provisioning, which
# otherwise grow two subtly different copies of the same waiting logic.
_boot() {
    local v="$1" wait="${2:-180}" ip runlog

    ensure_dir "$WK_VM_DIR"
    runlog="$WK_VM_DIR/${v#wk-}.run.log"

    if [ "$(_vm_state "$v")" != running ]; then
        # nohup, not a bare `&`: the VM must outlive the command that started
        # it. A backgrounded child of `wk vm start` dies with the terminal,
        # which looks exactly like the VM crashing on boot.
        nohup "$(_tart_bin)" run --no-graphics "$v" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting $v (log: $runlog)"
    fi

    ip=$(_tart ip "$v" --wait "$wait" 2>/dev/null | grep .) \
        || die "$v did not come up within ${wait}s; see $runlog"
    _wait_ssh "$ip" || die "$v is up at $ip but ssh never answered; see $runlog"
    echo "$ip"
}

t_stop() {
    local v; v=$(_vm "$1")
    [ "$(_vm_state "$v")" = running ] || { info "$1 is not running"; return 0; }
    _tart stop "$v"
    info "stopped $1"
}

t_exec() {
    local name="$1"; shift
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # Two layers of quoting, both needed. ssh joins its command arguments with
    # spaces and hands the result to a remote shell, so the command has to be
    # one already-quoted string; and it is wrapped in a *login* shell because
    # the guest's PATH additions (Claude, ~/.local/bin) live in the profile.
    local cmd; cmd=$(sh_quote "$@")
    _ssh "$ip" "bash -lc $(sh_quote "$cmd")"
}

t_exec_tty() {
    local name="$1"; shift
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    local cmd; cmd=$(sh_quote "$@")
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    exec ssh -t $(_ssh_opts) "$WK_VM_USER@$ip" "bash -lc $(sh_quote "$cmd")"
}

t_enter() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    exec ssh -t $(_ssh_opts) "$WK_VM_USER@$ip" "cd $(t_src "$name") 2>/dev/null; exec \$SHELL -l"
}

# rsync rather than a mount: no --dir is ever passed to `tart run`, because a
# shared directory is exactly the host-filesystem hole this target exists to
# not have. The cost is that the tooling has to be pushed on every build, and
# rsync makes that a no-op when nothing changed.
t_sync_tools() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    debug "syncing wk-tools -> $name"
    rsync -az --delete --exclude '.git/' \
        -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_VM_USER@$ip:$(t_tools "$name")/"
}

t_destroy() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$v" != "$WK_VM_BASE" ] || die "refusing to delete the golden base (wk vm base --rebuild)"

    if [ "$(_vm_state "$v")" != absent ]; then
        _tart stop "$v" 2>/dev/null || true
        _tart delete "$v"
        info "deleted VM $v"
    fi

    # The guest's disk goes with `tart delete`, but the host-side status files,
    # build logs and boot log do not -- and "wk rm reclaims everything it
    # created" has to stay true here too.
    local ws; ws=$(wk_ws_dir "$name")
    [ -d "$ws" ] && { rm -rf "$ws"; info "removed $ws"; }
    rm -f "$WK_VM_DIR/$name.run.log"
}

# Build the base once, so every workspace starts warm.
#
# This is the whole reason a macOS workspace is usable. A cold Apple-port build
# is ~1.5 hours; an incremental one on top of an existing tree is minutes. And
# because `tart clone` is copy-on-write, a 39 GB build tree and a 10 GB
# compilation cache sitting in the base cost each workspace *nothing* -- the
# clone is still a second and a megabyte.
#
# It is also the answer to "where does the cache live". Xcode's compilation
# cache (DerivedData/CompilationCache.noindex, content-addressed, ~10 GB after
# a full build) is inside the guest, so it dies with `wk rm`. Putting it in the
# base means destroying a workspace never destroys the cache -- the base is not
# a workspace and `wk rm` refuses to touch it.
_prebuild_base() {
    local ip="$1"
    [ -n "$WK_VM_BASE_PREBUILD" ] || { info "base prebuild disabled"; return 0; }

    # shellcheck disable=SC1090
    . "$WK_ROOT/build/configs.sh"
    config_load "$WK_VM_BASE_PREBUILD" \
        || die "WK_VM_BASE_PREBUILD='$WK_VM_BASE_PREBUILD' is not a known config"

    # Sized from what the base guest actually has, the same way cmd/build sizes
    # a workspace build.
    local jobs
    jobs=$(WK_CGROUP_MB=$(_vm_get "$WK_VM_BASE" Memory) \
           WK_CGROUP_CORES=$(_vm_get "$WK_VM_BASE" CPU) build_jobs)

    info "pre-building '$WK_VM_BASE_PREBUILD' in the base with -j$jobs"
    log  "  This is the slow one and it happens once. Every workspace cloned"
    log  "  from this base inherits the build tree and the compilation cache."

    rsync -az --delete --exclude '.git/' -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_VM_USER@$ip:$(t_tools "$WK_VM_BASE")/"

    config_build_env "$(t_src "$WK_VM_BASE")" "$jobs" 10
    local cmd
    cmd="env $(sh_quote "${CFG_ENV[@]}") $(sh_quote "$(t_tools "$WK_VM_BASE")/build/build-in-target.sh")"

    local t0; t0=$(date +%s)
    if _ssh "$ip" "bash -lc $(sh_quote "$cmd")" > "$WK_VM_DIR/base-build.log" 2>&1; then
        info "base prebuild finished in $(( ($(date +%s) - t0) / 60 ))m"
    else
        warn "base prebuild FAILED after $(( ($(date +%s) - t0) / 60 ))m -- the base is
  still usable, but every workspace will pay for a cold build.
  log: $WK_VM_DIR/base-build.log"
        # first_error is in lib/watchdog.sh, which not every caller sources.
        if command -v first_error >/dev/null 2>&1; then
            first_error "$WK_VM_DIR/base-build.log" 2>/dev/null | sed 's/^/    /' || true
        fi
    fi
}

# --- helpers -----------------------------------------------------------------

_ssh_opts() {
    # ServerAliveInterval matters more than it looks: provisioning and builds
    # both go quiet for long stretches, and without keepalives a NAT timeout
    # drops the connection mid-build and reports it as a build failure.
    printf '%s' "-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
-o LogLevel=ERROR -o ConnectTimeout=10 -o BatchMode=yes \
-o ServerAliveInterval=60 -o ServerAliveCountMax=10 -i $WK_VM_KEY"
}

_ssh() {
    local ip="$1"; shift
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_VM_USER@$ip" "$@"
}

_ip() {
    local v; v=$(_vm "$1")
    [ "$(_vm_state "$v")" = running ] || return 1
    _tart ip "$v" --wait 30 2>/dev/null | grep .
}

_wait_ssh() {
    local ip="$1" i=0
    while [ "$i" -lt 60 ]; do
        _ssh "$ip" true 2>/dev/null && return 0
        sleep 2; i=$((i+1))
    done
    return 1
}

# Free space on the host, in GB. The guest disks are sparse, so what matters is
# what the host can still back -- not what the guest thinks it has.
_host_free_gb() {
    df -g / 2>/dev/null | awk 'NR==2 {print $4}'
}

_check_host_disk() {
    local free; free=$(_host_free_gb)
    [ -n "$free" ] || return 0

    if [ "$free" -lt "$WK_HOST_FREE_MIN_GB" ]; then
        die "only ${free} GB free on the host.
    A macOS guest's disk is sparse: the guest believes it has ${WK_VM_DISK_GB} GB,
    but every byte it writes has to come from here. At this point a build will
    fail as an I/O error inside the guest, which names nothing useful.

      wk vm ls                     what exists
      wk rm <name>                 reclaim a workspace
      tart prune --space-budget 0  drop the OCI image cache"
    elif [ "$free" -lt "$WK_HOST_FREE_WARN_GB" ]; then
        warn "${free} GB free on the host -- a Release build tree is ~39 GB and a Debug one ~78 GB, so this may not be enough to finish"
    fi
}

_check_guest_limit() {
    local running; running=$(_running_count)
    [ "${running:-0}" -lt "$WK_VM_MAX" ] && return 0
    die "$running macOS VM(s) are already running.
    Apple's licence permits $WK_VM_MAX per host and Virtualization.framework enforces it;
    a third fails with VZErrorDomain code 6. Stop one first:  wk vm stop <name>"
}

# --- resource envelope -------------------------------------------------------
# The mac VM competes with the podman VM for the same physical memory, and on a
# 32 GB machine the podman VM alone already holds the entire envelope. Two
# hypervisors each promised 20 GB is how a desktop starts swapping, so this is
# checked rather than hoped for.

_podman_mem_mb() {
    have podman || { echo 0; return; }
    podman machine inspect "${WK_MACHINE:-wk}" --format '{{.Resources.Memory}}' 2>/dev/null || echo 0
}

_podman_running() {
    have podman || return 1
    [ "$(podman machine inspect "${WK_MACHINE:-wk}" --format '{{.State}}' 2>/dev/null)" = running ]
}

# What a *new* VM gets.
_vm_cpus()   { echo "${WK_VM_CPUS:-$(envelope_cores)}"; }
_vm_mem_mb() { echo "${WK_VM_MEM_MB:-$(envelope_mem_mb)}"; }

# What an *existing* VM was actually created with, which is a different
# question and the one that matters when sizing a build. A VM created with
# WK_VM_MEM_MB=8192 keeps that allocation for life; deriving the job count from
# the default envelope on a later command would size the build for memory the
# guest does not have, and the result is an OOM in the middle of a link rather
# than an obvious error up front.
# _vm_get <full-vm-name> <field>
_vm_get() {
    _tart get "$1" --format json 2>/dev/null | jq -r --arg f "$2" '.[$f] // empty'
}

# _vm_configured <workspace-name> <field>
_vm_configured() { _vm_get "$(_vm "$1")" "$2"; }

# Memory already promised to running guests. The Apple limit caps the *number*
# of guests at two, and nothing caps their combined size -- so without this,
# starting a second 20 GB guest beside a first one is permitted right up to the
# point where the host starts swapping.
_committed_mem_mb() {
    local total=0 v m
    for v in $(_local_vms | jq -r '.[]|select(.State=="running")|.Name'); do
        m=$(_vm_get "$v" Memory)
        [ -n "$m" ] && total=$(( total + m ))
    done
    echo "$total"
}

t_cores() {
    local c; c=$(_vm_configured "$1" CPU)
    [ -n "$c" ] && { echo "$c"; return 0; }
    _vm_cpus
}

t_mem_mb() {
    local m; m=$(_vm_configured "$1" Memory)
    [ -n "$m" ] && { echo "$m"; return 0; }
    _vm_mem_mb
}

# _check_memory_budget <label> <requested-mb>
_check_memory_budget() {
    local name="$1" mine="$2" podman_mb guests total budget spare
    podman_mb=0
    _podman_running && podman_mb=$(_podman_mem_mb)
    guests=$(_committed_mem_mb)
    total=$(( mine + podman_mb + guests ))
    budget=$(envelope_mem_mb)

    [ "$total" -le "$budget" ] && return 0

    if [ -n "${WK_VM_SHARE:-}" ]; then
        warn "'$name' (${mine}MB) on top of ${podman_mb}MB podman + ${guests}MB of running guests exceeds the ${budget}MB envelope -- continuing because WK_VM_SHARE is set"
        return 0
    fi

    # What would actually fit alongside what is already committed. Suggesting a
    # number is the difference between a useful refusal and one that just
    # restates the problem -- and when nothing fits, saying so is the honest
    # answer.
    spare=$(( budget - podman_mb - guests ))

    # Advice, not a command line: this is reached both for a workspace and for
    # the golden base, and "wk vm start wk-base" is not a thing anyone can run.
    local advice
    if [ "$spare" -ge 4096 ]; then
        advice="      WK_VM_MEM_MB=$spare, then retry
          run it in the ${spare}MB that is actually free"
    else
        advice="      (only ${spare}MB is unspoken for, which is not enough to build in --
       freeing one of the above is the realistic option)"
    fi

    local rows=""
    [ "$podman_mb" -gt 0 ] && rows="$rows
      $(printf '%-26s %6s MB   running' "podman machine '${WK_MACHINE:-wk}'" "$podman_mb")"
    [ "$guests" -gt 0 ] && rows="$rows
      $(printf '%-26s %6s MB   running' "other macOS guest(s)" "$guests")"
    rows="$rows
      $(printf '%-26s %6s MB   requested' "macOS VM '$name'" "$mine")
      $(printf '%-26s %6s MB   (%s MB total, %s MB kept for the desktop)' \
            'host envelope' "$budget" "$(host_mem_mb)" "$WK_RESERVE_MB")"

    die "not enough memory to start '$name'.
$rows

    Everything holding memory holds all of it, whether or not it is busy, so
    this refuses rather than letting you find out during a link.

      podman machine stop ${WK_MACHINE:-wk}
          free the whole envelope (workspaces and their state survive)
      wk vm stop <name>
          free a running guest
$advice
      WK_VM_SHARE=1, then retry
          proceed anyway"
}

# --- the golden base ---------------------------------------------------------
# Built once and then never rebuilt, because everything expensive about a macOS
# build environment -- the Setup Assistant, a multi-hour Xcode install, the
# WebKit clone -- is paid inside it exactly once and inherited by every clone.

_base_exists() { [ "$(_vm_state "$WK_VM_BASE")" != absent ]; }

_ensure_base() {
    _base_exists && return 0

    require jq "jq is required (macOS ships it at /usr/bin/jq)"

    if ! _tart list --format json --source oci 2>/dev/null | jq -e --arg i "$WK_VM_IMAGE" '.[]|select(.Name==$i)' >/dev/null; then
        info "pulling $WK_VM_IMAGE -- tens of GB, once only"
        _tart pull "$WK_VM_IMAGE"
    fi

    info "creating the golden base VM '$WK_VM_BASE'"
    _tart clone "$WK_VM_IMAGE" "$WK_VM_BASE"
    _tart set "$WK_VM_BASE" --cpu "$(_base_cpus)" --memory "$(_base_mem_mb)"

    _provision_base
}

# Provisioning runs against the base VM only. Every workspace inherits the
# result through the clone, so nothing here is ever re-run per workspace.
_provision_base() {
    _check_guest_limit
    _check_memory_budget "$WK_VM_BASE" "$(_base_mem_mb)"
    ensure_dir "$WK_VM_DIR" 0700

    # Before booting: tart can only grow a disk, and only while the VM is off.
    # Idempotent -- asking for a size it already has is accepted and does
    # nothing, so `wk vm base --refresh` is how an undersized base gets fixed.
    local cur; cur=$(_vm_get "$WK_VM_BASE" Disk)
    if [ -n "$cur" ] && [ "$cur" -lt "$WK_VM_DISK_GB" ]; then
        info "growing the base disk ${cur}GB -> ${WK_VM_DISK_GB}GB"
        _tart set "$WK_VM_BASE" --disk-size "$WK_VM_DISK_GB"
    fi
    # Also while it is off: the base ends provisioning with a full build, so it
    # needs the same envelope a workspace gets, not a token allocation.
    _tart set "$WK_VM_BASE" --cpu "$(_base_cpus)" --memory "$(_base_mem_mb)"
    _check_host_disk

    if [ ! -f "$WK_VM_KEY" ]; then
        ssh-keygen -q -t ed25519 -N '' -C wk-vm -f "$WK_VM_KEY"
        changed "generated the macOS VM ssh key"
    fi

    # A first boot has Setup Assistant work to get through and is much slower
    # than a warm one, hence the longer wait than t_start uses.
    #
    # ssh is not reachable yet on a pristine image -- the key goes in below --
    # so this waits only for an address, and _boot's ssh probe would time out.
    local runlog="$WK_VM_DIR/base.run.log"
    local ip
    if [ "$(_vm_state "$WK_VM_BASE")" != running ]; then
        nohup "$(_tart_bin)" run --no-graphics "$WK_VM_BASE" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting the base VM for provisioning (log: $runlog)"
    fi
    ip=$(_tart ip "$WK_VM_BASE" --wait 300 2>/dev/null | grep .) \
        || die "base VM did not boot; see $runlog"

    # The guest agent is how the key gets in without ever typing the image's
    # default password, and without leaving password auth working afterwards.
    # Idempotent, so re-provisioning an existing base is safe.
    info "installing the wk ssh key over the guest agent"
    local pub; pub=$(cat "$WK_VM_KEY.pub")
    _tart exec "$WK_VM_BASE" /bin/sh -c \
        "mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
         grep -qxF '$pub' ~/.ssh/authorized_keys 2>/dev/null || \
         echo '$pub' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys" \
    || die "could not reach the guest agent in '$WK_VM_BASE'.
    \`tart exec\` needs the Tart guest agent, which the Cirrus Labs images ship
    but a vanilla macOS image does not. Without it there is no way to get an ssh
    key in unattended -- log in once with the image's own credentials and append
    $WK_VM_KEY.pub to ~/.ssh/authorized_keys by hand, then re-run."

    _wait_ssh "$ip" || die "ssh key was installed but ssh still refuses; see $runlog"

    info "provisioning the base VM (Xcode licence, WebKit checkout, Claude CLI)"
    rsync -az -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/vm/provision-base.sh" "$WK_VM_USER@$ip:/tmp/provision-base.sh"
    _ssh "$ip" "bash /tmp/provision-base.sh" || die "base provisioning failed"

    _prebuild_base "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    changed "golden base VM '$WK_VM_BASE' is ready"
}
