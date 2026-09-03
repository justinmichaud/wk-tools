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
#   WK_VM_PASSWORD             that account's password *after* provisioning
#   WK_VM_IMAGE_PASSWORD       the one the pulled image ships with, used once
#                              to change it
#   WK_VM_CPUS/WK_VM_MEM_MB    a workspace guest's allocation (default: the envelope)
#   WK_VM_BASE_CPUS/_MEM_MB    the golden base's allocation (default: the same envelope)
#   WK_VM_BASE_PREBUILD       config pre-built into the base; empty disables it
#   WK_VM_DISK_GB              the sparse disk ceiling every guest is cloned with
#   WK_VM_DISPLAY               the guest's minimum window content size
#   WK_VM_CLOCK_SKEW            seconds of clock error tolerated before a start
#                               resets the guest's clock from this host
#   WK_VM_SHELLS_WARN           shells left in a guest before `wk vm check`
#                               calls it an accumulation
#   WK_VM_MEM_FREE_WARN_PCT     memory left in a guest, as macOS's own free
#                               percentage, before `wk vm check` calls it low
#   WK_VM_SWAP_WARN_MB          swap used in a guest before it is reported
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

# The guest's login, and it is deliberately trivial. Nothing trusts this
# account: the guest holds no credential of yours (the push keys stay in the
# store, `wk ai claude` runs against a checkout and nothing else), it is
# Softnet-filtered down to one reachable address, and `wk rm` destroys it. What
# the password actually costs is typing: it is asked for at the guest's own
# window -- a screen unlock, an Xcode prompt, an installer -- while everything
# wk does arrives over ssh with a key.
#
# Two names because they are two different facts: the image ships one
# (admin/admin, Cirrus Labs) and provisioning changes it to the other, once.
WK_VM_PASSWORD="${WK_VM_PASSWORD:-1}"
WK_VM_IMAGE_PASSWORD="${WK_VM_IMAGE_PASSWORD:-admin}"

# Said wherever a person is about to look at a guest's window -- provisioning
# it, starting it, entering it, opening an editor on it. One function, so the
# four places cannot disagree with each other or with what provisioning set.
vm_login_note() {
    log "  the guest's own window logs in as $WK_VM_USER / $WK_VM_PASSWORD"
    log "  (wk itself uses an ssh key; this is for a prompt on the screen)"
    log "  wk vm check <name>   what is in front of that window, whether that"
    log "                       password is still what the base gave it, and what"
    log "                       is piling up in there"
}

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
# WK_VM_BASE_CPUS / WK_VM_BASE_MEM_MB: what the golden base is provisioned
# with; default the whole envelope, since the base builds WebKit once.
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

# How far the guest's clock may be out before a start resets it (_set_guest_clock).
# Not zero: the reading is taken over ssh, so a sub-second round trip is built
# into every comparison and disciplining to it would rewrite the clock on every
# start for no gain. What this is guarding against is days, not seconds.
WK_VM_CLOCK_SKEW="${WK_VM_CLOCK_SKEW:-30}"

# What `wk vm check` calls an accumulation, and what it calls low. A guest is
# long-lived and holds its whole allocation whether or not it is busy
# (_check_memory_budget), so what a build in there eventually runs out of is
# whatever else stayed resident. Thresholds rather than a bare listing: a
# report that never says "this is too many" leaves the reading to be judged by
# whoever is already stuck.
#
# Twelve shells: an editor with a few terminal panes, an agent and two ssh
# sessions all at once, measured as nothing wrong. macOS itself acts on the
# free percentage memory_pressure reports, and below 15% it is paging.
WK_VM_SHELLS_WARN="${WK_VM_SHELLS_WARN:-12}"
WK_VM_MEM_FREE_WARN_PCT="${WK_VM_MEM_FREE_WARN_PCT:-15}"
WK_VM_SWAP_WARN_MB="${WK_VM_SWAP_WARN_MB:-1024}"

# Guest display size, in points (tart defaults the unit to "pt"). Deliberately
# SMALLER than the host desktop: Tart pins the window's *minimum* content
# size to this resolution, and AppKit drops fullScreenPrimary once minSize
# exceeds the screen. --display-refit then tracks the *window* on every
# resize, so full screen gives a real Retina desktop.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"

# Host space to remain for a sparse guest disk to grow into, or a build
# fails as an I/O error inside the guest, naming nothing real: warn under
# WK_HOST_FREE_WARN_GB, refuse under WK_HOST_FREE_MIN_GB.
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

t_os() { echo macos; }

