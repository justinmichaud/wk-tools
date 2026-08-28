# Target driver: a disposable macOS VM, for building the Apple ports. Uses
# Tart. Four facts shape this driver: Apple permits exactly two *running*
# macOS VMs per host, Virtualization.framework-enforced (VZErrorDomain code
# 6 on a third), checked in t_start rather than t_create since the limit is
# on running guests; `tart clone` is APFS copy-on-write, so a golden base VM
# is built once, with Xcode and a WebKit checkout in it, and every workspace
# is a free clone of it; `tart exec` runs commands through the guest agent
# the Cirrus Labs images ship, injecting the ssh key without ever typing the
# default password; and a macOS guest cannot be firewalled from outside (see
# "isolation" below).
#
# --- isolation ----------------------------------------------------------------
# A macOS guest gets the same three properties a container workspace gets, by
# different means:
#   host filesystem   unreachable: no --dir is ever passed to `tart run`.
#   disposable        `wk rm` deletes the whole guest.
#   filtered egress   Softnet, a userspace packet filter Tart runs as a
#                     subprocess on the HOST. Default-deny, with one address
#                     allowed: the host's own, where wk-proxy listens.
# The filter is deliberately outside the guest: anything running as root in
# the guest -- exactly what is being sandboxed -- could rewrite a `pf` inside
# it. Softnet needs root, so it is installed SUID root once at setup time by
# host/macos/softnet.sh; `wk` itself still never calls sudo. WK_VM_UNFILTERED=1
# turns the filter off for a guest that needs the open network, and says so
# loudly: quietly less confined than it looks is worse than openly unconfined.

# Every WK_VM_*/WK_HOST_* name in this file is overridable; documented here
# once rather than at each read:
#   WK_VM_IMAGE            the OCI image a fresh golden base is cloned from
#   WK_VM_BASE              the golden base VM's own name (tests/test_prompts.py
#                            and tests/test_clobbering.py/test_registry_free.py
#                            point WK_VM_BASE/WK_VM_STORE at a fake one)
#   WK_VM_MAX                running-guest ceiling (Apple permits 2 per host)
#   WK_VM_USER                the account every guest is provisioned with
#   WK_VM_CPUS/WK_VM_MEM_MB    a workspace guest's allocation (default: the envelope)
#   WK_VM_BASE_CPUS/_MEM_MB    the golden base's allocation (default: the same envelope)
#   WK_VM_BASE_PREBUILD       config pre-built into the base; empty disables it
#   WK_VM_DISK_GB              the sparse disk ceiling every guest is cloned with
#   WK_VM_DISPLAY               the guest's minimum window content size
#   WK_VM_SHARE                proceed past the memory-budget refusal anyway
#   WK_VM_UNFILTERED            boot with no Softnet egress filter (see above)
#   WK_HOST_FREE_MIN_GB/_WARN_GB   host disk headroom the sparse guest disk needs
#   WK_HOST_WEBKIT               a host checkout to seed the golden base from
#   WK_VM_STORE                  where this driver's own state lives (below)
#   WK_VM_SUBNET/WK_VM_PROXY_ADDR/WK_VM_PROXY_PORT/WK_SOFTNET_BIN
#       the guest-facing bridge and egress proxy (below)
WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode:26.5}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"
WK_VM_USER="${WK_VM_USER:-admin}"

# The egress boundary for guests: the proxy listens on the host's address on
# the guest-facing bridge, the one address Softnet lets the guest reach. NOT
# vmnet's usual 192.168.64.1 -- Softnet runs its own network, discovered from
# the live interface since it is machine-specific.
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

# softnet's directory goes on PATH for *tart's* benefit, not ours: tart
# resolves softnet through PATH when it builds the guest's network, so
# verifying the binary by absolute path is not enough. A non-interactive
# ssh -- every fleet verb -- has only /usr/bin:/bin:/usr/sbin:/sbin.
case ":$PATH:" in
    *":$(dirname "$WK_SOFTNET_BIN"):"*) ;;
    *) PATH="$(dirname "$WK_SOFTNET_BIN"):$PATH"; export PATH ;;
esac

