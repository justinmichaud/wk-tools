# Target driver: a disposable macOS VM on Tart. `tart clone` is APFS copy-on-write, so a
# golden base with Xcode and a checkout is built once and every workspace is a free clone.
# Apple permits two *running* macOS VMs per host (a third fails with VZErrorDomain code 6).

WK_VM_IMAGE="${WK_VM_IMAGE:-ghcr.io/cirruslabs/macos-tahoe-xcode:26.5}"
WK_VM_BASE="${WK_VM_BASE:-wk-base}"
WK_VM_MAX="${WK_VM_MAX:-2}"
WK_VM_USER="${WK_VM_USER:-admin}"

# The image ships admin/admin (Cirrus Labs) and it is kept: macOS refuses the change
# from inside the guest, and only the guest's own login window ever asks for it.
WK_VM_PASSWORD="${WK_VM_PASSWORD:-admin}"

vm_login_note() {
    log "  the guest's own window logs in as $WK_VM_USER / $WK_VM_PASSWORD"
    log "  (wk itself uses an ssh key; this is for a prompt on the screen)"
    log "  wk vm check <name>   what is in front of that window, and what is"
    log "                       piling up in it"
}

# Softnet runs its own network, not vmnet's usual 192.168.64.1, so the host address the guest can reach is discovered from the live interface.
WK_VM_SUBNET="${WK_VM_SUBNET:-192.168.2}"
WK_VM_PROXY_PORT="${WK_VM_PROXY_PORT:-3128}"

_proxy_addr() {
    [ -n "${WK_VM_PROXY_ADDR:-}" ] && { echo "$WK_VM_PROXY_ADDR"; return 0; }
    local a
    a=$(ifconfig 2>/dev/null | awk -v net="$WK_VM_SUBNET." '
        $1 == "inet" && index($2, net) == 1 { print $2; exit }')
    echo "${a:-${WK_VM_SUBNET}.1}"
}
WK_SOFTNET_BIN="${WK_SOFTNET_BIN:-/usr/local/bin/softnet}"

# tart resolves softnet through PATH, and a non-interactive ssh has only /usr/bin:/bin:/usr/sbin:/sbin.
case ":$PATH:" in
    *":$(dirname "$WK_SOFTNET_BIN"):"*) ;;
    *) PATH="$(dirname "$WK_SOFTNET_BIN"):$PATH"; export PATH ;;
esac

command -v envelope_mem_mb >/dev/null 2>&1 || . "$WK_ROOT/lib/resources.sh"
WK_VM_BASE_CPUS="${WK_VM_BASE_CPUS:-}"
WK_VM_BASE_MEM_MB="${WK_VM_BASE_MEM_MB:-}"
_base_cpus()   { echo "${WK_VM_BASE_CPUS:-$(envelope_cores)}"; }
_base_mem_mb() { echo "${WK_VM_BASE_MEM_MB:-$(envelope_mem_mb)}"; }

WK_VM_BASE_PREBUILD="${WK_VM_BASE_PREBUILD-mac-release}"

# The prepared image's stock 140 GB does not fit even one build. A ceiling, not an allocation: the disk is sparse and the clones are copy-on-write.
WK_VM_DISK_GB="${WK_VM_DISK_GB:-320}"

# Not zero: the reading is taken over ssh, so a round trip is in every compare.
WK_VM_CLOCK_SKEW="${WK_VM_CLOCK_SKEW:-30}"

# Twelve shells measured as nothing wrong; macOS pages below 15% free (memory_pressure).
WK_VM_SHELLS_WARN="${WK_VM_SHELLS_WARN:-12}"
WK_VM_MEM_FREE_WARN_PCT="${WK_VM_MEM_FREE_WARN_PCT:-15}"
WK_VM_SWAP_WARN_MB="${WK_VM_SWAP_WARN_MB:-1024}"

# In points. Tart pins the window's *minimum* content size to it, and AppKit drops fullScreenPrimary once minSize exceeds the screen.
WK_VM_DISPLAY="${WK_VM_DISPLAY:-1280x800}"

WK_HOST_FREE_WARN_GB="${WK_HOST_FREE_WARN_GB:-80}"
WK_HOST_FREE_MIN_GB="${WK_HOST_FREE_MIN_GB:-25}"

WK_STORE="${WK_VM_STORE:-$(wk_state_dir)}"
WK_VM_DIR="$WK_STORE/vm"
WK_VM_KEY="$WK_VM_DIR/id_ed25519"