t_create() {
    local name="$1" why
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" = absent ] || die "workspace '$name' already exists"

    _ensure_base

    # A warning and not a refusal: a clone of a base built from older inputs is
    # still a usable workspace, and the rebuild costs hours -- so the choice is
    # the person's, made with the cost and the consequence in front of them.
    # Everything the base carries and this tree has since changed is inherited
    # by this clone, the account's password included (vm/provision-base.sh);
    # t_start converges what it can, and a password is not one of them.
    if why=$(vm_base_stale); then
        warn "'$WK_VM_BASE' predates its own provisioning inputs: $why.
  '$name' is a clone of it, so it carries what that base was built with -- the
  image's password if the change came later, and the desktop settings of the day
  it was sealed. 'wk vm check $name' says which of them are still wrong.
      wk vm base --rebuild     hours; existing guests are unaffected"
    fi

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

# Everything a start puts into a guest, in one place. t_start has two arms --
# a guest that is already running, and one this start booted -- and a step
# delivered on only one of them is half a delivery, so both call this.
#
# Best-effort line by line: a guest that came up is up, and refusing to report
# that because one ssh failed is the wrong trade.
_converge_guest() { # <name> <ip>
    local name="$1" ip="$2"
    _push_tools "$name" "$ip" || warn "wk-tools in $name is not this tree's commit; 'wk sync --tools' puts it there once it is committed"
    _write_marker "$name" "$ip" || debug "could not write the workspace marker in $name"
    _write_shell_rc "$name" "$ip" || warn "could not wire $name's shell; 'wk' will not be on PATH in there"
    _write_lldbinit "$name" "$ip" || debug "could not write .lldbinit in $name"
    _set_guest_clock "$name" "$ip" || warn "could not set $name's clock; TLS in there will fail as CERT_NOT_YET_VALID"
    _set_guest_egress "$name" "$ip" || warn "could not set $name's egress; nothing in there will reach the outside"
    _write_claude_config "$name" "$ip" || warn "could not link ~/.claude in $name; an agent in there would have no instructions"
    _write_agent_secrets "$name" "$ip" || warn "could not write the agent credentials into $name; an agent in there will ask you to log in"
    _write_deploy_keys "$name" "$ip" || warn "could not write $name's ssh config and deploy keys; a push from in there is refused ('wk push status')"
    _settle_desktop "$name" "$ip" || warn "could not settle $name's desktop; 'wk vm check $name' says what is in front of the window"
    # After the settle, or it reports the state the settle was fixing.
    _report_desktop "$name"
}

t_start() {
    local name="$1" ip
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" != absent ] || die "no such workspace: $name"
    if [ "$(_vm_state "$v")" = running ]; then
        ip=$(_ip "$name")
        # The proxy is the guest's only route out and it outlives no crash of
        # its own: a start against a running guest converges it too, or a
        # guest whose proxy died has no egress and no way back short of a
        # reboot. Idempotent -- _start_host_proxy asks the socket first.
        _start_host_proxy || true
        _converge_guest "$name" "$ip"
        echo "$ip"
        return 0
    fi

    _check_guest_limit
    _check_memory_budget "$name" "$(t_mem_mb "$name")"
    _check_host_disk

    ip=$(_boot "$v" 180)
    _converge_guest "$name" "$ip"
    echo "$ip"
}

# On every start, not only in the golden base: `defaults -currentHost` writes
# per hardware UUID and `tart clone` gives the clone a new one, so a screen
# saver the base turned off is armed again in every guest cloned from it
# (measured with `wk vm check`). Setup Assistant's panes come back per clone
# for the same reason -- macOS counts a clone as a new install.
#
# vm/desktop.sh over stdin, the same file vm/provision-base.sh runs: one
# implementation, and it needs no wk-tools inside the guest to have been synced.
# The password goes down the stream as the script's own first line, never in
# the ssh command: an argument is in `ps` on both machines, and this one
# unlocks the guest's screen.
_settle_desktop() { # <name> <ip>
    {
        printf 'WK_VM_PASSWORD=%s\n' "$(sh_quote "$WK_VM_PASSWORD")"
        cat "$WK_ROOT/vm/desktop.sh"
    } | _ssh "$2" "bash -s" >/dev/null 2>&1
}

# The settle above changes things and says nothing; this is the measurement of
# what it left, printed where the person will see it. `wk vm start` is the
# moment for it -- they are about to look at that window, exactly like
# vm_login_note -- and it is the same probe, the same findings and the same
# renderer `wk vm check` uses, so a start and a check cannot disagree.
#
# On stderr, since t_start's stdout is the guest's address, and best-effort:
# a guest that came up is up, and a probe that did not answer is not a reason
# to fail the start.
_report_desktop() { # <name>
    local probe
    probe=$(vm_desktop_probe "$1" 2>/dev/null) || return 0
    [ -n "$probe" ] || return 0
    log "  the guest's desktop, as it is now ('wk vm check $1' asks again):"
    vm_render_findings <<FINDINGS || true
$(vm_desktop_findings "$probe")
FINDINGS
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

        # Recorded so `wk ai claude` can tell how this guest was *booted*.
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

# The same scp and the same rsync, the other way: one file in, and a tree in
# one transfer. `t_exec <ws> "cat > file"` is no more a byte pipe inbound
# than outbound -- the login shell is in the way either way.
t_push() {
    local name="$1" src="$2" dest="$3"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$src" "$WK_VM_USER@$ip:$dest"
}

t_push_dir() {
    local name="$1" src="$2" dest="$3"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete -e "ssh $(_ssh_opts)" "$src/" "$WK_VM_USER@$ip:$dest/"
}

# One `test` over this driver's own ssh. Stdin from /dev/null: this runs
# inside a command substitution that inherits it, and ssh would drink it.
t_path_kind() {
    local name="$1" p="$2"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    _ssh "$ip" "if [ -d $(sh_quote "$p") ]; then echo dir
         elif [ -e $(sh_quote "$p") ]; then echo file
         else echo absent; fi" 2>/dev/null < /dev/null | tr -d '\r'
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
# (vm/provision-base.sh); without it `wk ai claude` starts an agent that was
# never told it is in a workspace. Symlinks rather than copies: every start
# resets $HOME/wk-tools to this tree's commit (_push_tools), so a copy goes stale.
_write_claude_config() {
    local name="$1" ip="$2" tools; tools=$(t_tools "$name")
    _ssh "$ip" "[ -d $(sh_quote "$tools/claude") ] || exit 1
        mkdir -p \$HOME/.claude
        for f in settings.json hooks CLAUDE.md skills; do
            ln -sfn $(sh_quote "$tools/claude")/\$f \$HOME/.claude/\$f
        done"
}

# The agents' credentials, written from this host whenever the workspace comes
# up. A guest cannot reach the store -- it is inside the podman VM, and the
# guest's whole filesystem boundary is that it mounts nothing of ours -- so a
# copy is the only way, unlike a container, which symlinks the live mount.
#
# One row per named secret (wk_agent_secrets, lib/store.sh), the same table
# container/firstrun.sh links and cmd/remote copies to a build box: a name
# added there reaches a guest with nothing to change here.
#
# Rewritten every start rather than provisioned once, so `wk key set <name>`
# rotating one converges here too, and each is removed when the store has
# none: a guest holding a credential the host has withdrawn is the one state
# this must not leave behind, so the removal is unconditional rather than an
# else. 0600, on stdin -- an argument is in `ps` inside the guest -- and never
# echoed.
_write_agent_secrets() { # <name> <ip>
    local name="$1" ip="$2" sname sfile shome svar val n=0
    while read -r sname sfile shome svar; do
        [ -n "$sname" ] || continue
        val=$(wk_agent_secret "$sname")
        if [ -z "$val" ]; then
            # </dev/null: this loop's stdin is the table below, and _ssh is a
            # plain ssh, which would drink it.
            _ssh "$ip" "rm -f \$HOME/$(sh_quote "$shome")" </dev/null || return 1
            continue
        fi
        printf '%s\n' "$val" \
            | _ssh "$ip" "umask 077 && cat > \$HOME/$(sh_quote "$shome")" || return 1
        n=$((n + 1))
    done <<EOF
$(wk_agent_secrets)
EOF
    debug "agent credentials in $name: $n"
}

# The ProxyCommand a guest's ssh needs to reach github.com at all. Softnet
# allows exactly one address -- the host's own on the guest bridge, where
# wk-proxy listens -- so a direct TCP connection to port 22 is dropped, and
# the proxy is an HTTP CONNECT one. github.com:22 is in its allowlist
# (container/proxy/wk-proxy.py); nothing else here is.
#
# macOS's own nc speaks CONNECT with `-X connect`, and there is no other nc in
# a Cirrus Labs image. Absolute path: ssh runs this through /bin/sh with the
# guest's own environment, not the login PATH.
_ssh_proxy_command() {
    printf '/usr/bin/nc -X connect -x %s:%s %%h %%p' "$(_proxy_addr)" "$WK_VM_PROXY_PORT"
}

# The deploy keys, and the ssh config that picks one per fork -- written from
# this host whenever the workspace comes up, the same arrangement and for the
# same reason as the agent token above: a guest mounts nothing of ours, so a
# copy is the only way.
#
# Written every start rather than provisioned once, so `wk push on` reaches a
# running guest, and *removed* whenever the store has no key -- push is off,
# none was ever registered, or the store cannot be read from here at all. A
# guest holding a key the host has withdrawn is the one state this must not
# leave behind, so the removal is unconditional rather than an else.
#
# The config goes in whether or not there is a key behind it: an IdentityFile
# pointing at nothing is the off position and says so as `no such identity`,
# exactly like a container's dangling symlink into /secrets. So the two halves
# never have to agree about anything.
_write_deploy_keys() { # <name> <ip>
    local name="$1" ip="$2" remote repo alias key idf n=0 total=0

    if ! wk_push_store_local && ! _podman_running; then
        warn "the podman machine holds the deploy keys and is not running, so '$name'
  is given none. 'wk start' then 'wk vm start $name' to hand them over."
    fi

    _ssh "$ip" "umask 077 && mkdir -p \$HOME/.ssh && cat > \$HOME/.ssh/config" <<EOF || return 1
# wk: written by targets/vm.sh on every start. Whether the keys these name are
# here at all is 'wk push'.
$(wk_ssh_alias_blocks '~/.ssh' id_ "$(_ssh_proxy_command)")
EOF

    while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        total=$((total + 1))
        key=$(wk_push_key "$remote")
        # sh_quote, like every other command string this file sends: the fork
        # name lands in a shell over there, and $HOME is expanded there too.
        idf="\$HOME/.ssh/id_$(sh_quote "$remote")"
        if [ -n "$key" ]; then
            # chmod as well as umask: umask decides the mode of a file the
            # write *creates*, and an id_ file already there at 0644 keeps it.
            printf '%s\n' "$key" \
                | _ssh "$ip" "umask 077 && cat > $idf && chmod 600 $idf" || return 1
            n=$((n + 1))
        else
            # </dev/null: ssh drinks stdin, and stdin here is the fork list.
            _ssh "$ip" "rm -f $idf" </dev/null || return 1
        fi
    done <<EOF
$(wk_push_forks)
EOF
    debug "deploy keys in $name: $n of $total"
}

# `wk push on|off` after the keys have moved in the store: a guest holds a
# copy, so the switch has to be thrown in the guest too, and this host is the
# machine that drives its guests (cmd/push).
#
# Only the running ones. Booting a guest to take a key out of it is a side
# effect nobody asked this command for, and a stopped guest is converged by
# t_start before anything in it can run.
vm_push_keys_converge() {
    local g ip rc=0
    for g in $(target_workspaces); do
        [ "$(t_info "$g" 2>/dev/null)" = running ] || continue
        if ! ip=$(_ip "$g"); then
            printf '  %-24s running, no address yet -- not converged\n' "$g" >&2
            rc=1; continue
        fi
        if _write_deploy_keys "$g" "$ip" >/dev/null; then
            printf '  %-24s %s\n' "$g" "the guest's copy now matches the store" >&2
        else
            printf '  %-24s FAILED -- it may still hold a key\n' "$g" >&2
            rc=1
        fi
    done
    return "$rc"
}

# What the guests actually hold, for `wk push status`: one tab-separated
# `<guest> <state> <forks with a key>` row each, asked of the guest rather
# than of a record. Read-only -- nothing is started, and a stopped guest is
# reported as unread rather than guessed at.
vm_push_keys_state() {
    local g ip state forks held r
    for g in $(target_workspaces); do
        state=$(t_info "$g" 2>/dev/null) || state=unknown
        if [ "$state" != running ] || ! ip=$(_ip "$g"); then
            printf '%s\t%s\t\n' "$g" "${state:-unknown}"
            continue
        fi
        # What is in there, intersected with the forks this switch knows: an
        # account's own id_ed25519 is a private key, but it is not a fork's
        # deploy key and naming it here would report a push wk cannot make.
        held=$(_ssh "$ip" 'ls "$HOME"/.ssh/id_* 2>/dev/null' \
                   </dev/null 2>/dev/null | tr -d '\r' | sed 's|.*/id_||' | tr '\n' ' ')
        forks=""
        for r in $(wk_push_forks | awk 'NF {print $1}'); do
            case " $held " in *" $r "*) forks="$forks$r " ;; esac
        done
        printf '%s\t%s\t%s\n' "$g" "running" "${forks% }"
    done
}

# Everything in the guest that names the egress proxy, written from this host
# on every start -- the address is the host's own on the guest bridge
# (_proxy_addr) and can change, so nothing may bake it into an image. Two
# halves, and a guest needs both:
#
#   the system proxy   WebKit's network process does not read
#                      http_proxy/https_proxy, so MiniBrowser loading
#                      https://webkit.org/ gives a blank window while curl to
#                      the same host goes straight through.
#   ~/.wk-egress       http_proxy/https_proxy, for every shell that reads an
#                      rc -- vm/shell-rc.sh sources it from all four. A
#                      profile alone would reach only *login* shells, and an
#                      editor's terminal pane is not one: a pane with no proxy
#                      has no egress at all, and reports it as every host in
#                      the world being unreachable.
#
# The variable names are spelled here rather than in shell/bashrc, and the
# whole file is written from this side: that rc lives in the guest's own copy
# of wk-tools, which only a build refreshes, and a guest must not be without
# egress until someone builds in it. targets/container.sh names the same six
# for the same reason -- each target delivers its own.
#
# Best effort, like the marker: it warns rather than errors, since the failure
# is otherwise silent.
_set_guest_egress() {
    local name="$1" ip="$2" addr=""
    [ -n "${WK_VM_UNFILTERED:-}" ] || addr=$(_proxy_addr)
    debug "guest egress in $name: ${addr:-off}"

    _ssh "$ip" "bash -s" <<EOF
set -u
addr='$addr'
port='$WK_VM_PROXY_PORT'

# The shell half first, so a guest whose network service cannot be identified
# below still gets a working proxy in its shells.
if [ -z "\$addr" ]; then
    rm -f "\$HOME/.wk-egress"
else
    cat > "\$HOME/.wk-egress" <<WKEGRESS
# wk: written by targets/vm.sh on every start; sourced by every shell
# (vm/shell-rc.sh). Softnet denies everything but this address.
export http_proxy=http://\$addr:\$port
export https_proxy=http://\$addr:\$port
export HTTP_PROXY=http://\$addr:\$port
export HTTPS_PROXY=http://\$addr:\$port
export no_proxy=localhost,127.0.0.1,::1
export NO_PROXY=localhost,127.0.0.1,::1
WKEGRESS
fi

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

# The guest's clock, set from this host on every start.
#
# A guest cannot keep its own time, and the boundary is why. `tart clone` hands
# a clone the golden base's clock rather than the host's, and nothing inside
# can correct it: Softnet allows exactly one address, and NTP is UDP, which an
# HTTP CONNECT proxy cannot carry. So a guest's clock is the date its base was
# sealed, further behind with every day that base ages.
#
# What that looks like from inside is not a clock. Every TLS handshake to a
# certificate issued after that date fails to verify -- Claude Code reports
# `Failed to connect to platform.claude.com: CERT_NOT_YET_VALID` -- so a stale
# guest presents as one service after another being unreachable, and reads like
# an egress refusal that widening the allowlist would fix. It is not one.
#
# This host is the only time source the guest can reach, so this host sets it.
# Idempotent by measurement, like the system proxy above: a guest already
# within WK_VM_CLOCK_SKEW seconds costs one ssh and no sudo.
_set_guest_clock() { # <name> <ip>
    local name="$1" ip="$2" skew
    # The guest decides whether to write, so an in-time guest needs no sudo and
    # prints nothing. `date -u MMDDhhmmCCYY.ss` is BSD date's set form; the
    # value is UTC on both sides, so neither end's timezone enters into it.
    skew=$(_ssh "$ip" "env WK_NOW_EPOCH=$(date -u +%s) WK_NOW_SET=$(date -u +%m%d%H%M%Y.%S) \
                           WK_SKEW=$(sh_quote "$WK_VM_CLOCK_SKEW") bash -s" <<'EOF'
set -u
skew=$(( WK_NOW_EPOCH - $(date -u +%s) ))
[ "$skew" -ge 0 ] || skew=$(( - skew ))
[ "$skew" -gt "$WK_SKEW" ] || exit 0
sudo -n date -u "$WK_NOW_SET" >/dev/null || exit 1
echo "$skew"
EOF
    ) || return 1
    [ -n "$skew" ] || return 0
    info "$name's clock was ${skew}s out; set from this host"
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

# The guest's shells, pointed at the shared rc -- which is where `wk` gets onto
# PATH (shell/path.sh), so `wk build` typed inside a guest resolves at all.
# Written on every start, like the marker and .lldbinit, and from the same file
# vm/provision-base.sh runs (vm/shell-rc.sh): one implementation, and a guest
# cloned from a base that predates it converges here rather than needing the
# base rebuilt.
_write_shell_rc() { # <name> <ip>
    _ssh "$2" "bash -s $(sh_quote "$(t_tools "$1")")" < "$WK_ROOT/vm/shell-rc.sh"
}

command -v tools_push >/dev/null 2>&1 || . "$WK_ROOT/lib/tools.sh"

# A checkout of this tree's HEAD, pushed as a git bundle (tools_push,
# lib/tools.sh) rather than mounted: no --dir is ever passed to `tart run`,
# since a shared directory is the host-filesystem hole this target exists not
# to have. A guest holds a commit that exists, which is what `wk status`
# compares by sha; an uncommitted tree here is refused, not copied. Shared
# with _prebuild_base and _provision_base, into a *running* golden base.
_push_tools() {
    local name="$1" ip="$2"
    # The transport tools_push runs its one command over, passed as a command
    # *prefix* rather than a wrapper function: the address is an argument, so
    # nothing here leaves a global function behind holding a stale `ip`.
    tools_push "$(t_tools "$name")" _ssh "$ip"
}

t_sync_tools() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    _push_tools "$name" "$ip" || return 1
    _write_marker "$name" "$ip"
}

# This target's furniture (`wk sync --tools`): a guest carries its own copy
# of wk-tools, and there is nothing else -- a macOS guest's "base" is the
# golden image, which this driver builds rather than snapshots. Only the
# guests that are up: booting one to refresh a file would be a side effect
# nobody asked this command for.
t_sync() {
    local g rc=0
    for g in $(target_workspaces); do
        if [ "$(t_info "$g" 2>/dev/null)" != running ]; then
            printf '  %-24s %s\n' "$g" "not running -- skipped" >&2
            continue
        fi
        if t_sync_tools "$g"; then printf '  %-24s ok\n' "$g" >&2
        else rc=1; fi
    done
    return "$rc"
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
    # of the same name (`wk ai claude` refuses on it, cmd/ai).
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

    _push_tools "$WK_VM_BASE" "$ip" \
        || die "the base cannot be pre-built without wk-tools in it (see above)"

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

# The inputs that produced this base, as one hash: the three scripts
# provisioning runs inside it, the image it is cloned from, the account, and
# whether the password change applies at all. Never the password itself: a
# 16-character digest over public files plus a trivial password is a password a
# reader of the marker could recover, and the value needs no record here anyway
# -- vm/desktop.sh authenticates it in the guest on every start and `wk vm
# check` reports a guest whose password is not the one this run expects.
#
# A record of what produced an artifact, not a cached verdict (CLAUDE.md, "No
# stored copy of a recomputable fact"): nothing reads this to decide the base is
# current. vm_base_stale recomputes the hash on every read and compares, so a
# script edited here makes every base built before it read stale immediately.
#
# python3 rather than shasum: it is present on every host and image here, and
# hashing is the whole of what this needs from it.
_base_inputs_hash() {
    {
        cat "$WK_ROOT/vm/provision-base.sh" "$WK_ROOT/vm/desktop.sh" \
            "$WK_ROOT/vm/shell-rc.sh"
        printf 'image=%s\nuser=%s\npassword_change=%s\n' \
            "$WK_VM_IMAGE" "$WK_VM_USER" \
            "$([ "$WK_VM_PASSWORD" = "$WK_VM_IMAGE_PASSWORD" ] && echo no || echo yes)"
    } | python3 -c 'import hashlib,sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])'
}

# Why the base does not match the inputs that would produce it now, or nothing
# when it does: prints the reason and succeeds when it is stale, so a caller
# reads `if why=$(vm_base_stale); then`. Read-only, recomputed every time, and
# it never rebuilds anything -- the rebuild is hours and is the person's to run.
vm_base_stale() {
    local rec
    rec=$(marker_field "$(_base_marker)" inputs)
    if [ -z "$rec" ]; then
        echo "provisioned before this record existed"
        return 0
    fi
    [ "$rec" = "$(_base_inputs_hash)" ] && return 1
    echo "vm/provision-base.sh, vm/desktop.sh, vm/shell-rc.sh, WK_VM_IMAGE, WK_VM_USER or the password change has changed since it was built"
    return 0
}

# The base, in the `<state> <what> <remedy>` shape vm_desktop_findings and
# remote/deps.sh's wk_remote_findings use, so `wk vm ls`, `wk vm check` and `wk
# doctor` all report it through their own renderer and none of them re-decides
# what counts as wrong.
vm_base_findings() {
    local why
    _f() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }

    if ! _base_exists; then
        _f wrong "no golden base VM '$WK_VM_BASE' -- there is nothing for a guest to be cloned from" \
                 "wk vm base   (hours: the image pull, Xcode, a checkout, a prebuild)"
    elif [ ! -f "$(_base_marker)" ]; then
        _f wrong "'$WK_VM_BASE' exists but provisioning never finished in it" \
                 "wk vm base --refresh   (re-runs provisioning; nothing is re-downloaded)"
    elif why=$(vm_base_stale); then
        _f wrong "'$WK_VM_BASE' predates its own provisioning inputs: $why -- every guest cloned from it carries what that base was built with, the account's password included" \
                 "wk vm base --rebuild   (hours; existing guests are unaffected)"
    else
        _f ok "golden base '$WK_VM_BASE' matches its provisioning inputs"
    fi

    unset -f _f
    return 0
}

_base_mark_ready() {
    ensure_dir "$WK_VM_DIR" 0700 >/dev/null
    printf 'image=%s
prebuild=%s
inputs=%s
finished=%s
' \
        "$WK_VM_IMAGE" "${WK_VM_BASE_PREBUILD:-none}" "$(_base_inputs_hash)" \
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

    # Before anything here speaks TLS. A pulled image arrives with the clock it
    # was built with, and provisioning's very first act is an HTTPS clone --
    # which a stale clock fails as a certificate that is not yet valid, naming
    # the certificate rather than the clock. A refusal, not a warning: every
    # workspace is a clone of what this builds, and a base sealed at the wrong
    # date hands that date to all of them (_set_guest_clock).
    _set_guest_clock "$WK_VM_BASE" "$ip" \
        || die "could not set the clock in '$WK_VM_BASE'. Passwordless sudo is what it
    needs, and the base is built from the image WK_VM_IMAGE names -- check that
    image rather than patching the guest:  ssh into it and run  sudo -n true"

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
    # The whole checkout: provision-base.sh links ~/.claude out of it too.
    _push_tools "$WK_VM_BASE" "$ip" \
        || die "the base cannot be provisioned without wk-tools in it (see above)"
    # No proxy address: the base boots unfiltered and provisioning names no
    # proxy. A clone's is written at start, by _set_guest_egress.
    vm_login_note
    _ssh "$ip" "env WK_VM_DISPLAY=$(sh_quote "$WK_VM_DISPLAY") WK_VM_USER=$(sh_quote "$WK_VM_USER") WK_VM_PASSWORD=$(sh_quote "$WK_VM_PASSWORD") WK_VM_IMAGE_PASSWORD=$(sh_quote "$WK_VM_IMAGE_PASSWORD") bash $(t_tools "$WK_VM_BASE")/vm/provision-base.sh" \
        || die "base provisioning failed"

    _prebuild_base "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    # Last: until this exists the guest is rubble that the next run destroys.
    _base_mark_ready
    changed "golden base VM '$WK_VM_BASE' is ready"
}

# --- is this guest on an empty desktop? --------------------------------------
# What vm/desktop-probe.sh found, turned into findings -- one per line,
# tab-separated `<state> <what> <remedy>`, state one of ok | wrong | note. The
# same shape remote/deps.sh's wk_remote_findings uses, so `wk vm check` and `wk
# doctor --all` read alike and neither re-decides what counts as wrong.
#
# One line per finding, remedy included: every renderer reads these a line at a
# time, so a remedy wrapped across two lines loses its second half.
#
# Everything here is provisioned into the golden base
# (vm/provision-base.sh), so the remedy for most of it is a base that carries
# it -- `wk vm base --rebuild` -- never a patch applied to the running guest
# (CLAUDE.md, "No in-place upgrades").
vm_desktop_findings() { # <probe output>
    local probe="$1" v
    _f() { printf '%s\t%s\t%s\n' "$1" "$2" "${3:-}"; }
    _v() { printf '%s\n' "$probe" | sed -n "s|^$1=||p" | tail -1; }
    local rebuild="wk vm base --rebuild, then re-create this guest"

    local restart="wk vm stop <name> && wk vm start <name>"
    v=$(_v console_user)
    case "$v" in
        root|""|"?") _f wrong "nobody is logged in at the window (console user '$v') -- there is no desktop to draw on" \
                              "$restart  (auto-login logs it back in; the account is a base setting)" ;;
        *)           _f ok "logged in at the window as $v" ;;
    esac

    case "$(_v screenlock)" in
        off)     _f ok "screen lock off" ;;
        on)      _f wrong "the screen lock is on, so this guest comes up asking for a password" \
                          "$rebuild  -- vm/desktop.sh leaves the lock alone unless the account's password is the one wk set, and will not guess at it" ;;
        *)       _f note "screen lock could not be read (sysadminctl needs passwordless sudo in there)" ;;
    esac

    [ "$(_v idletime)" = 0 ] \
        && _f ok "screen saver off" \
        || _f wrong "the screen saver is armed (idleTime=$(_v idletime)), and it occludes the window it covers" "$rebuild"

    [ "$(_v displaysleep)" = 0 ] \
        && _f ok "display sleep off" \
        || _f wrong "the display sleeps after $(_v displaysleep) minutes" "$rebuild"

    v=$(_v setupassistant_pending)
    [ -z "$v" ] \
        && _f ok "Setup Assistant already clicked through" \
        || _f wrong "Setup Assistant will put a modal pane on the desktop:$v" "$rebuild"

    # The offer that puts a panel on the desktop is the *system* one: the
    # per-user domain is what System Settings shows, and softwareupdated acts on
    # /Library/Preferences. `?` there is "no such key", which is not off, so it
    # is reported as unsettled rather than as fine. An empty reading is a third
    # thing again -- the guest answered with an older probe than this report --
    # and is a note, since nothing about the guest has been established.
    case "$(_v update_check_system)" in
        0)  _f ok "Software Update checks off where softwareupdated reads them (/Library/Preferences)" ;;
        "") _f note "the guest did not answer about Software Update, so what it will offer on that desktop is unknown" \
                    "$restart  -- a start re-runs this probe" ;;
        *)  _f wrong "Software Update will offer an upgrade on the desktop (system AutomaticCheckEnabled=$(_v update_check_system)) -- a guest is a clone of a pinned image and upgrading it means nothing" "$rebuild" ;;
    esac

    case "$(_v update_schedule)" in
        off) _f ok "the scheduled update check is off" ;;
        "")  _f note "softwareupdate did not report its schedule in there, so whether the check comes back on its own is unknown" \
                     "$restart  -- a start re-runs this probe" ;;
        *)   _f wrong "the scheduled update check is $(_v update_schedule) in there, so the offer comes back on its own" "$rebuild" ;;
    esac

    case "$(_v update_check):$(_v update_download)" in
        0:0) _f ok "Software Update offers off in the login account too" ;;
        *)   _f note "the account's own Software Update settings read check=$(_v update_check), download=$(_v update_download) -- what System Settings shows at that window, not what softwareupdated obeys" "$rebuild" ;;
    esac

    case "$(_v update_autoinstall_system)" in
        0)  _f ok "macOS updates will not install themselves" ;;
        "") ;;   # the note above already says this guest answered nothing here
        *)  _f wrong "macOS updates are set to install themselves in there (AutomaticallyInstallMacOSUpdates=$(_v update_autoinstall_system)), which reboots the guest -- mid-build, if that is when one lands" "$rebuild" ;;
    esac

    # Setup Assistant's "what is new in macOS" pane is a Software Update screen
    # by another name, and it is the one a clone gets: Buddy shows it whenever
    # these keys do not already name the running system.
    v=$(_v os_product)
    if [ -z "$v" ] || [ "$v" = '?' ]; then
        _f note "the guest did not say which macOS it runs, so Setup Assistant's 'what is new in macOS' pane cannot be judged from here" \
                "$restart  -- a start re-runs this probe"
    elif [ "$(_v setupassistant_seen_product)" = "$v" ]; then
        _f ok "Setup Assistant has already seen macOS $v"
    else
        _f wrong "Setup Assistant will show its 'what is new in macOS' pane (it last saw $(_v setupassistant_seen_product), this guest runs $v)" "$rebuild"
    fi

    v=$(_v panels)
    [ -z "$v" ] \
        && _f ok "nothing modal on screen now" \
        || _f wrong "something is on the desktop right now: $v" \
                    "$restart  -- the settings above stop the next one, and killing this one would take the desktop session with it (vm/desktop.sh)"

    # The account, not its password: these findings are printed on every `wk vm
    # start`, and vm_login_note is the one place the password is stated.
    _f note "the guest's own window logs in as $(_v user)" \
            "wk itself uses an ssh key; 'wk vm start' and 'wk vm enter' state that account's password"

    unset -f _f _v
    return 0
}