# The golden base gets the full envelope: provisioning it ends in a complete
# WebKit build (_prebuild_base), not just a clone. _base_cpus/_base_mem_mb
# call envelope_cores/envelope_mem_mb lazily, so sourcing this driver never
# requires the caller to have sourced lib/resources.sh first (`wk start`/
# `wk stop` load it only to stop a guest); guarded, like targets/local.sh
# already does it, since sourcing twice is safe.
command -v envelope_mem_mb >/dev/null 2>&1 || . "$WK_ROOT/lib/resources.sh"
WK_VM_BASE_CPUS="${WK_VM_BASE_CPUS:-}"
WK_VM_BASE_MEM_MB="${WK_VM_BASE_MEM_MB:-}"
_base_cpus()   { echo "${WK_VM_BASE_CPUS:-$(envelope_cores)}"; }
_base_mem_mb() { echo "${WK_VM_BASE_MEM_MB:-$(envelope_mem_mb)}"; }

# What the base is built with before it is sealed; every workspace inherits
# the result. Empty disables it -- the base is then source-only and the first
# build in each workspace is a cold one.
WK_VM_BASE_PREBUILD="${WK_VM_BASE_PREBUILD:-mac-release}"

# Disk, and this one is not optional: the prepared image's stock 140 GB does
# not fit even one build. A ceiling, not an allocation: the disk is sparse
# and the clones are copy-on-write, so an unused gigabyte costs nothing here
# and instead risks overcommitting the *host*, which _check_host_disk is for.
WK_VM_DISK_GB="${WK_VM_DISK_GB:-320}"

# Guest display size, in points (tart defaults the unit to "pt"). Deliberately
# SMALLER than the host desktop: Tart pins the window's *minimum* content
# size to this resolution, and AppKit drops fullScreenPrimary once minSize
# exceeds the screen. --display-refit then tracks the *window* on every
# resize, so full screen gives a real Retina desktop.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"

# Host space to remain for a sparse guest disk to grow into, or a build
# fails as an I/O error inside the guest, naming nothing real.
WK_HOST_FREE_WARN_GB="${WK_HOST_FREE_WARN_GB:-80}"
WK_HOST_FREE_MIN_GB="${WK_HOST_FREE_MIN_GB:-25}"

# $WK_STORE defaults to /var/lib/wk, right inside the podman VM and wrong on
# a macOS workstation, where nothing may write outside $HOME.
WK_STORE="${WK_VM_STORE:-$(wk_state_dir)}"
WK_VM_DIR="$WK_STORE/vm"
WK_VM_KEY="$WK_VM_DIR/id_ed25519"

# --- the tart binary ---------------------------------------------------------
# Not installed by ./setup: it needs the com.apple.security.virtualization
# entitlement, so it stays inside the signed app bundle Cirrus Labs ships
# rather than being copied to a bare path. Resolved through any symlinks,
# since launching the bundled app from outside the .app loses
# fullScreenPrimary (LaunchServices never associates the process with it).
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
    Licence: FSL-1.1-ALv2; internal use is a Permitted Purpose (README.md, Setup)."
    "$bin" "$@"
}

_vm() { echo "wk-$1"; }

# `tart list` mixes local VMs and cached OCI images, spelling Source "local"
# case-folded. No tart means an empty list, not an empty string, which
# t_info would read as a state.
_vm_json() {
    local bin
    bin=$(_tart_bin) || { echo '[]'; return 0; }
    "$bin" list --format json 2>/dev/null || echo '[]'
}

_vm_query() {
    _vm_json | python3 -c '
import json, sys
q, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
vms = [v for v in json.load(sys.stdin) if str(v.get("Source", "")).lower() == "local"]
if q == "state":
    print(next((v.get("State", "absent") for v in vms if v.get("Name") == arg), "absent"))
elif q == "running":
    print("\n".join(v["Name"] for v in vms if v.get("State") == "running"))
elif q == "running_count":
    print(sum(1 for v in vms if v.get("State") == "running"))
elif q == "list":
    for v in vms:
        n = v.get("Name", "")
        if n.startswith("wk-") and n != arg:
            print(n[3:] + "\t" + v.get("State", ""))
' "$@"
}