# tart needs the com.apple.security.virtualization entitlement, so it stays in the signed app bundle; launched from outside the .app it loses fullScreenPrimary.
_tart_bin() {
    local p
    if command -v tart >/dev/null 2>&1; then p=$(command -v tart)
    elif [ -x "$HOME/.local/bin/tart" ]; then p="$HOME/.local/bin/tart"
    elif [ -x "$HOME/.local/share/tart/tart.app/Contents/MacOS/tart" ]; then
        p="$HOME/.local/share/tart/tart.app/Contents/MacOS/tart"
    else return 1
    fi
    # `readlink -f` only grew symlink-chain resolution on recent macOS, and bash 3.2 must still work.
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

# `tart delete` leaves the `tart run` process alive, and a runner for a VM that no longer
# exists still holds one of Apple's two slots (measured 2026-09-04: base.run.log said "The number of VMs exceeds the system limit").
_vm_delete() { # <vm name>
    local v="$1" rc=0 pids
    _tart stop "$v" >/dev/null 2>&1 || true
    _tart delete "$v" || rc=$?
    pids=$(_vm_runners "$v")
    if [ -n "$pids" ]; then
        # shellcheck disable=SC2086 -- one signal for the whole list.
        kill $pids 2>/dev/null || true
        pids=$(_vm_runners "$v")
        [ -z "$pids" ] || warn "a 'tart run' for '$v' is still alive (pid $pids) and holds a
    VM slot the next guest needs:  kill -9 $pids"
    fi
    return "$rc"
}

_vm_runners() { # <vm name>
    pgrep -f "tart run .*[[:space:]]$1\$" 2>/dev/null || true
}

# `tart list` mixes local VMs and cached OCI images, spelling Source "local" case-folded.
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

t_src()   { echo "/Users/$WK_VM_USER/WebKit"; }
t_tools() { echo "/Users/$WK_VM_USER/wk-tools"; }

t_mirror_dir() { mirror_beside_checkout "$(t_src "$1")"; }

t_needs_base() { return 1; }

t_store_init() {
    ensure_dir "$WK_STORE"
    ensure_dir "$WK_STORE/ws"
    ensure_dir "$WK_VM_DIR" 0700
}

t_list() {
    _vm_query list "$WK_VM_BASE"
}

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

t_ssh_user() { printf '%s' "$WK_VM_USER"; }

# A guest cannot see a unix socket across the hypervisor, so this one is created by sshd and carried by this host's `ssh -R`.
t_agent_sock() { printf '/Users/%s/.wk-ssh-agent.sock' "$WK_VM_USER"; }

t_os() { echo macos; }

t_create() {
    local name="$1" why
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" = absent ] || die "workspace '$name' already exists"

    _ensure_base

    if why=$(vm_base_stale); then
        warn "'$WK_VM_BASE' predates its own provisioning inputs: $why.
  '$name' is a clone of it, so it carries what that base was built with -- the
  desktop settings of the day it was sealed included. 'wk vm check $name' says
  which of them are still wrong.
      wk vm base --rebuild     hours; existing guests are unaffected"
    fi

    local running; running=$(_running_count)
    [ "${running:-0}" -ge "$WK_VM_MAX" ] && \
        warn "$running macOS VM(s) already running; you will have to stop one before starting '$name'"

    info "cloning $WK_VM_BASE -> $v (APFS copy-on-write)"
    local t0; t0=$(date +%s)
    _tart clone "$WK_VM_BASE" "$v"
    # Set.swift assigns displayRefit unconditionally, so a `tart set` omitting --display-refit clears it.
    _tart set "$v" --cpu "$(_vm_cpus)" --memory "$(_vm_mem_mb)" --random-mac --random-serial \
        --display "$WK_VM_DISPLAY" --display-refit
    debug "clone took $(( $(date +%s) - t0 ))s"

    [ $(( $(date +%s) - t0 )) -gt 60 ] && \
        warn "the clone took $(( $(date +%s) - t0 ))s -- APFS copy-on-write may not be in play; check disk use"

    ensure_dir "$(wk_ws_dir "$name")"

    : > "$(wk_ws_dir "$name")/$WK_READY_MARKER"
}

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
    _write_deploy_keys "$name" "$ip" || warn "could not write $name's ssh config and public key halves; a push from in there is refused ('wk push status')"
    _agent_converge_guest "$name" "$ip" || warn "could not converge $name's ssh-agent forward; 'wk push status' says what it can reach"
    _settle_desktop "$name" "$ip" || warn "could not settle $name's desktop; 'wk vm check $name' says what is in front of the window"
    _report_desktop "$name"
}

t_start() {
    local name="$1" ip
    local v; v=$(_vm "$name")

    [ "$(_vm_state "$v")" != absent ] || die "no such workspace: $name"
    if [ "$(_vm_state "$v")" = running ]; then
        ip=$(_ip "$name")
        _start_host_proxy || true
        _converge_guest "$name" "$ip"
    else
        _check_guest_limit
        _check_memory_budget "$name" "$(t_mem_mb "$name")"
        _check_host_disk

        ip=$(_boot "$v" 180)
        _converge_guest "$name" "$ip"
    fi

    # One exit, so every command that starts a guest states the login.
    vm_login_note
    echo "$ip"
}

# `defaults -currentHost` writes per hardware UUID and `tart clone` gives the clone a new one, so a screen saver the base turned off is armed again in every clone.
_settle_desktop() { # <name> <ip>
    {
        printf 'WK_VM_PASSWORD=%s\n' "$(sh_quote "$WK_VM_PASSWORD")"
        cat "$WK_ROOT/vm/desktop.sh"
    } | _ssh "$2" "bash -s" >/dev/null 2>&1
}

_report_desktop() { # <name>
    local probe
    probe=$(vm_desktop_probe "$1" 2>/dev/null) || return 0
    [ -n "$probe" ] || return 0
    log "  the guest's desktop, as it is now ('wk vm check $1' asks again):"
    vm_render_findings <<FINDINGS || true
$(vm_desktop_findings "$probe")
FINDINGS
}

# --net-softnet-block=0.0.0.0/0 is default-deny, and longest-prefix-match makes the single --net-softnet-allow "nothing except the proxy".
_softnet_flags() {
    if [ -n "${WK_VM_UNFILTERED:-}" ]; then
        warn "WK_VM_UNFILTERED=1 -- this guest gets the open network, with no egress filter"
        return 0
    fi
    # Fail closed: Softnet applies at `tart run` and cannot be added later, so a guest booted without it has the open network for its whole life.
    [ -x "$WK_SOFTNET_BIN" ] || die "softnet is not installed, so this guest's egress would not be filtered.
    Install it:  ./setup --stage softnet   (needs a terminal for sudo)
    Or set WK_VM_UNFILTERED=1 to boot with the open network anyway."
    printf '%s\n' --net-softnet \
        "--net-softnet-block=0.0.0.0/0" \
        "--net-softnet-allow=$(_proxy_addr)/32"
}

