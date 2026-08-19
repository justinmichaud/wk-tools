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
# --- isolation ----------------------------------------------------------------
#
# A macOS guest gets the same three properties a container workspace gets, by
# different means:
#
#   host filesystem   unreachable. No --dir is ever passed, so /Users does not
#                     exist in the guest by construction.
#   disposable        `wk rm` deletes the whole guest.
#   filtered egress   Softnet, a userspace packet filter that Tart runs as a
#                     subprocess on the HOST. Default-deny, with one address
#                     allowed: the host's own, where wk-proxy listens. The
#                     guest therefore reaches the network only through the same
#                     hostname allowlist a container uses.
#
# The filter is deliberately outside the guest. `pf` inside it would not be a
# boundary at all: anything running as root in the guest can rewrite it, and
# "root in the guest" is exactly what is being sandboxed.
#
# Softnet needs root (vmnet does), so it is installed SUID root once at setup
# time by host/macos/softnet.sh. That is the same trade the quiesce helper
# makes; `wk` itself still never calls sudo.
#
# WK_VM_UNFILTERED=1 turns the filter off for a guest that needs the open
# network. It says so loudly, because a workspace that is quietly less confined
# than it looks is worse than one that is openly unconfined.

WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode:26.5}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"
WK_VM_USER="${WK_VM_USER:-admin}"

# The egress boundary for guests. The proxy listens on the host's address on
# the guest-facing bridge, which is also the one address Softnet lets the guest
# reach.
#
# That address is NOT vmnet's usual 192.168.64.1: Softnet runs its own network
# and puts the host at 192.168.2.1. Discovered from the live interface where
# possible, because assuming it is how this was wrong the first time -- the
# proxy failed to bind an address that did not exist on this machine.
WK_VM_SUBNET="${WK_VM_SUBNET:-192.168.2}"
WK_VM_PROXY_PORT="${WK_VM_PROXY_PORT:-3128}"