_vm_state()      { _vm_query state "$1"; }
_running_count() { _vm_query running_count; }

# --- contract ----------------------------------------------------------------

t_src()   { echo "/Users/$WK_VM_USER/WebKit"; }
t_tools() { echo "/Users/$WK_VM_USER/wk-tools"; }

# `wk new` resolves a base *snapshot*; there are none here, since cloning
# the golden VM directly is the checkout.
t_needs_base() { return 1; }

# Only what this target actually uses: the overlay scheme's mirror,
# snapshots and download caches have no macOS-VM equivalent.
t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
    ensure_dir "$WK_VM_DIR" 0700
}

t_list() {
    # The golden base is infrastructure, not a workspace: listing it invites
    # `wk rm`, which would throw away the only expensive thing here.
    _vm_query list "$WK_VM_BASE"
}

# On the host: the golden guest is not running while the clone happens, so
# there is nothing to write to yet. Rules out a `tart clone`/`tart set`
# killed part-way, which `tart list` reports happily regardless.
t_created() { [ -f "$(wk_ws_dir "$1")/$WK_READY_MARKER" ]; }

t_info() {
    local st; st=$(_vm_state "$(_vm "$1")")
    [ "$st" = absent ] && { echo absent; return 0; }
    t_created "$1" || { echo creating; return 0; }
    echo "$st"
}

t_ssh_host() {
    local ip; ip=$(_ip "$1") || return 1
    echo "$WK_VM_USER@$ip"
}

# A guest is reached at an address, so there is no proxy to run (the default
# refuses); the account is the one the golden image was provisioned with.
t_ssh_user() { printf '%s' "$WK_VM_USER"; }

t_create() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" = absent ] || die "workspace '$name' already exists"

    _ensure_base

    # Not fatal -- the guest limit applies to running VMs, not created ones.
    local running; running=$(_running_count)
    [ "${running:-0}" -ge "$WK_VM_MAX" ] && \
        warn "$running macOS VM(s) already running; you will have to stop one before starting '$name'"

    info "cloning $WK_VM_BASE -> $v (APFS copy-on-write)"
    local t0; t0=$(date +%s)
    _tart clone "$WK_VM_BASE" "$v"
    # --display-refit is passed on every `tart set`, not just the one meant to
    # change it: Set.swift assigns displayRefit unconditionally, so a
    # `tart set` that omits the flag silently clears it.
    _tart set "$v" --cpu "$(_vm_cpus)" --memory "$(_vm_mem_mb)" --random-mac --random-serial \
        --display "$WK_VM_DISPLAY" --display-refit
    debug "clone took $(( $(date +%s) - t0 ))s"

    # A clone that took minutes fell back to a real copy, not clonefile(2).
    [ $(( $(date +%s) - t0 )) -gt 60 ] && \
        warn "the clone took $(( $(date +%s) - t0 ))s -- APFS copy-on-write may not be in play; check disk use"

    ensure_dir "$(wk_ws_dir "$name")"

    # Last: a kill before here leaves a guest read as `creating`, which
    # `wk new` remakes from scratch.
    : > "$(wk_ws_dir "$name")/$WK_READY_MARKER"
}

t_start() {
    local name="$1" ip
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" != absent ] || die "no such workspace: $name"
    if [ "$(_vm_state "$v")" = running ]; then
        ip=$(_ip "$name")
        _write_marker "$name" "$ip" || debug "could not write the workspace marker in $name"
        _write_lldbinit "$name" "$ip" || debug "could not write .lldbinit in $name"
        _set_guest_proxy "$name" "$ip" || warn "could not set the guest's system proxy; the browser in $name will reach nothing"
        _write_claude_config "$name" "$ip" || warn "could not link ~/.claude in $name; an agent in there would have no instructions"
        echo "$ip"
        return 0
    fi

    _check_guest_limit
    _check_memory_budget "$name" "$(t_mem_mb "$name")"
    _check_host_disk

    # Best-effort marker below: a guest that came up is up, and refusing to
    # report that because one ssh failed is the wrong trade.
    ip=$(_boot "$v" 180)
    _write_marker "$name" "$ip" || debug "could not write the workspace marker in $name"
    _write_lldbinit "$name" "$ip" || debug "could not write .lldbinit in $name"
    _set_guest_proxy "$name" "$ip" || warn "could not set the guest's system proxy; the browser in $name will reach nothing"
    _write_claude_config "$name" "$ip" || warn "could not link ~/.claude in $name; an agent in there would have no instructions"
    echo "$ip"
}