_proxy_pidfile() { echo "$WK_VM_DIR/proxy.pid"; }

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

    addr=$(_proxy_addr)
    while [ "$i" -lt 30 ]; do
        ifconfig 2>/dev/null | grep -q "inet $addr " && break
        sleep 0.5; i=$((i + 1))
    done
    if ! ifconfig 2>/dev/null | grep -q "inet $addr "; then
        warn "the guest bridge never got address $addr; not starting the proxy"
        return 1
    fi

    # The injector first: the proxy hands it api.github.com's CONNECT, and one started after the proxy answers those with 502 until it is up.
    _start_host_inject || true

    WK_PROXY_UNIX=0 \
    WK_PROXY_TCP="$addr:$WK_VM_PROXY_PORT" \
    WK_STORE="$WK_STORE" \
    WK_INJECT_SOCK="$(_inject_sock)" \
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

# container/proxy/github-inject.py, the same program the podman machine runs for containers: it terminates TLS for api.github.com and adds the Authorization header, so a guest opens a PR without holding the token.
_inject_sock()  { echo "$WK_VM_DIR/github-inject.sock"; }
_inject_dir()   { echo "$WK_VM_DIR/github-inject"; }
_inject_ca()    { echo "$WK_VM_DIR/wk-github-ca.pem"; }
_inject_pat()   { echo "$WK_VM_DIR/push-github-pat"; }
_inject_read_pat() { echo "$WK_VM_DIR/read-github-pat"; }

_inject_running() { [ -S "$(_inject_sock)" ] && nc -z -U "$(_inject_sock)" 2>/dev/null; }

_start_host_inject() {
    ensure_dir "$WK_VM_DIR"
    push_agent_pat_sync _agent_exec "$(_inject_read_pat)" \
        || warn "could not converge $(_inject_read_pat); a read from a guest answers 401"
    _inject_running && return 0
    local log="$WK_VM_DIR/github-inject.log" i=0
    WK_INJECT_SOCK="$(_inject_sock)" \
    WK_INJECT_DIR="$(_inject_dir)" \
    WK_INJECT_CA_OUT="$(_inject_ca)" \
    WK_INJECT_PAT="$(_inject_pat)" \
    WK_INJECT_READ_PAT="$(_inject_read_pat)" \
    nohup /usr/bin/python3 "$WK_ROOT/container/proxy/github-inject.py" >"$log" 2>&1 &
    echo $! > "$WK_VM_DIR/github-inject.pid"
    disown 2>/dev/null || true
    while [ "$i" -lt 40 ]; do
        _inject_running && { info "GitHub API injector on $(_inject_sock)"; return 0; }
        sleep 0.25; i=$((i + 1))
    done
    warn "the GitHub API injector did not start, so 'git-webkit pr' in a guest
  will fail; see $log"
    return 1
}

# A guest cannot see a unix socket across the hypervisor, so the ssh-agent holding the private halves runs *here* and an `ssh -N -R` carries its socket in.
_agent_sock()    { echo "$WK_VM_DIR/ssh-agent.sock"; }
_agent_pidfile() { echo "$WK_VM_DIR/ssh-agent.pid"; }

_agent_exec() { sh -c "$1"; }

_start_host_agent() {
    push_agent_ensure _agent_exec "$(_agent_sock)" && return 0
    ensure_dir "$WK_VM_DIR"
    # ssh-agent refuses to bind a path that exists.
    rm -f "$(_agent_sock)"
    nohup /usr/bin/ssh-agent -D -a "$(_agent_sock)" \
        >"$WK_VM_DIR/ssh-agent.log" 2>&1 &
    echo $! > "$(_agent_pidfile)"
    disown 2>/dev/null || true

    local i=0
    while [ "$i" -lt 20 ]; do
        push_agent_ensure _agent_exec "$(_agent_sock)" && return 0
        sleep 0.2; i=$((i + 1))
    done
    warn "the guests' ssh-agent did not start, so no guest can push;
  see $WK_VM_DIR/ssh-agent.log"
    return 1
}

_forward_status() { echo "$WK_VM_DIR/$1.agent-forward"; }

_agent_forward_start() { # <name> <ip>
    with_lock "vm-agent-forward-$1" -- _agent_forward_start_locked "$@"
}

_agent_forward_start_locked() { # <name> <ip>
    local name="$1" ip="$2" sf log pid
    command -v detach_run >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    sf=$(_forward_status "$name")
    detach_alive "$sf" && return 0
    log="$WK_VM_DIR/$name.agent-forward.log"
    _ssh "$ip" "rm -f $(sh_quote "$(t_agent_sock)")" </dev/null || return 1
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    pid=$(detach_run "$sf" "$log" -- \
        ssh $(_ssh_opts) -N -R "$(t_agent_sock):$(_agent_sock)" "$WK_VM_USER@$ip")
    status_write "$sf" state=running pid="$pid" log="$log" stage=forwarding
    sleep 0.5
    detach_alive "$sf" && return 0
    [ "$(status_field "$sf" pid)" = "$pid" ] && rm -f "$sf"
    return 1
}

_agent_forward_stop() { # <name>
    with_lock "vm-agent-forward-$1" -- _agent_forward_stop_locked "$1"
}

_agent_forward_stop_locked() { # <name>
    local sf pid
    command -v detach_alive >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    sf=$(_forward_status "$1")
    pid=$(status_field "$sf" pid)
    [ -z "$pid" ] || kill "$pid" 2>/dev/null || true
    rm -f "$sf"
}

_agent_converge_guest() { # <name> <ip>
    local name="$1" ip="$2"
    if push_agent_list _agent_exec "$(_agent_sock)" | grep -q .; then
        _agent_forward_start "$name" "$ip" || return 1
    else
        _agent_forward_stop "$name"
        _ssh "$ip" "rm -f $(sh_quote "$(t_agent_sock)")" </dev/null || return 1
    fi
}