# The probe, run in a guest. One implementation, so every caller sends the same
# evidence-gatherer. `_ssh_in`, not t_exec: this must answer about a guest whose
# wk-tools copy is older than this file.
vm_desktop_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    _ssh "$ip" 'bash -s' < "$WK_ROOT/vm/desktop-probe.sh"
}

# One renderer for every findings stream here -- the desktop, the base, what is
# resident in a guest -- so `wk vm start` and `wk vm check` cannot describe the
# same guest in two voices. Findings on stdin; the count of `wrong` ones is the
# exit status, which is what `wk vm check` exits with.
#
# On stderr: a report is not output anything pipes, and t_start's stdout is the
# guest's address.
vm_render_findings() {
    local state what remedy bad=0
    while IFS="$(printf '\t')" read -r state what remedy; do
        [ -n "$state" ] || continue
        case "$state" in
            ok)    printf '  \033[32mok\033[0m    %s\n' "$what" >&2 ;;
            wrong) printf '  \033[31m--\033[0m    %s\n' "$what" >&2
                   [ -z "$remedy" ] || printf '        -> %s\n' "$remedy" >&2
                   bad=$((bad + 1)) ;;
            note)  printf '  \033[33m??\033[0m    %s\n' "$what" >&2
                   [ -z "$remedy" ] || printf '        %s\n' "$remedy" >&2 ;;
        esac
    done
    return "$bad"
}