# The tart flags that confine a guest's egress: --net-softnet-block=0.0.0.0/0
# is default-deny, and longest-prefix-match makes the single
# --net-softnet-allow "nothing except the proxy".
_softnet_flags() {
    if [ -n "${WK_VM_UNFILTERED:-}" ]; then
        warn "WK_VM_UNFILTERED=1 -- this guest gets the open network, with no egress filter"
        return 0
    fi
    # Fail closed: Softnet applies at `tart run` and cannot be added later,
    # so a guest booted without it has the open network for its whole life.
    [ -x "$WK_SOFTNET_BIN" ] || die "softnet is not installed, so this guest's egress would not be filtered.
    Install it:  ./setup --stage softnet   (needs a terminal for sudo)
    Or set WK_VM_UNFILTERED=1 to boot with the open network anyway."
    printf '%s\n' --net-softnet \
        "--net-softnet-block=0.0.0.0/0" \
        "--net-softnet-allow=$(_proxy_addr)/32"
}

# wk-proxy.py, the same one containers use, listening on TCP since a guest
# cannot see a unix socket. Started on demand, since it binds an address
# that exists only while a VM is running.
_proxy_pidfile() { echo "$WK_VM_DIR/proxy.pid"; }

# Ask the socket, not the pidfile: a proxy routinely outlives the shell
# that recorded it, so the file alone risks a false "no egress".
_proxy_running() {
    local pf; pf=$(_proxy_pidfile)
    [ -f "$pf" ] && kill -0 "$(cat "$pf" 2>/dev/null)" 2>/dev/null && return 0
    lsof -nP -iTCP@"$(_proxy_addr)":"$WK_VM_PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1 || return 1
    debug "a proxy is listening on $(_proxy_addr):$WK_VM_PROXY_PORT that this pidfile does not name"
}

_start_host_proxy() {
    [ -n "${WK_VM_UNFILTERED:-}" ] && return 0
    _proxy_running && { debug "host proxy already running"; return 0; }

    ensure_dir "$WK_VM_DIR"
    local log="$WK_VM_DIR/proxy.log" addr i=0

    # A proxy started before the address exists dies with EADDRNOTAVAIL.
    addr=$(_proxy_addr)
    while [ "$i" -lt 30 ]; do
        ifconfig 2>/dev/null | grep -q "inet $addr " && break
        sleep 0.5; i=$((i + 1))
    done
    if ! ifconfig 2>/dev/null | grep -q "inet $addr "; then
        warn "the guest bridge never got address $addr; not starting the proxy"
        return 1
    fi

    # WK_PROXY_UNIX=0: that socket is for containers, in the podman VM.
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

# Shared by t_start and base provisioning, to avoid two copies of the same
# start-and-wait-for-ssh logic.
_boot() {
    local v="$1" wait="${2:-180}" ip runlog

    ensure_dir "$WK_VM_DIR"
    runlog="$WK_VM_DIR/${v#wk-}.run.log"

    if [ "$(_vm_state "$v")" != running ]; then
        # Computed *before* the tart command line: inline, a die in
        # _softnet_flags would kill only the subshell and tart would run
        # anyway, unfiltered and silent.
        local sflags
        sflags=$(_softnet_flags)

        # Recorded so `wk claude` can tell how this guest was *booted*.
        if [ -z "$sflags" ]; then
            : > "$WK_VM_DIR/${v#wk-}.unfiltered"
        else
            rm -f "$WK_VM_DIR/${v#wk-}.unfiltered"
        fi

        # nohup, not a bare `&`, or the VM dies with the terminal --
        # indistinguishable from crashing on boot. Windowed, deliberately: a
        # macOS guest is the one workspace kind with a real GPU (README.md),
        # and interacting with MiniBrowser is the point of this target.
        # shellcheck disable=SC2086 -- deliberate word splitting of the flags.
        nohup "$(_tart_bin)" run $sflags "$v" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting $v (log: $runlog)"
    fi

    # Default dhcp resolver works behind Softnet; the arp resolver does not.
    ip=$(_tart ip "$v" --wait "$wait" 2>/dev/null | grep .) \
        || die "$v did not come up within ${wait}s; see $runlog"

    # Only now can the proxy bind: the guest bridge exists only once running.
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
    # Two layers of quoting: ssh joins its arguments with spaces, so the
    # command must be one already-quoted string, in a *login* shell for PATH.
    local cmd; cmd=$(sh_quote "$@")
    _ssh "$ip" "bash -lc $(sh_quote "$cmd")"
}

# scp rather than `t_exec cat`: a command whose stdout goes through a login
# shell is not a byte pipe.
t_pull() {
    local name="$1" src="$2" dest="$3"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$src" "$dest"
}

# rsync, since a build tree is tens of thousands of files.
t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    _t_pull_dir_excludes "$@"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    mkdir -p "$dest"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} -e "ssh $(_ssh_opts)" \
        "$WK_VM_USER@$ip:$src/" "$dest/"
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