_boot() {
    local v="$1" wait="${2:-180}" ip runlog

    ensure_dir "$WK_VM_DIR"
    runlog="$WK_VM_DIR/${v#wk-}.run.log"

    if [ "$(_vm_state "$v")" != running ]; then
        # Computed *before* the tart command line: inline, a die in _softnet_flags would kill only the subshell and tart would run anyway.
        local sflags
        sflags=$(_softnet_flags)

        if [ -z "$sflags" ]; then
            : > "$WK_VM_DIR/${v#wk-}.unfiltered"
        else
            rm -f "$WK_VM_DIR/${v#wk-}.unfiltered"
        fi

        # nohup, not a bare `&`, or the VM dies with the terminal. Windowed: a macOS guest is the one workspace kind with a real GPU.
        # shellcheck disable=SC2086 -- deliberate word splitting of the flags.
        nohup "$(_tart_bin)" run $sflags "$v" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting $v (log: $runlog)"
    fi

    # Default dhcp resolver works behind Softnet; the arp resolver does not.
    ip=$(_tart ip "$v" --wait "$wait" 2>/dev/null | grep .) \
        || die "$v did not come up within ${wait}s; see $runlog"

    _start_host_proxy || true

    _wait_ssh "$ip" || die "$v is up at $ip but ssh never answered; see $runlog"
    echo "$ip"
}

t_stop() {
    local v; v=$(_vm "$1")
    _agent_forward_stop "$1"
    [ "$(_vm_state "$v")" = running ] || { info "$1 is not running"; return 0; }
    _tart stop "$v"
    info "stopped $1"
}

t_exec() {
    local name="$1"; shift
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # Two layers of quoting: ssh joins its arguments with spaces, so the command must be one already-quoted string, in a *login* shell for PATH.
    local cmd; cmd=$(sh_quote "$@")
    _ssh "$ip" "bash -lc $(sh_quote "$cmd")"
}

t_pull() {
    local name="$1" src="$2" dest="$3"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$src" "$dest"
}

t_pull_dir() {
    local name="$1" src="$2" dest="$3"; shift 3
    _t_pull_dir_excludes "$@"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    mkdir -p "$dest"
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    rsync -a --delete ${_T_PULL_EXCLUDES[@]+"${_T_PULL_EXCLUDES[@]}"} -e "ssh $(_ssh_opts)" \
        "$WK_VM_USER@$ip:$src/" "$dest/"
}

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

# The marker that tells the guest's own wk that it *is* a workspace, and which one. Never written into the golden base.
_write_marker() {
    local name="$1" ip="$2"
    _ssh "$ip" "printf '%s\n' \
        '# wk: this machine IS a workspace. Written by targets/vm.sh.' \
        $(sh_quote "name=$name") $(sh_quote "src=$(t_src "$name")") \
        $(sh_quote "config=${WK_VM_BASE_PREBUILD-mac-release}") \
        > \$HOME/.wk-workspace"
}

_write_claude_config() {
    local name="$1" ip="$2" tools; tools=$(t_tools "$name")
    _ssh "$ip" "[ -d $(sh_quote "$tools/claude") ] || exit 1
        mkdir -p \$HOME/.claude
        for f in settings.json hooks CLAUDE.md skills; do
            ln -sfn $(sh_quote "$tools/claude")/\$f \$HOME/.claude/\$f
        done"
}

# One row per named secret (wk_agent_secrets, lib/store.sh), rewritten every start so a rotation converges and removed when the store has none: a guest holding a withdrawn credential is the state this must not leave behind.
# A *file* row is a credential its own tool rewrites in place, spending a refresh token, so it is never copied in -- it is delivered as absence, and the guest authenticates into a store this host never writes (CLAUDE_SECURESTORAGE_CONFIG_DIR, vm/shell-rc.sh).
_write_agent_secrets() { # <name> <ip>
    local name="$1" ip="$2" sname sfile shome svar skind val here n=0
    while read -r sname sfile shome svar skind; do
        [ -n "$sname" ] || continue
        if [ "$skind" = file ]; then
            here=1
        else
            here=0; wk_agent_secret_present "$sname" || here=$?
            [ "$here" -lt 2 ] || return 1
        fi
        if [ "$here" -ne 0 ]; then
            _ssh "$ip" "rm -f \$HOME/$(sh_quote "$shome")" </dev/null || return 1
            continue
        fi
        val=$(wk_agent_secret "$sname")
        printf '%s\n' "$val" \
            | _ssh "$ip" "umask 077 && cat > \$HOME/$(sh_quote "$shome")" || return 1
        n=$((n + 1))
    done <<EOF
$(wk_agent_secrets)
EOF
    debug "agent credentials in $name: $n"
}

t_agent_secret_present() { # <name> <secret>
    local name="$1" sname="$2" ip probe
    [ "$(wk_agent_secret_kind "$sname")" = file ] || { wk_agent_secret_present "$sname"; return; }
    ip=$(_ip "$name") || return 1
    probe="test -s \"\$CLAUDE_SECURESTORAGE_CONFIG_DIR/$(wk_agent_secret_field "$sname" 2)\""
    _ssh "$ip" "bash -lc $(sh_quote "$probe")" </dev/null >/dev/null 2>&1
}

t_agent_secret_remedy() { # <name> <secret>
    local name="$1" sname="$2"
    [ "$(wk_agent_secret_kind "$sname")" = file ] || { agent_secret_store_remedy "$sname"; return; }
    printf "a guest logs in for itself and this host is never a second holder: 'wk enter %s', then 'claude auth login' once" "$name"
}

# Softnet allows one address, where wk-proxy listens, so a guest's direct TCP to port 22 is dropped and this proxy is an HTTP CONNECT one; github.com:22 is in its allowlist.
# macOS's own nc speaks CONNECT with `-X connect`, and there is no other nc in a Cirrus Labs image. Absolute path: ssh runs this through /bin/sh, not the login PATH.
_ssh_proxy_command() {
    printf '/usr/bin/nc -X connect -x %s:%s %%h %%p' "$(_proxy_addr)" "$WK_VM_PROXY_PORT"
}