# --- what is resident in this guest? -----------------------------------------
# A guest is long-lived and holds its whole memory allocation whether or not it
# is busy, so what a build in there eventually runs out of is whatever else
# stayed resident. Nothing wk runs in a guest outlives its ssh session -- t_exec
# is one `bash -lc`, t_exec_tty and t_enter exec an `ssh -t` that ends with the
# connection, and this driver's _ssh_opts sets no ControlPersist, so no
# multiplexed master lingers to hold a session open. What accumulates is
# therefore something that keeps its own process: an editor's remote server
# (Zed's outlives the window, and each terminal pane in it is another shell), an
# agent, or a detached build.
#
# vm/load-probe.sh gathers the raw readings; the arithmetic is here, where a
# test can drive it against a captured sample.
vm_load_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    _ssh "$ip" 'bash -s' < "$WK_ROOT/vm/load-probe.sh"
}

# Findings in the same `<state> <what> <remedy>` shape as the rest. python3
# because grouping a few hundred `proc=` rows by executable and summing their
# RSS is exactly the arithmetic bash must not be doing (CLAUDE.md).
vm_load_findings() { # <probe output>
    printf '%s\n' "$1" | python3 -c '
import re, sys

shells_warn, free_warn, swap_warn = (int(a) for a in sys.argv[1:4])

procs, vals = [], {}
for line in sys.stdin.read().splitlines():
    key, _, val = line.partition("=")
    if key == "proc":
        rss, _, comm = val.strip().partition(" ")
        if rss.isdigit():
            procs.append((int(rss), comm.strip()))
    elif key:
        vals[key] = val.strip()

def out(state, what, remedy=""):
    print("\t".join((state, what, remedy)))

def mb(kb):
    return int(kb / 1024)

# The families that accumulate, each matched on the executable itself so an
# argument mentioning "zed" is not counted as one.
FAMILIES = (
    ("shell", r"(^|/)(-?zsh|bash|sh|dash|tcsh|fish|login)$"),
    ("editor remote server", r"(zed-remote-server|\.zed_server/|\.vscode-server/)"),
    ("agent", r"(^|/)claude$|/claude/versions/"),
    ("ssh session", r"(^|/)sshd(-session)?$"),
)

groups = {}
for rss, comm in procs:
    for fam, pat in FAMILIES:
        if re.search(pat, comm):
            n, kb = groups.get(fam, (0, 0))
            groups[fam] = (n + 1, kb + rss)
            break

def named(fam):
    n, kb = groups.get(fam, (0, 0))
    return n, kb

shells, shell_kb = named("shell")
if shells > shells_warn:
    # In the order in which one holds the others open: an editor remote server
    # keeps a shell per pane, an agent keeps its own, an ssh session is one
    # shell and goes when it ends. Every family present is named -- a report
    # that picks the largest count would name sshd and leave the cause out.
    holders = ["%d %s process(es)" % (groups[f][0], f)
               for f in ("editor remote server", "agent", "ssh session")
               if f in groups]
    why = (" -- alongside " + ", ".join(holders)) if holders else ""
    out("wrong",
        "%d shells are resident in there, holding %d MB%s" % (shells, mb(shell_kb), why),
        "wk vm stop <name> && wk vm start <name> takes them all with it; closing the "
        "editor window does not, since its remote server outlives it")
else:
    out("ok", "%d shells resident (%d MB)" % (shells, mb(shell_kb)))

n, kb = named("editor remote server")
if n:
    out("note",
        "an editor remote server is running in there: %d process(es), %d MB" % (n, mb(kb)),
        "it outlives the editor window, and every terminal pane in it leaves a "
        "shell behind; a guest restart is what clears both")
n, kb = named("agent")
if n:
    out("note", "%d agent process(es) in there, %d MB" % (n, mb(kb)),
        "each `wk ai claude` session in a guest is one of these")

free = vals.get("mem_free_pct", "")
total = vals.get("mem_total_mb", "?")
if not free.isdigit():
    out("note", "memory pressure could not be read in there (memory_pressure said nothing)")
elif int(free) < free_warn:
    top = ", ".join("%s (%d MB)" % (c.rsplit("/", 1)[-1], mb(r))
                    for r, c in sorted(procs, reverse=True)[:3])
    out("wrong",
        "%s%% of the %s MB in that guest is free, and macOS calls that pressure: "
        "the biggest resident processes are %s" % (free, total, top),
        "wk vm stop <name> && wk vm start <name>; a build in there is otherwise "
        "paging, and every number it produces is about the paging")
else:
    out("ok", "%s%% of the %s MB in that guest is free" % (free, total))

# sysctl vm.swapusage, raw: "total = 2048.00M  used = 512.25M  free = ...".
m = re.search(r"used = ([0-9.]+)M", vals.get("swapusage", ""))
if m and float(m.group(1)) > swap_warn:
    out("note", "the guest is using %d MB of swap" % float(m.group(1)),
        "it holds a fixed allocation, so this is the guest paging inside itself: "
        "a build here is slower than its numbers say")
' "$WK_VM_SHELLS_WARN" "$WK_VM_MEM_FREE_WARN_PCT" "$WK_VM_SWAP_WARN_MB"
}