# The marker that tells the guest's own wk that it *is* a workspace, and
# which one -- without it, `wk build` in there tries to reach a podman
# machine a macOS guest can never host (targets/local.sh). Never written into
# the golden base, since the base is not a workspace. config= is what a bare
# `wk run`/`wk test` reaches for: a macOS guest builds only the Apple ports.
_write_marker() {
    local name="$1" ip="$2"
    _ssh "$ip" "printf '%s\n' \
        '# wk: this machine IS a workspace. Written by targets/vm.sh.' \
        $(sh_quote "name=$name") $(sh_quote "src=$(t_src "$name")") \
        $(sh_quote "config=${WK_VM_BASE_PREBUILD:-mac-release}") \
        > \$HOME/.wk-workspace"
}

# The agent's own configuration. Provisioning links these
# (vm/provision-base.sh); without it `wk claude` starts an agent that was
# never told it is in a workspace. Symlinks rather than copies: `wk build`
# re-rsyncs $HOME/wk-tools with --delete on every run, so a copy goes stale.
_write_claude_config() {
    local name="$1" ip="$2" tools; tools=$(t_tools "$name")
    _ssh "$ip" "[ -d $(sh_quote "$tools/claude") ] || exit 1
        mkdir -p \$HOME/.claude
        for f in settings.json hooks CLAUDE.md skills; do
            ln -sfn $(sh_quote "$tools/claude")/\$f \$HOME/.claude/\$f
        done"
}