_write_deploy_keys() { # <name> <ip>
    local name="$1" ip="$2" remote repo alias pub idf n=0 total=0

    _ssh "$ip" "umask 077 && mkdir -p \$HOME/.ssh && cat > \$HOME/.ssh/config" <<EOF || return 1
# wk: written by targets/vm.sh on every start. Whether the agent these name
# holds a key at all is 'wk push'.
$(wk_ssh_alias_blocks "/Users/$WK_VM_USER/.ssh" id_ .pub "$(t_agent_sock)" "$(_ssh_proxy_command)")
EOF

    while read -r remote repo alias; do
        [ -n "$remote" ] || continue
        total=$((total + 1))
        pub=$(_wk_secret_read "$(wk_secrets_dir)/build_key_$remote.pub")
        idf="\$HOME/.ssh/id_$(sh_quote "$remote").pub"
        if [ -n "$pub" ]; then
            printf '%s\n' "$pub" | _ssh "$ip" "cat > $idf" || return 1
            n=$((n + 1))
        else
            _ssh "$ip" "rm -f $idf" </dev/null || return 1
        fi
    done <<EOF
$(wk_push_forks)
EOF
    debug "public deploy halves in $name: $n of $total"
}

vm_push_keys_converge() { # <on|off>
    local action="$1" g ip rc=0
    if [ "$action" = on ]; then
        _start_host_agent || return 1
        push_agent_load _agent_exec "$(_agent_sock)" >/dev/null || rc=1
        push_agent_pat_write _agent_exec "$(_inject_pat)" \
            || push_agent_pat_clear _agent_exec "$(_inject_pat)"
    else
        push_agent_clear _agent_exec "$(_agent_sock)" || true
        push_agent_pat_clear _agent_exec "$(_inject_pat)" || true
    fi

    for g in $(target_workspaces); do
        [ "$(t_info "$g" 2>/dev/null)" = running ] || continue
        if ! ip=$(_ip "$g"); then
            printf '  %-24s running, no address yet -- not converged\n' "$g" >&2
            rc=1; continue
        fi
        if ! _write_deploy_keys "$g" "$ip" >/dev/null; then
            printf '  %-24s FAILED -- its ssh config was not rewritten\n' "$g" >&2
            rc=1; continue
        fi
        if _agent_converge_guest "$g" "$ip"; then
            if [ "$action" = on ]; then
                printf '  %-24s %s\n' "$g" "reaches the agent on this host" >&2
            else
                printf '  %-24s %s\n' "$g" "no agent socket -- a push in there is refused" >&2
            fi
        else
            printf '  %-24s FAILED -- it may still reach the agent\n' "$g" >&2
            rc=1
        fi
    done
    return "$rc"
}

vm_push_keys_state() {
    local g ip state keys n
    keys=$(push_agent_list _agent_exec "$(_agent_sock)")
    n=$(printf '%s' "$keys" | grep -c . || true)
    for g in $(target_workspaces); do
        state=$(t_info "$g" 2>/dev/null) || state=unknown
        if [ "$state" != running ] || ! ip=$(_ip "$g"); then
            printf '%s\t%s\t\n' "$g" "${state:-unknown}"
            continue
        fi
        if [ "$n" -gt 0 ] \
            && _ssh "$ip" "test -S $(sh_quote "$(t_agent_sock)")" </dev/null 2>/dev/null; then
            printf '%s\trunning\t%s\n' "$g" "$n key(s) through the agent on this host"
        else
            printf '%s\trunning\t\n' "$g"
        fi
    done
}