# The host's address on the guest bridge. Read from the interface when a guest
# is up (authoritative), otherwise the documented default.
_proxy_addr() {
    [ -n "${WK_VM_PROXY_ADDR:-}" ] && { echo "$WK_VM_PROXY_ADDR"; return 0; }
    local a
    a=$(ifconfig 2>/dev/null | awk -v net="$WK_VM_SUBNET." '
        $1 == "inet" && index($2, net) == 1 { print $2; exit }')
    echo "${a:-${WK_VM_SUBNET}.1}"
}
WK_SOFTNET_BIN="${WK_SOFTNET_BIN:-/usr/local/bin/softnet}"

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

# Guest display size. The stock image is 1024x768, which is too small to do any
# real browser work in, and nothing ever changed it -- t_create set cpu, memory,
# mac and serial and left the display alone, so every workspace inherited it.
# Points, not pixels: tart defaults the unit to "pt" for a macOS guest.
#
# Deliberately SMALLER than the host desktop, and this is the whole trick.
# Tart pins the window's *minimum* content size to this resolution
# (Run.swift, `.frame(minWidth: config.display.width, ...)`), and AppKit drops
# NSWindowCollectionBehavior.fullScreenPrimary for fullScreenNone the moment a
# window's minSize exceeds screen.frame. So setting this to the host's own
# logical size -- 1920x1080 here -- produces a window that can neither shrink
# nor go full screen, with its resize corner hanging off the bottom of the
# screen. Measured on this host: 1080 fails, 1048 is the exact threshold.
# Bigger is emphatically not better; 3840x2160 would be strictly worse.
#
# Small instead, and let the guest follow the window up. --display-refit sets
# VZVirtualMachineView.automaticallyReconfiguresDisplay, so the *guest* tracks
# the *window* on every live resize -- go full screen and the guest becomes
# 1920x1080pt at 2x, i.e. a real 3840x2160 Retina desktop, which is the target
# you actually want for MiniBrowser. That path only exists if the window can be
# resized in the first place.
#
# Upstream considers this working as intended: PR #1086 proposed dropping the
# minimum and was rejected (issue #1087), the recommendation there being to
# make the configured display small. Unchanged as of tart 2.35.0, the newest
# release, so do not expect this to be fixed for you.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"

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
#
# The path is resolved through any symlinks, and that matters more than it
# looks. `tart` on PATH is normally a symlink into the .app, and launching a
# bundled app through a path *outside* its bundle means LaunchServices never
# associates the process with the bundle. The window is then created without
# NSWindowCollectionBehavior.fullScreenPrimary, so the green button zooms
# instead of entering full screen and there is no way to get it back at
# runtime. Measured with an otherwise identical binary: launched by its real
# path inside Contents/MacOS, fullScreenPrimary is true; launched through a
# symlink to exactly that file, it is false.
_tart_bin() {
    local p
    if command -v tart >/dev/null 2>&1; then p=$(command -v tart)
    elif [ -x "$HOME/.local/bin/tart" ]; then p="$HOME/.local/bin/tart"
    elif [ -x "$HOME/.local/share/tart/tart.app/Contents/MacOS/tart" ]; then
        p="$HOME/.local/share/tart/tart.app/Contents/MacOS/tart"
    else return 1
    fi
    # `readlink -f` only grew symlink-chain resolution on recent macOS, and
    # this file has to keep working under bash 3.2 on an older one.
    if readlink -f "$p" >/dev/null 2>&1; then readlink -f "$p"
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$p"
    else echo "$p"
    fi
}

_tart() {
    local bin
    bin=$(_tart_bin) || die "tart is not installed.
    Install the signed bundle (it needs the virtualization entitlement, so the
    .app must stay intact):
      mkdir -p ~/.local/share/tart ~/.local/bin
      curl -fsSLO https://github.com/cirruslabs/tart/releases/latest/download/tart.tar.gz
      tar -xzf tart.tar.gz -C ~/.local/share/tart/
      ln -sfn ~/.local/share/tart/tart.app/Contents/MacOS/tart ~/.local/bin/tart
    Licence: FSL-1.1-ALv2; internal use is a Permitted Purpose (SETUP.md section 8)."
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
    # --display-refit is repeated on every `tart set`, not just the one that
    # means to change it: Set.swift assigns displayRefit unconditionally, so any
    # `tart set` that omits the flag silently clears it (tart issue #1248, still
    # open on main). Passing it always is cheaper than discovering that later.
    _tart set "$v" --cpu "$(_vm_cpus)" --memory "$(_vm_mem_mb)" --random-mac --random-serial \
        --display "$WK_VM_DISPLAY" --display-refit
    debug "clone took $(( $(date +%s) - t0 ))s"

    # A clone that took minutes fell back to a real copy instead of using
    # clonefile(2), and the disk cost then multiplies per workspace. Worth
    # knowing immediately rather than when the disk fills.
    [ $(( $(date +%s) - t0 )) -gt 60 ] && \
        warn "the clone took $(( $(date +%s) - t0 ))s -- APFS copy-on-write may not be in play; check disk use"

    ensure_dir "$(wk_ws_dir "$name")"
}

t_start() {
    local name="$1" ip
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" != absent ] || die "no such workspace: $name"
    if [ "$(_vm_state "$v")" = running ]; then
        ip=$(_ip "$name")
        _write_marker "$name" "$ip" || debug "could not write the workspace marker in $name"
        echo "$ip"
        return 0
    fi

    _check_guest_limit
    _check_memory_budget "$name" "$(t_mem_mb "$name")"
    _check_host_disk

    # t_start's stdout is the guest's address, so _boot's is captured and
    # re-echoed rather than left to fall through. Best-effort marker: a guest
    # that came up is up, and refusing to report that because one ssh failed
    # would be the wrong trade.
    ip=$(_boot "$v" 180)
    _write_marker "$name" "$ip" || debug "could not write the workspace marker in $name"
    echo "$ip"
}

# The tart flags that confine a guest's egress.
#
# --net-softnet-block=0.0.0.0/0 is default-deny; the single --net-softnet-allow
# is the host, where wk-proxy listens. Longest-prefix-match wins in Softnet and
# a /32 beats the /0, so this is "nothing except the proxy".
_softnet_flags() {
    if [ -n "${WK_VM_UNFILTERED:-}" ]; then
        warn "WK_VM_UNFILTERED=1 -- this guest gets the open network, with no egress filter"
        return 0
    fi
    # Fail closed: a guest booted without the filter has the open network for
    # its whole lifetime, because Softnet applies at `tart run` and cannot be
    # added to a running guest. Booting unfiltered is available, but only by
    # saying so.
    [ -x "$WK_SOFTNET_BIN" ] || die "softnet is not installed, so this guest's egress would not be filtered.
    Install it:  ./setup --stage softnet   (needs a terminal for sudo)
    Or set WK_VM_UNFILTERED=1 to boot with the open network anyway."
    printf '%s\n' --net-softnet \
        "--net-softnet-block=0.0.0.0/0" \
        "--net-softnet-allow=$(_proxy_addr)/32"
}

# The proxy the guest is allowed to reach: the same wk-proxy.py the containers
# use, with the same allowlist, listening on TCP because a guest cannot see a
# unix socket across the hypervisor boundary.
#
# Started on demand rather than at login. It binds to the vmnet gateway
# address, and that interface exists only while a VM is running -- a launchd
# agent at boot would simply fail to bind.
_proxy_pidfile() { echo "$WK_VM_DIR/proxy.pid"; }

_proxy_running() {
    local pf; pf=$(_proxy_pidfile)
    [ -f "$pf" ] && kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null
}

_start_host_proxy() {
    [ -n "${WK_VM_UNFILTERED:-}" ] && return 0
    _proxy_running && { debug "host proxy already running"; return 0; }

    ensure_dir "$WK_VM_DIR"
    local log="$WK_VM_DIR/proxy.log" addr i=0

    # Wait for the address to actually exist before binding it. The guest
    # bridge is created as the guest boots and its address is configured a
    # moment later, so a proxy started the instant `tart ip` answers loses a
    # race and dies with EADDRNOTAVAIL on an address that appears half a second
    # afterwards.
    addr=$(_proxy_addr)
    while [ "$i" -lt 30 ]; do
        ifconfig 2>/dev/null | grep -q "inet $addr " && break
        sleep 0.5; i=$((i + 1))
    done
    if ! ifconfig 2>/dev/null | grep -q "inet $addr "; then
        warn "the guest bridge never got address $addr; not starting the proxy"
        return 1
    fi

    # WK_PROXY_UNIX=0: no unix socket here. That one is for containers, lives
    # in the podman VM, and has nothing to do with this.
    WK_PROXY_UNIX=0 \
    WK_PROXY_TCP="$addr:$WK_VM_PROXY_PORT" \
    WK_STORE="$WK_STORE" \
    nohup /usr/bin/python3 "$WK_ROOT/container/proxy/wk-proxy.py" >"$log" 2>&1 &
    echo $! > "$(_proxy_pidfile)"
    disown 2>/dev/null || true

    i=0
    while [ "$i" -lt 20 ]; do
        _proxy_running || break
        grep -q "listening on $addr" "$log" 2>/dev/null && {
            info "egress proxy on $addr:$WK_VM_PROXY_PORT"
            return 0
        }
        sleep 0.5; i=$((i + 1))
    done

    warn "the host egress proxy did not start; the guest will have no egress at all
  (Softnet denies everything except the proxy address). See $log"
    return 1
}

# _boot <vm-name> <seconds> -- start a guest if it is not already up, and echo
# its address once ssh answers. Shared by t_start and base provisioning, which
# otherwise grow two subtly different copies of the same waiting logic.
_boot() {
    local v="$1" wait="${2:-180}" ip runlog

    ensure_dir "$WK_VM_DIR"
    runlog="$WK_VM_DIR/${v#wk-}.run.log"

    if [ "$(_vm_state "$v")" != running ]; then
        # The flags are computed *before* the tart command line: inside the
        # $(...) below, a die in _softnet_flags would kill only the subshell and
        # tart would run anyway -- unfiltered, silently. The separate assignment
        # is what lets the failure actually stop the boot.
        local sflags
        sflags=$(_softnet_flags)

        # Recorded so `wk claude` can tell how this guest was *booted*, which
        # is what decides whether its egress is filtered -- the binary existing
        # now says nothing about a guest started before it was installed.
        if [ -z "$sflags" ]; then
            : > "$WK_VM_DIR/${v#wk-}.unfiltered"
        else
            rm -f "$WK_VM_DIR/${v#wk-}.unfiltered"
        fi

        # nohup, not a bare `&`: the VM must outlive the command that started
        # it. A backgrounded child of `wk vm start` dies with the terminal,
        # which looks exactly like the VM crashing on boot.
        #
        # Windowed, deliberately -- there is no --no-graphics here and no flag
        # to put it back. A macOS guest is the one workspace kind that has a
        # real GPU (Virtualization.framework backs its Metal stack with the
        # host's; a Linux guest gets no GPU device at all, which is why the
        # podman machine can never be accelerated -- see SETUP.md). The window
        # presents that framebuffer directly. --no-graphics does not take the
        # GPU away, but the only way back to the screen is then VNC or Screen
        # Sharing, which capture and re-encode every frame: fine for poking at
        # a UI, worthless for anything measuring rendering. Since interacting
        # with MiniBrowser is the point of this target, headless is not a mode
        # worth carrying a second code path for.
        # shellcheck disable=SC2086 -- deliberate word splitting of the flags.
        nohup "$(_tart_bin)" run $sflags "$v" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting $v (log: $runlog)"
    fi

    # The default dhcp resolver works behind Softnet -- measured. Tart's
    # documentation warns that the *arp* resolver does not, which is easy to
    # over-read; the agent resolver also works but is slower to become
    # available during boot, and using it here cost a 180 s timeout on a guest
    # that had in fact started perfectly.
    ip=$(_tart ip "$v" --wait "$wait" 2>/dev/null | grep .) \
        || die "$v did not come up within ${wait}s; see $runlog"

    # Only now can the proxy bind: the guest bridge, and the host address on
    # it, come into existence with the first running guest. Starting it earlier
    # fails with EADDRNOTAVAIL on an address that does not exist yet.
    _start_host_proxy || true

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

# The marker that tells the guest's own wk that it *is* a workspace, and which
# one -- without it, `wk build` in there tries to reach a podman machine that a
# macOS guest can never host. See targets/local.sh.
#
# Written from the host, not by provisioning, and deliberately not into the
# golden base: the base is not a workspace, and a marker in it would be
# inherited by every clone naming the base. The host is also the only side that
# knows what a clone was called -- left to guess, the guest would fall back to
# its own hostname, which on a Cirrus Labs image is `Manageds-Virtual-Machine`,
# a name appearing nowhere in `wk ls`.
#
# Called from two places, because either alone leaves a real gap: t_sync_tools,
# so it is true before every build and test, and t_start, so a guest that is
# booted and handed straight to `wk claude` has it without a host-side build
# first -- which is the case that matters most, since that agent's only way to
# build is this interface. Idempotent and cheap: one ssh.
#
# config= is what a bare `wk run` or `wk test` in the guest reaches for. The
# tree the guest inherited from the base is $WK_VM_BASE_PREBUILD's, and a macOS
# guest can build nothing but the Apple ports, so the jsc-release default that
# is right in a container is never right here.
_write_marker() {
    local name="$1" ip="$2"
    _ssh "$ip" "printf '%s\n' \
        '# wk: this machine IS a workspace. Written by targets/vm.sh.' \
        $(sh_quote "name=$name") $(sh_quote "src=$(t_src "$name")") \
        $(sh_quote "config=${WK_VM_BASE_PREBUILD:-mac-release}") \
        > \$HOME/.wk-workspace"
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

    _write_marker "$name" "$ip"
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

    # Detached, and polled -- NOT a foreground `ssh <long command>`.
    #
    # This build takes over an hour and it used to run in the foreground of one
    # ssh session, which meant any blip on that connection killed it. That is
    # not hypothetical: the last base prebuild died at
    # "client_loop: send disconnect: Broken pipe" with no BUILD SUCCEEDED, an
    # hour and a half in, and left a base that looked built but was not. A
    # build that dies at minute 95 is the slowest possible outcome, so the
    # build is started with nohup, its exit status is written to a file in the
    # guest, and this side merely watches for that file. Dropping the poll
    # connection now costs one retry instead of the whole build.
    local rlog="/tmp/wk-base-build.log" rrc="/tmp/wk-base-build.rc"
    local inner="( $cmd ) > $rlog 2>&1; echo \$? > $rrc"
    _ssh "$ip" "rm -f $rrc; nohup bash -lc $(sh_quote "$inner") >/dev/null 2>&1 </dev/null & disown" \
        || die "could not start the base prebuild"

    local t0; t0=$(date +%s) rc="" mins=0
    while [ -z "$rc" ]; do
        sleep 30
        # A failed poll means the connection blipped, not that the build died --
        # keep waiting. The build itself is no longer attached to this ssh.
        rc=$(_ssh "$ip" "cat $rrc 2>/dev/null" 2>/dev/null | tr -dc '0-9')
        [ -n "$rc" ] && break
        mins=$(( ($(date +%s) - t0) / 60 ))
        [ $(( mins % 10 )) -eq 0 ] && [ "$mins" -gt 0 ] && \
            info "base prebuild still running (${mins}m)"
    done
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$rlog" "$WK_VM_DIR/base-build.log" 2>/dev/null || true

    if [ "$rc" = 0 ]; then
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

    # Seed the checkout from a clone that already exists on the host, if there
    # is one. Cloning WebKit from GitHub happens through the egress proxy, and
    # the same objects are usually sitting on local disk already -- the host's
    # .git is ~1.8 GB. A bare `git clone --local` hardlinks rather than copies,
    # so making the seed costs almost nothing, and rsync moves it over the
    # vmnet bridge rather than the internet.
    #
    # Best-effort throughout: if there is no host checkout, or any step fails,
    # nothing is left behind and provision-base.sh falls back to cloning from
    # GitHub exactly as before. This must never be the reason a base fails.
    local hostwk="${WK_HOST_WEBKIT:-$HOME/Development/WebKit}"
    if [ -d "$hostwk/.git" ]; then
        local seed; seed=$(mktemp -d)
        info "seeding the guest checkout from $hostwk"
        if git clone --bare --quiet --single-branch --branch main \
               "$hostwk" "$seed/wk-seed.git" 2>/dev/null; then
            rsync -a -e "ssh $(_ssh_opts)" \
                "$seed/wk-seed.git/" "$WK_VM_USER@$ip:/tmp/wk-seed.git/" 2>/dev/null \
                || warn "could not copy the seed to the guest; it will clone from GitHub"
        else
            warn "could not make a seed clone from $hostwk; the guest will clone from GitHub"
        fi
        rm -rf "$seed"
    fi

    info "provisioning the base VM (Xcode licence, WebKit checkout, Claude CLI)"
    # The whole tree, not just the provisioning script: provision-base.sh links
    # ~/.claude (settings, hooks, CLAUDE.md, skills) out of it, and a guest
    # without those runs a skip-permissions agent with no policy at all.
    rsync -az --delete --exclude '.git/' -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_VM_USER@$ip:$(t_tools "$WK_VM_BASE")/"
    # _proxy_addr, not $WK_VM_PROXY_ADDR. The variable is normally *unset* --
    # it is an override, and the address is otherwise derived from the bridge
    # that only exists once a guest is running. Passing the raw variable sent
    # an empty value, provision-base.sh fell back to its hardcoded Tart default
    # of 192.168.64.1, and the guest was left pointing at an address nothing
    # listens on: every fetch inside the guest timed out, which looks exactly
    # like the egress filter doing its job rather than a misconfiguration.
    _ssh "$ip" "env WK_VM_PROXY_ADDR=$(sh_quote "$(_proxy_addr)") WK_VM_PROXY_PORT=$(sh_quote "$WK_VM_PROXY_PORT") WK_VM_DISPLAY=$(sh_quote "$WK_VM_DISPLAY") bash $(t_tools "$WK_VM_BASE")/vm/provision-base.sh" \
        || die "base provisioning failed"

    _prebuild_base "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    changed "golden base VM '$WK_VM_BASE' is ready"
}