# The guest's *system* proxy, not the environment variables provisioning
# writes into ~/.zprofile: WebKit's network process does not read
# http_proxy/https_proxy, so MiniBrowser loading https://webkit.org/ gives a
# blank window while curl to the same host goes straight through. Set from
# the host at start since the address (_proxy_addr) can change. Best effort,
# like the marker: it warns rather than errors, since the failure is
# otherwise silent.
_set_guest_proxy() {
    local name="$1" ip="$2" addr=""
    [ -n "${WK_VM_UNFILTERED:-}" ] || addr=$(_proxy_addr)
    debug "guest system proxy in $name: ${addr:-off}"

    _ssh "$ip" "bash -s" <<EOF
set -u
addr='$addr'
port='$WK_VM_PROXY_PORT'

# The service to configure is the one carrying the default route, not a name
# hardcoded here: the Cirrus Labs image ships several, and which one is real
# is a property of the guest, not of this repo.
dev=\$(route -n get default 2>/dev/null | awk '/interface:/{print \$2}')
[ -n "\$dev" ] || { echo "no default route in the guest" >&2; exit 1; }
svc=\$(networksetup -listnetworkserviceorder | awk -v d="\$dev" '
    /^\([0-9]+\)/ { name = substr(\$0, index(\$0, ") ") + 2) }
    index(\$0, "Device: " d ")") { print name; exit }')
[ -n "\$svc" ] || { echo "no network service owns \$dev" >&2; exit 1; }

read_state() {
    networksetup -getsecurewebproxy "\$svc" | awk '
        /^Enabled:/ { e = \$2 } /^Server:/ { s = \$2 } /^Port:/ { p = \$2 }
        END { print e ":" s ":" p }'
}

if [ -z "\$addr" ]; then
    [ "\$(read_state)" = "No::0" ] && exit 0
    sudo -n networksetup -setwebproxystate "\$svc" off &&
    sudo -n networksetup -setsecurewebproxystate "\$svc" off
    exit
fi

# Idempotent by measurement rather than by a marker file: three networksetup
# writes on every boot would be slow and would log three times over.
[ "\$(read_state)" = "Yes:\$addr:\$port" ] && exit 0
sudo -n networksetup -setwebproxy "\$svc" "\$addr" "\$port" &&
sudo -n networksetup -setsecurewebproxy "\$svc" "\$addr" "\$port" &&
sudo -n networksetup -setproxybypassdomains "\$svc" localhost 127.0.0.1
EOF
}

# WebKit's lldb helpers, wired up the same way container/firstrun.sh wires
# them up -- summaries for WTF::String, JSValue and the rest, without which
# a backtrace through JSC is a wall of hex. container/lldb/rr.py is left out
# because rr is Linux-only.
_write_lldbinit() {
    local name="$1" ip="$2"
    {
        printf '%s\n' "# wk: written by targets/vm.sh. See wk run --lldb, wk gui --lldb."
        printf '%s\n' "command script import $(t_src "$name")/Tools/lldb/lldb_webkit.py"
        cat "$WK_ROOT/dotfiles/lldbinit"
    } | _ssh "$ip" "cat > \$HOME/.lldbinit"
}

# rsync rather than a mount: no --dir is ever passed to `tart run`, since a
# shared directory is the host-filesystem hole this target exists not to
# have. Shared with _prebuild_base and _provision_base, into a *running*
# golden base.
_push_tools() {
    local name="$1" ip="$2"
    rsync -az --delete --exclude '.git/' \
        -e "ssh $(_ssh_opts)" \
        "$WK_ROOT/" "$WK_VM_USER@$ip:$(t_tools "$name")/"
}

t_sync_tools() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    debug "syncing wk-tools -> $name"
    _push_tools "$name" "$ip"
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

    # The guest's disk goes with `tart delete`, but the host-side status
    # files, build logs and boot log do not.
    local ws; ws=$(wk_ws_dir "$name")
    [ -d "$ws" ] && { rm -rf "$ws"; info "removed $ws"; }
    rm -f "$WK_VM_DIR/$name.run.log"
    # The unfiltered marker too, or it would falsely refuse the next guest
    # of the same name (`wk claude` refuses on it, cmd/claude).
    rm -f "$WK_VM_DIR/$name.unfiltered"
}

# Build the base once, so every workspace starts warm: `tart clone` is
# copy-on-write, so the build tree and compilation cache in the base cost
# each workspace nothing to inherit, and would die with `wk rm` if kept
# in a workspace instead.
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

    _push_tools "$WK_VM_BASE" "$ip"

    config_build_env "$(t_src "$WK_VM_BASE")" "$jobs" 10
    local cmd
    cmd="env $(sh_quote "${CFG_ENV[@]}") $(sh_quote "$(t_tools "$WK_VM_BASE")/build/build-in-target.sh")"

    # Detached, and polled -- NOT a foreground `ssh <long command>`: this
    # build takes over an hour, and any blip on a foreground connection kills
    # it, leaving a base that looks built but is not. detach_remote/
    # detach_wait_remote (lib/detach.sh) own the nohup spelling and poll loop.
    command -v detach_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    local rlog="/tmp/wk-base-build.log" rrc="/tmp/wk-base-build.rc"
    detach_remote _prebuild_ssh "$rlog" "$rrc" -- bash -lc "$cmd" \
        || die "could not start the base prebuild"

    local t0; t0=$(date +%s)
    local rc; rc=$(detach_wait_remote _prebuild_ssh "$rlog" "$rrc")
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