# The proxy address is the host's own on the guest bridge and can change, so no image may bake it in. A guest needs both halves:
#   WebKit's network process does not read http_proxy/https_proxy (MiniBrowser gives a blank window where curl goes through), so the *system* proxy is set too; ~/.wk-egress carries http_proxy/https_proxy into all four rc files, where a profile would reach only login shells.
_set_guest_egress() {
    local name="$1" ip="$2" addr="" ca=""
    [ -n "${WK_VM_UNFILTERED:-}" ] || addr=$(_proxy_addr)
    [ -z "$addr" ] || ca=$(cat "$(_inject_ca)" 2>/dev/null) || ca=""
    debug "guest egress in $name: ${addr:-off}"

    _ssh "$ip" "bash -s" <<EOF
set -u
addr='$addr'
port='$WK_VM_PROXY_PORT'
ghuser=$(sh_quote "$(wk_github_user)")
cat > /tmp/.wk-github-ca.new <<'WKCA'
$ca
WKCA

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

# The injector's CA, and the placeholder credential 'git-webkit pr' reads
# (webkitcorepy). The bundle is the system's *plus* that CA, never that CA
# alone: these variables replace the trust store outright.
if grep -q 'BEGIN CERTIFICATE' /tmp/.wk-github-ca.new 2>/dev/null; then
    mv /tmp/.wk-github-ca.new "\$HOME/.wk-github-ca.pem"
    cat /etc/ssl/cert.pem "\$HOME/.wk-github-ca.pem" > "\$HOME/.wk-ca-bundle.pem"
    cat >> "\$HOME/.wk-egress" <<WKCAENV
export REQUESTS_CA_BUNDLE=\$HOME/.wk-ca-bundle.pem
export CURL_CA_BUNDLE=\$HOME/.wk-ca-bundle.pem
export GIT_SSL_CAINFO=\$HOME/.wk-ca-bundle.pem
export GITHUB_COM_USERNAME=\$ghuser
export GITHUB_COM_TOKEN=wk-injects-this
export SSL_CERT_FILE=\$HOME/.wk-ca-bundle.pem
export GH_TOKEN=wk-injects-this
WKCAENV
else
    rm -f /tmp/.wk-github-ca.new "\$HOME/.wk-github-ca.pem" "\$HOME/.wk-ca-bundle.pem"
fi

# The service to configure is the one carrying the default route: the Cirrus
# Labs image ships several, and which is real is a property of the guest.
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

# `tart clone` hands a clone the golden base's clock and nothing inside can correct it: Softnet allows one address, and NTP is UDP, which an HTTP CONNECT proxy cannot carry.
# From inside that looks like every TLS handshake failing as CERT_NOT_YET_VALID, not like a clock. Idempotent by measurement: a guest within WK_VM_CLOCK_SKEW costs no sudo.
_set_guest_clock() { # <name> <ip>
    local name="$1" ip="$2" skew
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

_write_lldbinit() {
    local name="$1" ip="$2"
    {
        printf '%s\n' "# wk: written by targets/vm.sh. See wk run --lldb, wk gui --lldb."
        printf '%s\n' "command script import $(t_src "$name")/Tools/lldb/lldb_webkit.py"
        cat "$WK_ROOT/dotfiles/lldbinit"
    } | _ssh "$ip" "cat > \$HOME/.lldbinit"
}

_write_shell_rc() { # <name> <ip>
    _ssh "$2" "bash -s $(sh_quote "$(t_tools "$1")")" < "$WK_ROOT/vm/shell-rc.sh"
}

command -v tools_push >/dev/null 2>&1 || . "$WK_ROOT/lib/tools.sh"

# A git bundle of this tree's HEAD (tools_push, lib/tools.sh) rather than a mount: no --dir is ever passed to `tart run`, and an uncommitted tree here is refused.
_push_tools() {
    local name="$1" ip="$2"
    tools_push "$(t_tools "$name")" _ssh "$ip"
}

t_sync_tools() {
    local name="$1"
    local ip; ip=$(_ip "$name") || die "'$name' is not running (wk vm start $name)"
    _push_tools "$name" "$ip" || return 1
    _write_marker "$name" "$ip"
}

t_sync() {
    local g rc=0 ip m
    command -v mirror_refresh_script >/dev/null 2>&1 || . "$WK_ROOT/lib/store.sh"
    for g in $(target_workspaces); do
        if [ "$(t_info "$g" 2>/dev/null)" != running ]; then
            printf '  %-24s %s\n' "$g" "not running -- skipped" >&2
            continue
        fi
        t_sync_tools "$g" || { rc=1; continue; }
        if ! ip=$(_ip "$g"); then
            printf '  %-24s %s\n' "$g" "tools ok, no address to refresh the mirror over" >&2
            rc=1
            continue
        fi
        m=$(t_mirror_dir "$g")
        if _ssh "$ip" "if [ -d $(sh_quote "$m") ]; then
                           $(mirror_refresh_script "$m")
                       else echo mirror-absent
                       fi" \
               | while read -r _tag _name _state; do
                     case "$_tag" in
                         mirror-fetch)
                             printf '  %-24s %s\n' "$g" "mirror $_name $_state" >&2 ;;
                         mirror-absent)
                             printf '  %-24s %s\n' "$g" \
                                 "tools ok, no mirror in this guest -- 'wk vm base --rebuild' puts one in every guest made after it" >&2 ;;
                     esac
                 done
        then printf '  %-24s ok\n' "$g" >&2
        else rc=1; fi
    done
    return "$rc"
}

t_destroy() {
    local name="$1"
    local v; v=$(_vm "$name")

    [ "$v" != "$WK_VM_BASE" ] || die "refusing to delete the golden base (wk vm base --rebuild)"

    if [ "$(_vm_state "$v")" != absent ]; then
        _vm_delete "$v"
        info "deleted VM $v"
    fi

    local ws; ws=$(wk_ws_dir "$name")
    [ -d "$ws" ] && { rm -rf "$ws"; info "removed $ws"; }
    rm -f "$WK_VM_DIR/$name.run.log"
    rm -f "$WK_VM_DIR/$name.unfiltered"
}

_base_prebuilt=""

# Never fails the provisioning it is the last step of: the completion marker is written after it, so anything fatal here would leave a base the next `_ensure_base` deletes as rubble.
_prebuild_base() {
    local ip="$1"
    _base_prebuilt=""
    [ -n "$WK_VM_BASE_PREBUILD" ] || { info "base prebuild disabled"; return 0; }

    # shellcheck disable=SC1090
    . "$WK_ROOT/build/configs.sh"
    config_load "$WK_VM_BASE_PREBUILD" "$(t_os)"

    local jobs
    jobs=$(WK_CGROUP_MB=$(_vm_get "$WK_VM_BASE" Memory) \
           WK_CGROUP_CORES=$(_vm_get "$WK_VM_BASE" CPU) build_jobs)

    info "pre-building '$WK_VM_BASE_PREBUILD' in the base with -j$jobs"
    log  "  This is the slow one and it happens once. Every workspace cloned"
    log  "  from this base inherits the build tree and the compilation cache."

    _push_tools "$WK_VM_BASE" "$ip" || {
        warn "the base cannot be pre-built without wk-tools in it (see above) --
  the base is still usable, but every workspace will pay for a cold build."
        return 0
    }

    config_build_env "$(t_src "$WK_VM_BASE")" "$jobs" 10
    local cmd
    cmd="env $(sh_quote "${CFG_ENV[@]}") $(sh_quote "$(t_tools "$WK_VM_BASE")/build/build-in-target.sh")"

    # Detached and polled, not a foreground `ssh <long command>`: this build takes over an hour, and any blip on the connection kills it.
    command -v detach_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    local rlog="/tmp/wk-base-build.log" rrc="/tmp/wk-base-build.rc"
    detach_remote _base_ssh "$rlog" "$rrc" -- bash -lc "$cmd" || {
        warn "could not start the base prebuild -- the base is still usable,
  but every workspace will pay for a cold build."
        return 0
    }

    local t0; t0=$(date +%s)
    local rc; rc=$(detach_wait_remote _base_ssh "$rlog" "$rrc")
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$rlog" "$WK_VM_DIR/base-build.log" 2>/dev/null || true

    if [ "$rc" = 0 ]; then
        info "base prebuild finished in $(( ($(date +%s) - t0) / 60 ))m"
        _base_prebuilt="$WK_VM_BASE_PREBUILD"
    else
        warn "base prebuild FAILED after $(( ($(date +%s) - t0) / 60 ))m -- the base is
  still usable, but every workspace will pay for a cold build.
  log: $WK_VM_DIR/base-build.log"
        if command -v first_error >/dev/null 2>&1; then
            first_error "$WK_VM_DIR/base-build.log" 2>/dev/null | sed 's/^/    /' || true
        fi
    fi
}

command -v _unpinned_host_key_opts >/dev/null 2>&1 || . "$WK_ROOT/lib/reach.sh"

_ssh_opts() {
    # ServerAliveInterval: builds go quiet for long stretches, and without keepalives a NAT timeout drops the connection mid-build and reports it as a build failure.
    printf '%s' "$(_ssh_opts_base "$(wk_ssh_timeout)") $(_unpinned_host_key_opts) \
-o ServerAliveInterval=60 -o ServerAliveCountMax=10 -i $WK_VM_KEY"
}

_ssh() {
    local ip="$1"; shift
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    ssh $(_ssh_opts) "$WK_VM_USER@$ip" "$@"
}

# `ip` is the caller's own local, in scope through bash's dynamic scoping.
_base_ssh() { _ssh "$ip" "$@"; }

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

_podman_mem_mb() {
    have podman || { echo 0; return; }
    podman machine inspect "${WK_MACHINE:-wk}" --format '{{.Resources.Memory}}' 2>/dev/null || echo 0
}

_podman_running() {
    have podman || return 1
    [ "$(podman machine inspect "${WK_MACHINE:-wk}" --format '{{.State}}' 2>/dev/null)" = running ]
}

# An unreadable answer counts as busy: stopping a machine underneath something is the mistake this guards.
_podman_containers_running() {
    _podman_running || { echo 0; return 0; }
    podman machine ssh "${WK_MACHINE:-wk}" -- 'podman ps -q | grep -c . || true' \
        </dev/null 2>/dev/null | tr -dc '0-9' | grep . || echo 1
}

_vm_cpus()   { echo "${WK_VM_CPUS:-$(envelope_cores)}"; }
_vm_mem_mb() { echo "${WK_VM_MEM_MB:-$(envelope_mem_mb)}"; }

_vm_get() {
    _tart get "$1" --format json 2>/dev/null | python3 -c '
import json, sys
v = json.load(sys.stdin).get(sys.argv[1])
print("" if v is None else v)' "$2"
}

_vm_configured() { _vm_get "$(_vm "$1")" "$2"; }

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

    # An idle podman machine is not a reason to refuse a guest: it holds the whole envelope whether or not anything runs in it, and `wk` starts it again on the next container command.
    if [ "$podman_mb" -gt 0 ] && [ "$(_podman_containers_running)" = 0 ] \
       && [ $(( mine + guests )) -le "$budget" ]; then
        info "stopping the idle podman machine to free ${podman_mb}MB for '$name'"
        podman machine stop "${WK_MACHINE:-wk}" >/dev/null 2>&1 || true
        _podman_running || return 0
        warn "the podman machine did not stop; '$name' may not fit"
    fi
    _podman_running && podman_mb=$(_podman_mem_mb) || podman_mb=0
    total=$(( mine + podman_mb + guests ))
    [ "$total" -le "$budget" ] && return 0

    if [ -n "${WK_VM_SHARE:-}" ]; then
        warn "'$name' (${mine}MB) on top of ${podman_mb}MB podman + ${guests}MB of running guests exceeds the ${budget}MB envelope -- continuing because WK_VM_SHARE is set"
        return 0
    fi

    spare=$(( budget - podman_mb - guests ))

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

_base_exists() { [ "$(_vm_state "$WK_VM_BASE")" != absent ]; }

_base_marker() { echo "$WK_VM_DIR/base.ready"; }

_base_ready() { _base_exists && [ -f "$(_base_marker)" ]; }

# The inputs that produced this base, as one hash: vm_base_stale recomputes and compares on every read, so a script edited here makes every base built before it read stale at once.
_base_inputs_hash() {
    {
        cat "$WK_ROOT/vm/provision-base.sh" "$WK_ROOT/vm/desktop.sh" \
            "$WK_ROOT/vm/shell-rc.sh"
        printf 'image=%s\nuser=%s\n' "$WK_VM_IMAGE" "$WK_VM_USER"
    } | python3 -c 'import hashlib,sys
print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest()[:16])'
}

# Prints the reason and succeeds when the base is stale, so a caller reads `if why=$(vm_base_stale); then`.
vm_base_stale() {
    local rec
    rec=$(marker_field "$(_base_marker)" inputs)
    if [ -z "$rec" ]; then
        echo "provisioned before this record existed"
        return 0
    fi
    [ "$rec" = "$(_base_inputs_hash)" ] && return 1
    echo "vm/provision-base.sh, vm/desktop.sh, vm/shell-rc.sh, WK_VM_IMAGE or WK_VM_USER has changed since it was built"
    return 0
}

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
        _f wrong "'$WK_VM_BASE' predates its own provisioning inputs: $why -- every guest cloned from it carries what that base was built with" \
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
        "$WK_VM_IMAGE" "${_base_prebuilt:-none}" "$(_base_inputs_hash)" \
        "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$(_base_marker)"
}

_check_prebuild_config() {
    [ -n "$WK_VM_BASE_PREBUILD" ] || return 0
    # shellcheck disable=SC1090
    . "$WK_ROOT/build/configs.sh"
    ( config_load "$WK_VM_BASE_PREBUILD" "$(t_os)" ) >/dev/null 2>&1 \
        || die "WK_VM_BASE_PREBUILD='$WK_VM_BASE_PREBUILD' is not a build config for
    this target.  wk build --list names them; empty disables the prebuild."
}