command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_ssh_opts() {
    # ServerAliveInterval matters more than it looks: provisioning and builds
    # go quiet for long stretches, and without keepalives a NAT timeout drops
    # the connection mid-build and reports it as a build failure. A guest's
    # key is minted fresh on every clone at the same address, which is what
    # _unpinned_host_key_opts (lib/reach.sh) is for.
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") $(_unpinned_host_key_opts) \
-o ServerAliveInterval=60 -o ServerAliveCountMax=10 -i $WK_VM_KEY"
}

_ssh() {
    local ip="$1"; shift
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_VM_USER@$ip" "$@"
}

# The ssh-fn detach_remote/detach_wait_remote (lib/detach.sh) want. `ip` is
# `_prebuild_base`'s own local, in scope here through bash's dynamic scoping
# since this is only ever called while that frame is on the stack.
_prebuild_ssh() { _ssh "$ip" "$@"; }

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

# In GB: the guest disks are sparse, so what matters is what the host can
# still back, not what the guest thinks it has.
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
# The mac VM competes with the podman VM for the same physical memory, so
# this is checked rather than hoped for.

_podman_mem_mb() {
    have podman || { echo 0; return; }
    podman machine inspect "${WK_MACHINE:-wk}" --format '{{.Resources.Memory}}' 2>/dev/null || echo 0
}

_podman_running() {
    have podman || return 1
    [ "$(podman machine inspect "${WK_MACHINE:-wk}" --format '{{.State}}' 2>/dev/null)" = running ]
}

_vm_cpus()   { echo "${WK_VM_CPUS:-$(envelope_cores)}"; }
_vm_mem_mb() { echo "${WK_VM_MEM_MB:-$(envelope_mem_mb)}"; }

# What an *existing* VM was created with: it keeps that allocation for
# life, so sizing a build from the default envelope instead risks an OOM.
_vm_get() {
    _tart get "$1" --format json 2>/dev/null | python3 -c '
import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print("" if v is None else v)' "$2"
}

_vm_configured() { _vm_get "$(_vm "$1")" "$2"; }