_ensure_base() {
    _base_ready && return 0
    _check_prebuild_config

    if _base_exists; then
        warn "'$WK_VM_BASE' exists but was never finished (no completion marker)"
        log  "  destroying it and starting again -- an unprovisioned base is rubble,"
        log  "  and every workspace cloned from one inherits whatever it is missing."
        _vm_delete "$WK_VM_BASE" 2>/dev/null || true
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

_provision_base() {
    _check_prebuild_config
    _check_guest_limit
    _check_memory_budget "$WK_VM_BASE" "$(_base_mem_mb)"
    ensure_dir "$WK_VM_DIR" 0700

    # tart can only grow a disk while the VM is off.
    local cur; cur=$(_vm_get "$WK_VM_BASE" Disk)
    if [ -n "$cur" ] && [ "$cur" -lt "$WK_VM_DISK_GB" ]; then
        info "growing the base disk ${cur}GB -> ${WK_VM_DISK_GB}GB"
        _tart set "$WK_VM_BASE" --disk-size "$WK_VM_DISK_GB"
    fi
    _tart set "$WK_VM_BASE" --cpu "$(_base_cpus)" --memory "$(_base_mem_mb)"
    _check_host_disk

    if [ ! -f "$WK_VM_KEY" ]; then
        ssh-keygen -q -t ed25519 -N '' -C wk-vm -f "$WK_VM_KEY"
        changed "generated the macOS VM ssh key"
    fi

    # A first boot has Setup Assistant work to get through, hence the longer wait than t_start uses.
    local runlog="$WK_VM_DIR/base.run.log"
    local ip
    if [ "$(_vm_state "$WK_VM_BASE")" != running ]; then
        nohup "$(_tart_bin)" run --no-graphics "$WK_VM_BASE" >"$runlog" 2>&1 &
        disown 2>/dev/null || true
        info "booting the base VM for provisioning (log: $runlog)"
    fi
    ip=$(_tart ip "$WK_VM_BASE" --wait 300 2>/dev/null | grep .) \
        || die "base VM did not boot; see $runlog"

    # The guest agent is how the key gets in the FIRST time, without typing the default password; `tart ip --wait` answers before the agent is listening.
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

    # Before anything here speaks TLS: provisioning's first act is an HTTPS clone, which a stale clock fails as a not-yet-valid certificate. A refusal, since a base sealed at the wrong date hands it to every clone.
    _set_guest_clock "$WK_VM_BASE" "$ip" \
        || die "could not set the clock in '$WK_VM_BASE'. Passwordless sudo is what it
    needs, and the base is built from the image WK_VM_IMAGE names -- check that
    image rather than patching the guest:  ssh into it and run  sudo -n true"

    info "provisioning the base VM (Xcode licence, WebKit mirror and checkout, Claude CLI)"
    _push_tools "$WK_VM_BASE" "$ip" \
        || die "the base cannot be provisioned without wk-tools in it (see above)"
    vm_login_note
    # Detached and polled, for the reason _prebuild_base gives (measured 2026-09-04: "Read from remote host: Connection reset by peer" an hour into the clone, which died with it).
    command -v detach_remote >/dev/null 2>&1 || . "$WK_ROOT/lib/detach.sh"
    local plog="/tmp/wk-base-provision.log" prc="/tmp/wk-base-provision.rc"
    detach_remote _base_ssh "$plog" "$prc" -- \
        env WK_VM_DISPLAY="$WK_VM_DISPLAY" WK_VM_USER="$WK_VM_USER" \
            WK_VM_PASSWORD="$WK_VM_PASSWORD" \
            WK_VM_MIRROR="$(t_mirror_dir "$WK_VM_BASE")" \
            bash "$(t_tools "$WK_VM_BASE")/vm/provision-base.sh" \
        || die "could not start base provisioning in '$WK_VM_BASE'"
    local prov_rc; prov_rc=$(detach_wait_remote _base_ssh "$plog" "$prc")
    # shellcheck disable=SC2046 -- deliberate word splitting of the option list.
    scp -q $(_ssh_opts) "$WK_VM_USER@$ip:$plog" "$WK_VM_DIR/base-provision.log" 2>/dev/null || true
    [ "$prov_rc" = 0 ] || die "base provisioning failed (rc=$prov_rc).
    What it printed is in $WK_VM_DIR/base-provision.log; the base is rubble
    until this finishes, and a re-run starts it again:  wk vm base --refresh"

    _prebuild_base "$ip"

    info "shutting the base VM down"
    _tart stop "$WK_VM_BASE"
    _base_mark_ready
    changed "golden base VM '$WK_VM_BASE' is ready"
}

# Findings, one per line, tab-separated `<state> <what> <remedy>` with state ok | wrong | note. Every renderer reads a line at a time, so a remedy wrapped over two lines loses its second half.
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

    # The offer that puts a panel on the desktop is the *system* one: softwareupdated acts on /Library/Preferences, while the per-user domain is what System Settings shows. `?` is "no such key", which is not off.
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

    # Setup Assistant's "what is new in macOS" pane is a Software Update screen by another name: Buddy shows it whenever these keys do not already name the running system.
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

    _f note "the guest's own window logs in as $(_v user)" \
            "wk itself uses an ssh key; 'wk vm start' and 'wk vm enter' state that account's password"

    unset -f _f _v
    return 0
}

# `_ssh`, not t_exec: this must answer about a guest whose wk-tools copy is older than this file.
vm_desktop_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    _ssh "$ip" 'bash -s' < "$WK_ROOT/vm/desktop-probe.sh"
}

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

# A guest holds its whole memory allocation whether or not it is busy, and nothing wk runs in one outlives its ssh session, so what accumulates keeps its own process: an editor's remote server, an agent, or a detached build.
vm_load_probe() { # <name>
    local ip; ip=$(_ip "$1") || return 1
    [ -n "$ip" ] || return 1
    _ssh "$ip" 'bash -s' < "$WK_ROOT/vm/load-probe.sh"
}

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