# Memory already promised to running guests, optionally excluding one by
# name so a caller starting or re-using that guest does not count it as
# competition with itself.
_committed_mem_mb() {
    local skip="${1:-}" total=0 v m
    for v in $(_vm_query running); do
        [ -n "$skip" ] && [ "$v" = "$skip" ] && continue
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

_check_memory_budget() {
    local name="$1" mine="$2" podman_mb guests total budget spare
    podman_mb=0
    _podman_running && podman_mb=$(_podman_mem_mb)
    guests=$(_committed_mem_mb "$name")
    total=$(( mine + podman_mb + guests ))
    budget=$(envelope_mem_mb)

    [ "$total" -le "$budget" ] && return 0

    if [ -n "${WK_VM_SHARE:-}" ]; then
        warn "'$name' (${mine}MB) on top of ${podman_mb}MB podman + ${guests}MB of running guests exceeds the ${budget}MB envelope -- continuing because WK_VM_SHARE is set"
        return 0
    fi

    # A useful refusal names a number instead of just restating the problem.
    spare=$(( budget - podman_mb - guests ))

    # Advice, not a command line: this is reached for the golden base too,
    # and "wk vm start wk-base" is not a thing anyone can run.
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
# Built once, never rebuilt: everything expensive is paid inside it exactly
# once and inherited by every clone.

_base_exists() { [ "$(_vm_state "$WK_VM_BASE")" != absent ]; }

# Existing is not the same as finished: a base that failed provisioning
# partway would otherwise be inherited by every clone. Same completion
# protocol as every other artifact here: a marker written last (README.md).
_base_marker() { echo "$WK_VM_DIR/base.ready"; }

_base_ready() { _base_exists && [ -f "$(_base_marker)" ]; }

_base_mark_ready() {
    ensure_dir "$WK_VM_DIR" 0700 >/dev/null
    printf 'image=%s
prebuild=%s
finished=%s
' \
        "$WK_VM_IMAGE" "${WK_VM_BASE_PREBUILD:-none}" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$(_base_marker)"
}

_ensure_base() {
    _base_ready && return 0

    if _base_exists; then
        warn "'$WK_VM_BASE' exists but was never finished (no completion marker)"
        log  "  destroying it and starting again -- an unprovisioned base is rubble,"
        log  "  and every workspace cloned from one inherits whatever it is missing."
        _tart delete "$WK_VM_BASE" 2>/dev/null || true
    fi
    rm -f "$(_base_marker)"

    if ! _tart list --format json --source oci 2>/dev/null | python3 -c '
import json, sys
sys.exit(0 if any(v.get("Name") == sys.argv[1] for v in json.load(sys.stdin)) else 1)' "$WK_VM_IMAGE"; then
        info "pulling $WK_VM_IMAGE -- tens of GB, once only"
        _tart pull "$WK_VM_IMAGE"
    fi

    info "creating the golden base VM '$WK_VM_BASE'"
    _tart clone "$WK_VM_IMAGE" "$WK_VM_BASE"
    _tart set "$WK_VM_BASE" --cpu "$(_base_cpus)" --memory "$(_base_mem_mb)"

    _provision_base
}

# Runs against the base VM only; every workspace inherits the result.
_provision_base() {
    _check_guest_limit
    _check_memory_budget "$WK_VM_BASE" "$(_base_mem_mb)"
    ensure_dir "$WK_VM_DIR" 0700

    # tart can only grow a disk while the VM is off; `wk vm base --refresh`
    # is how an undersized base gets fixed.
    local cur; cur=$(_vm_get "$WK_VM_BASE" Disk)
    if [ -n "$cur" ] && [ "$cur" -lt "$WK_VM_DISK_GB" ]; then
        info "growing the base disk ${cur}GB -> ${WK_VM_DISK_GB}GB"
        _tart set "$WK_VM_BASE" --disk-size "$WK_VM_DISK_GB"
    fi
    # Also while off: the base ends provisioning with a full build, so it
    # needs the same envelope a workspace gets, not a token allocation.
    _tart set "$WK_VM_BASE" --cpu "$(_base_cpus)" --memory "$(_base_mem_mb)"
    _check_host_disk

    if [ ! -f "$WK_VM_KEY" ]; then
        ssh-keygen -q -t ed25519 -N '' -C wk-vm -f "$WK_VM_KEY"
        changed "generated the macOS VM ssh key"
    fi

    # A first boot has Setup Assistant work to get through, hence the longer
    # wait than t_start uses. ssh is not reachable until the key goes in below.
    local runlog="$WK_VM_DIR/base.run.log"
    local ip
    if [ "$(_vm_state "$WK_VM_BASE")" != running ]; then
        nohup "$(_tart_bin)" run --no-graphics "$WK_VM_BASE" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting the base VM for provisioning (log: $runlog)"
    fi
    ip=$(_tart ip "$WK_VM_BASE" --wait 300 2>/dev/null | grep .) \
        || die "base VM did not boot; see $runlog"

    # The guest agent is how the key gets in the FIRST time, without typing
    # the default password. Later, ssh is the better question to ask: `tart
    # ip --wait` answers before the agent is listening.
    if _wait_ssh "$ip"; then
        debug "ssh already works in '$WK_VM_BASE'; no key to install"
    else
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
    fi

    # Seed the checkout from a clone on the host, if there is one: `git
    # clone --local` hardlinks rather than copies, so it costs almost
    # nothing. Best-effort: any failure falls back to cloning from GitHub.
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
    # The whole tree: provision-base.sh links ~/.claude out of it too.
    _push_tools "$WK_VM_BASE" "$ip"
    # _proxy_addr, not $WK_VM_PROXY_ADDR: the variable is normally *unset*, so
    # passing it raw sends provision-base.sh to Tart's hardcoded default of
    # 192.168.64.1, where nothing listens.
    _ssh "$ip" "env WK_VM_PROXY_ADDR=$(sh_quote "$(_proxy_addr)") WK_VM_PROXY_PORT=$(sh_quote "$WK_VM_PROXY_PORT") WK_VM_DISPLAY=$(sh_quote "$WK_VM_DISPLAY") bash $(t_tools "$WK_VM_BASE")/vm/provision-base.sh" \
        || die "base provisioning failed"

    _prebuild_base "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    # Last: until this exists the guest is rubble that the next run destroys.
    _base_mark_ready
    changed "golden base VM '$WK_VM_BASE' is ready"
}
